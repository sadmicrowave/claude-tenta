"""cw-admin: the administrator's commands, run on your own computer. Everything that changes the worker asks first and shows what it will do.

  WORKER     status [--hours N]     check
  JOBS       review [job...]        approve <job>|--all        cancel <job>      kill <job> [--hard]
  SECURITY   quarantine [job]       unlock                     output [job]
  REGISTRY   adopt <profile-job>    retire <class>             proposals [list|merge|tidy]
  RELEASE    diff [rev]             apply [rev]

Every name, address and folder comes from the configuration (cw.toml); nothing here knows an install. Passwords are never typed through
this program: sudo asks you itself, on your own terminal.
"""
import argparse
import sys
import time
from pathlib import Path

from .. import approvals, clihelp, config, registry, render
from .. import ui as _ui
from ..client import offload as off
from ..client import cli as client_cli
from ..client.repo import ClientError, ClientRepos
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, paint
from . import jobs, regedit, release
from .proposals import Proposals
from .ssh import Ssh


def emit(*a, **k):
    k.setdefault("stream", sys.stderr)
    _ui.say(*a, **k)


class Stop(Exception):
    def __init__(self, code=1):
        self.code = code


def stop(title, *details, code=1, tag="STOPPED", color=YELLOW, **kw):
    emit(title, *details, tag=tag, color=color, **kw)
    raise Stop(code)


class Context:
    def __init__(self, repos, ssh, source, *, name="claude-ballast", config_dir=None, code_dir=None, home=None, ask=input, is_tty=None,
                 run_result=None, main_branch="main", sleep=time.sleep):
        self.repos, self.ssh, self.source, self.name = repos, ssh, source, name
        self.config_dir, self.code_dir = config_dir or f"/etc/{name}", code_dir or f"/opt/{name}"
        self.home = Path(home) if home else Path("~/.cw").expanduser()
        self.props, self.viewed = Proposals(repos, main_branch), jobs.Viewed(self.home)
        self.ask, self.sleep = ask, sleep
        self.is_tty = is_tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
        self.run_result = run_result or (lambda jid: client_cli.main(["result", jid, "--wait", "--approved"]))

    @classmethod
    def from_config(cls, cfg):
        repos = ClientRepos(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch)
        home = Path(cfg.paths.client_home).expanduser()
        return cls(repos, Ssh(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port), release.Source(home / "source.git", cfg.updates.source, cfg.updates.branch),
                   name=cfg.identity.name, config_dir=cfg.paths.config, code_dir=cfg.paths.code, home=home, main_branch=cfg.github.main_branch)

    # the commands on the server, run as the worker through its own sudo entry point (read-only ones need no password)
    def remote(self, *args):
        return self.ssh.interactive(f"{render.WORKER_CMD} {self.ssh.quote(args)}")

    def confirm(self, prompt, yes=False):
        if yes:
            return True
        if not self.is_tty():
            raise ClientError("This needs your answer", "It asks a question, and nobody is at the keyboard.", ["Run it in a terminal, or add --yes if you are sure."])
        return self.ask(paint(f"\n{prompt} [y/N] ", BOLD, YELLOW)).strip().lower() == "y"


