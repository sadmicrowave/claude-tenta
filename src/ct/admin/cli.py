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
import copy
import sys
import textwrap
import time
from pathlib import Path

from .. import alias, cards, transport, __version__, approvals, clihelp, config, diffview, registry, render, review, theme, updatecheck
from .. import ui as _ui
from ..client import offload as off
from ..client import refuse
from ..client import cli as client_cli
from ..client.repo import ClientError, ClientRepos
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, paint
from ..setup import tui
from . import actionadd, jobs, regedit, release, selfupdate, watch as watch_mod, workerrepos, workers
from .proposals import Proposals
from .ssh import Ssh
from ..words import plural


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
        self.store = transport.store_for(cfg) if cfg is not None else "GitHub"              # what jobs and results pass through: the words of a message ("GitHub", "the mailbox")
        self.config_dir, self.code_dir = config_dir or f"/etc/{name}", code_dir or f"/opt/{name}"
        self.home = Path(home) if home else Path("~/.ct").expanduser()
        self.props, self.viewed = Proposals(repos, main_branch), jobs.Viewed(self.home)
        self.ask, self.sleep = ask, sleep
        self.is_tty = is_tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())
        self.run_result = run_result or (lambda jid: client_cli.main(["result", jid, "--wait", "--approved"]))
        self.workers_file, self.cfg = Path(workers_file or (self.home / "workers.toml")), cfg

    @classmethod
    def from_config(cls, cfg):
        repos = ClientRepos.from_config(cfg)
        home = Path(cfg.paths.client_home).expanduser()
        return cls(repos, Ssh.from_config(cfg), release.Source(home / "source.git", cfg.updates.source, cfg.updates.branch),
                   name=cfg.identity.name, config_dir=cfg.paths.config, code_dir=cfg.paths.code, home=home, main_branch=cfg.github.main_branch,
                   workers_file=Path(cfg.source).parent / "workers.toml" if cfg.source else None, cfg=cfg)

    def fleet(self):
        """[(name, ClientRepos)] for every worker this computer administers: the main one first, then those in workers.toml."""
        out = [(self.name, self.repos)]
        for name, path in workers.load_list(self.workers_file).items():
            try:
                c = config.load(path, env={"CT_CONFIG": ""})
                out.append((name, ClientRepos.from_config(c)))
            except Exception:
                out.append((name, ClientRepos(self.home / name / "queue", self.home / name / "results", "", "")))       # listed but its configuration is unreadable: a row with no state
        return out

    def for_worker(self, name):
        """The same commands aimed at one named worker: its address, account and repositories come from its own configuration, listed once in workers.toml."""
        from .. import alias
        name = alias.resolve(name, [self.name, "main", *workers.load_list(self.workers_file)])             # an alias means its worker
        if name in (self.name, "main"):
            return self
        path = workers.load_list(self.workers_file).get(name)
        if not path:
            raise refuse.error("placement.unknown_worker", ("Name", name))
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
            raise refuse.error("prompt.no_terminal")
        return self.ask(_ui.confirm_prompt(prompt)).strip().lower() == "y"


