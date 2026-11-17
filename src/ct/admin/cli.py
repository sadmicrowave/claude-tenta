"""ct-admin: the administrator's commands, run on your own computer. Everything that changes the worker asks first and shows what it will do.

  WORKER     status [--hours N]     check
  JOBS       review [job...]        approve <job>|--all        cancel <job>      kill <job> [--hard]
  SECURITY   quarantine [job]       unlock                     output [job]
  REGISTRY   adopt <profile-job>    retire <class>             proposals [list|merge|tidy]
  RELEASE    diff [rev]             apply [rev]

Every name, address and folder comes from the configuration (ct.toml); nothing here knows an install. Passwords are never typed through
this program: sudo asks you itself, on your own terminal.
"""
import argparse
import json
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from .. import __version__, approvals, clihelp, config, registry, render, review, theme, updatecheck
from .. import ui as _ui
from ..client import offload as off
from ..client import cli as client_cli
from ..client.repo import ClientError, ClientRepos
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, paint
from ..setup import tui
from . import actionadd, jobs, regedit, release, selfupdate, workers
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
    def __init__(self, repos, ssh, source, *, name="claude-tenta", config_dir=None, code_dir=None, home=None, ask=input, is_tty=None,
                 run_result=None, main_branch="main", sleep=time.sleep, workers_file=None, cfg=None):
        self.repos, self.ssh, self.source, self.name = repos, ssh, source, name
        self.config_dir, self.code_dir = config_dir or f"/etc/{name}", code_dir or f"/opt/{name}"
        self.home = Path(home) if home else Path("~/.ct").expanduser()
        self.props, self.viewed = Proposals(repos, main_branch), jobs.Viewed(self.home)
        self.ask, self.sleep = ask, sleep
        self.is_tty = is_tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
        self.run_result = run_result or (lambda jid: client_cli.main(["result", jid, "--wait", "--approved"]))
        self.workers_file, self.cfg = Path(workers_file or (self.home / "workers.toml")), cfg

    @classmethod
    def from_config(cls, cfg):
        repos = ClientRepos(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch)
        home = Path(cfg.paths.client_home).expanduser()
        return cls(repos, Ssh(cfg.server.host, cfg.server.admin_user, cfg.server.ssh_port), release.Source(home / "source.git", cfg.updates.source, cfg.updates.branch),
                   name=cfg.identity.name, config_dir=cfg.paths.config, code_dir=cfg.paths.code, home=home, main_branch=cfg.github.main_branch,
                   workers_file=Path(cfg.source).parent / "workers.toml" if cfg.source else None, cfg=cfg)

    def fleet(self):
        """[(name, ClientRepos)] for every worker this computer administers: the main one first, then those in workers.toml."""
        out = [(self.name, self.repos)]
        for name, path in workers.load_list(self.workers_file).items():
            try:
                c = config.load(path, env={"CT_CONFIG": ""})
                out.append((name, ClientRepos(c.path("client_queue"), c.path("client_results"), c.queue_url, c.results_url, jobs_branch=c.github.jobs_branch)))
            except Exception:
                out.append((name, ClientRepos(self.home / name / "queue", self.home / name / "results", "", "")))       # listed but its configuration is unreadable: a row with no state
        return out

    def for_worker(self, name):
        """The same commands aimed at one named worker: its address, account and repositories come from its own configuration, listed once in workers.toml."""
        if name in (self.name, "main"):
            return self
        path = workers.load_list(self.workers_file).get(name)
        if not path:
            raise ClientError(f"There is no worker called {name!r}", "Workers are named once, in ct-admin workers add.", example="ct-admin workers")
        c = Context.from_config(config.load(path, env={"CT_CONFIG": ""}))
        c.ask, c.sleep, c.is_tty = self.ask, self.sleep, self.is_tty
        return c

    def targets(self, name=None):
        """[(name, Context)]: the one worker named, or every worker this computer administers."""
        if name:
            return [(name, self.for_worker(name))]
        return [(self.name, self)] + [(n, self.for_worker(n)) for n in workers.load_list(self.workers_file)]

    # the commands on the server, run as the worker through its own sudo entry point (read-only ones need no password)
    def remote(self, *args):
        return self.ssh.interactive(f"{render.WORKER_CMD} {self.ssh.quote(args)}")

    def confirm(self, prompt, yes=False):
        if yes:
            return True
        if not self.is_tty():
            raise ClientError("This needs your answer", "It asks a question, and nobody is at the keyboard.", ["Run it in a terminal, or add --yes if you are sure."])
        return self.ask(_ui.confirm_prompt(prompt)).strip().lower() == "y"