def fail_from(e):
    emit(e.title, e.why, tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
    return 2


# ---- worker ----
def cmd_status(ctx, a):
    return ctx.remote("status", "--hours", str(a.hours))


def cmd_check(ctx, a):
    out = sys.stdout
    print(paint(f"Checking the connection to {ctx.ssh.host}", BOLD, stream=out) + "\n")
    r = ctx.ssh._run(ctx.ssh.argv("true", batch=True), capture_output=True, text=True)
    if r.returncode:
        print(f"{paint('FAIL', BOLD, RED, stream=out)}  key login does not work: {(r.stderr or '').strip()}")
        emit("Set up SSH key login first", "Password logins are not used by cw-admin.", tag="FIX", color=YELLOW, run=f"ssh-copy-id {ctx.ssh.target}")
        return 1
    print(f"{paint('OK  ', BOLD, GREEN, stream=out)}  key login works without a password")
    r = ctx.ssh._run(ctx.ssh.argv(f"{render.WORKER_CMD} status", batch=True), capture_output=True, text=True)
    if r.returncode:
        print(f"{paint('NOTE', BOLD, YELLOW, stream=out)}  `{render.WORKER_CMD} status` asked for a password or failed; read-only commands normally need none (access.sudo)")
    else:
        print(f"{paint('OK  ', BOLD, GREEN, stream=out)}  `{render.WORKER_CMD}` read-only commands work without a password")
    print()
    return 0


# ---- jobs ----
def cmd_review(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.fetch_queue()
    todo = [(i, r.read_result(i)) for i in a.ids] if a.ids else jobs.waiting(r)
    for i in a.ids:
        jobs.require_id(i)
    if not todo:
        emit("Nothing is waiting for approval", tag="OK", color=GREEN)
        return 0
    gone = []
    for jid, pub in todo:
        raw = jobs.job_bytes(r, jid)
        if raw is None:
            gone.append(jid)
            continue
        print(paint(f"\n=== {jid} " + "=" * max(4, 60 - len(jid)), BOLD, CYAN, stream=sys.stdout))
        import json
        print(jobs.describe(json.loads(raw), pub, ctx.name))
        ctx.viewed.mark(jid, jobs.digest_of(raw))
    if gone:
        emit("Showing the worker's own copy", f"{len(gone)} job(s) are no longer on GitHub. It may ask for your sudo password once.", tag="NOTE", color=YELLOW)
        if ctx.remote("review", *gone):
            raise ClientError("Could not show those jobs", "They were NOT marked as viewed.", example="cw-admin check")
        for jid in gone:
            ctx.viewed.mark(jid, "")
    emit("Viewed", f"{len(todo)} job(s) noted as viewed.", tag="DONE", color=GREEN, run_label="Approve the ones you viewed", run="cw-admin approve --all")
    return 0


def cmd_approve(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.fetch_queue()
    return approve_all(ctx, a) if a.all else approve_one(ctx, a)


# ---- approving from this computer: sign here, publish, nothing to log in to ----
def approval_key(ctx):
    return ctx.home / "approval" / "key"


def use_signed(ctx, a):
    """True when approvals should be signed here. False means the SSH way: you chose it (--ssh), no approval key was ever made here, or the worker is an older
    release that does not publish which keys it trusts. If a key exists but this worker does not trust it, that is an error, not a silent fallback."""
    if getattr(a, "ssh", False) or not approval_key(ctx).exists():
        return False
    info = ctx.repos.registry_info() or {}
    if "approvers" not in info:
        return False
    pub = Path(str(approval_key(ctx)) + ".pub")
    mine = approvals.fingerprint(pub.read_text()) if pub.exists() else None
    if mine not in info["approvers"]:
        raise ClientError(f"{ctx.name} does not trust your approval key yet",
                          "Approvals are signed here and checked on the worker against a key it was set up with. This worker has " + ("a different one" if info["approvers"] else "none") + ".",
                          ["Run the install again; it gives the worker your key: cw-setup install", "Or approve this one by logging in to the worker: add --ssh"], example="cw-setup install")
    return True


def sign_and_publish(ctx, items):
    """items: [(job id, the SHA-256 of the exact job file that was reviewed)]. Signs each approval with the key here (it asks for its passphrase if it has one)
    and publishes them to the queue repository for the worker to pick up."""
    files = {}
    for jid, digest in items:
        record = approvals.make_record(jid, digest)
        try:
            files[approvals.path_in_repo(jid)] = approvals.bundle(record, approvals.sign(record, approval_key(ctx)))
        except ValueError as e:
            raise ClientError("Could not sign the approval", str(e), ["Is the key at " + str(approval_key(ctx)) + " the one cw-setup made? Run: cw-setup install"])
    ctx.repos.push_files(approvals.BRANCH, files, "approve " + ", ".join(j for j, _ in items))


def publish_registry(ctx):
    """Sign the registry.yaml that is on main of the queue repository, here, and publish the signature. The worker's registry-sync timer installs that exact
    text within minutes, and nothing else: GitHub access alone cannot change what the worker may run. Returns the digest that was signed."""
    ctx.repos.fetch_queue()
    text = ctx.repos.show_file(ctx.props.base, "registry.yaml")
    if text is None:
        raise ClientError("There is no registry.yaml on main to sign", "Nothing has been merged into your registry yet.", example="cw-admin proposals")
    import hashlib
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = approvals.make_registry_record(digest)
    try:
        signature = approvals.sign(record, approval_key(ctx), namespace=approvals.REGISTRY_NAMESPACE)
    except ValueError as e:
        raise ClientError("Could not sign the registry", str(e), ["Is the key at " + str(approval_key(ctx)) + " the one cw-setup made? Run: cw-setup install"])
    ctx.repos.push_files(approvals.BRANCH, {approvals.REGISTRY_PATH: approvals.bundle(record, signature)}, f"registry: approve {digest[:12]}")
    return digest


def wait_for_registry(ctx, name, present, timeout=600, step=10):
    """Wait (in a terminal) until the worker's published class list shows `name` added (present) or removed. Returns True when it did."""
    if not ctx.is_tty():
        return False
    emit("Waiting for the worker to install it", "It checks about every five minutes. Ctrl-C stops waiting, not the install.", tag="WAITING", color=CYAN)
    waited = 0
    while waited <= timeout:
        ctx.repos.pull_results()
        info = ctx.repos.registry_info() or {}
        if (name in (info.get("classes") or {})) == present:
            return True
        ctx.sleep(step)
        waited += step
    return False


def install_registry_change(ctx, a, name=None, present=True):
    """The last step of adopting or retiring a class, or merging a registry change: signed here when the worker trusts this computer's key (no login at all),
    otherwise the old way, an install over SSH."""
    if use_signed(ctx, a):
        publish_registry(ctx)
        if name is not None and wait_for_registry(ctx, name, present):
            emit("Installed on the worker", f"{name} is {'available' if present else 'gone'}. Signed here; no login to {ctx.name} was needed.", tag="DONE", color=GREEN)
        else:
            emit("Signed and published", f"{ctx.name} installs it within about five minutes, no login needed.", tag="NEXT", color=CYAN, run_label="Check later", run="cw-run classes")
        return 0
    return apply(ctx, argparse.Namespace(rev=None, yes=True, force=True, no_verify=False, full=False, quiet_done=True))


def approve_all(ctx, a):
    ready, skipped = jobs.classify(jobs.waiting(ctx.repos), ctx.viewed.load(), lambda j: jobs.job_bytes(ctx.repos, j))
    if not ready and not skipped:
        emit("Nothing is waiting for approval", tag="OK", color=GREEN)
        return 0
    out = sys.stdout
    for jid, why in skipped:
        print(f"{paint('SKIPPED  ', BOLD, YELLOW, stream=out)}{jid}  {why}")
    for jid, _ in ready:
        print(f"{paint('READY    ', BOLD, GREEN, stream=out)}{jid}  viewed")
    if not ready:
        raise ClientError("None of the waiting jobs have been viewed", "Review them first; that is what lets them be approved together.", example="cw-admin review")
    if not ctx.confirm(f"Approve these {len(ready)} job(s)?", a.yes):
        stop("Not approved", "You answered no. They stay waiting.")
    if use_signed(ctx, a):
        sign_and_publish(ctx, ready)
        emit("Approved", f"{len(ready)} job(s), signed on this computer. No login to {ctx.name} was needed.", tag="APPROVED", color=GREEN)
        rc = 0
    else:
        rc = ctx.remote("approve", "--many", *[f"{j}:{d}" for j, d in ready])
    if rc == 0:
        emit("Queued", f"{ctx.name} runs them one after another; you can go away.", tag="NEXT", color=CYAN, run_label="Check later", run="cw-run jobs")
    return rc


def approve_one(ctx, a):
    import json
    jid = jobs.require_id(a.id) if a.id else None
    if not jid:
        raise ClientError("Say which job to approve", "Give a job id, or --all for every job you have already viewed.", example="cw-admin approve --all")
    raw = jobs.job_bytes(ctx.repos, jid)
    if raw is None:
        return approve_on_worker(ctx, a, jid)
    digest, job = jobs.digest_of(raw), json.loads(raw)
    banner = ctx.repos.pull_results()
    if banner:
        emit(f"{ctx.name} is LOCKED", ("What happened", banner.get("kind")), ("Job", banner.get("job")), tag="LOCKED", color=RED, run_label="Review it, then unlock it",
             run=["cw-admin quarantine", "cw-admin unlock"])
    print(jobs.describe(job, ctx.repos.read_result(jid), ctx.name))
    ctx.viewed.mark(jid, digest)
    print("\n" + paint(f"Job file digest {digest[:12]}...  (approval is pinned to this exact file)", DIM, stream=sys.stdout))
    if not ctx.confirm("Approve this exact job?", a.yes):
        stop("Not approved", "You answered no. The job stays waiting.")
    if use_signed(ctx, a):
        sign_and_publish(ctx, [(jid, digest)])
        emit("Approved", f"{jid}, signed on this computer. No login to {ctx.name} was needed; it picks the job up within a minute.", tag="APPROVED", color=GREEN)
        rc = 0
    else:
        rc = ctx.remote("approve", jid, "--sha256", digest)
    return follow(ctx, a, rc, jid)


def approve_on_worker(ctx, a, jid):
    """The file is gone from GitHub but the worker kept its own first-seen copy: review and approve it THERE, so what you read is what runs."""
    pub = ctx.repos.read_result(jid)
    if not pub or pub.get("status") != "awaiting_approval":
        raise ClientError(f"No job called {jid}", f"Its file is not on the jobs branch, and {ctx.name} does not show it waiting for approval.", example="cw-run jobs")
    emit("The job file is no longer on GitHub", f"{ctx.name} kept its own copy, so you review and approve it there.", tag="NOTE", color=YELLOW)
    rc = ctx.remote("approve", jid, *(["--yes"] if a.yes else []))
    ctx.viewed.mark(jid, "")
    return follow(ctx, a, rc, jid)


def follow(ctx, a, rc, jid):
    if rc == 0 and not a.no_follow and ctx.is_tty():
        emit("Approved: waiting for the result", "Ctrl-C stops waiting, not the job.", tag="WAITING", color=CYAN)
        return ctx.run_result(jid)
    return rc


def cmd_cancel(ctx, a):
    return ctx.remote("cancel", jobs.require_id(a.id))


def cmd_kill(ctx, a):
    return ctx.remote("kill", jobs.require_id(a.id), *(["--hard"] if a.hard else []))


# ---- security ----
def cmd_quarantine(ctx, a):
    return ctx.remote("quarantine", *([jobs.require_id(a.id)] if a.id else []))


def cmd_output(ctx, a):
    return ctx.remote("output", *([jobs.require_id(a.id)] if a.id else []))


def cmd_unlock(ctx, a):
    return ctx.remote("unlock")


# ---- registry and proposals ----
def merge_branch(ctx, branch, *, yes=False, subject=None):
    """Show a branch, squash it into main (validating a registry change first), push, delete the branch. Returns the new main commit."""
    p = ctx.props
    p.check_name(branch)
    p.fetch()
    if p.contained(branch):
        emit(f"{branch} is already merged into main", "Nothing to merge.", tag="OK", color=GREEN)
        return p.git("rev-parse", p.base).stdout.strip()
    print(p.log(branch))
    sha, msg, tree = p.squash(branch, subject)
    reg = p.tree_file(tree, "registry.yaml")
    if reg is not None:
        try:
            problems = registry.validate(registry.parse(reg), "/nonexistent-tools-root", allow_empty=True)
        except Exception as e:                                          # invalid YAML
            problems = [f"not valid YAML: {e}"]
        problems = [x for x in problems if "tools" not in x]
        if problems:
            raise ClientError(f"{branch} would leave an invalid registry", "\n".join(problems[:5]), ["Fix the proposal; nothing was merged."])
    print(paint("This becomes ONE commit on main:", BOLD, stream=sys.stdout) + "\n")
    print("\n".join("  " + line for line in msg.splitlines()) + "\n")
    if not ctx.confirm(f"Squash-merge {branch} into main and delete the branch?", yes):
        stop("Not merged", "You answered no.")
    p.push_main(sha)
    gone = p.delete(branch)
    p.fetch()
    emit(f"Merged {branch} into main as {release.short(sha)}", "The branch was deleted." if gone else "The branch is still on GitHub; `cw-admin proposals tidy` removes it.",
         tag="MERGED", color=GREEN)
    return sha


def cmd_proposals(ctx, a):
    p, act = ctx.props, a.action or "list"
    ctx.repos.need_setup()
    p.fetch()
    if act == "list":
        rows = p.listing()
        if not rows:
            emit("No open proposals", "main is all there is.", tag="OK", color=GREEN)
            return 0
        w = max(len(r[0]) for r in rows)
        out = sys.stdout
        print(paint(f"{'BRANCH':<{w}}  {'STATE':<12}  LAST COMMIT", BOLD, stream=out))
        for b, ahead, subj, merged in rows:
            state = "merged" if merged else f"{ahead} to review"
            print(f"{paint(f'{b:<{w}}', BOLD, stream=out)}  {paint(f'{state:<12}', *((DIM,) if merged else (YELLOW,)), stream=out)}  {subj}")
        emit("What you can do with these", tag="NEXT", color=CYAN, run_label="Merge one, merge them all, or clean up", run=["cw-admin proposals merge <branch>", "cw-admin proposals merge --all", "cw-admin proposals tidy"])
        return 0
    if act == "merge":
        if a.all:
            return merge_all(ctx, a)
        if not a.branch:
            raise ClientError("Say which branch to merge", "Or use --all.", example="cw-admin proposals merge --all")
        merge_branch(ctx, a.branch, yes=a.yes, subject=a.message)
        return after_merge(ctx, a)
    if act == "publish":
        return install_registry_change(ctx, a)
    if act == "tidy":
        done = [b for b in p.branches() if p.contained(b)]
        if not done:
            emit("Nothing to tidy", "No merged branches are left.", tag="OK", color=GREEN)
            return 0
        if not ctx.confirm(f"Delete {len(done)} merged branch(es) ({', '.join(done)}) from GitHub?", a.yes):
            stop("Nothing deleted", "You answered no.")
        gone = [b for b in done if p.delete(b)]
        emit(f"Deleted {len(gone)} branch(es)", tag="DONE", color=GREEN)
        return 0 if len(gone) == len(done) else 1
    raise ClientError(f"Unknown action {act!r}", "Use list, merge or tidy.", example="cw-admin help proposals")


def after_merge(ctx, a):
    """A merge changes GitHub only. With an approval key here the registry is signed and published at once; otherwise it waits for an install."""
    if use_signed(ctx, a):
        return install_registry_change(ctx, a)
    emit("Install it", "Merging changes GitHub only; the worker has it after the next install.", tag="NEXT", color=CYAN, run="cw-admin apply")
    return 0


def merge_all(ctx, a):
    """Merge every pending proposal one by one (each is shown and asked about, unless --yes). Registry changes are included: they are
    validated before each merge, and nothing reaches the worker until you run apply."""
    pending = ctx.props.pending()
    if not pending:
        emit("Nothing to merge", tag="OK", color=GREEN)
        return 0
    merged = []
    for b in pending:
        try:
            merge_branch(ctx, b, yes=a.yes)
            merged.append(b)
        except ClientError as e:
            fail_from(e)
        except Stop:
            pass
        ctx.props.fetch()
    if merged and use_signed(ctx, a):
        install_registry_change(ctx, a)
        return 0 if len(merged) == len(pending) else 1
    emit(f"Merged {len(merged)} of {len(pending)}", tag="DONE" if len(merged) == len(pending) else "PARTLY", color=GREEN if len(merged) == len(pending) else YELLOW,
         run_label="Install what is on main", run="cw-admin apply")
    return 0 if len(merged) == len(pending) else 1


def cmd_adopt(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.pull_results()
    jobs.require_id(a.id)
    res = r.read_result(a.id)
    if not res:
        raise ClientError(f"No result for {a.id} yet", "Profile it first, then adopt it.", example=f"cw-run result {a.id} --wait")
    names = off.NameBook(ctx.home)
    preview = off.propose(r, a.id, res, name=a.name, tier=a.tier, dry_run=True, names=names) if res.get("profile", {}).get("verdict") != "does-not-fit" else None
    if preview is None:
        out = off.propose(r, a.id, res, names=names)
        emit("It does not fit the worker", "It was recorded as an exclusion; nothing was added to the registry.", *out["reasons"], tag="EXCLUDED", color=YELLOW)
        return 0
    emit(f"Adopt {preview['name']}", ("Tier", preview["tier"]), ("Cost", preview["entry"]["cost"]), ("Branch", preview["branch"]), tag="PREVIEW", color=CYAN)
    if not ctx.confirm(f"Add {preview['name']} to the registry and install it on {ctx.ssh.host}?", a.yes):
        stop("Not adopted", "You answered no. Nothing was changed.")
    out = off.propose(r, a.id, res, name=preview["name"], tier=a.tier, names=names)
    merge_branch(ctx, out["branch"], yes=True)
    rc = install_registry_change(ctx, a, name=preview["name"], present=True)
    if rc == 0:
        emit(f"{preview['name']} is adopted", f"{ctx.name} publishes its new class list within a minute.", tag="DONE", color=GREEN, run_label="Check the list", run="cw-run classes")
    return rc


def cmd_retire(ctx, a):
    r, p = ctx.repos, ctx.props
    r.need_setup()
    p.fetch()
    text = r.show_file(p.base, "registry.yaml") or ""
    where = regedit.class_block(text, a.cls)
    if not where:
        raise ClientError(f"There is no class called {a.cls!r} on main", "Nothing to retire.", [f"Classes: {', '.join(regedit.class_names(text))}"] if regedit.class_names(text) else None, example="cw-run classes")
    if regedit.is_profile_kind(text, a.cls):
        raise ClientError(f"{a.cls} cannot be retired", "It is what profiling runs on; without it nothing new can be tested on the worker.")
    block = text.splitlines()[where[0]:where[1]]
    print(paint(f"Retire the class {a.cls}", BOLD, stream=sys.stdout) + "\n")
    for line in block:
        print(paint("  - " + line, RED, stream=sys.stdout))
    emit("What happens", ("Registry", "the class is removed; the worker refuses new jobs of this class"), ("Queued jobs", "any still waiting for it are rejected"),
         ("Software", "packages it needed stay installed"), ("Profile history", "kept, so `cw-run offload fit` still remembers it was measured"), tag="NOTE", color=YELLOW)
    if not ctx.confirm(f"Remove {a.cls} and install the new registry on {ctx.ssh.host} now?", a.yes):
        stop("Not retired", "You answered no. Nothing was changed.")
    branch = f"chore/retire-{a.cls}"
    r.push_files(branch, {"registry.yaml": regedit.without_class(text, a.cls)}, f"chore(registry): retire the {a.cls} class\n\nRemoved from registry.yaml with cw-admin retire.", base=p.main)
    merge_branch(ctx, branch, yes=True)
    rc = install_registry_change(ctx, a, name=a.cls, present=False)
    if rc == 0:
        emit(f"{a.cls} is retired", tag="DONE", color=GREEN, run_label="Check the list", run="cw-run classes")
    return rc


# ---- release ----
def show_diff(ctx, sha, full=False):
    """Print what installing `sha` changes. Returns True if anything differs from what is installed."""
    now = release.installed_rev(ctx.ssh, ctx.name)
    out = sys.stdout
    if not now:
        emit("The installed version is unknown", "Showing the target only; everything counts as new.", tag="NOTE", color=YELLOW)
        return True
    if now == sha:
        emit("Nothing to change", f"{ctx.name} is already at {release.short(sha)}.", tag="OK", color=GREEN)
        return False
    files = ctx.source.changed_files(now, sha)
    if files is None:
        emit("Cannot compare the two versions", f"{release.short(now)} is not in the release source.", tag="NOTE", color=YELLOW)
        return True
    root = [f for f in files if release.is_root_run(f[1])]
    print(paint(f"{release.short(now)} -> {release.short(sha)}: {len(files)} file(s) change, {len(root)} of them run as root or set the sandbox", BOLD, stream=out) + "\n")
    for status, path in sorted(files, key=lambda f: (not release.is_root_run(f[1]), f[1])):
        print(f"  {paint('ROOT' if release.is_root_run(path) else '    ', BOLD, RED, stream=out)} {status}  {path}")
    print()
    print(ctx.source.diff_text(now, sha, () if full else release.ROOT_RUN))
    if not full and not root:
        print(paint("(no root-run files change; --full shows all the code)", DIM, stream=out))
    return True


def cmd_diff(ctx, a):
    show_diff(ctx, ctx.source.resolve(a.rev), a.full)
    return 0


def apply(ctx, a):
    sha = ctx.source.resolve(a.rev)
    changed = show_diff(ctx, sha, a.full)
    if not changed and not a.force:
        emit("Nothing to change", f"{ctx.name} is already at this version.", tag="OK", color=GREEN, run_label="Run the install again anyway", run="cw-admin apply --force")
        return 0
    if not ctx.confirm(f"Install {release.short(sha)} on {ctx.ssh.host}?", a.yes):
        stop("Not installed", "You answered no.")
    prev = release.installed_rev(ctx.ssh, ctx.name)
    rc = ctx.ssh.interactive(f"sudo {render.UPDATE_CMD} --rev {sha}")
    if rc:
        raise ClientError(f"The install failed on {ctx.ssh.host} (exit {rc})", "The update tool restores what it changed when it can. Read the messages above.", ["Fix the cause, then run apply again."])
    if not a.no_verify and not verify(ctx, sha, prev, a.yes):
        return 1
    if not getattr(a, "quiet_done", False):
        emit(f"Installed on {ctx.name}", "It publishes its new class list within a minute.", tag="DONE", color=GREEN, run_label="See what it can run now", run="cw-run classes")
    return 0


def verify(ctx, sha, prev, yes):
    """True if the worker is healthy after the install. If not, offers to put the previous version back (never automatically)."""
    out = sys.stdout
    print("\n" + paint("Checking that the worker still works", BOLD, CYAN, stream=out))
    checks = release.health_checks(ctx.ssh, release.health_command(ctx.name, ctx.config_dir, ctx.code_dir), sha)
    for what, ok, detail in checks:
        print(f"  {paint('OK  ' if ok else 'FAIL', BOLD, GREEN if ok else RED, stream=out)}  {what}  {paint('(' + detail + ')', DIM, stream=out)}")
    problems = [w for w, ok, _ in checks if not ok]
    if not problems and release.code_changed(ctx.source, prev, sha):
        ok, detail = selftest(ctx)
        if ok is not None:
            print(f"  {paint('OK  ' if ok else 'FAIL', BOLD, GREEN if ok else RED, stream=out)}  a test job ran and came back  {paint('(' + detail + ')', DIM, stream=out)}")
            if not ok:
                problems.append("a test job did not come back")
    if not problems:
        return True
    emit("The new version did not pass its checks", "; ".join(problems), tag="FAILED", color=RED)
    if not prev or prev == sha:
        emit("There is no earlier version to go back to", tag="NOTE", color=YELLOW)
        return False
    back = f"cw-admin apply {prev[:9]} --no-verify"
    if yes or not ctx.is_tty():
        emit("Not rolled back automatically", "Put the previous version back yourself:", tag="NOTE", color=YELLOW, run=back)
        return False
    if ctx.ask(paint(f"\nPut the previous version ({release.short(prev)}) back now? [y/N] ", BOLD, YELLOW)).strip().lower() != "y":
        emit("Left as it is", "To go back later:", tag="NOTE", color=YELLOW, run=back)
        return False
    rc = ctx.ssh.interactive(f"sudo {render.UPDATE_CMD} --rev {prev}")
    after = release.health_checks(ctx.ssh, release.health_command(ctx.name, ctx.config_dir, ctx.code_dir), prev) if rc == 0 else []
    ok = bool(after) and all(x[1] for x in after)
    emit(f"Rolled back to {release.short(prev)}" if rc == 0 else "The rollback failed", "The worker passes its checks again." if ok else "It still fails some checks; read the list above and the worker's journal.",
         tag="ROLLED BACK" if ok else "WARNING", color=GREEN if ok else RED)
    return False


def selftest(ctx, wait_s=240):
    """Run one real job end to end (GitHub, worker, guard, result). Returns (ok, detail); ok is None when no suitable class exists."""
    info = ctx.repos.registry_info() or {}
    if release.SELFTEST_CLASS not in (info.get("classes") or {}):
        return None, f"the worker has no {release.SELFTEST_CLASS} class to test with"
    rc = client_cli.main(["submit", release.SELFTEST_CLASS, "--wait", "--timeout", str(wait_s)])
    return rc == 0, "ok" if rc == 0 else f"exit {rc}"


# ---- parser and help ----
GROUPS = [("Worker", ["status", "check"]), ("Jobs", ["review", "approve", "cancel", "kill"]), ("Security", ["quarantine", "unlock", "output"]),
          ("Registry", ["adopt", "retire", "proposals"]), ("Release", ["diff", "apply"])]

HELP = {
    "status": ("health, what the worker is doing, recent use, and its safety checks", [("Show the report", "cw-admin status"), ("Look further back", "cw-admin status --hours 48")]),
    "check": ("check the connection to the worker", [("Is key login working, and do read-only commands need no password?", "cw-admin check")]),
    "review": ("show waiting jobs and note you viewed them (asks nothing)", [("Show every waiting job", "cw-admin review"), ("Show one", "cw-admin review profile-k7m2xq")]),
    "approve": ("approve a job exactly as you saw it, then watch it", [("Review, approve and wait for the result", "cw-admin approve profile-k7m2xq"), ("Approve every waiting job you viewed", "cw-admin approve --all")]),
    "cancel": ("stop a job that has not started yet", [("Cancel it", "cw-admin cancel some-job")]),
    "kill": ("stop a running job", [("Ask it to stop", "cw-admin kill some-job"), ("Stop the whole process tree", "cw-admin kill some-job --hard")]),
    "quarantine": ("list results held back by the output guard, or read one", [("List them", "cw-admin quarantine"), ("Read one in full", "cw-admin quarantine some-job")]),
    "unlock": ("clear a security lockdown after you have looked at it", [("Unlock", "cw-admin unlock")]),
    "output": ("the full output of a failed command (GitHub only gets its error lines)", [("List the kept outputs", "cw-admin output"), ("Read one", "cw-admin output some-job")]),
    "adopt": ("turn a finished profile run into a class and install it", [("Preview, ask once, then register, merge and install", "cw-admin adopt profile-k7m2xq --name ssh-version")]),
    "retire": ("remove a class from the registry and install that", [("Show what is removed, ask once, then merge and install", "cw-admin retire ssh-version")]),
    "proposals": ("list, merge or tidy the changes waiting on branches", [("See what is waiting", "cw-admin proposals"), ("Merge one", "cw-admin proposals merge registry/ssh-version"), ("Merge them all", "cw-admin proposals merge --all"), ("Delete merged branches", "cw-admin proposals tidy"), ("Sign the registry on main so the worker installs it", "cw-admin proposals publish")]),
    "diff": ("show what installing a version would change", [("Compare the installed version with the latest", "cw-admin diff"), ("All the code, not only root-run files", "cw-admin diff --full")]),
    "apply": ("install a version on the worker, check it, offer to roll back", [("Install the latest", "cw-admin apply"), ("Install one commit", "cw-admin apply 1a2b3c4d"), ("Install again even if nothing differs", "cw-admin apply --force")]),
}
EXAMPLES = [("See what needs your attention", "cw-admin proposals"), ("Approve a waiting job", "cw-admin approve <job>"), ("Install the latest release", "cw-admin apply")]


def build_parser():
    ap = argparse.ArgumentParser(add_help=False)
    sp = ap.add_subparsers(dest="cmd")
    yes = lambda s: s.add_argument("--yes", action="store_true", help="do not ask; answer yes")      # noqa: E731
    s = sp.add_parser("status", add_help=False)
    s.add_argument("--hours", type=int, default=12, metavar="N", help="hours of use to summarise (default %(default)s, at most 168)")
    sp.add_parser("check", add_help=False)
    s = sp.add_parser("review", add_help=False)
    s.add_argument("ids", nargs="*", metavar="job-id", help="job ids (default: every job waiting for approval)")
    s = sp.add_parser("approve", add_help=False)
    s.add_argument("id", nargs="?", help="the job id (cw-run jobs lists them)")
    s.add_argument("--all", action="store_true", help="approve every waiting job you have viewed and that has not changed since")
    s.add_argument("--no-follow", action="store_true", help="approve and return; do not wait for the result")
    s.add_argument("--ssh", action="store_true", help="approve by logging in to the worker (needs its sudo password) instead of signing here")
    yes(s)
    s = sp.add_parser("cancel", add_help=False)
    s.add_argument("id", help="the job id")
    s = sp.add_parser("kill", add_help=False)
    s.add_argument("id", help="the job id")
    s.add_argument("--hard", action="store_true", help="also kill the whole process tree")
    s = sp.add_parser("quarantine", add_help=False)
    s.add_argument("id", nargs="?", help="a job id, to read its withheld result in full")
    sp.add_parser("unlock", add_help=False)
    s = sp.add_parser("output", add_help=False)
    s.add_argument("id", nargs="?", help="a job id, to read the full output of its failed command")
    s = sp.add_parser("adopt", add_help=False)
    s.add_argument("id", help="the profile job id (cw-run jobs lists them)")
    s.add_argument("--name", help="the class name (default: the one chosen when profiling)")
    s.add_argument("--tier", choices=["auto", "approve"], default="auto", help="auto runs as soon as you submit it; approve waits for your OK each time")
    yes(s)
    s = sp.add_parser("retire", add_help=False)
    s.add_argument("cls", metavar="class", help="the class to remove (cw-run classes lists them)")
    yes(s)
    s = sp.add_parser("proposals", add_help=False)
    s.add_argument("action", nargs="?", choices=["list", "merge", "tidy", "publish"], help="list (default), merge, tidy, or publish (sign the registry on main so the worker installs it)")
    s.add_argument("--ssh", action="store_true", help="install over SSH (needs the worker's sudo password) instead of signing here")
    s.add_argument("branch", nargs="?", help="with merge: the branch")
    s.add_argument("-m", "--message", help="with merge: subject line for the squash commit")
    s.add_argument("--all", action="store_true", help="with merge: every pending proposal, one by one")
    yes(s)
    for n in ("diff", "apply"):
        s = sp.add_parser(n, add_help=False)
        s.add_argument("rev", nargs="?", help="a commit on the release branch (default: its latest)")
        s.add_argument("--full", action="store_true", help="show all the code that changes, not only the root-run files")
        if n == "apply":
            s.add_argument("--force", action="store_true", help="install even when the worker is already at this version")
            s.add_argument("--no-verify", action="store_true", help="do not check the worker (or run a test job) after installing")
            yes(s)
    return ap, sp


DISPATCH = {"status": cmd_status, "check": cmd_check, "review": cmd_review, "approve": cmd_approve, "cancel": cmd_cancel, "kill": cmd_kill,
            "quarantine": cmd_quarantine, "unlock": cmd_unlock, "output": cmd_output, "adopt": cmd_adopt, "retire": cmd_retire,
            "proposals": cmd_proposals, "diff": cmd_diff, "apply": apply}


def main(argv=None, ctx=None, cfg=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap, sp = build_parser()
    if clihelp.show_cli_help(["cw-admin", *argv], "cw-admin", "review, approve and install things on your worker", sp, HELP, EXAMPLES,
                             see=[("cw-run help", "send jobs and read results")], groups=GROUPS):
        return 0
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        emit("That is not a command cw-admin understands", "Run cw-admin help to see the commands.", tag="ERROR", color=RED)
        return 2
    if not a.cmd:
        emit("No command given", "Run cw-admin help to see the commands.", tag="ERROR", color=RED)
        return 2
    try:
        if ctx is None:
            ctx = Context.from_config(cfg or config.load())
        return DISPATCH[a.cmd](ctx, a)
    except ClientError as e:
        return fail_from(e)
    except Stop as e:
        return e.code
    except config.ConfigError as e:
        emit("The configuration has problems", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