def fail_from(e):
    emit(e.title, *e.facts, cards.Why(e.why or ""), tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
    return 2


# ---- worker ----
def cmd_status(ctx, a):
    rc = ctx.remote("status", "--hours", str(a.hours))
    _ui.related(("Jobs waiting for you", "ct-admin review"), ("Compare every worker you run", "ct-admin workers"), ("See what a worker can run", "ct-run classes"))
    return rc


REMEMBER_WHERE_IT_ANSWERED = ("check", "status", "wake", "audit")


def cmd_check(ctx, a):
    r = ctx.ssh._run(ctx.ssh.argv("true", batch=True), capture_output=True, text=True)
    if r.returncode:
        emit("Key login to the worker does not work", ("Host", ctx.ssh.host), ("Said", (r.stderr or "").strip() or "no answer"), "Password logins are not used by ct-admin.",
             tag="ERROR", color=RED, run_label="Set up SSH key login first", run=f"ssh-copy-id {ctx.ssh.target}")
        return 1
    rows = [("Key login", "works without a password")]
    r = ctx.ssh._run(ctx.ssh.argv(f"{render.WORKER_CMD} status", batch=True), capture_output=True, text=True)
    if r.returncode:
        emit("Connected", ("Host", ctx.ssh.host), *rows, ("Read-only commands", f"`{render.WORKER_CMD} status` asked for a password or failed; they normally need none (access.sudo)"), tag="NOTE", color=YELLOW)
    else:
        emit("Connected", ("Host", ctx.ssh.host), *rows, ("Read-only commands", f"`{render.WORKER_CMD}` works without a password"), tag="OK", color=GREEN)
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
        emit("Showing the worker's own copy", f"{plural(len(gone), 'job')} are no longer on {ctx.store}. It may ask for your sudo password once.", tag="NOTE", color=YELLOW)
        if ctx.remote("review", *gone):
            raise refuse.error("review.show_failed", example="ct-admin check")
        for jid in gone:
            ctx.viewed.mark(jid, "")
    emit("Viewed", f"{plural(len(todo), 'job')} noted as viewed.", tag="DONE", color=GREEN, run_label="Approve the ones you viewed", run="ct-admin approve --all")
    return 0


def cmd_approve(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.fetch_queue()
    return approve_all(ctx, a) if a.all else approve_one(ctx, a)


# ---- approving from this computer: sign here, publish, nothing to log in to ----
def class_info(ctx, job):
    """What the worker published about the job's class (None if it did not), so a job that runs as root is shown as such, with what it published about which tool the class would really run
    (only for a class that names tool folders)."""
    info = ((ctx.repos.registry_info() or {}).get("classes") or {}).get(job.get("class"))
    if info and info.get("tools"):
        check = ((workers.read_state(ctx.repos.results) or {}).get("tool_check") or {}).get(job.get("class"))
        info = {**info, "tool_check": check}
    return info


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
        raise refuse.error("approval.key.untrusted", ("Worker", alias.show(ctx.name)), example="ct-setup install", has="a different one" if info["approvers"] else "none")
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
            raise refuse.error("approval.sign_failed", steps=["Is the key at " + str(approval_key(ctx)) + " the one ct-setup made? Run: ct-setup install"], reason=str(e))
    ctx.repos.push_files(approvals.BRANCH, files, "approve " + ", ".join(j for j, _ in items))


def publish_registry(ctx):
    """Sign the registry.yaml that is on main of the queue repository, here, and publish the signature. The worker's registry-sync timer installs that exact
    text within minutes, and nothing else: GitHub access alone cannot change what the worker may run. Returns the digest that was signed."""
    ctx.repos.fetch_queue()
    text = ctx.repos.show_file(ctx.props.base, "registry.yaml")
    if text is None:
        raise refuse.error("registry.none_on_main", example="ct-admin proposals")
    import hashlib
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    record = approvals.make_registry_record(digest)
    try:
        signature = approvals.sign(record, approval_key(ctx), namespace=approvals.REGISTRY_NAMESPACE)
    except ValueError as e:
        raise refuse.error("registry.sign_failed", steps=["Is the key at " + str(approval_key(ctx)) + " the one ct-setup made? Run: ct-setup install"], reason=str(e))
    ctx.repos.push_files(approvals.BRANCH, {approvals.REGISTRY_PATH: approvals.bundle(record, signature)}, f"registry: approve {digest[:12]}")
    return digest


def wait_for_registry(ctx, name, present, timeout=600, step=10):
    """Wait (in a terminal) until the worker's published class list shows `name` added (present) or removed. Returns True when it did."""
    if not ctx.is_tty():
        return False
    emit("Waiting for the worker to install it", "It checks about every five minutes. Ctrl-C stops waiting, not the install.", tag="WAITING", color=CYAN,
         run_label="To check on it", run=["ct-admin status"])
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
        raise refuse.error("order.sign_failed", steps=["Is the key at " + str(approval_key(ctx)) + " the one ct-setup made? Run: ct-setup install"], reason=str(e))
    nonce = json.loads(record)["nonce"]
    ctx.repos.push_files(approvals.BRANCH, {approvals.control_path(nonce): approvals.bundle(record, signature)}, f"control: {action}" + (f" {job}" if job else ""))


def wait_until(ctx, done, what, timeout=180, step=5):
    """In a terminal, poll the published results until done() is true. Returns True when it was."""
    if not ctx.is_tty():
        return False
    emit(f"Waiting for the worker to {what}", "It picks orders up within about a minute. Ctrl-C stops waiting, not the order.", tag="WAITING", color=CYAN,
         run_label="To check on it", run=["ct-admin status"])
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
    _ui.say("Waiting jobs", *cards.job_blocks([(jid, [("STATE", cards.Styled([("SKIPPED", "warn")])), ("WHY", why)]) for jid, why in skipped] + [(jid, [("STATE", cards.Styled([("READY", "ok")])), ("WHY", "viewed")]) for jid, _ in ready]),
            tag="APPROVE", color=_ui.PRIMARY, stream=sys.stdout)
    if not ready:
        raise refuse.error("review.none_viewed", example="ct-admin review")
    if not ctx.confirm(f"Approve these {plural(len(ready), 'job')}?", a.yes):
        stop("Not approved", "You answered no. They stay waiting.")
    if use_signed(ctx, a):
        sign_and_publish(ctx, ready)
        emit("Approved", f"{plural(len(ready), 'job')}, signed on this computer. No login to {ctx.name} was needed.", tag="APPROVED", color=GREEN)
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
        raise refuse.error("approve.which", example="ct-admin approve --all")
    raw = jobs.job_bytes(ctx.repos, jid)
    if raw is None:
        return approve_on_worker(ctx, a, jid)
    digest, job = jobs.digest_of(raw), json.loads(raw)
    if jobs.is_expired(raw):                                                     # before anything is shown or signed: the worker would refuse it, so do not let a person approve it
        raise refuse.error("job.expired.approve", ("Job", jid), ("Sent", str(job.get("created") or "unknown")), example=f"ct-run submit {job.get('class') or '<class>'}", name=ctx.name)
    banner = ctx.repos.pull_results()
    if banner:
        emit("A worker is LOCKED", ("Worker", alias.show(ctx.name)), ("What happened", banner.get("kind")), ("Job", banner.get("job")), tag="LOCKED", color=RED, run_label="Review it, then unlock it",
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
        raise refuse.error("job.unknown", ("Job", jid), example="ct-run jobs", name=ctx.name)
    emit(f"The job file is no longer on {ctx.store}", f"{ctx.name} kept its own copy, so you review and approve it there.", tag="NOTE", color=YELLOW)
    rc = ctx.remote("approve", jid, *(["--yes"] if a.yes else []))
    ctx.viewed.mark(jid, "")
    return follow(ctx, a, rc, jid)


def follow(ctx, a, rc, jid):
    if rc == 0 and not a.no_follow and ctx.is_tty():
        emit("Approved: waiting for the result", "Ctrl-C stops waiting, not the job. cancel stops a job that has not started; kill stops one that is running.", tag="WAITING", color=CYAN,
             run_label="To check on it or stop it", run=[f"ct-run result {jid}", f"ct-admin cancel {jid}", f"ct-admin kill {jid}"])
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
        emit("Nothing to tidy", f"There are no signed approvals or orders on {ctx.store}.", tag="OK", color=GREEN)
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
        emit("Nothing to tidy", f"{plural(len(names), 'file')} on {ctx.store}, none finished or stale.", tag="OK", color=GREEN)
        return 0
    emit(f"{plural(len(old), 'file')} can go", *[f"{n}  ({why})" for n, why in old[:12]], tag="TIDY", color=CYAN)
    if not ctx.confirm(f"Remove them from {ctx.store}?", a.yes):
        stop("Nothing removed", "You answered no.")
    ctx.repos.push_files(approvals.BRANCH, {}, f"tidy: remove {len(old)} finished approvals and orders", remove=[n for n, _ in old])
    emit("Tidied", f"{plural(len(old), 'file')} removed.", tag="DONE", color=GREEN)
    return 0


def _latest_release(ctx):
    """(entry, tag): the newest release, asked once. tag is None when the source has none yet."""
    entry = updatecheck.check_now(ctx.source.url)
    if "error" in entry:
        raise refuse.error("update.check_failed", steps=[f"Is {ctx.source.url} reachable? Try: git ls-remote --tags {ctx.source.url}"], reason=entry["error"])
    updatecheck.save(ctx.home, {**updatecheck.load(ctx.home), **entry})
    return entry, entry.get("tag")


def _show_release_notes(ctx, tag):
    try:
        ctx.source.update()
        notes = ctx.source.git("tag", "-l", "--format=%(contents)", tag, check=False).stdout.strip()
        if notes:
            _ui.document("What changed", "\n".join(notes.splitlines()[:15]), tag="NOTES", stream=sys.stdout)
    except ClientError:
        pass                                                          # the notes are a courtesy; the update does not depend on them


def _update_this_computer(ctx, a, tag):
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


def cmd_update(ctx, a):
    """Is there a newer release? Show what changed, then (only if you say yes) update this computer and, separately, the worker."""
    entry, tag = _latest_release(ctx)
    state = workers.read_state(ctx.repos.results) or {}
    if not tag:
        emit("No releases yet", f"{ctx.source.url} has no release-X.Y.Z tag.", tag="NOTE", color=YELLOW)
        return 0
    info = updatecheck.pending(entry, __version__, state.get("release"), behind=lambda w, t: updatecheck.is_behind(ctx.home, w, t) is not False)         # asked to update: offer the worker unless it is provably AHEAD of the release
    if not info:
        emit("Up to date", f"This computer is on {__version__} and the latest release is {tag}.", tag="OK", color=GREEN)
        return 0
    emit("A new release is available", ("Latest release", f"{tag} ({entry['commit'][:9]})"), ("This computer", __version__), ("The worker", (state.get("release") or "unknown")[:9]), tag="UPDATE", color=CYAN)
    _show_release_notes(ctx, tag)
    if a.check:
        return 0
    if info["this_computer"]:
        _update_this_computer(ctx, a, tag)
    if info["worker"]:
        if ctx.confirm(f"Install {tag} on {ctx.ssh.host} now? (this logs in to it and asks for its sudo password)", a.yes):
            return apply(ctx, argparse.Namespace(rev=entry["commit"], yes=a.yes, force=False, no_verify=False, full=False))
        emit("The worker was not touched", "To do it later:", tag="NOTE", color=YELLOW, run="ct-admin apply")
    return 0


def update_plan(c, entry, home):
    """What `update` would do to one worker: "left" (it runs another platform), "current" (provably at or ahead of the release) or "update" (behind, or its release is not known: apply decides over SSH)."""
    state = workers.read_state(c.repos.results) or {}
    if state.get("platform") and state["platform"] != release.PLATFORM:
        return "left", state["platform"]
    rel = state.get("release")
    if not rel:
        return "update", None
    behind = updatecheck.pending(entry, "999.0.0", rel, behind=lambda w, t: updatecheck.is_behind(home, w, t) is not False)      # as `update` always asked: behind or not known is offered, only provably ahead is not. This computer's own version is not what is asked here: only whether THIS worker is behind
    return ("update" if behind and behind["worker"] else "current"), rel


def update_roster(ctx, targets, entry, tag, plans):
    """The devices an update would touch, said before anything is installed or asked."""
    local = updatecheck.pending(entry, __version__)
    by = {}
    for (name, c), (what, extra) in zip(targets, plans):
        by.setdefault(what, []).append(f"{alias.show(name)} ({c.ssh.host}{', runs ' + extra if what == 'left' else ''})")
    rows = [("This computer", f"{__version__} to {tag}" if local else f"{__version__}, already current")]
    rows += [(label, ", ".join(by[key])) for key, label in (("update", "To update"), ("current", "Already current"), ("left", "Left alone")) if key in by]
    emit(f"The update would touch {plural(len(targets) + 1, 'device')}", *rows, ("Release", f"{tag} ({entry['commit'][:9]})"), tag="ROSTER", color=CYAN, stream=sys.stdout)


def cmd_update_fleet(ctx, targets, a):
    """`update` with several workers: the release is looked up once, what changed is shown once, this computer is updated once, then each worker is updated in turn like `apply` does, and one card says how each went."""
    entry, tag = _latest_release(ctx)
    if not tag:
        emit("No releases yet", f"{ctx.source.url} has no release-X.Y.Z tag.", tag="NOTE", color=YELLOW)
        return 0
    plans = [update_plan(c, entry, ctx.home) for _, c in targets]
    local = updatecheck.pending(entry, __version__)
    if not local and all(w == "current" for w, _ in plans):
        emit("Up to date", f"This computer is on {__version__}, every worker is at {tag}.", tag="OK", color=GREEN)
        return 0
    emit("A new release is available", ("Latest release", f"{tag} ({entry['commit'][:9]})"), ("This computer", __version__), tag="UPDATE", color=CYAN)
    _show_release_notes(ctx, tag)
    update_roster(ctx, targets, entry, tag, plans)
    if a.check:
        return 0
    if local:
        _update_this_computer(ctx, a, tag)
    todo = [t for t, (w, _) in zip(targets, plans) if w != "current"]
    sub = argparse.Namespace(rev=entry["commit"], yes=a.yes, force=False, no_verify=False, full=False, outcomes=[(n, "current") for (n, _), (w, _) in zip(targets, plans) if w == "current"], worker=None, cmd="apply")
    rc = run_fleet(ctx, "apply", todo, sub, label="update") if todo else 0
    fleet_apply_summary(sub.outcomes, entry["commit"], verb="update")
    return rc


def cmd_locate(ctx, a):
    """Find a worker whose address changed: scan this computer's networks, read the SSH host keys of what answers (nothing is logged in to) and report the machine that shows the key this computer
    recorded for that worker. A name is never proof; the key is. Offers to save the new address (asks first, keeps the old one)."""
    from .. import rediscover
    from ..setup import discover
    name = a.worker or ctx.name
    c = ctx.for_worker(name)
    host, port = c.ssh.host, c.ssh.port
    recorded = rediscover.recorded_keys(host, port)
    nets = discover.local_networks()
    real, virtual = [(i, n) for i, n in nets if not discover.is_virtual(i)], [(i, n) for i, n in nets if discover.is_virtual(i)]
    emit("Looking for the worker", f"Checking where {name} was, then the quicker places, then your networks. Each step stops as soon as a computer shows the key this computer recorded for the worker.", tag="SCAN", color=_ui.PRIMARY, stream=sys.stdout)
    m, rung, devices = rediscover.ladder(host, port, name, ctx.home, recorded=recorded, scan_real=lambda until: discover.scan(real, until=until) if real else ([], []), scan_virtual=lambda until: discover.scan(virtual, until=until) if virtual else ([], []))
    if m.kind == "one" and rung == "the configured address":
        rediscover.remember(ctx.home, name, host, port)
        emit("A worker is where its configuration says", ("Worker", alias.show(name)), ("Address", host), "Its SSH host key is the one this computer recorded.", tag="OK", color=GREEN, stream=sys.stdout)
        return 0
    by_ip = {d.ip: d for d in devices}
    if m.kind != "one":
        steps = {"unknown": ["Connect to it once from here so its key is recorded:  ssh " + c.ssh.target + " true"], "none": ["Check that it is on and on the same network, then run this again.", "A computer that is on a different network cannot be found from here.",
                 f"If the worker was reinstalled, it has a new SSH key. Forget the old one with  ssh-keygen -R {host}  and connect once  (ssh {c.ssh.target} true)  to trust the new one."],
                 "several": ["Find which one it is by its name or what it is, then set server.host in its configuration by hand."]}[m.kind]
        emit("The worker was not found", m.note, *[("Also found", ", ".join(m.addresses))] * bool(m.addresses), tag="NOT FOUND", color=YELLOW, steps=steps, steps_label="What to do", stream=sys.stdout)
        return 1
    ip = m.addresses[0]
    d = by_ip.get(ip)
    emit("A worker was found", ("Worker", alias.show(name)), ("Address", ip), ("Name it announces", (d.name if d and d.name else "none")), ("Proof", "it shows the SSH host key this computer recorded for the worker"), ("Found by", rung), tag="FOUND", color=GREEN, stream=sys.stdout)
    path = getattr(c.cfg, "source", None) if c.cfg is not None else None
    if not path:
        emit("Set the new address by hand", "There is no configuration file to change for this worker.", tag="NEXT", color=YELLOW, run=f"ct-setup install --set server.host={ip}", stream=sys.stdout)
        return 0
    if not (a.yes or ctx.confirm(f"Save {ip} as the address of {name}? The old one ({host}) is kept in the file as a comment.")):
        emit("Nothing was changed", f"The address of {name} is still {host}.", tag="STOPPED", color=YELLOW, stream=sys.stdout)
        return 0
    was = rediscover.save_address(path, ip)
    note = []
    try:
        from ..client.repo import ClientRepos
        new = config.load(path, env={"CT_CONFIG": ""})
        ClientRepos.from_config(new).clone_all(Path(new.paths.client_home).expanduser())
    except Exception as e:                                              # the address is saved; pointing the local clones at it is what `ct-run setup` does
        note.append(f"Point this computer's copies at it with `ct-run setup` ({' '.join(str(e).split())[:120]}).")
    emit("A worker's address was updated", ("Worker", alias.show(name)), ("Now", ip), ("Was", was), ("Configuration", str(path)), *note, tag="DONE", color=GREEN, run="ct-admin check", stream=sys.stdout)
    return 0


RELATION_TEXT = {"current": "the release apply would install", "behind": "behind the release apply would install", "unpublished": "a commit that is not in the release source",
                 "unknown": "cannot be compared"}


def fleet_releases(ctx):
    """[(worker, release, relation)] from what each worker last published (no login to any of them), compared with the release `ct-admin apply` would install."""
    try:
        ctx.source.update()                                          # best effort: with no network the comparison is made against what was last fetched
    except Exception:
        pass
    out = []
    for name, repos in ctx.fleet():
        doc = workers.read_state(repos.results) or {}
        rev = (doc.get("release") or "")[:40]
        try:
            rel = ctx.source.relation(rev) if rev else "unknown"
        except Exception:
            rel = "unknown"
        out.append((name, rev, rel))
    return out


def release_signature(ctx):
    """(signature, plain text) when the workers do not all run the release apply would install, else None. The signature changes only when who-runs-what changes (so the alert is not repeated)."""
    rows = fleet_releases(ctx)
    revs = {r for _, r, _ in rows if r}
    if not rows or (len(revs) <= 1 and all(rel in ("current", "unknown") for _, _, rel in rows)):
        return None
    return ",".join(f"{n}:{r[:9]}:{rel}" for n, r, rel in rows), "; ".join(f"{n} runs {r[:9] or '?'} ({RELATION_TEXT[rel]})" for n, r, rel in rows) + ". ct-admin apply brings them together."


def release_card(ctx, stream=None):
    """One card when the workers do not all run the release `ct-admin apply` would install (or each other's): who runs what, and the one command that brings them together. Returns True if shown."""
    rows = fleet_releases(ctx)
    revs = {r for _, r, _ in rows if r}
    if not rows or (len(revs) <= 1 and all(rel in ("current", "unknown") for _, _, rel in rows)):
        return False
    odd = [n for n, _, rel in rows if rel == "unpublished"]
    steps = ["ct-admin apply (every worker in turn, each shown what changes first)"] if any(rel == "behind" for _, _, rel in rows) else []
    if odd:
        steps.append(f"{', '.join(odd)} runs a commit the release source does not have: publish it (merge it into the release branch and push it) and then apply, or replace it with the published release: ct-admin apply --force --worker {odd[0]}")
    emit("The workers do not all run the same release", *cards.named([(alias.show(n), f"{r[:9] or '?'}  {RELATION_TEXT[rel]}") for n, r, rel in rows]),
         "Mixed releases mean a worker may lack a fix or a class the others have, and a job may behave differently depending on where it lands.",
         steps=steps or None, steps_label="How to bring them together", tag="RELEASES", color=YELLOW, stream=stream or sys.stdout)
    return True


def real_names(ctx):
    return [n for n, _ in ctx.fleet()]


def workers_rename(ctx, a):
    """`ct-admin workers rename WORKER NEW`: give a worker a name for the ct- commands (an alias, kept on this computer only; the worker, its hostname and its install are not touched).
    `ct-admin workers rename` with nothing else asks about every worker in turn. `--clear` removes an alias; `--mask` / `--show-real` choose whether the real name is shown beside it."""
    names = real_names(ctx)
    aliases, show_real = alias.load()
    if a.mask or a.show_real:
        alias.save(aliases, show_real=not a.mask)
        emit("Worker names are masked" if a.mask else "Worker names show the real name beside the alias", ("File", str(alias.path())), tag="DONE", color=GREEN)
        return 0
    if a.name:
        real = alias.resolve(a.name, names)
        if real not in names:
            raise refuse.error("placement.unknown_worker", ("Name", a.name), ("Yours", ", ".join(names)))
        if a.clear:
            aliases.pop(real, None)
        else:
            if not a.new:
                raise refuse.error("worker.rename.name_missing", ("Worker", alias.show(real)), example=f"ct-admin workers rename {real} <name>")
            problem = alias.check(a.new, real, names)
            if problem:
                raise refuse.error("worker.rename.unusable", ("Name", a.new), problem=problem)
            aliases[real] = a.new
        alias.save(aliases, show_real)
        emit("The name was cleared" if a.clear else "The worker has a new name", ("Worker", alias.show(real)), ("File", str(alias.path())), "Only this computer knows the name; the worker and its hostname are unchanged.", tag="DONE", color=GREEN,
             run="ct-admin workers", run_label="See them")
        return 0
    return workers_rename_all(ctx, names, aliases, show_real)


def workers_rename_all(ctx, names, aliases, show_real):
    """The fleet wizard: each worker in turn, its current name beside the question. Enter keeps it, `-` removes it, anything else is the new name."""
    if not ctx.is_tty:
        raise refuse.error("worker.rename.no_terminal")
    emit("Name your workers", "Each worker is asked about in turn. Enter keeps its name, - removes the name, anything else is the new name.",
         "The names live on this computer only: the workers and their hostnames are not touched.", tag="RENAME", color=CYAN)
    changed = 0
    for real in names:
        while True:
            now = aliases.get(real)
            answer = ctx.ask(f"  {real}   [{now or 'no name yet'}]  new name: ").strip()
            if not answer:
                break
            if answer == "-":
                changed += aliases.pop(real, None) is not None
                break
            problem = alias.check(answer, real, names)
            if problem:
                emit("That name cannot be used", ("Name", answer), problem, tag="NOTE", color=YELLOW)
                continue
            aliases[real] = answer
            changed += 1
            break
    alias.save(aliases, show_real)
    emit(f"{plural(changed, 'name')} changed", ("File", str(alias.path())), tag="DONE", color=GREEN, run="ct-admin workers", run_label="See them")
    return 0


def workers_use(ctx, a):
    """`ct-admin workers use WORKER --command CLASS`: dedicate a command to a worker for every folder on this computer (it is written to ~/.tenta.toml; ct-run use does the same for one folder)."""
    import argparse as ap
    here = client_cli.here
    pins = here.global_pins()
    if a.clear:
        if a.command:
            if pins.commands.pop(a.command, None) is None:
                emit("That command was not dedicated to a worker", ("Command", a.command), ("File", str(pins.path)), tag="NOTE", color=YELLOW)
                return 0
        elif a.name:
            pins.commands = {c: w for c, w in pins.commands.items() if w != a.name}
            pins.worker = None if pins.worker == a.name else pins.worker
        else:
            pins.worker, pins.commands = None, {}
        here.save(pins)
        emit("Released" if a.name else "Cleared", ("File", str(pins.path) if not pins.empty() else f"{pins.path} (removed, nothing left in it)"), tag="DONE", color=GREEN, run="ct-admin workers use", run_label="See what is left")
        return 0
    if not a.name:
        return client_cli.cmd_use(ctx, ctx.cfg, ap.Namespace(worker=None, command=None, clear=False, glob=True, force=False, override=False, retain=False))
    rc = client_cli.cmd_use(ctx, ctx.cfg, ap.Namespace(worker=a.name, command=a.command, clear=False, glob=True, force=a.force, override=False, retain=True))
    return rc


def repos_choice(ctx, a, mine):
    """keep | archive | delete for a removed worker's repositories. Never silent: --repos says it, a person at a terminal is asked (default keep, delete asks twice), anything else keeps."""
    if not mine:
        return "keep"
    how = getattr(a, "repos", None)
    if not how:
        if not ctx.is_tty():
            return "keep"
        emit("Its repositories on GitHub", *[("Repository", s) for _, s in mine], "Archive keeps them read-only; delete removes them and their deploy keys for good.", tag="ASK", color=YELLOW)
        how = {"a": "archive", "d": "delete"}.get(ctx.ask("Keep, archive or delete them? [k/a/d, default keep] ").strip().lower()[:1], "keep")
    if how == "delete" and not ctx.confirm("Delete " + " and ".join(s for _, s in mine) + " for good (no undo)?", getattr(a, "yes", False)):
        return "keep"
    return how


def cmd_workers(ctx, a):
    """List and compare the workers this computer administers, from what each one published (no login to any of them)."""
    action = a.action or "list"
    wfile = ctx.workers_file
    if action == "add":
        if not a.name or not a.config:
            raise refuse.error("worker.add.args", example="ct-admin workers add studio --config ~/.config/claude-tenta/studio.toml")
        try:
            workers.add(wfile, a.name, a.config)
        except ValueError as e:
            if getattr(e, "code", "") == "worker.repo.shared":                 # its own card, with the way out
                raise refuse.error("worker.repo.shared", ("Worker", alias.show(a.name)), **e.facts)
            raise refuse.error("worker.add.failed", reason=str(e))
        prepared = []
        if getattr(a, "github", False):
            try:
                prepared = workerrepos.prepare(config.load(Path(a.config).expanduser(), env={"CT_CONFIG": ""}), Path(a.config).expanduser(), wfile)
            except Exception as e:                                                   # listed already; the repositories step is said, and runs again with the same command
                if getattr(e, "code", "") == "worker.repo.shared":
                    raise refuse.error("worker.repo.shared", ("Worker", alias.show(a.name)), **e.facts)
                raise refuse.error("worker.add.failed", reason=str(e))
        emit("Worker added", ("Worker", alias.show(a.name)), ("Listed in", str(wfile)), *[("Queue", f"{w} ({s})") for r, w, s in prepared if r == "queue"], *[("Results", f"{w} ({s})") for r, w, s in prepared if r == "results"], *([("Clones", str(Path(prepared[-1][1]).parent))] if prepared and prepared[-1][0] == "clone" else []),
             *(["Its machine's deploy keys are added by ct-setup add-worker (or install), which also sets the machine up."] if prepared else []), tag="DONE", color=GREEN, run_label="Compare them", run="ct-admin workers")
        return 0
    if action == "remove":
        listed_at = workers.load_list(wfile).get(a.name)
        mine = workerrepos.removable(a.name, listed_at, wfile) if listed_at else []
        if not a.name or a.name not in workers.load_list(wfile):
            raise refuse.error("worker.unlisted", ("Name", a.name), example="ct-admin workers")
        how = repos_choice(ctx, a, mine)                                           # asked BEFORE the worker is taken off the list: a refusal here changes nothing
        workers.remove(wfile, a.name)
        results = workerrepos.retire([s for _, s in mine], how) if how != "keep" else []
        emit("Worker removed", ("Worker", alias.show(a.name)), "Its install on its own machine was not touched.",
             *[("Repository", f"{s}: {how}d" if ok else f"{s}: could not {how} ({said[:160]})") for s, ok, said in results],
             *[("Repository", f"{s}: kept (private, still yours)") for _, s in mine if how == "keep"],
             tag="DONE", color=GREEN)
        return 0
    if action == "use":
        return workers_use(ctx, a)
    if action == "rename":
        return workers_rename(ctx, a)
    rows, problems, regs, shared = [], [], {}, []
    for name, repos in ctx.fleet():
        try:
            if not a.no_refresh:
                repos.pull_results()
        except Exception as e:                                         # a worker that cannot be reached is a row, not a failure
            problems.append(f"{name}: could not refresh ({str(e).strip()[:80]})")
        rows.append((name, workers.read_state(repos.results)))
        regs[name] = repos.registry_info()
    for name, path in workers.load_list(wfile).items():                                    # a hand-edited list can still name one repository twice: said, never ignored
        try:
            hit = workers.repo_conflict(config.load(path, env={"CT_CONFIG": ""}), path, wfile)
        except Exception:
            hit = None
        if hit:
            shared.append((name, hit))
    cells = [workers.row_for(n, d, silent_s=(ctx.cfg.health.silent_hours if ctx.cfg else 24) * 3600) for n, d in rows]
    mine = client_cli.here.global_pins()
    for c in cells:
        c["dedicated"] = workers.dedicated_text(mine, c["name"])
        c["class_desc"] = {n: workers.brief(i) for n, i in ((regs.get(c["name"]) or {}).get("classes") or {}).items()}
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
    if shared:
        emit("Two workers share a repository", *[("Worker", alias.show(n)) for n, _ in shared], *[("Shared", f"{h[0]} with {h[1]}") for _, h in shared],
             "Each worker needs its own private repositories: give one of them new names (github.queue_repo and github.results_repo in its configuration), then set it up again.", tag="WARNING", color=RED)
    if problems:
        emit("Some workers could not be refreshed", *[("Problem", p) for p in problems], tag="NOTE", color=YELLOW)
    release_card(ctx, out)
    _ui.related(("Only workers whose Claude access works", "ct-admin workers --claude"), ("Add another worker", "ct-admin workers add <name> --config <file>"),
                ("Stop listing a worker", "ct-admin workers remove <name>"), ("Dedicate a command to a worker", "ct-admin workers use <name> --command <class>"), ("Give a worker a name here", "ct-admin workers rename <name> <new name>"), ("Look at one worker's health", "ct-admin status"))
    return 0


def cmd_action_hash(ctx, a):
    return ctx.remote("action-hash", a.path)


def cmd_action(ctx, a):
    """ct-admin action add|remove|list: root-level actions, registered and removed the same way adopt and retire handle classes."""
    ctx.repos.need_setup()
    if a.verb == "list":
        return action_list(ctx)
    if not a.source:
        raise refuse.error("action.argument", ("Command", f"ct-admin action {a.verb}"), example="ct-admin action add packs/fan-control" if a.verb == "add" else "ct-admin action remove fan-profile", need="a definition file or a pack folder" if a.verb == "add" else "the name of an action, or the pack folder it came from")
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
        raise refuse.error("action.definition.refused", problems="\n".join(problems[:6]))
    text = registry_text(ctx)
    have = (registry.parse(text) or {}).get("actions") or {} if text else {}
    have_actions, have_classes = regedit.action_names(text), regedit.class_names(text)
    for name, action in defn["actions"].items():
        twin = next((n for n, x in have.items() if n != name and (x.get("run") or [None])[0] == actionadd.program_of(action)), None)
        if twin:
            raise refuse.error("action.program.duplicate", ("Program", actionadd.program_of(action)), ("Registered as", str(twin)), steps=[f"Use the existing one, or remove it first: ct-admin action remove {twin}"], example=f"ct-admin action remove {twin}")
    plan, pins = [], {}
    screen = None if a.yes else tui.make()
    for name, action in defn["actions"].items():
        info = actionadd.ask_worker(ctx, actionadd.program_of(action))
        pins[name] = info["sha256"]
        again = name in have_actions
        if again and (have.get(name) or {}).get("sha256") == info["sha256"] and all(c in have_classes for c in defn["classes"]):
            emit("Already registered", ("Action", name), ("Program", info["path"]), ("Pinned to", info["sha256"]), "\n", "The program on the worker is the one it is pinned to, and its class exists. Nothing to change.",
                 tag="OK", color=GREEN, run_label="See what is registered", run="ct-admin action list")
            return 0
        if screen is not None:                                              # a real terminal: the wizard's own card, with its buttons
            if not screen.protection(actionadd.card_info(ctx.ssh.target, name, action, info, again)):
                stop("Not registered", "You answered no. Nothing was changed.")
        else:
            emit("Pin the root-level action again" if again else "Register a root-level action", ("Action", name), *actionadd.summary(name, action, info),
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
        raise refuse.error("action.unknown", ("Action", str(missing[0])), example=f"ct-admin action remove {close[0]}" if close else "ct-admin action list", known=", ".join(known) or "none")
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
            emit("Remove a root-level action", ("Action", name), ("Program", " ".join((have.get(name) or {}).get("run") or [])), ("Class removed", ", ".join(classes) or "none"),
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


def cmd_watch(ctx, a):
    """One pass (or a loop) over every worker: read what each published and alert on changes. A Direct worker cannot reach a phone, so this computer does it for it."""
    import os
    from ..alerts import Notifier
    cfg = ctx.cfg
    url = (os.environ.get("CT_ALERT_URL") or (cfg.alerts.url if cfg and cfg.alerts.kind != "none" else "")) or ""
    notifier = Notifier(url=url, product=(cfg.identity.name if cfg else "claude-tenta"), syslog=False, log=lambda m: None)
    memory = watch_mod.Memory(ctx.home / "watch.json")
    said = []

    def say(name, kind, text):
        said.append(name)
        emit("Fleet alert" if kind in ("locked", "unreachable", "silent") else "Fleet note", ("Worker", alias.show(name)), ("Kind", kind), text, tag="ALERT" if kind in ("locked", "unreachable", "silent") else "NOTE", color=RED if kind in ("locked", "unreachable", "silent") else YELLOW, stream=sys.stdout)
    from .. import rediscover
    hosts = {n: (c.ssh.host, c.ssh.port) for n, c in ctx.targets() if c.cfg is not None and transport.for_config(c.cfg).kind == "direct"}      # only a worker reached by address can have moved

    def on_ok(name, repos):
        if name in hosts:
            rediscover.remember(ctx.home, name, *hosts[name])

    def on_fail(name, repos):
        if name not in hosts:
            return ""
        m, rung, _ = rediscover.ladder(*hosts[name][:1], hosts[name][1], name, ctx.home, full=False)             # the cheap rungs only: a monitoring pass never waits for a scan
        return f"It answers at {m.addresses[0]} (found by {rung}): ct-admin locate --worker {name}." if m.kind == "one" else f"If its address changed, ct-admin locate --worker {name} looks for it."
    while True:
        raised = watch_mod.one_pass(ctx.fleet(), memory, notifier, say=say, force=not a.loop, on_ok=on_ok, on_fail=on_fail, releases=lambda: release_signature(ctx))
        if not a.loop:
            break
        time.sleep(watch_mod.BASE_S)
    if not raised:
        emit("Nothing has changed", f"Every worker was read; none locked, went quiet or became unreachable since the last look.{'' if url else ' No alert address is set, so changes show here only.'}", tag="OK", color=GREEN, stream=sys.stdout)
    return 0


def cmd_wake(ctx, a):
    """Tell the worker (or every worker) to stop idling and look for jobs now, over the same SSH login ct-admin check uses. No password: it changes only when the next check happens."""
    rc = 0
    for name, c in ctx.targets(a.worker):
        emit("Waking a worker", ("Worker", alias.show(name)), tag="WAKE", color=CYAN, stream=sys.stdout)
        rc = c.remote("wake") or rc
    return rc


def cmd_audit(ctx, a):
    """Read-only check that each worker still matches its configuration. It runs as root through the update entry point, so the worker's sudo asks for your password."""
    rc = 0
    for name, c in ctx.targets(a.worker):
        emit("Auditing a worker", ("Worker", alias.show(name)), tag="AUDIT", color=CYAN, stream=sys.stdout)
        rc = c.ssh.interactive(f"sudo {render.UPDATE_CMD} --audit") or rc
    return rc


def cmd_tripwire_test(ctx, a):
    """On the worker, as root (asks for the worker's sudo password): read a decoy credential, prove the worker locks itself down, then unlock it. Needs a machine with a kernel audit."""
    rc = 0
    for name, c in ctx.targets(a.worker):
        emit("Testing the tripwire", ("Worker", alias.show(name)), tag="TRIPWIRE", color=CYAN, stream=sys.stdout)
        rc = c.ssh.interactive(f"sudo {render.UPDATE_CMD} --tripwire-test") or rc
    return rc


def cmd_unlock(ctx, a):
    if not use_signed(ctx, a):
        return ctx.remote("unlock")
    banner = ctx.repos.pull_results()
    if not banner:
        emit("Not locked", f"{ctx.name} reports no lockdown.", tag="OK", color=GREEN)
        return 0
    emit("A worker is LOCKED", ("Worker", alias.show(ctx.name)), ("What happened", banner.get("kind")), ("Job", banner.get("job")), ("Since", banner.get("since")), tag="LOCKED", color=RED,
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
        emit("Already merged into main", ("Branch", branch), "Nothing to merge.", tag="OK", color=GREEN)
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
            raise refuse.error("proposal.registry.invalid", ("Branch", branch), steps=[f"Open the {branch} branch of the queue repository, change what the problems above name, push it, then run this again. Nothing was merged."], problems="\n".join(problems[:5]))
    subject_line, _, rest = msg.partition("\n")
    emit("Merge a branch into main", ("Branch", branch), ("Adds", f"{plural(len(p.commits(branch)), 'commit')}, squashed into ONE"), *[("Commit", f"{h}  {s}  ({who}, {when})") for h, s, who, when in p.commits(branch)],
         *[("File", diffview.counts(path, add, gone)) for path, add, gone in p.files(branch)], ("Becomes", subject_line),
         *([("Message", line) for line in rest.strip().splitlines() if line.strip()][:6]), tag="MERGE", color=_ui.PRIMARY)
    if not ctx.confirm(f"Squash-merge {branch} into main and delete the branch?", yes):
        stop("Not merged", "You answered no.")
    p.push_main(sha)
    gone = p.delete(branch)
    p.fetch()
    emit("A branch was merged into main", ("Branch", branch), ("Release", release.short(sha)), "The branch was deleted." if gone else f"The branch is still on {ctx.store}; `ct-admin proposals tidy` removes it.",
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
        _ui.say("Changes waiting on branches", *cards.job_blocks([(b, [("STATE", cards.Styled([("merged", "muted")]) if merged else cards.Styled([(f"{ahead} to review", "warn")])), ("LAST COMMIT", subj)]) for b, ahead, subj, merged in rows], label="BRANCH"),
                tag="PROPOSALS", color=_ui.PRIMARY, stream=sys.stdout)
        emit("What you can do with these", tag="NEXT", color=CYAN, run_label="Merge one, merge them all, or clean up", run=["ct-admin proposals merge <branch>", "ct-admin proposals merge --all", "ct-admin proposals tidy"])
        return 0
    if act == "merge":
        if a.all:
            return merge_all(ctx, a)
        if not a.branch:
            raise refuse.error("proposal.which_branch", example="ct-admin proposals merge --all")
        merge_branch(ctx, a.branch, yes=a.yes, subject=a.message)
        return after_merge(ctx, a)
    if act == "publish":
        return install_registry_change(ctx, a)
    if act == "tidy":
        done = [b for b in p.branches() if p.contained(b)]
        if not done:
            emit("Nothing to tidy", "No merged branches are left.", tag="OK", color=GREEN)
            return 0
        if not ctx.confirm(f"Delete {len(done)} merged branch(es) ({', '.join(done)}) from {ctx.store}?", a.yes):
            stop("Nothing deleted", "You answered no.")
        gone = [b for b in done if p.delete(b)]
        emit(f"Deleted {len(gone)} branch(es)", tag="DONE", color=GREEN)
        return 0 if len(gone) == len(done) else 1
    raise refuse.error("proposal.action.unknown", ("Action", str(act)), example="ct-admin help proposals")


def after_merge(ctx, a):
    """A merge changes GitHub only. With an approval key here the registry is signed and published at once; otherwise it waits for an install."""
    if use_signed(ctx, a):
        return install_registry_change(ctx, a)
    emit("Install it", f"Merging changes {ctx.store} only; the worker has it after the next install.", tag="NEXT", color=CYAN, run="ct-admin apply")
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
        raise refuse.error("adopt.result.missing", ("Job", a.id), example=f"ct-run result {a.id} --wait")
    names, packs = off.NameBook(ctx.home), off.PackBook(ctx.home)
    preview = off.propose(r, a.id, res, name=a.name, tier=a.tier, dry_run=True, names=names, packs=packs) if res.get("profile", {}).get("verdict") != "does-not-fit" else None
    if preview is None:
        out = off.propose(r, a.id, res, names=names, packs=packs)
        emit("It does not fit the worker", "It was recorded as an exclusion; nothing was added to the registry.", *out["reasons"], tag="EXCLUDED", color=YELLOW)
        return 0
    emit("Adopt a class", ("Class", preview["name"]), ("Tier", preview["tier"]), ("Cost", preview["entry"]["cost"]), ("Branch", preview["branch"]), tag="PREVIEW", color=CYAN)
    if not ctx.confirm(f"Add {preview['name']} to the registry and install it on {ctx.ssh.host}?", a.yes):
        stop("Not adopted", "You answered no. Nothing was changed.")
    out = off.propose(r, a.id, res, name=preview["name"], tier=a.tier, names=names, packs=packs)
    merge_branch(ctx, out["branch"], yes=True)
    rc = install_registry_change(ctx, a, name=preview["name"], present=True)
    if rc == 0:
        emit("A class was adopted", ("Class", preview["name"]), f"{ctx.name} publishes its new class list within a minute.", tag="DONE", color=GREEN, run_label="Check the list", run="ct-run classes")
    return rc


def cmd_retire(ctx, a):
    r, p = ctx.repos, ctx.props
    r.need_setup()
    p.fetch()
    text = r.show_file(p.base, "registry.yaml") or ""
    where = regedit.class_block(text, a.cls)
    if not where:
        raise refuse.error("class.unknown_on_main", ("Class", a.cls), steps=([f"Classes: {', '.join(regedit.class_names(text))}"] if regedit.class_names(text) else []), example="ct-run classes")
    if regedit.is_profile_kind(text, a.cls):
        raise refuse.error("class.retire.protected", ("Class", a.cls))
    block = text.splitlines()[where[0]:where[1]]
    emit("Retire a class", ("Class", a.cls), *[("Removes", line.strip()) for line in block if line.strip() and not line.strip().startswith("#")], tag="RETIRE", color=RED)
    emit("What happens", ("Registry", "the class is removed; the worker refuses new jobs of this class"), ("Queued jobs", "any still waiting for it are rejected"),
         ("Software", "packages it needed stay installed"), ("Profile history", "kept, so `ct-run offload fit` still remembers it was measured"), tag="NOTE", color=YELLOW)
    if not ctx.confirm(f"Remove {a.cls} and install the new registry on {ctx.ssh.host} now?", a.yes):
        stop("Not retired", "You answered no. Nothing was changed.")
    branch = f"chore/retire-{a.cls}"
    r.push_files(branch, {"registry.yaml": regedit.without_class(text, a.cls)}, f"chore(registry): retire the {a.cls} class\n\nRemoved from registry.yaml with ct-admin retire.", base=p.main)
    merge_branch(ctx, branch, yes=True)
    rc = install_registry_change(ctx, a, name=a.cls, present=False)
    if rc == 0:
        emit("A class was retired", ("Class", a.cls), tag="DONE", color=GREEN, run_label="Check the list", run="ct-run classes")
    return rc


def class_facts(c):
    """(tier, network, tools) of a class as the registry text says it, with the defaults a missing line means."""
    return c.get("tier", "approve"), bool(c.get("network", False)), list(c.get("tools") or [])


def cmd_class(ctx, a):
    """`ct-admin class show CLASS` says what a class may do; `ct-admin class set CLASS ...` changes its approval tier, its network access and its tool folders without editing registry.yaml by hand
    (the change is shown first, asked about, merged and installed the way retire and adopt are)."""
    r, p = ctx.repos, ctx.props
    r.need_setup()
    p.fetch()
    text = r.show_file(p.base, "registry.yaml") or ""
    mine = registry.parse(text).get("classes") or {} if text.strip() else {}
    published = (r.registry_info() or {}).get("classes") or {}
    name = a.cls
    from_pack = name not in mine and name in published
    if name not in mine and not from_pack:
        raise refuse.error("class.unknown", ("Class", name), steps=([f"Your classes: {', '.join(sorted(mine))}"] if mine else []), example="ct-run classes")
    if from_pack and a.verb == "set":
        raise refuse.error("class.from_pack", ("Class", name), example=f"ct-admin class show {name}")
    tier, network, tools = class_facts(mine[name] if name in mine else published[name])
    repeat = bool((mine[name] if name in mine else published[name]).get("repeatable"))
    if a.verb == "show":
        state = workers.read_state(r.results) or {}
        iso = state.get("isolation") or {}
        roots = list(iso.get("tool_roots") or [])
        rows = [("Class", name), ("Defined in", "a pack (change it there)" if from_pack else "your registry"), ("Tier", tier + ("  (waits for your approval each time)" if tier == "approve" else "  (runs as soon as it is sent)")), ("Network", "yes: jobs of this class can reach the network" if network else "no")]
        rows += [("Safe to repeat", "yes: if a worker goes silent before it answers, the job is sent again to another" if repeat else "no: if a worker goes silent before it answers, the job is only marked lost")]
        if tools:
            allowed = lambda t: registry.inside(t, roots)                                                    # noqa: E731
            rows += [("Tool folders" if i == 0 else "", t + ("" if allowed(t) else "   NOT inside a folder this worker allows")) for i, t in enumerate(tools)]
            rows += [("This worker", f"isolation {iso.get('mode', 'full')}" + (f"; allowed folders: {', '.join(roots)}" if roots else "; no tool folders allowed"))]
            rows += jobs.tool_check_rows(((state.get("tool_check") or {}).get(name)), tools)
        else:
            rows += [("Tool folders", "none")]
        change = [] if from_pack else ([f"ct-admin class set {name} --tier approve"] if tier == "auto" else []) + ([f"ct-admin class set {name} --network off"] if network else []) + [f"ct-admin class set {name} --tools <folder> [<folder> ...]"]      # only changes that tighten, or add tool folders: never a suggestion that loosens, and never one copied first
        emit("What a class may do", *rows, tag="CLASS", color=CYAN, **({"run": change, "run_label": "Change it"} if change else {}))          # the safer change first (a copy of the first command goes to the clipboard); nothing for a pack's class
        return 0
    changes, notes = {}, []
    if a.tier:
        changes["tier"] = a.tier
    if a.network:
        changes["network"] = a.network == "on"
    if a.repeatable:
        changes["repeatable"] = a.repeatable == "on"
    if a.tools:
        flat = [t for group in a.tools for t in group]
        changes["tools"] = None if flat == ["none"] else flat
    if not changes:
        raise refuse.error("class.set.nothing", ("Class", name), example=f"ct-admin class set {name} --tier approve")
    new_tier = changes.get("tier", tier)
    new_net = changes.get("network", network)
    if new_net and new_tier == "auto" and "tier" not in changes:                          # never end up silently with a network class that runs unapproved
        changes["tier"] = new_tier = "approve"
        notes.append("It is also set to approve: a class with network access should not run without your approval each time. To keep it automatic, say so: --tier auto.")
    new_text = regedit.set_class_fields(text, name, {k: v for k, v in changes.items()})
    problems = registry.validate(registry.parse(new_text), "/nonexistent-tools-root", allow_empty=True)
    if problems:
        raise refuse.error("class.set.invalid", ("Class", name), problems="\n".join(problems[:5]))
    now_tier, now_net, now_tools = class_facts(registry.parse(new_text)["classes"][name])
    now_repeat = bool(registry.parse(new_text)["classes"][name].get("repeatable"))
    if (now_tier, now_net, now_tools, now_repeat) == (tier, network, tools, repeat):
        emit("Nothing to change", ("Class", name), "It already is that way.", tag="NOTE", color=YELLOW)
        return 0
    rows = [("Class", name)]
    if now_tier != tier:
        rows.append(("Tier", f"{tier}  ->  {now_tier}"))
    if now_net != network:
        rows.append(("Network", f"{'yes' if network else 'no'}  ->  {'yes' if now_net else 'no'}"))
    if now_repeat != repeat:
        rows.append(("Safe to repeat", f"{'yes' if repeat else 'no'}  ->  {'yes' if now_repeat else 'no'}"))
    if now_tools != tools:
        rows += [("Tool folders" if i == 0 else "", f) for i, f in enumerate(now_tools)] or [("Tool folders", "none (removed)")]
    emit("Change a class", *rows, *notes, tag="CHANGE", color=RED if (now_net and now_tier == "auto") else CYAN)
    dangerous = now_net and now_tier == "auto"
    if dangerous:
        emit("This class would run with the network and without your approval", "Anyone who can send this job will run it with the network, each time, without you seeing it first.", tag="WARNING", color=RED)
        if not ctx.is_tty():                                                                       # never taken from --yes: a security choice is answered by a person at a terminal
            raise refuse.error("class.network.needs_terminal", ("Class", name))
        if ctx.ask(_ui.confirm_prompt(f"Allow {name} to use the network without approval?")).strip().lower() != "y":
            stop("Not changed", "You answered no. Nothing was changed.")
    elif not ctx.confirm(f"Change {name} and install the new registry on {ctx.ssh.host} now?", a.yes):
        stop("Not changed", "You answered no. Nothing was changed.")
    branch = f"chore/class-{name}"
    r.push_files(branch, {"registry.yaml": new_text}, f"chore(registry): change the {name} class\n\nChanged in registry.yaml with ct-admin class set.", base=p.main)
    merge_branch(ctx, branch, yes=True)
    rc = install_registry_change(ctx, a, name=None, present=True)
    if rc == 0:
        emit("A class was changed", ("Class", name), tag="DONE", color=GREEN, run_label="Check it", run=f"ct-admin class show {name}")
    return rc


# ---- release ----
def show_diff(ctx, sha, full=False, quiet=False):
    """Print what installing `sha` changes. Returns True if anything differs from what is installed."""
    now = release.installed_rev(ctx.ssh, ctx.name)
    out = sys.stdout
    if not now:
        emit("The installed version is unknown", "Showing the target only; everything counts as new.", tag="NOTE", color=YELLOW)
        return True
    if now == sha:
        if not quiet:
            emit("Nothing to change", f"{ctx.name} is already at {release.short(sha)}.", tag="OK", color=GREEN)
        return False
    files = ctx.source.changed_files(now, sha)
    if files is None:
        emit("Cannot compare the two versions", f"{release.short(now)} is not in the release source.", tag="NOTE", color=YELLOW)
        return True
    root = [f for f in files if release.is_root_run(f[1])]
    _ui.table("What will change", ["", "change", "file"],
              [[("ROOT", "bad") if release.is_root_run(path) else "", diffview.status_cell(status, status), diffview.status_cell(path, status)] for status, path in sorted(files, key=lambda f: (not release.is_root_run(f[1]), f[1]))],
              tag="CHANGES", color=_ui.YELLOW if root else _ui.PRIMARY, stream=out,
              notes=(f"From release {release.short(now)} to release {release.short(sha)}.", f"{plural(len(files), 'file')} change; {len(root)} of them run as root or set the sandbox.",) + (() if full or root else ("No root-run files change; --full shows all the code.",)))
    text = ctx.source.diff_text(now, sha, () if full else release.ROOT_RUN)
    if text.strip():                                         # no root-run file changed: the table above already says so, and an empty box says nothing
        _ui.document("The change itself", text, tag="DIFF", stream=out, as_diff=True)
    return True


def cmd_diff(ctx, a):
    show_diff(ctx, ctx.source.resolve(a.rev), a.full)
    return 0


def fleet_apply_summary(outcomes, rev, verb="apply"):
    """One card after `apply` has visited several workers: who was already current, who was updated, who was left alone and why."""
    by = {}
    for name, what in outcomes:
        by.setdefault(what, []).append(name)
    rows = [(label, ", ".join(alias.show(n) for n in by[key])) for key, label in (("installed", "Updated"), ("current", "Already current"), ("left", "Left alone"), ("failed", "Failed or declined")) if key in by]
    bad = "failed" in by or "left" in by
    emit(f"{verb} finished: {plural(len(outcomes), 'worker')}", *rows, ("Release", release.short(rev) if rev else "?"),
         *(["A worker left alone was not changed: the card above says why (another platform, or a commit the release source does not have)."] if "left" in by else []),
         tag=verb.upper(), color=YELLOW if bad else GREEN, stream=sys.stdout)


def apply(ctx, a):
    outcomes = getattr(a, "outcomes", None)
    theirs = (workers.read_state(ctx.repos.results) or {}).get("platform")
    if theirs and theirs != release.PLATFORM:                                                   # this release installs on Linux; another platform has its own release line
        emit("This release is not for that worker", ("Worker", alias.show(ctx.name)), ("It runs", theirs), ("This release is for", release.PLATFORM),
             "Nothing was changed. A worker on another platform is brought up to date with the release line made for it.", tag="NOT CHANGED", color=YELLOW,
             steps=["Updating a Windows worker that is already installed is not built yet (docs/design/WINDOWS-SPECIAL-USE.md); its own release line exists."] if theirs == "windows" else None, steps_label="What to do")
        if outcomes is not None:
            outcomes.append((ctx.name, "left"))
        return 1
    sha = ctx.source.resolve(a.rev)
    now = release.installed_rev(ctx.ssh, ctx.name)
    if now and now != sha and ctx.source.relation(now) == "unpublished" and not a.force:
        emit("A worker runs a commit the release source does not have", ("Worker", alias.show(ctx.name)), ("It runs", release.short(now)), ("Apply would install", release.short(sha)),
             "That commit was never published to the release branch, so installing the published release would REPLACE it (and may be older). It is left as it is.",
             steps=["Publish it: merge it into the release branch and push it to the release source, then run ct-admin apply again.", f"Or replace it with the published release anyway: ct-admin apply --force --worker {ctx.name}"],
             steps_label="Your choice", tag="NOT CHANGED", color=YELLOW)
        if outcomes is not None:
            outcomes.append((ctx.name, "left"))
        return 1
    changed = show_diff(ctx, sha, a.full, quiet=outcomes is not None)
    if not changed and not a.force and outcomes is not None:         # already current, in a fleet run: said in the summary, not in two more cards
        outcomes.append((ctx.name, "current"))
        return 0
    if not changed and not a.force:                      # show_diff has already said "Nothing to change"; this adds only how to install anyway, for THIS worker (a bare --force would force every one)
        emit("Install anyway?", f"Only if you want {ctx.name} reinstalled at the same version.", tag="NEXT", color=CYAN, run_label="Run the install again anyway", run=f"ct-admin apply --force --worker {ctx.name}")
        return 0
    if not ctx.confirm(f"Install {release.short(sha)} on {ctx.ssh.host}?", a.yes):
        stop("Not installed", "You answered no.")
    prev = release.installed_rev(ctx.ssh, ctx.name)
    rc = ctx.ssh.interactive(f"sudo {render.UPDATE_CMD} --rev {sha}")
    if rc:
        raise refuse.error("apply.install_failed", ("Host", ctx.ssh.host), ("Exit code", str(rc)))
    if not a.no_verify and not verify(ctx, sha, prev, a.yes):
        return 1
    if outcomes is not None:
        outcomes.append((ctx.name, "installed"))
    if not getattr(a, "quiet_done", False):
        emit("Installed", ("Worker", alias.show(ctx.name)), "It publishes its new class list within a minute.", tag="DONE", color=GREEN, run_label="See what it can run now", run="ct-run classes")
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
    emit("Rolled back" if rc == 0 else "The rollback failed", *([("Release", release.short(prev))] if rc == 0 else []), "The worker passes its checks again." if ok else "It still fails some checks; read the list above and the worker's journal.",
         tag="ROLLED BACK" if ok else "WARNING", color=GREEN if ok else RED)
    return False


def selftest(ctx, wait_s=240):
    """Run one real job end to end (GitHub, worker, guard, result). Returns (ok, detail); ok is None when no suitable class exists."""
    info = ctx.repos.registry_info() or {}
    if release.SELFTEST_CLASS not in (info.get("classes") or {}):
        return None, f"the worker has no {release.SELFTEST_CLASS} class to test with"
    argv = ["submit", release.SELFTEST_CLASS, "--here", "--wait", "--timeout", str(wait_s)]            # --here: the check is ABOUT this worker, so the orchestrator must not pick another
    rc = client_cli.main(argv, ctx=client_cli.Context.from_config(ctx.cfg), cfg=ctx.cfg) if getattr(ctx, "cfg", None) is not None else client_cli.main(argv)
    return rc == 0, "ok" if rc == 0 else f"exit {rc}"


# ---- parser and help ----
GROUPS = [("Worker", ["status", "check", "wake", "audit", "tripwire-test", "workers", "locate", "watch"]), ("Jobs", ["review", "approve", "cancel", "kill"]), ("Security", ["quarantine", "unlock", "output"]),
          ("Registry", ["adopt", "retire", "class", "action", "proposals", "action-hash"]), ("Release", ["diff", "apply", "update", "clean"]), ("Look", ["theme"])]

HELP = {
    "status": ("health, what the worker is doing, recent use, and its safety checks", [("Show the report", "ct-admin status"), ("Look further back", "ct-admin status --hours 48")]),
    "workers": ("compare the workers you administer: hardware, Claude access and state, side by side", [("Compare them all", "ct-admin workers"), ("Only those with working Claude access", "ct-admin workers --claude"), ("Add another worker", "ct-admin workers add studio --config ~/.config/claude-tenta/studio.toml"), ("Give a worker a name here", "ct-admin workers rename tenta3 lab-vm"), ("Name every worker in turn", "ct-admin workers rename")]),
    "locate": ("find a worker whose address changed, by the SSH host key this computer recorded for it (a name is never proof); offers to save the new address", [("Look for the main worker", "ct-admin locate"), ("Look for another one", "ct-admin locate --worker studio")]),
    "watch": ("read what every worker published and alert on changes: locked, unlocked, gone quiet, unreachable (a Direct worker cannot send alerts itself)", [("One look at every worker", "ct-admin watch"), ("Keep watching, each worker on its own schedule", "ct-admin watch --loop")]),
    "wake": ("end idle or sleep now: every worker, or one, looks for jobs right away", [("Wake every worker", "ct-admin wake"), ("Wake one", "ct-admin wake --worker studio")]),
    "audit": ("check that each worker still matches its configuration (read-only; asks for the worker's sudo password)", [("Audit every worker", "ct-admin audit"), ("Audit one", "ct-admin audit --worker studio")]),
    "tripwire-test": ("prove the worker locks itself down when a job reads a decoy credential, then unlock it (needs the worker's sudo password)", [("Run it on every worker", "ct-admin tripwire-test"), ("Run it on one", "ct-admin tripwire-test --worker studio")]),
    "check": ("check the connection to the worker", [("Is key login working, and do read-only commands need no password?", "ct-admin check")]),
    "review": ("show waiting jobs and note you viewed them (asks nothing)", [("Show every waiting job", "ct-admin review"), ("Show one", "ct-admin review profile-k7m2xq")]),
    "approve": ("approve a job exactly as you saw it, then watch it", [("Review, approve and wait for the result", "ct-admin approve profile-k7m2xq"), ("Approve every waiting job you viewed", "ct-admin approve --all")]),
    "cancel": ("stop a job that has not started yet", [("Cancel it", "ct-admin cancel some-job")]),
    "kill": ("stop a running job", [("Ask it to stop", "ct-admin kill some-job"), ("Stop the whole process tree", "ct-admin kill some-job --hard")]),
    "quarantine": ("list results held back by the output guard, or read one", [("List them", "ct-admin quarantine"), ("Read one in full", "ct-admin quarantine some-job")]),
    "unlock": ("clear a security lockdown after you have looked at it", [("Unlock", "ct-admin unlock")]),
    "output": ("the full output of a failed command (only its error lines are ever published)", [("List the kept outputs", "ct-admin output"), ("Read one", "ct-admin output some-job")]),
    "adopt": ("turn a finished profile run into a class and install it", [("Preview, ask once, then register, merge and install", "ct-admin adopt profile-k7m2xq --name ssh-version")]),
    "class": ("see or change what a class may do: its approval tier, network access and tool folders", [("What may it do?", "ct-admin class show nmap"), ("Ask for approval each time", "ct-admin class set nmap --tier approve"), ("Allow the network", "ct-admin class set nmap --network on"), ("Use tools that already exist", "ct-admin class set nmap --tools /opt/tools/nmap /opt/tools/bin")]),
    "retire": ("remove a class from the registry and install that", [("Show what is removed, ask once, then merge and install", "ct-admin retire ssh-version")]),
    "action": ("register, list or remove commands that must run as root", [("Register one from a pack or a YAML file", "ct-admin action add packs/fan-control"), ("After you changed the program, pin it again", "ct-admin action add packs/fan-control"), ("See what is registered", "ct-admin action list"), ("Remove one and the class that runs it", "ct-admin action remove fan-profile")]),
    "theme": ("show the colour roles, what they look like and where the theme came from", [("Show them", "ct-admin theme")]),
    "action-hash": ("print the SHA-256 to pin for a root-level action's program, and check it could be one", [("Hash a program on the worker", "ct-admin action-hash /usr/local/sbin/fan-profile")]),
    "proposals": ("list, merge or tidy the changes waiting on branches", [("See what is waiting", "ct-admin proposals"), ("Merge one", "ct-admin proposals merge registry/ssh-version"), ("Merge them all", "ct-admin proposals merge --all"), ("Delete merged branches", "ct-admin proposals tidy"), ("Sign the registry on main so the worker installs it", "ct-admin proposals publish")]),
    "clean": ("remove signed approvals and orders that are finished or stale from where they are passed (GitHub or the mailbox)", [("Show what can go and ask", "ct-admin clean")]),
    "update": ("is there a newer release? show what changed, then update this computer and every worker if you agree (each asks its own question)", [("Look and ask", "ct-admin update"), ("Only say whether there is one", "ct-admin update --check")]),
    "diff": ("show what installing a version would change", [("Compare the installed version with the latest, on every worker", "ct-admin diff"), ("One worker only", "ct-admin diff --worker tenta2"), ("All the code, not only root-run files", "ct-admin diff --full")]),
    "apply": ("install a version on the worker, check it, offer to roll back", [("Install the latest on every worker, one after the other (each asks first)", "ct-admin apply"), ("Install on one worker only", "ct-admin apply --worker tenta2"), ("Install one commit", "ct-admin apply 1a2b3c4d"), ("Install again even if nothing differs", "ct-admin apply --force")]),
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
    s = sp.add_parser("class", add_help=False)
    s.add_argument("verb", choices=["set", "show"], help="show: what a class may do | set: change its tier, network access or tool folders")
    s.add_argument("cls", metavar="class", help="the class (ct-run classes lists them)")
    s.add_argument("--tier", choices=["auto", "approve"], help="auto runs as soon as it is sent; approve waits for your OK each time")
    s.add_argument("--network", choices=["on", "off"], help="may jobs of this class reach the network?")
    s.add_argument("--repeatable", choices=["on", "off"], help="on: a job of this class that is lost because its worker went silent may be run again on another worker (only if running it twice does no harm)")
    s.add_argument("--tools", action="append", nargs="+", metavar="FOLDER", help="folders of tools that already exist on the worker (repeat or list several; none removes them); each must be inside a folder the worker allows")
    yes(s)
    s = sp.add_parser("action", add_help=False)
    s.add_argument("verb", choices=["add", "remove", "list"], help="add: register (or pin again) a root-level action | remove: take one away, with its class | list: show what is registered")
    s.add_argument("source", nargs="?", help="add: a YAML file or pack folder such as packs/fan-control | remove: an action's name, or that folder")
    yes(s)
    for verb in ("wake", "audit", "tripwire-test"):
        s = sp.add_parser(verb, add_help=False)
        s.add_argument("--worker", metavar="NAME", help="only this worker, by the name in ct-admin workers (default: every worker)")
    s = sp.add_parser("locate", add_help=False)
    s.add_argument("--worker", metavar="NAME", help="the worker to look for, by the name in ct-admin workers (default: the main one)")
    yes(s)
    s = sp.add_parser("watch", add_help=False)
    s.add_argument("--loop", action="store_true", help="keep going: look at every worker on its own schedule (a quiet worker every minute, an unreachable one less often each time)")
    sp.add_parser("theme", add_help=False)
    s = sp.add_parser("action-hash", add_help=False)
    s.add_argument("path", help="the program on the worker, for example /usr/local/sbin/fan-profile")
    s = sp.add_parser("clean", add_help=False)
    yes(s)
    s = sp.add_parser("update", add_help=False)
    s.add_argument("--check", action="store_true", help="only say whether a newer release exists; change nothing")
    yes(s)
    s = sp.add_parser("workers", add_help=False)
    s.add_argument("action", nargs="?", choices=["list", "add", "remove", "use", "rename"], help="list (default): compare the workers; add or remove one; use: dedicate a command to a worker; rename: give a worker a name here (an alias)")
    s.add_argument("name", nargs="?", help="with add or remove: the worker's name")
    s.add_argument("new", nargs="?", help="with rename: the new name (an alias, kept on this computer only)")
    s.add_argument("--config", help="with add: the worker's configuration file")
    s.add_argument("--github", action="store_true", help="with add: also create the worker's two private repositories on GitHub (with gh) and clone them")
    s.add_argument("--repos", choices=["keep", "archive", "delete"], help="with remove: what to do with the worker's two repositories on GitHub (asked at a terminal; default keep)")
    yes(s)
    s.add_argument("--mask", action="store_true", help="with rename: show only aliases, never the real names")
    s.add_argument("--show-real", action="store_true", help="with rename: show an alias with the real name beside it (the default)")
    s.add_argument("--command", metavar="class", help="with use: the job class that always goes to that worker, from every folder")
    s.add_argument("--clear", action="store_true", help="with use: undo it (a worker: release it; with --command: just that command; neither: everything in ~/.tenta.toml); with rename: remove the worker's name")
    s.add_argument("--force", action="store_true", help="with use: add the command even if the computer's worker already covers it")
    s.add_argument("--claude", action="store_true", help="only workers whose Claude access works and that are not locked")
    s.add_argument("--json", action="store_true", help="print the comparison as JSON")
    s.add_argument("--no-refresh", action="store_true", help="use what was last downloaded; do not pull first")
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
    for n in FLEET_COMMANDS + PICK_COMMANDS:                                     # defined below: this runs after the module has loaded
        if not any("--worker" in x.option_strings for x in sp.choices[n]._actions):
            sp.choices[n].add_argument("--worker", metavar="NAME", help=("only this worker, by the name in ct-admin workers (default: every worker, one after the other)" if n in FLEET_COMMANDS
                                                                       else "act on this worker, by the name in ct-admin workers (default: the main one)"))
    return ap, sp


FLEET_COMMANDS = ("update", "apply", "diff", "check", "status", "quarantine", "unlock")     # every worker, one after the other (each asked its own questions), unless --worker names one
PICK_COMMANDS = ("clean", "adopt", "retire", "class", "action", "proposals")      # one worker: the main one, or the one named with --worker


def worker_releases(ctx):
    """[(worker name, the release it runs)] for every Linux worker, for the banner: it counts them all, not only the main one."""
    try:
        got = [(n, workers.read_state(c.repos.results) or {}) for n, c in ctx.targets()]
    except Exception:                                                 # the banner is a courtesy: fall back to the main worker alone
        got = [(ctx.name, workers.read_state(ctx.repos.results) or {})]
    return [(n, st.get("release")) for n, st in got if not st.get("platform") or st["platform"] == release.PLATFORM]


def run_fleet(ctx, cmd, targets, a, label=None):
    """Visit each worker in turn with a heading; one that fails or is declined does not stop the rest. Returns the worst exit code."""
    rc = 0
    verb = label or cmd
    for name, c in targets:
        _ui.show([cards.rule(f"{verb}: {alias.show(name)}", "WORKER", _ui.PRIMARY)], stream=sys.stdout)          # one line, not an empty frame
        try:
            r = DISPATCH[cmd](c, a)
        except ClientError as e:
            fail_from(e)
            r = 2
        except SystemExit as e:                               # a declined question or a stop() on one worker: carry on with the next
            r = e.code if isinstance(e.code, int) else 1
        if getattr(a, "outcomes", None) is not None and not any(n == name for n, _ in a.outcomes):
            a.outcomes.append((name, "failed" if r else "current"))        # raised or declined before it could say what it did
        rc = max(rc, r or 0)
    return rc


def run_command(ctx, a):
    """Run a command on the workers it is about. A fleet command visits every worker in turn with a heading each; one that fails, or is declined, does not stop the rest, and the exit code is the worst of them.
    `--worker NAME` narrows either kind to one worker. Everything else is run on the one context it was given."""
    if a.cmd in FLEET_COMMANDS:
        targets = ctx.targets(getattr(a, "worker", None))
        if a.cmd == "update":                                             # one worker: as it always was; several: the release is looked up and shown once, then each worker in turn
            if len(targets) == 1:
                return DISPATCH["update"](targets[0][1], a)
            with _ui.collecting_related() as footer:
                rc = cmd_update_fleet(ctx, targets, a)
            footer.flush()
            return rc
        if a.cmd == "apply" and len(targets) > 1 and not getattr(a, "rev", None):
            a = copy.copy(a)                                              # ONE release for the whole fleet, decided once (each worker keeps its own copy of the release source, fetched at its own time)
            a.rev = ctx.source.resolve(None)
        if a.cmd == "apply" and len(targets) > 1:
            a = copy.copy(a)
            a.outcomes = []                                               # what happened to each worker, said once at the end: a worker that is already current needs no card of its own
        if len(targets) == 1:
            return DISPATCH[a.cmd](targets[0][1], a)
        with _ui.collecting_related() as footer:                      # what to do next is the same for every worker: said once, after the last one
            rc = run_fleet(ctx, a.cmd, targets, a)
        if a.cmd == "apply" and getattr(a, "outcomes", None):
            fleet_apply_summary(a.outcomes, a.rev)
        if a.cmd in ("status", "check"):                              # the pulse: who runs what, once, with the fix
            try:
                release_card(ctx)
            except Exception:                                         # never able to break the command it follows
                pass
        footer.flush()
        return rc
    if a.cmd in PICK_COMMANDS and getattr(a, "worker", None):
        ctx = ctx.for_worker(a.worker)
    return DISPATCH[a.cmd](ctx, a)


DISPATCH = {"status": cmd_status, "check": cmd_check, "review": cmd_review, "approve": cmd_approve, "cancel": cmd_cancel, "kill": cmd_kill,
            "quarantine": cmd_quarantine, "unlock": cmd_unlock, "output": cmd_output, "adopt": cmd_adopt, "retire": cmd_retire, "class": cmd_class, "workers": cmd_workers, "locate": cmd_locate, "watch": cmd_watch, "update": cmd_update, "clean": cmd_clean, "action-hash": cmd_action_hash, "action": cmd_action, "theme": cmd_theme, "wake": cmd_wake, "audit": cmd_audit, "tripwire-test": cmd_tripwire_test,
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
        title, why, steps, example, facts = clihelp.usage_problem(e, "ct-admin") if isinstance(e, clihelp.UsageError) else ("That is not a command ct-admin understands", "ct-admin has no command by that name.", ["Run ct-admin help to see the commands."], None, [])
        emit(title, *facts, cards.Why(why or ""), tag="ERROR", color=RED, steps=steps, steps_label="Try", run=example, run_label="Did you mean")
        return 2
    if not a.cmd:
        emit("No command given", "Run ct-admin help to see the commands.", tag="ERROR", color=RED)
        return 2
    try:
        real = ctx is None
        if ctx is None:
            cfg = cfg or config.load()
            ctx = Context.from_config(cfg)
        jid = getattr(a, "id", None)
        if jid:                                                           # a job that was sent to another worker is approved, read and stopped THERE
            from ..client import place
            other = place.worker_of(ctx.home, jid)
            if other and other != ctx.name:
                ctx = ctx.for_worker(other)
        rc = run_command(ctx, a)
        if rc == 0 and a.cmd in REMEMBER_WHERE_IT_ANSWERED and not getattr(a, "worker", None):       # the worker answered over SSH: note where, so a later failure can try that address first
            from .. import rediscover
            try:                                                      # best effort: noting an address must never turn a command that worked into one that failed
                rediscover.remember(ctx.home, ctx.name, ctx.ssh.host, ctx.ssh.port)
            except Exception:
                pass
        if real and a.cmd != "update" and not getattr(a, "json", False):
            updatecheck.after_command(cfg.updates, ctx.home, __version__, worker_releases(ctx))
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