def fail_from(e):
    emit(e.title, e.why, tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
    return 2


# ---- worker ----
def cmd_status(ctx, a):
    rc = ctx.remote("status", "--hours", str(a.hours))
    _ui.related(("Jobs waiting for you", "ct-admin review"), ("Compare every worker you run", "ct-admin workers"), ("See what the worker can run", "ct-run classes"))
    return rc


def cmd_check(ctx, a):
    r = ctx.ssh._run(ctx.ssh.argv("true", batch=True), capture_output=True, text=True)
    if r.returncode:
        emit("Key login to the worker does not work", ("Host", ctx.ssh.host), ("Said", (r.stderr or "").strip() or "no answer"), "Password logins are not used by ct-admin.",
             tag="ERROR", color=RED, run_label="Set up SSH key login first", run=f"ssh-copy-id {ctx.ssh.target}")
        return 1
    rows = [("Key login", "works without a password")]
    r = ctx.ssh._run(ctx.ssh.argv(f"{render.WORKER_CMD} status", batch=True), capture_output=True, text=True)
    if r.returncode:
        emit(f"Connected to {ctx.ssh.host}", *rows, ("Read-only commands", f"`{render.WORKER_CMD} status` asked for a password or failed; they normally need none (access.sudo)"), tag="NOTE", color=YELLOW)
    else:
        emit(f"Connected to {ctx.ssh.host}", *rows, ("Read-only commands", f"`{render.WORKER_CMD}` works without a password"), tag="OK", color=GREEN)
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
        import json
        job = json.loads(raw)
        card = jobs.review_card(job, pub, ctx.name, info=class_info(ctx, job), digest=jobs.digest_of(raw))
        _ui.say(card.pop("title"), *card.pop("details"), stream=sys.stdout, **card)
        ctx.viewed.mark(jid, jobs.digest_of(raw))
    if gone:
        emit("Showing the worker's own copy", f"{len(gone)} job(s) are no longer on GitHub. It may ask for your sudo password once.", tag="NOTE", color=YELLOW)
        if ctx.remote("review", *gone):
            raise ClientError("Could not show those jobs", "They were NOT marked as viewed.", example="ct-admin check")
        for jid in gone:
            ctx.viewed.mark(jid, "")
    emit("Viewed", f"{len(todo)} job(s) noted as viewed.", tag="DONE", color=GREEN, run_label="Approve the ones you viewed", run="ct-admin approve --all")
    return 0


def cmd_approve(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.fetch_queue()
    return approve_all(ctx, a) if a.all else approve_one(ctx, a)


# ---- approving from this computer: sign here, publish, nothing to log in to ----
def class_info(ctx, job):
    """What the worker published about the job's class (None if it did not), so a job that runs as root is shown as such."""
    return ((ctx.repos.registry_info() or {}).get("classes") or {}).get(job.get("class"))


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
                          ["Run the install again; it gives the worker your key: ct-setup install", "Or approve this one by logging in to the worker: add --ssh"], example="ct-setup install")
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
            raise ClientError("Could not sign the approval", str(e), ["Is the key at " + str(approval_key(ctx)) + " the one ct-setup made? Run: ct-setup install"])
    ctx.repos.push_files(approvals.BRANCH, files, "approve " + ", ".join(j for j, _ in items))


def publish_registry(ctx):
    """Sign the registry.yaml that is on main of the queue repository, here, and publish the signature. The worker's registry-sync timer installs that exact
    text within minutes, and nothing else: GitHub access alone cannot change what the worker may run. Returns the digest that was signed."""
    ctx.repos.fetch_queue()
    text = ctx.repos.show_file(ctx.props.base, "registry.yaml")
    if text is None:
        raise ClientError("There is no registry.yaml on main to sign", "Nothing has been merged into your registry yet.", example="ct-admin proposals")
    import hashlib
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = approvals.make_registry_record(digest)
    try:
        signature = approvals.sign(record, approval_key(ctx), namespace=approvals.REGISTRY_NAMESPACE)
    except ValueError as e:
        raise ClientError("Could not sign the registry", str(e), ["Is the key at " + str(approval_key(ctx)) + " the one ct-setup made? Run: ct-setup install"])
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
            emit("Signed and published", f"{ctx.name} installs it within about five minutes, no login needed.", tag="NEXT", color=CYAN, run_label="Check later", run="ct-run classes")
        return 0
    return apply(ctx, argparse.Namespace(rev=None, yes=True, force=True, no_verify=False, full=False, quiet_done=True))


def send_order(ctx, action, job=None, hard=False, lock=None):
    """Sign an order to the worker (unlock, cancel or kill) here and publish it. The worker carries it out within about a minute, at most once, and only if the
    signature checks; nothing about it needs a login."""
    record = approvals.make_control_record(action, job=job, hard=hard, lock=lock)
    try:
        signature = approvals.sign(record, approval_key(ctx), namespace=approvals.CONTROL_NAMESPACE)
    except ValueError as e:
        raise ClientError("Could not sign the order", str(e), ["Is the key at " + str(approval_key(ctx)) + " the one ct-setup made? Run: ct-setup install"])
    nonce = json.loads(record)["nonce"]
    ctx.repos.push_files(approvals.BRANCH, {approvals.control_path(nonce): approvals.bundle(record, signature)}, f"control: {action}" + (f" {job}" if job else ""))


def wait_until(ctx, done, what, timeout=180, step=5):
    """In a terminal, poll the published results until done() is true. Returns True when it was."""
    if not ctx.is_tty():
        return False
    emit(f"Waiting for the worker to {what}", "It picks orders up within about a minute. Ctrl-C stops waiting, not the order.", tag="WAITING", color=CYAN)
    waited = 0
    while waited <= timeout:
        ctx.repos.pull_results()
        if done():
            return True
        ctx.sleep(step)
        waited += step
    return False


def approve_all(ctx, a):
    ready, skipped = jobs.classify(jobs.waiting(ctx.repos), ctx.viewed.load(), lambda j: jobs.job_bytes(ctx.repos, j))
    if not ready and not skipped:
        emit("Nothing is waiting for approval", tag="OK", color=GREEN)
        return 0
    _ui.table("Waiting jobs", ["state", "job", "why"], [[("SKIPPED", "warn"), jid, why] for jid, why in skipped] + [[("READY", "ok"), jid, "viewed"] for jid, _ in ready],
              tag="APPROVE", stream=sys.stdout)
    if not ready:
        raise ClientError("None of the waiting jobs have been viewed", "Review them first; that is what lets them be approved together.", example="ct-admin review")
    if not ctx.confirm(f"Approve these {len(ready)} job(s)?", a.yes):
        stop("Not approved", "You answered no. They stay waiting.")
    if use_signed(ctx, a):
        sign_and_publish(ctx, ready)
        emit("Approved", f"{len(ready)} job(s), signed on this computer. No login to {ctx.name} was needed.", tag="APPROVED", color=GREEN)
        rc = 0
    else:
        rc = ctx.remote("approve", "--many", *[f"{j}:{d}" for j, d in ready])
    if rc == 0:
        emit("Queued", f"{ctx.name} runs them one after another; you can go away.", tag="NEXT", color=CYAN, run_label="Check later", run="ct-run jobs")
    return rc


def approve_one(ctx, a):
    import json
    jid = jobs.require_id(a.id) if a.id else None
    if not jid:
        raise ClientError("Say which job to approve", "Give a job id, or --all for every job you have already viewed.", example="ct-admin approve --all")
    raw = jobs.job_bytes(ctx.repos, jid)
    if raw is None:
        return approve_on_worker(ctx, a, jid)
    digest, job = jobs.digest_of(raw), json.loads(raw)
    banner = ctx.repos.pull_results()
    if banner:
        emit(f"{ctx.name} is LOCKED", ("What happened", banner.get("kind")), ("Job", banner.get("job")), tag="LOCKED", color=RED, run_label="Review it, then unlock it",
             run=["ct-admin quarantine", "ct-admin unlock"])
    card = jobs.review_card(job, ctx.repos.read_result(jid), ctx.name, info=class_info(ctx, job), digest=digest)
    _ui.say(card.pop("title"), *card.pop("details"), stream=sys.stdout, **card)
    ctx.viewed.mark(jid, digest)
    if not ctx.confirm("Approve this exact job?", a.yes):
        stop("Not approved", "You answered no. The job stays waiting.")
    if use_signed(ctx, a):
        sign_and_publish(ctx, [(jid, digest)])
        emit("Approved", f"{jid}, signed on this computer. No login to {ctx.name} was needed; it picks the job up within seconds.", tag="APPROVED", color=GREEN)
        rc = 0
    else:
        rc = ctx.remote("approve", jid, "--sha256", digest)
    return follow(ctx, a, rc, jid)


def approve_on_worker(ctx, a, jid):
    """The file is gone from GitHub but the worker kept its own first-seen copy: review and approve it THERE, so what you read is what runs."""
    pub = ctx.repos.read_result(jid)
    if not pub or pub.get("status") != "awaiting_approval":
        raise ClientError(f"No job called {jid}", f"Its file is not on the jobs branch, and {ctx.name} does not show it waiting for approval.", example="ct-run jobs")
    emit("The job file is no longer on GitHub", f"{ctx.name} kept its own copy, so you review and approve it there.", tag="NOTE", color=YELLOW)
    rc = ctx.remote("approve", jid, *(["--yes"] if a.yes else []))
    ctx.viewed.mark(jid, "")
    return follow(ctx, a, rc, jid)


def follow(ctx, a, rc, jid):
    if rc == 0 and not a.no_follow and ctx.is_tty():
        emit("Approved: waiting for the result", "Ctrl-C stops waiting, not the job.", tag="WAITING", color=CYAN)
        return ctx.run_result(jid)
    return rc


def cmd_clean(ctx, a):
    """Remove the signed approvals and orders that are finished or stale from the approvals branch. They are harmless (the worker acts on each at most once, and an
    approval is for one exact file), but they would pile up. The current signed registry is always kept."""
    import time
    ctx.repos.fetch_queue()
    ref = f"origin/{approvals.BRANCH}"
    names = subprocess.run(["git", "-C", str(ctx.repos.queue), "ls-tree", "-r", "--name-only", ref], capture_output=True, text=True).stdout.split()
    if not names:
        emit("Nothing to tidy", "There are no signed approvals or orders on GitHub.", tag="OK", color=GREEN)
        return 0
    ctx.repos.pull_results()
    old, now = [], time.time()
    for name in names:
        if name == approvals.REGISTRY_PATH:
            continue
        try:
            rec = json.loads(json.loads(ctx.repos.show_file(ref, name) or "{}").get("record", "{}"))
        except ValueError:
            rec = {}
        if name.startswith(approvals.CONTROL_DIR + "/"):
            if rec.get("expires", 0) < now:
                old.append((name, "expired order"))
        elif name.startswith("approvals/"):
            result = ctx.repos.read_result(rec.get("job") or Path(name).stem)
            if (result and result.get("status") not in ("awaiting_approval", "running", "queued")) or rec.get("expires", 0) < now:
                old.append((name, "job finished or approval expired"))
    if not old:
        emit("Nothing to tidy", f"{len(names)} file(s) on GitHub, none finished or stale.", tag="OK", color=GREEN)
        return 0
    emit(f"{len(old)} file(s) can go", *[f"{n}  ({why})" for n, why in old[:12]], tag="TIDY", color=CYAN)
    if not ctx.confirm("Remove them from GitHub?", a.yes):
        stop("Nothing removed", "You answered no.")
    ctx.repos.push_files(approvals.BRANCH, {}, f"tidy: remove {len(old)} finished approvals and orders", remove=[n for n, _ in old])
    emit("Tidied", f"{len(old)} file(s) removed.", tag="DONE", color=GREEN)
    return 0


def cmd_update(ctx, a):
    """Is there a newer release? Show what changed, then (only if you say yes) update this computer and, separately, the worker."""
    entry = updatecheck.check_now(ctx.source.url)
    if "error" in entry:
        raise ClientError("Could not look for a newer release", entry["error"], [f"Is {ctx.source.url} reachable? Try: git ls-remote --tags {ctx.source.url}"])
    old = updatecheck.load(ctx.home)
    updatecheck.save(ctx.home, {**old, **entry})
    state = workers.read_state(ctx.repos.results) or {}
    tag = entry.get("tag")
    if not tag:
        emit("No releases yet", f"{ctx.source.url} has no release-X.Y.Z tag.", tag="NOTE", color=YELLOW)
        return 0
    info = updatecheck.pending(entry, __version__, state.get("release"))
    if not info:
        emit("Up to date", f"This computer is on {__version__} and the latest release is {tag}.", tag="OK", color=GREEN)
        return 0
    emit(f"{tag} is available", ("This computer", __version__), ("The worker", (state.get("release") or "unknown")[:9]), ("Latest release", f"{tag} ({entry['commit'][:9]})"), tag="UPDATE", color=CYAN)
    try:
        ctx.source.update()
        notes = ctx.source.git("tag", "-l", "--format=%(contents)", tag, check=False).stdout.strip()
        if notes:
            _ui.document("What changed", "\n".join(notes.splitlines()[:15]), tag="NOTES", stream=sys.stdout)
    except ClientError:
        pass                                                          # the notes are a courtesy; the update does not depend on them
    if a.check:
        return 0
    if info["this_computer"]:
        if ctx.confirm(f"Update this computer to {tag}?", a.yes):
            root = selfupdate.checkout_root()
            if root:
                new = selfupdate.update_checkout(root, ctx.source.url, tag)
                emit("This computer is updated", f"{root} is now at {new[:9]}.", tag="DONE", color=GREEN)
            else:
                selfupdate.update_package(ctx.source.url, tag)
                emit("This computer is updated", f"Installed {tag} with pip.", tag="DONE", color=GREEN)
        else:
            emit("Left as it is", "Nothing on this computer was changed.", tag="NOTE", color=YELLOW)
    if info["worker"]:
        if ctx.confirm(f"Install {tag} on {ctx.ssh.host} now? (this logs in to it and asks for its sudo password)", a.yes):
            return apply(ctx, argparse.Namespace(rev=entry["commit"], yes=a.yes, force=False, no_verify=False, full=False))
        emit("The worker was not touched", "To do it later:", tag="NOTE", color=YELLOW, run="ct-admin apply")
    return 0


def cmd_workers(ctx, a):
    """List and compare the workers this computer administers, from what each one published (no login to any of them)."""
    action = a.action or "list"
    wfile = ctx.workers_file
    if action == "add":
        if not a.name or not a.config:
            raise ClientError("Say the worker's name and its configuration file", "Each worker is a complete install with its own configuration.", example="ct-admin workers add studio --config ~/.config/claude-tenta/studio.toml")
        try:
            workers.add(wfile, a.name, a.config)
        except ValueError as e:
            raise ClientError("Could not add that worker", str(e))
        emit(f"Added {a.name}", f"Listed in {wfile}.", tag="DONE", color=GREEN, run_label="Compare them", run="ct-admin workers")
        return 0
    if action == "remove":
        if not a.name or not workers.remove(wfile, a.name):
            raise ClientError(f"There is no worker called {a.name!r} in the list", "The worker in your main configuration is always listed and is not removed here.", example="ct-admin workers")
        emit(f"Removed {a.name}", "Its install on its own machine was not touched.", tag="DONE", color=GREEN)
        return 0
    rows, problems = [], []
    for name, repos in ctx.fleet():
        try:
            if not a.no_refresh:
                repos.pull_results()
        except Exception as e:                                         # a worker that cannot be reached is a row, not a failure
            problems.append(f"{name}: could not refresh ({str(e).strip()[:80]})")
        rows.append((name, workers.read_state(repos.results)))
    cells = [workers.row_for(n, d) for n, d in rows]
    if a.claude:
        cells = [c for c in cells if c["has_claude"] and not c["locked"]]
    if a.json:
        print(json.dumps(cells, indent=2))                                          # output: data (--json)
        return 0
    out = sys.stdout
    if not cells:
        emit("No worker matches", "None of the listed workers has Claude access that works and is not locked." if a.claude else "No workers are listed.", tag="NOTE", color=YELLOW,
             run_label="See all of them", run="ct-admin workers")
        return 1 if a.claude else 0
    import shutil
    from .. import ui as ui_mod
    _ui.show([""] + workers.cards(cells, width=shutil.get_terminal_size((80, 24)).columns, color=ui_mod._color_on(out)), stream=out)
    if problems:
        emit("Some workers could not be refreshed", *[("Problem", p) for p in problems], tag="NOTE", color=YELLOW)
    _ui.related(("Only workers whose Claude access works", "ct-admin workers --claude"), ("Add another worker", "ct-admin workers add <name> --config <file>"),
                ("Stop listing a worker", "ct-admin workers remove <name>"), ("Look at one worker's health", "ct-admin status"))
    return 0


def cmd_action_hash(ctx, a):
    return ctx.remote("action-hash", a.path)


def cmd_action(ctx, a):
    """ct-admin action add|remove|list: root-level actions, registered and removed the same way adopt and retire handle classes."""
    ctx.repos.need_setup()
    if a.verb == "list":
        return action_list(ctx)
    if not a.source:
        raise ClientError(f"ct-admin action {a.verb} needs " + ("a definition file or a pack folder" if a.verb == "add" else "the name of an action, or the pack folder it came from"),
                          example="ct-admin action add packs/fan-control" if a.verb == "add" else "ct-admin action remove fan-profile")
    return action_add(ctx, a) if a.verb == "add" else action_remove(ctx, a)


def registry_text(ctx):
    ctx.props.fetch()
    return ctx.repos.show_file(ctx.props.base, "registry.yaml") or ""


def action_list(ctx):
    text = registry_text(ctx)
    acts = (registry.parse(text) or {}).get("actions") or {} if text else {}
    if not acts:
        emit("No root-level actions are registered", "Jobs on this worker never run anything as root.", tag="NOTE", color=YELLOW,
             run_label="Register one", run="ct-admin action add <definition file or pack folder>")
        return 0
    rows = []
    for name, act in acts.items():
        used = regedit.classes_using(text, name)
        rows.append((name, ("Program", " ".join(act.get("run") or [])), ("Pinned to", str(act.get("sha256", ""))[:16] + "…"), ("May write", ", ".join(act.get("writable") or []) or "nothing"),
                     ("Network", "yes" if act.get("network") else "none"), ("Class", ", ".join(used) or "none: nothing can run it")))
    for name, *facts in rows:
        emit(name, *facts, tag="ACTION", color=_ui.ROOT, stream=sys.stdout)
    _ui.related(("Register another", "ct-admin action add <definition file or pack folder>"), ("Pin one again after you changed its program", "ct-admin action add <definition file or pack folder>"),
                ("Remove one", "ct-admin action remove <name>"), ("Run one", "ct-run submit <class>"))
    return 0


def action_add(ctx, a):
    r, p = ctx.repos, ctx.props
    defn = actionadd.load_definition(a.source)
    problems = actionadd.check_definition(defn)
    if problems:
        raise ClientError("That definition would be refused", "\n".join(problems[:6]), ["Edit the YAML file to correct each problem listed above, then run the same command again. Nothing was sent anywhere."])
    text = registry_text(ctx)
    have = (registry.parse(text) or {}).get("actions") or {} if text else {}
    have_actions, have_classes = regedit.action_names(text), regedit.class_names(text)
    for name, action in defn["actions"].items():
        twin = next((n for n, x in have.items() if n != name and (x.get("run") or [None])[0] == actionadd.program_of(action)), None)
        if twin:
            raise ClientError(f"{actionadd.program_of(action)} is already registered, as {twin!r}", "The same program under a second name would be two ways to run it as root.",
                              [f"Use the existing one, or remove it first: ct-admin action remove {twin}"], example=f"ct-admin action remove {twin}")
    plan, pins = [], {}
    screen = None if a.yes else tui.make()
    for name, action in defn["actions"].items():
        info = actionadd.ask_worker(ctx, actionadd.program_of(action))
        pins[name] = info["sha256"]
        again = name in have_actions
        if again and (have.get(name) or {}).get("sha256") == info["sha256"] and all(c in have_classes for c in defn["classes"]):
            emit(f"{name} is already registered", ("Program", info["path"]), ("Pinned to", info["sha256"]), "\n", "The program on the worker is the one it is pinned to, and its class exists. Nothing to change.",
                 tag="OK", color=GREEN, run_label="See what is registered", run="ct-admin action list")
            return 0
        if screen is not None:                                              # a real terminal: the wizard's own card, with its buttons
            if not screen.protection(actionadd.card_info(ctx.ssh.target, name, action, info, again)):
                stop("Not registered", "You answered no. Nothing was changed.")
        else:
            emit(f"{'Pin again' if again else 'Register'} the root-level action {name}", *actionadd.summary(name, action, info),
                 "\n", f"Read the program before you say yes:  ssh {ctx.ssh.target} cat {info['path']}", tag="RUNS AS ROOT", color=_ui.ROOT)
        plan.append((name, action, info, again))
    new_classes = {n: b for n, b in defn["classes"].items() if n not in have_classes}
    if screen is None and not ctx.confirm(f"Register {', '.join(defn['actions'])} so jobs can run it as root on {ctx.ssh.host}?", a.yes):
        stop("Not registered", "You answered no. Nothing was changed.")
    for name, action, info, again in plan:
        if again:
            text = regedit.set_action_pin(text, name, info["sha256"]) or text
        else:
            text = regedit.add_entries(text, "actions", actionadd.entry_lines(name, action, info["sha256"]))
    for name, body in new_classes.items():
        text = regedit.add_entries(text, "classes", actionadd.class_lines(name, body))
    branch = "registry/action-" + "-".join(defn["actions"])[:40]
    r.push_files(branch, {"registry.yaml": text}, f"feat(registry): root-level action {', '.join(defn['actions'])}\n\nAdded with ct-admin action add; pinned to the program's SHA-256 as the worker reported it.", base=p.main)
    merge_branch(ctx, branch, yes=True)
    cls = next(iter(defn["classes"]), None)
    rc = install_registry_change(ctx, a, name=next(iter(new_classes), None), present=True)
    if rc == 0:
        emit("Ready", f"Jobs of the class {', '.join(defn['classes']) or ', '.join(defn['actions'])} run as root, each one only after you approve it.", tag="DONE", color=GREEN,
             run_label="Try one", run=f"ct-run submit {cls or '<class>'}")
    return rc


def action_remove(ctx, a):
    r, p = ctx.repos, ctx.props
    text = registry_text(ctx)
    known = regedit.action_names(text)
    src = Path(a.source).expanduser()
    names = list(actionadd.load_definition(a.source)["actions"]) if src.exists() else [a.source]       # a name, or the pack folder / file it came from
    missing = [n for n in names if n not in known]
    if missing:
        import difflib
        close = difflib.get_close_matches(missing[0], known, n=1, cutoff=0.5)
        raise ClientError(f"There is no root-level action called {missing[0]!r}", "Registered: " + (", ".join(known) or "none") + ".",
                          example=f"ct-admin action remove {close[0]}" if close else "ct-admin action list")
    removed_classes = []
    have = (registry.parse(text) or {}).get("actions") or {}
    screen = None if a.yes else tui.make()
    for name in names:
        classes = regedit.classes_using(text, name)
        removed_classes += classes
        if screen is not None:
            if not screen.protection(actionadd.card_remove_info(ctx.ssh.target, name, have.get(name) or {}, classes)):
                stop("Not removed", "You answered no. Nothing was changed.")
        else:
            emit(f"Remove the root-level action {name}", ("Program", " ".join((have.get(name) or {}).get("run") or [])), ("Class removed", ", ".join(classes) or "none"),
                 ("Queued jobs", "any still waiting for it are rejected"), ("The program", "stays on the worker, untouched"), tag="REMOVE", color=RED)
    if screen is None and not ctx.confirm(f"Remove {', '.join(names)} and install the new registry on {ctx.ssh.host} now?", a.yes):
        stop("Not removed", "You answered no. Nothing was changed.")
    for name in names:
        text = regedit.without_action(text, name)
    branch = "chore/remove-action-" + "-".join(names)[:40]
    r.push_files(branch, {"registry.yaml": text}, f"chore(registry): remove the root-level action {', '.join(names)}\n\nRemoved with ct-admin action remove, with the class that ran it.", base=p.main)
    merge_branch(ctx, branch, yes=True)
    rc = install_registry_change(ctx, a, name=removed_classes[0] if removed_classes else None, present=False)
    if rc == 0:
        emit("Removed", f"{', '.join(names)} can no longer run as root.", tag="DONE", color=GREEN, run_label="See what is left", run="ct-admin action list")
    return rc


def cmd_theme(ctx, a):
    """Show every colour role, what it looks like now and where the theme came from (so a theme.toml can be checked before you rely on it)."""
    rows = [(r, _ui.paint(f" {r} ", r if not r.endswith("_chip") else r, stream=sys.stdout) + "  " + what) for r, code, what in theme.preview()]
    emit("Colours", ("Theme", theme.source), *rows, *(("Problem", p) for p in theme.problems), tag="THEME" if not theme.problems else "PROBLEM", color=_ui.PRIMARY if not theme.problems else RED,
         steps=["Change a role in theme.toml (see docs/components/theme.md); every command picks it up, nothing else to edit."], steps_label="To change it", stream=sys.stdout)
    return 1 if theme.problems else 0


def cmd_cancel(ctx, a):
    jid = jobs.require_id(a.id)
    if not use_signed(ctx, a):
        return ctx.remote("cancel", jid)
    send_order(ctx, "cancel", job=jid)
    stopped = wait_until(ctx, lambda: (ctx.repos.read_result(jid) or {}).get("status") == "cancelled", "cancel it")
    emit("Cancelled" if stopped else "Order signed and published", f"{jid}: signed here, no login to {ctx.name} was needed." + ("" if stopped else " It is cancelled on the worker's next pass, if it has not started."),
         tag="DONE" if stopped else "NEXT", color=GREEN if stopped else CYAN)
    return 0


def cmd_kill(ctx, a):
    jid = jobs.require_id(a.id)
    if not use_signed(ctx, a):
        return ctx.remote("kill", jid, *(["--hard"] if a.hard else []))
    send_order(ctx, "kill", job=jid, hard=a.hard)
    stopped = wait_until(ctx, lambda: (ctx.repos.read_result(jid) or {}).get("status") in ("killed", "error", "limit", "ok"), "stop it")
    emit("Stopped" if stopped else "Order signed and published", f"{jid}: signed here, no login to {ctx.name} was needed." + ("" if stopped else " The worker stops it on its next pass, if it is still running."),
         tag="DONE" if stopped else "NEXT", color=GREEN if stopped else CYAN)
    return 0


# ---- security ----
def cmd_quarantine(ctx, a):
    rc = ctx.remote("quarantine", *([jobs.require_id(a.id)] if a.id else []))
    _ui.related(None if a.id else ("Read one held result in full", "ct-admin quarantine <job>"), ("Clear a lockdown after you have looked", "ct-admin unlock"))
    return rc


def cmd_output(ctx, a):
    rc = ctx.remote("output", *([jobs.require_id(a.id)] if a.id else []))
    _ui.related(None if a.id else ("Read one in full", "ct-admin output <job>"), ("See the job's published result", "ct-run result <job>"))
    return rc


def cmd_wake(ctx, a):
    """Tell the worker (or every worker) to stop idling and look for jobs now, over the same SSH login ct-admin check uses. No password: it changes only when the next check happens."""
    rc = 0
    for name, c in ctx.targets(a.worker):
        emit(f"Waking {name}", tag="WAKE", color=CYAN, stream=sys.stdout)
        rc = c.remote("wake") or rc
    return rc


def cmd_audit(ctx, a):
    """Read-only check that each worker still matches its configuration. It runs as root through the update entry point, so the worker's sudo asks for your password."""
    rc = 0
    for name, c in ctx.targets(a.worker):
        emit(f"Auditing {name}", tag="AUDIT", color=CYAN, stream=sys.stdout)
        rc = c.ssh.interactive(f"sudo {render.UPDATE_CMD} --audit") or rc
    return rc


def cmd_unlock(ctx, a):
    if not use_signed(ctx, a):
        return ctx.remote("unlock")
    banner = ctx.repos.pull_results()
    if not banner:
        emit("Not locked", f"{ctx.name} reports no lockdown.", tag="OK", color=GREEN)
        return 0
    emit(f"{ctx.name} is LOCKED", ("What happened", banner.get("kind")), ("Job", banner.get("job")), ("Since", banner.get("since")), tag="LOCKED", color=RED,
         run_label="Look at the details first (read-only, no password)", run="ct-admin quarantine")
    if not ctx.confirm("Unlock it? Only do this once you have looked at what happened.", getattr(a, "yes", False)):
        stop("Not unlocked", "You answered no. It stays locked.")
    send_order(ctx, "unlock", lock=banner.get("since"))
    cleared = wait_until(ctx, lambda: not ctx.repos.pull_results(), "unlock")
    emit("Unlocked" if cleared else "Unlock signed and published", f"Signed here; no login to {ctx.name} was needed." + ("" if cleared else " It unlocks on its next pass."),
         tag="DONE" if cleared else "NEXT", color=GREEN if cleared else CYAN)
    return 0


# ---- registry and proposals ----
def merge_branch(ctx, branch, *, yes=False, subject=None):
    """Show a branch, squash it into main (validating a registry change first), push, delete the branch. Returns the new main commit."""
    p = ctx.props
    p.check_name(branch)
    p.fetch()
    if p.contained(branch):
        emit(f"{branch} is already merged into main", "Nothing to merge.", tag="OK", color=GREEN)
        return p.git("rev-parse", p.base).stdout.strip()
    sha, msg, tree = p.squash(branch, subject)
    reg = p.tree_file(tree, "registry.yaml")
    if reg is not None:
        try:
            problems = registry.validate(registry.parse(reg), "/nonexistent-tools-root", allow_empty=True)
        except Exception as e:                                          # invalid YAML
            problems = [f"not valid YAML: {e}"]
        problems = [x for x in problems if "tools" not in x]
        if problems:
            raise ClientError(f"{branch} would leave an invalid registry", "\n".join(problems[:5]), [f"Correct the problem listed above on the {branch} branch of the queue repository, push it, then run this again. Nothing was merged."])
    subject_line, _, rest = msg.partition("\n")
    emit(f"Merge {branch} into main", ("Adds", f"{len(p.commits(branch))} commit(s), squashed into ONE"), *[("Commit", f"{h}  {s}  ({who}, {when})") for h, s, who, when in p.commits(branch)],
         *[("File", f"{path}  +{add} -{gone}") for path, add, gone in p.files(branch)], ("Becomes", subject_line),
         *([("Message", line) for line in rest.strip().splitlines() if line.strip()][:6]), tag="MERGE", color=_ui.PRIMARY)
    if not ctx.confirm(f"Squash-merge {branch} into main and delete the branch?", yes):
        stop("Not merged", "You answered no.")
    p.push_main(sha)
    gone = p.delete(branch)
    p.fetch()
    emit(f"Merged {branch} into main as {release.short(sha)}", "The branch was deleted." if gone else "The branch is still on GitHub; `ct-admin proposals tidy` removes it.",
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
        _ui.table("Changes waiting on branches", ["branch", "state", "last commit"],
                  [[b, ("merged", "muted") if merged else (f"{ahead} to review", "warn"), subj] for b, ahead, subj, merged in rows], tag="PROPOSALS", stream=sys.stdout)
        emit("What you can do with these", tag="NEXT", color=CYAN, run_label="Merge one, merge them all, or clean up", run=["ct-admin proposals merge <branch>", "ct-admin proposals merge --all", "ct-admin proposals tidy"])
        return 0
    if act == "merge":
        if a.all:
            return merge_all(ctx, a)
        if not a.branch:
            raise ClientError("Say which branch to merge", "Or use --all.", example="ct-admin proposals merge --all")
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
    raise ClientError(f"Unknown action {act!r}", "Use list, merge or tidy.", example="ct-admin help proposals")


def after_merge(ctx, a):
    """A merge changes GitHub only. With an approval key here the registry is signed and published at once; otherwise it waits for an install."""
    if use_signed(ctx, a):
        return install_registry_change(ctx, a)
    emit("Install it", "Merging changes GitHub only; the worker has it after the next install.", tag="NEXT", color=CYAN, run="ct-admin apply")
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
         run_label="Install what is on main", run="ct-admin apply")
    return 0 if len(merged) == len(pending) else 1


def cmd_adopt(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.pull_results()
    jobs.require_id(a.id)
    res = r.read_result(a.id)
    if not res:
        raise ClientError(f"No result for {a.id} yet", "Profile it first, then adopt it.", example=f"ct-run result {a.id} --wait")
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
        emit(f"{preview['name']} is adopted", f"{ctx.name} publishes its new class list within a minute.", tag="DONE", color=GREEN, run_label="Check the list", run="ct-run classes")
    return rc


def cmd_retire(ctx, a):
    r, p = ctx.repos, ctx.props
    r.need_setup()
    p.fetch()
    text = r.show_file(p.base, "registry.yaml") or ""
    where = regedit.class_block(text, a.cls)
    if not where:
        raise ClientError(f"There is no class called {a.cls!r} on main", "Nothing to retire.", [f"Classes: {', '.join(regedit.class_names(text))}"] if regedit.class_names(text) else None, example="ct-run classes")
    if regedit.is_profile_kind(text, a.cls):
        raise ClientError(f"{a.cls} cannot be retired", "It is what profiling runs on; without it nothing new can be tested on the worker.")
    block = text.splitlines()[where[0]:where[1]]
    emit(f"Retire the class {a.cls}", *[("Removes", line.strip()) for line in block if line.strip() and not line.strip().startswith("#")], tag="RETIRE", color=RED)
    emit("What happens", ("Registry", "the class is removed; the worker refuses new jobs of this class"), ("Queued jobs", "any still waiting for it are rejected"),
         ("Software", "packages it needed stay installed"), ("Profile history", "kept, so `ct-run offload fit` still remembers it was measured"), tag="NOTE", color=YELLOW)
    if not ctx.confirm(f"Remove {a.cls} and install the new registry on {ctx.ssh.host} now?", a.yes):
        stop("Not retired", "You answered no. Nothing was changed.")
    branch = f"chore/retire-{a.cls}"
    r.push_files(branch, {"registry.yaml": regedit.without_class(text, a.cls)}, f"chore(registry): retire the {a.cls} class\n\nRemoved from registry.yaml with ct-admin retire.", base=p.main)
    merge_branch(ctx, branch, yes=True)
    rc = install_registry_change(ctx, a, name=a.cls, present=False)
    if rc == 0:
        emit(f"{a.cls} is retired", tag="DONE", color=GREEN, run_label="Check the list", run="ct-run classes")
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
    _ui.table(f"{release.short(now)} to {release.short(sha)}", ["", "change", "file"],
              [[("ROOT", "bad") if release.is_root_run(path) else "", status, path] for status, path in sorted(files, key=lambda f: (not release.is_root_run(f[1]), f[1]))],
              tag="CHANGES", color=_ui.YELLOW if root else _ui.PRIMARY, stream=out,
              notes=(f"{len(files)} file(s) change; {len(root)} of them run as root or set the sandbox.",) + (() if full or root else ("No root-run files change; --full shows all the code.",)))
    _ui.document("The change itself", ctx.source.diff_text(now, sha, () if full else release.ROOT_RUN), tag="DIFF", stream=out)
    return True


def cmd_diff(ctx, a):
    show_diff(ctx, ctx.source.resolve(a.rev), a.full)
    return 0


def apply(ctx, a):
    sha = ctx.source.resolve(a.rev)
    changed = show_diff(ctx, sha, a.full)
    if not changed and not a.force:
        emit("Nothing to change", f"{ctx.name} is already at this version.", tag="OK", color=GREEN, run_label="Run the install again anyway", run="ct-admin apply --force")
        return 0
    if not ctx.confirm(f"Install {release.short(sha)} on {ctx.ssh.host}?", a.yes):
        stop("Not installed", "You answered no.")
    prev = release.installed_rev(ctx.ssh, ctx.name)
    rc = ctx.ssh.interactive(f"sudo {render.UPDATE_CMD} --rev {sha}")
    if rc:
        raise ClientError(f"The install failed on {ctx.ssh.host} (exit {rc})", "The update tool restores what it changed when it can. Read the messages above.", ["Read the error above: it names the step that failed. Correct it on the worker or on this computer, then run the same ct-admin apply command again."])
    if not a.no_verify and not verify(ctx, sha, prev, a.yes):
        return 1
    if not getattr(a, "quiet_done", False):
        emit(f"Installed on {ctx.name}", "It publishes its new class list within a minute.", tag="DONE", color=GREEN, run_label="See what it can run now", run="ct-run classes")
    return 0


def verify(ctx, sha, prev, yes):
    """True if the worker is healthy after the install. If not, offers to put the previous version back (never automatically)."""
    out = sys.stdout
    checks = list(release.health_checks(ctx.ssh, release.health_command(ctx.name, ctx.config_dir, ctx.code_dir), sha))
    problems = [w for w, ok, _ in checks if not ok]
    if not problems and release.code_changed(ctx.source, prev, sha):
        ok, detail = selftest(ctx)
        if ok is not None:
            checks.append(("a test job ran and came back", ok, detail))
            if not ok:
                problems.append("a test job did not come back")
    _ui.table("Checking that the worker still works", ["", "check", "detail"], [[("OK", "ok") if ok else ("FAIL", "bad"), what, detail] for what, ok, detail in checks],
              tag="CHECKED" if not problems else "FAILED", color=_ui.GREEN if not problems else _ui.RED, stream=out)
    if not problems:
        return True
    emit("The new version did not pass its checks", "; ".join(problems), tag="FAILED", color=RED)
    if not prev or prev == sha:
        emit("There is no earlier version to go back to", tag="NOTE", color=YELLOW)
        return False
    back = f"ct-admin apply {prev[:9]} --no-verify"
    if yes or not ctx.is_tty():
        emit("Not rolled back automatically", "Put the previous version back yourself:", tag="NOTE", color=YELLOW, run=back)
        return False
    if ctx.ask(_ui.confirm_prompt(f"Put the previous version ({release.short(prev)}) back now?")).strip().lower() != "y":
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
GROUPS = [("Worker", ["status", "check", "wake", "audit", "workers"]), ("Jobs", ["review", "approve", "cancel", "kill"]), ("Security", ["quarantine", "unlock", "output"]),
          ("Registry", ["adopt", "retire", "action", "proposals", "action-hash"]), ("Release", ["diff", "apply", "update", "clean"]), ("Look", ["theme"])]

HELP = {
    "status": ("health, what the worker is doing, recent use, and its safety checks", [("Show the report", "ct-admin status"), ("Look further back", "ct-admin status --hours 48")]),
    "workers": ("compare the workers you administer: hardware, Claude access and state, side by side", [("Compare them all", "ct-admin workers"), ("Only those with working Claude access", "ct-admin workers --claude"), ("Add another worker", "ct-admin workers add studio --config ~/.config/claude-tenta/studio.toml")]),
    "wake": ("end idle or sleep now: every worker, or one, looks for jobs right away", [("Wake every worker", "ct-admin wake"), ("Wake one", "ct-admin wake --worker studio")]),
    "audit": ("check that each worker still matches its configuration (read-only; asks for the worker's sudo password)", [("Audit every worker", "ct-admin audit"), ("Audit one", "ct-admin audit --worker studio")]),
    "check": ("check the connection to the worker", [("Is key login working, and do read-only commands need no password?", "ct-admin check")]),
    "review": ("show waiting jobs and note you viewed them (asks nothing)", [("Show every waiting job", "ct-admin review"), ("Show one", "ct-admin review profile-k7m2xq")]),
    "approve": ("approve a job exactly as you saw it, then watch it", [("Review, approve and wait for the result", "ct-admin approve profile-k7m2xq"), ("Approve every waiting job you viewed", "ct-admin approve --all")]),
    "cancel": ("stop a job that has not started yet", [("Cancel it", "ct-admin cancel some-job")]),
    "kill": ("stop a running job", [("Ask it to stop", "ct-admin kill some-job"), ("Stop the whole process tree", "ct-admin kill some-job --hard")]),
    "quarantine": ("list results held back by the output guard, or read one", [("List them", "ct-admin quarantine"), ("Read one in full", "ct-admin quarantine some-job")]),
    "unlock": ("clear a security lockdown after you have looked at it", [("Unlock", "ct-admin unlock")]),
    "output": ("the full output of a failed command (GitHub only gets its error lines)", [("List the kept outputs", "ct-admin output"), ("Read one", "ct-admin output some-job")]),
    "adopt": ("turn a finished profile run into a class and install it", [("Preview, ask once, then register, merge and install", "ct-admin adopt profile-k7m2xq --name ssh-version")]),
    "retire": ("remove a class from the registry and install that", [("Show what is removed, ask once, then merge and install", "ct-admin retire ssh-version")]),
    "action": ("register, list or remove commands that must run as root", [("Register one from a pack or a YAML file", "ct-admin action add packs/fan-control"), ("After you changed the program, pin it again", "ct-admin action add packs/fan-control"), ("See what is registered", "ct-admin action list"), ("Remove one and the class that runs it", "ct-admin action remove fan-profile")]),
    "theme": ("show the colour roles, what they look like and where the theme came from", [("Show them", "ct-admin theme")]),
    "action-hash": ("print the SHA-256 to pin for a root-level action's program, and check it could be one", [("Hash a program on the worker", "ct-admin action-hash /usr/local/sbin/fan-profile")]),
    "proposals": ("list, merge or tidy the changes waiting on branches", [("See what is waiting", "ct-admin proposals"), ("Merge one", "ct-admin proposals merge registry/ssh-version"), ("Merge them all", "ct-admin proposals merge --all"), ("Delete merged branches", "ct-admin proposals tidy"), ("Sign the registry on main so the worker installs it", "ct-admin proposals publish")]),
    "clean": ("remove signed approvals and orders that are finished or stale from GitHub", [("Show what can go and ask", "ct-admin clean")]),
    "update": ("is there a newer release? show what changed, then update this computer and the worker if you agree", [("Look and ask", "ct-admin update"), ("Only say whether there is one", "ct-admin update --check")]),
    "diff": ("show what installing a version would change", [("Compare the installed version with the latest", "ct-admin diff"), ("All the code, not only root-run files", "ct-admin diff --full")]),
    "apply": ("install a version on the worker, check it, offer to roll back", [("Install the latest", "ct-admin apply"), ("Install one commit", "ct-admin apply 1a2b3c4d"), ("Install again even if nothing differs", "ct-admin apply --force")]),
}
EXAMPLES = [("See what needs your attention", "ct-admin proposals"), ("Approve a waiting job", "ct-admin approve <job>"), ("Install the latest release", "ct-admin apply")]


def build_parser():
    ap = clihelp.Parser(prog="ct-admin", add_help=False)
    sp = ap.add_subparsers(dest="cmd")
    yes = lambda s: s.add_argument("--yes", action="store_true", help="do not ask; answer yes")      # noqa: E731
    s = sp.add_parser("status", add_help=False)
    s.add_argument("--hours", type=int, default=12, metavar="N", help="hours of use to summarise (default %(default)s, at most 168)")
    sp.add_parser("check", add_help=False)
    s = sp.add_parser("review", add_help=False)
    s.add_argument("ids", nargs="*", metavar="job-id", help="job ids (default: every job waiting for approval)")
    s = sp.add_parser("approve", add_help=False)
    s.add_argument("id", nargs="?", help="the job id (ct-run jobs lists them)")
    s.add_argument("--all", action="store_true", help="approve every waiting job you have viewed and that has not changed since")
    s.add_argument("--no-follow", action="store_true", help="approve and return; do not wait for the result")
    s.add_argument("--ssh", action="store_true", help="approve by logging in to the worker (needs its sudo password) instead of signing here")
    yes(s)
    s = sp.add_parser("action", add_help=False)
    s.add_argument("verb", choices=["add", "remove", "list"], help="add: register (or pin again) a root-level action | remove: take one away, with its class | list: show what is registered")
    s.add_argument("source", nargs="?", help="add: a YAML file or pack folder such as packs/fan-control | remove: an action's name, or that folder")
    yes(s)
    for verb in ("wake", "audit"):
        s = sp.add_parser(verb, add_help=False)
        s.add_argument("--worker", metavar="NAME", help="only this worker, by the name in ct-admin workers (default: every worker)")
    sp.add_parser("theme", add_help=False)
    s = sp.add_parser("action-hash", add_help=False)
    s.add_argument("path", help="the program on the worker, for example /usr/local/sbin/fan-profile")
    s = sp.add_parser("clean", add_help=False)
    yes(s)
    s = sp.add_parser("update", add_help=False)
    s.add_argument("--check", action="store_true", help="only say whether a newer release exists; change nothing")
    yes(s)
    s = sp.add_parser("workers", add_help=False)
    s.add_argument("action", nargs="?", choices=["list", "add", "remove"], help="list (default): compare the workers; add or remove one")
    s.add_argument("name", nargs="?", help="with add or remove: the worker's name")
    s.add_argument("--config", help="with add: the worker's configuration file")
    s.add_argument("--claude", action="store_true", help="only workers whose Claude access works and that are not locked")
    s.add_argument("--json", action="store_true", help="print the comparison as JSON")
    s.add_argument("--no-refresh", action="store_true", help="use what was last downloaded; do not pull from GitHub first")
    s = sp.add_parser("cancel", add_help=False)
    s.add_argument("id", help="the job id")
    s.add_argument("--ssh", action="store_true", help="log in to the worker (needs its sudo password) instead of signing the order here")
    s = sp.add_parser("kill", add_help=False)
    s.add_argument("id", help="the job id")
    s.add_argument("--hard", action="store_true", help="also kill the whole process tree")
    s.add_argument("--ssh", action="store_true", help="log in to the worker (needs its sudo password) instead of signing the order here")
    s = sp.add_parser("quarantine", add_help=False)
    s.add_argument("id", nargs="?", help="a job id, to read its withheld result in full")
    s = sp.add_parser("unlock", add_help=False)
    s.add_argument("--ssh", action="store_true", help="log in to the worker (needs its sudo password) instead of signing the order here")
    yes(s)
    s = sp.add_parser("output", add_help=False)
    s.add_argument("id", nargs="?", help="a job id, to read the full output of its failed command")
    s = sp.add_parser("adopt", add_help=False)
    s.add_argument("id", help="the profile job id (ct-run jobs lists them)")
    s.add_argument("--name", help="the class name (default: the one chosen when profiling)")
    s.add_argument("--tier", choices=["auto", "approve"], default="auto", help="auto runs as soon as you submit it; approve waits for your OK each time")
    yes(s)
    s = sp.add_parser("retire", add_help=False)
    s.add_argument("cls", metavar="class", help="the class to remove (ct-run classes lists them)")
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
            "quarantine": cmd_quarantine, "unlock": cmd_unlock, "output": cmd_output, "adopt": cmd_adopt, "retire": cmd_retire, "workers": cmd_workers, "update": cmd_update, "clean": cmd_clean, "action-hash": cmd_action_hash, "action": cmd_action, "theme": cmd_theme, "wake": cmd_wake, "audit": cmd_audit,
            "proposals": cmd_proposals, "diff": cmd_diff, "apply": apply}


def main(argv=None, ctx=None, cfg=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap, sp = build_parser()
    if clihelp.show_cli_help(["ct-admin", *argv], "ct-admin", "review, approve and install things on your worker", sp, HELP, EXAMPLES,
                             see=[("ct-run help", "send jobs and read results")], groups=GROUPS):
        return 0
    try:
        a = ap.parse_args(argv)
    except (clihelp.UsageError, SystemExit) as e:
        title, why, steps, example = clihelp.usage_problem(e, "ct-admin") if isinstance(e, clihelp.UsageError) else ("That is not a command ct-admin understands", "Run ct-admin help to see the commands.", None, None)
        emit(title, why, tag="ERROR", color=RED, steps=steps, steps_label="Try", run=example, run_label="Did you mean")
        return 2
    if not a.cmd:
        emit("No command given", "Run ct-admin help to see the commands.", tag="ERROR", color=RED)
        return 2
    try:
        real = ctx is None
        if ctx is None:
            cfg = cfg or config.load()
            ctx = Context.from_config(cfg)
        rc = DISPATCH[a.cmd](ctx, a)
        if real and a.cmd != "update" and not getattr(a, "json", False):
            updatecheck.after_command(cfg.updates, ctx.home, __version__, (workers.read_state(ctx.repos.results) or {}).get("release"))
        return rc
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
