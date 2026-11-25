"""ct-run: queue a job, read its result, list your jobs and what the worker can run.

  setup                          clone the two private repos on this computer
  submit <class> [options]       queue a job (-p name=value, --prompt, --file, --dir/--include); waits for the result in a terminal
  result <job> [--wait]          read a job's result; --wait keeps checking until it finishes
  jobs [-n N]                    your most recent jobs, and the ones waiting for your approval
  classes                        what the worker can run today, as it published it
  prune [--days N] [--force]     squash old job files out of the jobs branch
  offload log hook|top|show|clear   learn which of your commands are worth running on the worker
  offload fit -- <cmd>           is this command registered, excluded or unknown?  (exit 0 / 3 / 5 / 4)
  offload history                every fit check you or Claude made
  offload profile ... -- <cmd>   test-run one exact command on the worker (you approve it)
  offload propose <profile-job>  turn a profile result into a registry proposal, or an exclusion
  offload measure                suggest the repeat settings from your own command log
  offload recommend              a better worker for a pinned command, from your own finished jobs
  offload exclusions             commands that were tested and do not fit

Everything (the two repo addresses, the folders, the names in messages) comes from the configuration; nothing here knows an install.
"""
import argparse
from datetime import datetime, timezone
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .. import alias, cards, clihelp, config, transport
from .. import ui as _ui
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, paint
from . import candidacy, cap, refuse
from .. import patch as patch_mod
from . import offload as off
from . import submit as sub
from . import here, place, view
from .repo import PENDING, ClientError, ClientRepos
from ..words import plural


def emit(*a, **k):
    """Status and error messages go to stderr; stdout carries only what a script may want (a job id, a result, JSON)."""
    k.setdefault("stream", sys.stderr)
    _ui.say(*a, **k)


class Context:
    def __init__(self, repos, *, product="the worker", admin="ct-admin", hints=None, label_of=None, ask=input, sleep=time.sleep, clock=time.time, run_admin=None,
                 is_tty=None, own_tools=(), home=None, max_in_flight=0, fleet=None, auto_adopt=False, kind="github", repeat_runs=3, repeat_days=14, recommend_runs=3, recommend_gain=25):
        self.kind = kind                                   # the transport: jobs leave the person's network unless it is "direct"
        self.repos, self.product, self.admin, self.own_tools = repos, product, admin, tuple(own_tools)
        self.home = Path(home) if home else Path("~/.ct").expanduser()
        self.fitlog, self.names = off.FitLog(self.home), off.NameBook(self.home)
        self.packs = off.PackBook(self.home)
        self.adopted, self.auto_adopt = off.AdoptBook(self.home), bool(auto_adopt)
        self.commands = off.CommandBook(self.home)
        self.repeat_runs, self.repeat_days = int(repeat_runs), int(repeat_days)
        self.recommend_runs, self.recommend_gain = int(recommend_runs), int(recommend_gain)
        self.hints = hints or [("on your computer", f"{admin} approve {{}}")]
        self.label_of = label_of or (lambda jid, cls: self.names.get(jid) or ("-" if cls in ("", "profile-run") else cls))
        self.ask, self.sleep, self.clock, self.run_admin = ask, sleep, clock, run_admin
        self.max_in_flight, self.fleet = int(max_in_flight or 0), fleet or (lambda: [("main", repos)])
        self.is_tty = is_tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())

    @classmethod
    def from_config(cls, cfg):
        view.set_pace(cfg.schedule, direct=transport.for_config(cfg).kind == "direct", store=transport.store_for(cfg))
        repos = ClientRepos.from_config(cfg)
        name = cfg.identity.name
        return cls(repos, product=name, admin="ct-admin", hints=[("on your computer", "ct-admin approve {}"), ("on the worker", "ct-worker approve {}")],
                   own_tools=("ct-run", "ct-admin", "ct-setup", "ct-help", "ct-worker"), home=Path(cfg.paths.client_home).expanduser(),
                   max_in_flight=cfg.limits.max_in_flight, fleet=lambda: fleet_of(cfg, repos), auto_adopt=cfg.offload.auto_adopt, repeat_runs=cfg.offload.repeat_runs, repeat_days=cfg.offload.repeat_days, recommend_runs=cfg.offload.recommend_runs, recommend_gain=cfg.offload.recommend_gain, run_admin=run_admin_command, kind=transport.for_config(cfg).kind)


def run_admin_command(args):
    """Run a ct-admin command on this computer, on the person's own terminal (an adoption, an approval). Returns its exit code."""
    return subprocess.run([sys.executable, "-m", "ct.admin.cli", *args]).returncode


def adopt_tier(res):
    """The tier a class is adopted with: auto (runs as soon as it is submitted) for a light or medium one, approve (waits for the person's OK each time) for a heavy one."""
    return "approve" if (res.get("profile") or {}).get("cost") == "heavy" else "auto"


def auto_adopt(ctx, res):
    """A profile result that fits is adopted and installed right away, signed with this computer's own approval key, unless the person switched it off (offload.auto_adopt).
    Only at a terminal, and once per result whatever came of it: a run that cannot be adopted (the name is taken) is said once, not retried every time it is shown.
    The tighter conditions (who may trigger it) come with the AI layer."""
    pr = res.get("profile") or {}
    if pr.get("verdict") != "fits" or not ctx.auto_adopt or not ctx.run_admin or not ctx.is_tty() or ctx.adopted.get(res["id"]):
        return
    tier = adopt_tier(res)
    ctx.adopted.remember(res["id"], tier)
    emit("Adopting it automatically", ("Job", res["id"]), ("Tier", tier + ("" if tier == "auto" else "   (heavy: it will wait for your OK each time it runs)")),
         "It fits this worker, so it is added to the registry and installed with your approval key. To stop doing this: ct-setup install --set offload.auto_adopt=false",
         tag="NOTE", color=CYAN, stream=sys.stderr)
    ctx.run_admin(["adopt", res["id"], "--tier", tier, "--yes", "--worker", ctx.product])


def fleet_of(cfg, main):
    """[(name, ClientRepos)] for the main worker and every one in workers.toml (the same list ct-admin workers shows), for the job cap."""
    from ..admin import workers as workers_mod
    out = [(cfg.identity.name, main)]
    wfile = Path(cfg.source).parent / "workers.toml" if cfg.source else None
    for name, path in (workers_mod.load_list(wfile) if wfile else {}).items():
        try:
            out.append((name, ClientRepos.from_config(config.load(path, env={"CT_CONFIG": ""}))))
        except Exception:
            continue                                                  # an unreadable worker configuration is the workers command's problem, not a reason to block sending
    return out


def fail_from(e):
    """Show a ClientError the way every other error is shown, and return its exit code."""
    emit(e.title, *e.facts, cards.Why(e.why or ""), tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
    return 2


# ---- commands ----
def cmd_setup(ctx, cfg):
    rows = ctx.repos.clone_all(cfg.paths.client_home)
    emit("This computer is set up", *[(what, path) for what, path in rows], tag="DONE", color=GREEN, run=["ct-run classes", "ct-run jobs"], run_label="Try it")
    return 0


def explain(ch, cls):
    emit("Where a job would go" if ch.worker else "No worker can take this job", *([(cards.Chip("WHERE", "primary"), alias.show(ch.worker))] if ch.worker else []), ("Command", cls), "\n", *place.rows(ch), tag="WHERE", color=CYAN, **({"run_label": "Send it there", "run": f"ct-run submit {cls} --worker {ch.worker}"} if ch.worker else {}))


def route_submit(ctx, cfg, a):
    """With several workers, choose one and say why (before sending, so the person sees where it goes); with one, nothing changes."""
    if getattr(a, "here", False):
        return ctx
    def say(ch):
        emit("Sending a job", (cards.Chip("WHERE", "primary"), alias.show(ch.worker)), ("Command", a.cls), "\n", *place.rows(ch), *(["Every worker is busy; it queues on this one."] if ch.busy_all else []), tag="PLACED", color=CYAN)
    forced, why = getattr(a, "worker", None), None
    if not forced:
        forced, why = pinned_worker(cfg, a.cls)
    if why:
        emit("Sending a job", (cards.Chip("WHERE", "primary"), alias.show(forced)), ("Command", a.cls), ("Why", why), tag="PINNED", color=CYAN)
    ctx2, _ch = place.route(ctx, cfg, a.cls, forced, say=say)
    return ctx2


def pinned_worker(cfg, cls):
    """(worker, why) the folder's .tenta.toml sends a job of this class to, or (None, None). A name that is not one of your workers any more is an error that names the file."""
    worker, why = here.pin_for(cls)
    if worker and cfg is not None:
        names = [e[0] for e in place.entries(cfg)]
        if worker not in names:
            raise refuse.error("placement.pin.unknown_worker", ("File", str(here.find())), ("Worker", alias.show(worker)), ("Yours", ", ".join(names)))
    return worker, why


def cmd_use(ctx, cfg, a):
    """Show, set or clear which worker this folder's jobs go to (the .tenta.toml in the folder, or the nearest one above it; --global: the one for this computer, ~/.tenta.toml)."""
    existing = here.global_path() if a.glob else here.find()
    pins = here.read(existing) if existing and existing.is_file() else here.Pins(here.global_path() if a.glob else Path.cwd().resolve() / here.FILE)
    scope = "this computer" if a.glob else "this folder"
    if a.clear and not a.worker:
        if a.command:
            if a.command not in pins.commands:
                emit("That command was not set to a worker here", ("Command", a.command), ("File", str(pins.path)), tag="NOTE", color=YELLOW)
                return 0
            del pins.commands[a.command]
        else:
            pins.worker, pins.commands = None, {}
        here.save(pins)
        emit("Cleared", ("File", str(pins.path) if not pins.empty() else f"{pins.path} (removed, nothing left in it)"), tag="DONE", color=GREEN)
        return 0
    if a.worker:
        return use_set(cfg, a, pins, scope)
    if pins.empty():
        emit("Nothing is set for this computer" if a.glob else "Nothing is set for this folder", "Jobs are placed by the scheduler, on whichever worker fits best.", tag="NOTE", color=YELLOW,
             run=["ct-run use <worker>", "ct-run use <worker> --command <class>"], run_label="Send this folder's jobs to one worker")
        return 0
    emit("Where jobs from this computer go" if a.glob else "Where this folder's jobs go", *use_rows(pins), tag="PINNED", color=CYAN, run=["ct-run use <worker>", "ct-run use --clear"], run_label="Change it")
    return 0


def use_set(cfg, a, pins, scope):
    names = [e[0] for e in place.entries(cfg)] if cfg is not None else []
    a.worker = alias.resolve(a.worker, names)                          # an alias means its worker; the file keeps the real name
    if names and a.worker not in names:
        raise refuse.error("placement.unknown_worker", ("Name", a.worker), ("Yours", ", ".join(names)))
    here.check_name("worker", a.worker)
    if a.command:
        here.check_name("command", a.command)
        if pins.worker == a.worker and not a.force:                 # it would only repeat what the folder already says
            emit("Not added: it would change nothing", ("Command", a.command), ("Worker", alias.show(a.worker)), ("File", str(pins.path)),
                 f"This {'computer' if a.glob else 'folder'} already sends everything to {a.worker}, so naming {a.command} would change nothing today. It would only keep {a.command} on {a.worker} if you later send the rest elsewhere.",
                 tag="NOTE", color=YELLOW, run=[f"ct-run use {a.worker} --command {a.command} --force"], run_label="Add it anyway")
            return 0
        pins.commands[a.command] = a.worker
    else:
        differ = {c: w for c, w in sorted(pins.commands.items()) if w != a.worker}
        override = a.override
        if differ and not a.override and not a.retain:
            emit("Some commands go to a different worker", ("File", str(pins.path)), "\n", *by_worker(differ), f"You are sending the rest of {scope} to {a.worker}.", tag="NOTICE", color=YELLOW)
            if sys.stdin.isatty():
                override = input(_ui.confirm_prompt(f"Send them to {a.worker} too? Answer n to keep each on its own worker.")).strip().lower() in ("y", "yes")
            else:
                emit("Kept where they are", "Nobody was there to answer; ct-run use --override replaces them.", tag="NOTE", color=YELLOW)
        if override:
            pins.commands = {c: w for c, w in pins.commands.items() if c not in differ and w != a.worker}
        pins.worker = a.worker
    here.save(pins)
    emit(("A command now goes to its own worker" if a.command else "This computer's jobs now go to one worker" if a.glob else "This folder's jobs now go to one worker"), *([("Command", a.command)] if a.command else []), ("Worker", alias.show(a.worker)), "\n", *use_rows(pins), *(["A command set to its own worker still goes there: it is more specific than the folder's worker."] if pins.commands and not a.command else []),
         tag="PINNED", color=GREEN, run=["ct-run use"], run_label="See it again")
    return 0


def by_worker(commands):
    """Commands set to their own worker, as card rows: the label WORKER and the worker's name, then each of its commands alone on a line below (no label), so nothing a person named is a label."""
    groups = {}
    for c, w in sorted(commands.items()):
        groups.setdefault(w, []).append(c)
    out = []
    for i, (w, cs) in enumerate(sorted(groups.items())):
        out += (["\n"] if i else []) + [("WORKER", alias.show(w))] + [("", c) for c in cs]
    return out


def use_rows(pins):
    rows = [("File", str(pins.path)), ("Jobs worker", alias.show(pins.worker) if pins.worker else "placed by the scheduler")]
    if pins.commands:
        rows += ["\n", *by_worker(pins.commands)]
    return rows


def cmd_where(ctx, cfg, a):
    """Say which worker a job of this class would go to, and why each of the others would not, without sending anything."""
    worker, why = pinned_worker(cfg, a.cls)
    if worker:
        emit("Where a job would go", (cards.Chip("WHERE", "primary"), alias.show(worker)), ("Command", a.cls), ("Why", why), tag="WHERE", color=CYAN, run=["ct-run use"], run_label="See or change it")
        return 0
    ents = place.entries(cfg) if cfg is not None else []
    if len(ents) < 2:
        emit("There is only one worker", f"Everything goes to {alias.show(ctx.product)}. Add more with ct-setup add-worker.", tag="NOTE", color=YELLOW)
        return 0
    views = place.gather(ents, lambda e: ctx.repos if e[0] == ents[0][0] else ClientRepos.from_config(e[1]), home=ctx.home)
    ch = place.decide(a.cls, views)
    explain(ch, a.cls)
    return 0 if ch.worker else 1


def show_left_out(left):
    """Say what was not sent, so nothing is dropped silently: what was left out by rule (and how to send one anyway), and each file that could not be sent with the reason and its code."""
    if left.excluded:
        names = sorted(left.excluded)
        rows = [("Left out", n) for n in names[:8]] + ([("And", plural(len(names) - 8, "more item"))] if len(names) > 8 else [])
        emit("Files left out by rule", *rows, (f"Decided by {left.source}. " if left.source else "Hidden files and folders, build output and dependency folders are not sent. ") + "To send one anyway, name it: --include 'PATH'.", tag="NOTE", color=YELLOW)
    if left.links:
        emit("Links not followed", *[("Link", n) for n, _, _ in left.links[:10]], *([f"And {plural(len(left.links) - 10, 'more link')}."] if len(left.links) > 10 else []),
             "A link can point at a file outside the project, so it is never followed. To send the file, copy it into the project.", tag="NOTE", color=YELLOW)
    if left.held:
        emit("Files held back", *[("File", n) for n, _, _ in left.held[:10]], *([f"And {plural(len(left.held) - 10, 'more file')}."] if len(left.held) > 10 else []),
             "Their names or text look like they hold a secret, so they are not sent. To send one, name it exactly: --include 'PATH'. A wildcard never sends one.", tag="NOTE", color=YELLOW)
    if left.warn:
        emit("Files that may contain a secret", *[("File", n) for n, _, _ in left.warn[:10]], *([f"And {plural(len(left.warn) - 10, 'more file')}."] if len(left.warn) > 10 else []),
             "Their text looks like a private key or an API token. They are being sent. If one is real, leave it out with --include.", tag="NOTE", color=YELLOW)
    if left.unsent:
        rows = [row for n, code, text in left.unsent[:10] for row in (("File", n), ("Reason", text), ("Code", code))]
        emit("Files that could not be sent", *rows, *([f"And {plural(len(left.unsent) - 10, 'more file')}."] if len(left.unsent) > 10 else []), tag="WARNING", color=YELLOW)


def show_summary(ctx, files, left):
    """The pre-job summary: what is about to leave this computer, how much of it, and where it goes. Above 1 GB on Direct it says so once (nothing blocks it)."""
    if not files:
        return
    count, size = sub.totals(files)
    where = "your own network (Direct)" if ctx.kind == "direct" else "a private GitHub repository"
    rows = [("Files", plural(count, "file")), ("Size", sub.size_text(size)), ("Goes to", where)]
    if left.held:
        rows.append(("Held back", plural(len(left.held), "file")))
    if left.warn:
        rows.append(("May hold a secret", plural(len(left.warn), "file")))
    big = ctx.kind == "direct" and size > sub.BIG_SET_BYTES
    emit("What will be sent", *rows, *(["This is a very large set. Direct has no size limit, but the worker needs the room and the transfer will take a while. Narrow it with --include if you can."] if big else []),
         tag="NOTE", color=YELLOW if big else CYAN)


def rerun_for(ctx, res):
    """For a profile run that stopped only for time: the command that sends it again with the next limit (None when there is none to suggest)."""
    pr = res.get("profile") or {}
    raw = ctx.commands.get(res["id"]) if pr.get("verdict") == "needs-longer" and pr.get("next_limit_s") else None
    return off.rerun_with_limit(raw, pr["next_limit_s"]) if raw else None


def cmd_submit(ctx, a):
    r = ctx.repos
    r.need_setup()
    params, files = sub.parse_params(a.p), {}
    prompt = Path(a.prompt_file).expanduser().read_text() if a.prompt_file else a.prompt
    for f in a.file or []:
        p = Path(f).expanduser()
        files[p.name] = p.read_text()
    info = r.registry_info()
    limits = sub.file_limits(info)
    left = sub.LeftOut()
    recipe = None if a.no_pack else (((info or {}).get("classes") or {}).get(a.cls) or {}).get("submit")
    a_dir, a_files, a_include, used = off.use_recipe(recipe, a.dir, a.file, a.include, Path.cwd())
    if used:
        emit("Using how this class was profiled", *[(k.capitalize(), ", ".join(v) if isinstance(v, list) else v) for k, v in recipe.items()],
             "Give --dir or --file to choose other files, or --no-pack to send none.", tag="NOTE", color=CYAN)
        for f in a_files or []:
            p = Path(f)
            files[p.name] = p.read_text()
    if a_dir:
        files.update(sub.collect_dir(a_dir, limits, a_include, left=left, hold_secret_text=ctx.kind != "direct"))
    show_left_out(left)
    show_summary(ctx, files, left)
    sub.check_complete(left, getattr(a, "send_incomplete", False))
    batches = [files]
    if files:
        batches = sub.plan_batches(files, bool(((info or {}).get("classes") or {}).get(a.cls, {}).get("batchable")), limits)
    job = sub.build_job(a.cls, params, prompt)
    with view.Busy("checking: how many jobs are already running on the workers"):               # a network round trip per worker, with nothing printed of its own
        cap.check(ctx.fleet(), ctx.max_in_flight, len(batches))
    base = a.id or sub.new_job_id(re.sub("[^a-z0-9-]", "-", a.cls.lower()), r.result_ids())
    ids = sub.job_ids(base, len(batches))
    if len(batches) > 1:
        emit(f"{plural(len(files), 'file')} split into {plural(len(batches), 'independent job')}", *ids, tag="NOTE", color=YELLOW)
    for jid, chunk in zip(ids, batches):
        with view.Busy(f"sending: the job to {alias.show(ctx.product)}"):
            sub.push_job(r, jid, {**job, **({"files": chunk} if chunk else {})})
        emit("Queued", ("Job", jid), ("Worker", alias.show(ctx.product)), f"{alias.show(ctx.product)} notices it as soon as it next looks: {view.pace_text()}.", tag="QUEUED", color=CYAN)
    if getattr(ctx, "home_main", None):                                   # sent to a worker other than the main one: remember where, so the result and the approval find it
        place.record(ctx.home_main, ids, ctx.product, a.cls)
    if place.prune_due(ctx.home):                         # housekeeping, an hour apart: it is a network round trip that almost never finds anything to do
        try:
            r.prune_jobs()
            place.prune_done(ctx.home)
        except ClientError:
            pass
    if not should_wait(ctx, a):
        print("\n".join(ids))                                               # output: data (job ids for a script)
        return 0
    return max(wait(ctx, jid, a.timeout, a.json) for jid in ids)


def should_wait(ctx, a):
    """A person typing in a terminal wants to watch it finish; a captured run (Claude, a script) does not want to block. --wait / --no-wait override."""
    if getattr(a, "no_wait", False):
        return False
    return bool(a.wait) or ctx.is_tty()


def waiting_rows(ctx, jid, res=None):
    """What a job that waits for approval IS, so the waiting card never says 'waiting' about nothing: its class, the exact command, and whether it runs as root."""
    res = res or ctx.repos.read_result(jid) or {}
    cls = res.get("class")
    info = ((ctx.repos.registry_info() or {}).get("classes") or {}).get(cls) or {}
    rows = [("Job", jid)] + ([("Class", cls)] if cls else [])
    if info.get("run"):
        rows.append(("Runs", " ".join(info["run"])))
    if info.get("privileged"):
        rows.append(("Runs as", "ROOT, outside the jail that limits every other job"))
    rows.append(("Why it waits", "approval is pinned to this exact job file; nothing runs until you OK it"))
    return rows


def awaiting_approval_screen(ctx, jid):
    """`result` on a job nobody has approved yet. A person at a terminal can approve it right here; a captured run only gets the instructions."""
    if ctx.is_tty() and ctx.run_admin:
        emit("Waiting for your approval", *waiting_rows(ctx, jid), tag="WAITING", color=YELLOW, stream=sys.stderr)
        try:
            yes = ctx.ask(_ui.confirm_prompt("Review it and approve it now?")).strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            yes = False
        if yes:
            return ctx.run_admin(["approve", jid])
    emit("Not approved yet", f"{alias.show(ctx.product)} has not started this job. If you have not approved it yet, run this:", *waiting_rows(ctx, jid), tag="WAITING", color=YELLOW,
         run=f"{ctx.admin} approve {jid}", run_label="Approve it", stream=sys.stderr)
    return 0


LAST_RESULT = {}     # what the last wait() ended with: {"status": ..., "note": ...} for a final result, {"locked": True}, or {} for a timeout (read by the installer)


def wait(ctx, jid, timeout, as_json, told=False, approved=False):
    """Poll until the job has a final result. Returns the exit code (0 only for status ok; 3 if the worker is locked; 1 on timeout)."""
    r = ctx.repos
    r.need_setup()
    LAST_RESULT.clear()
    told = told or approved
    start = ctx.clock()
    end, prog, res, late_told = start + timeout, view.Progress(clock=ctx.clock), None, False
    try:
        while ctx.clock() < end:
            prog.clear()
            locked = r.pull_results()
            res = r.read_result(jid)
            if res and res["status"] not in PENDING:
                LAST_RESULT.update(status=res["status"], note=res.get("note") or "")
                rc = view.print_result(res, as_json, admin=ctx.admin, suggest_name=suggestion_for(ctx, res), rerun=rerun_for(ctx, res))
                auto_adopt(ctx, res)
                return rc
            if locked:
                LAST_RESULT.update(locked=True)
                emit("Stopped waiting: the worker is locked", f"{jid} will not run until you unlock it.", "It stays queued and runs after you unlock.", tag="LOCKED",
                     color=RED, run=f"{ctx.admin} unlock", stream=sys.stderr)
                return 3
            if res and res["status"] == "awaiting_approval" and not told:
                emit("Waiting for your approval", *waiting_rows(ctx, jid, res), tag="WAITING", color=YELLOW, run_label="Approve it by running this", run=f"{ctx.admin} approve {jid}", stream=sys.stderr)
                told = True
            if res and res["status"] == "running" and told != "running":
                emit("Running now", ("Worker", alias.show(ctx.product)), "Waiting for it to finish. Ctrl-C stops waiting, not the job.", tag="RUNNING", color=CYAN, stream=sys.stderr,
                     run_label="To check on it or stop it", run=[f"ct-run result {jid}", f"{ctx.admin} kill {jid}"])
                told = "running"
            waited = int(ctx.clock() - start)
            if res and res["status"] == "queued" and told not in ("queued", "running"):
                emit("Queued on the worker", ("Worker", alias.show(ctx.product)), "The worker has it and starts it when a slot is free.", tag="QUEUED", color=YELLOW, stream=sys.stderr)
                told = "queued"
            if waited >= view.late_after() and not late_told and (not res or res["status"] in (None, "queued")):
                late_told = True
                prog.clear()
                emit("The worker has not picked the job up", ("Worker", alias.show(ctx.product)), ("Job", jid), ("Waited", plural(waited // 60, "minute")), f"Expected: {view.pace_text()}. This is longer than any of those, so the worker may be stopped, locked, or failing.",
                     tag="SLOW", color=RED, run_label="Wake it so it looks now (then see what it is doing with status)", run=f"{ctx.admin} wake", stream=sys.stderr)
            text, color = view.phase_of(res, jid, ctx.admin, approved, ctx.product, waited)
            for _ in range(10):                               # redraw ~5 times a second; ask GitHub again every ~2 s (the connection is shared, so each ask is cheap)
                prog.draw(text, color)
                ctx.sleep(0.2)
        prog.clear()
        emit("Stopped waiting", ("Waited", f"{timeout} s"), "The job may still run.", tag="TIMEOUT", color=YELLOW, run_label="Check on it later", run=f"ct-run result {jid}", stream=sys.stderr)
        return 1
    finally:
        prog.clear()


def suggestion_for(ctx, res):
    """The class name to suggest for a profile result that fits: the one chosen when it was profiled, else one derived from the command."""
    pr = res.get("profile")
    if not pr or pr.get("verdict") != "fits":
        return None
    return off.suggest_name(pr["argv"], off.taken_names(ctx.repos), preferred=ctx.names.get(res["id"]))


def cmd_result(ctx, a):
    r = ctx.repos
    r.need_setup()
    if a.wait:
        return wait(ctx, a.id, a.timeout, a.json, approved=getattr(a, "approved", False))
    r.pull_results()
    res = r.read_result(a.id)
    if res is not None:
        note_runs(ctx, {a.id: res})
    if res is None:
        if not r.job_known(a.id):
            emit("No such job", ("Job", a.id), "Check the id (ct-run jobs lists your jobs).", tag="ERROR", color=RED)
            return 1
        emit("No result yet", ("Job", a.id), ("Worker", alias.show(ctx.product)), f"{alias.show(ctx.product)} has not picked it up. It looks about once a minute.", tag="QUEUED", color=YELLOW, run=f"ct-run result {a.id} --wait")
        return 0
    if res["status"] == "awaiting_approval":
        return awaiting_approval_screen(ctx, a.id)
    rc = view.print_result(res, a.json, admin=ctx.admin, suggest_name=suggestion_for(ctx, res), rerun=rerun_for(ctx, res))
    auto_adopt(ctx, res)
    return rc


def jobs_of(repos, admin):
    """(results, rows) for one worker's repositories: its published results and the jobs still waiting on its jobs branch."""
    repos.need_setup()
    repos.fetch_queue()
    repos.pull_results()
    ids = repos.result_ids()
    results = {jid: res for jid in ids if (res := repos.read_result(jid))}
    listing = []
    out = repos.git(repos.queue, "ls-tree", "--name-only", f"origin/{repos.jobs_branch}", "jobs/", check=False).stdout
    for path in filter(None, out.split("\n")):
        try:
            listing.append((Path(path).stem, json.loads(repos.show_file(f"origin/{repos.jobs_branch}", path) or "")))
        except ValueError:
            pass

    def created_of(jid):
        return next((view.parse_when(j.get("created", "")) for i, j in listing if i == jid), None)
    return results, view.status_rows(results, created_of, None, view.queued_rows(ids, listing, admin=admin))


def note_runs(ctx, results):
    """Remember each finished job's worker and run time (recommend.RunLog), so `ct-run offload recommend` has evidence. Never breaks the command."""
    from . import recommend
    try:
        recommend.RunLog(ctx.home).observe(results, lambda jid: place.worker_of(getattr(ctx, "home_main", ctx.home), jid) or ctx.product, view.parse_when)
    except (OSError, TypeError, ValueError):                   # a recorder: whatever is wrong with it, the command that looked at the jobs still answers
        pass


def cmd_recommend(ctx, a, cfg=None):
    """`ct-run offload recommend`: for each command pinned to a worker, whether another worker has run it clearly faster. Changes nothing; prints the line that would."""
    from . import recommend
    rows = recommend.RunLog(ctx.home).read()
    pins = {}
    for p in (here.global_pins(), here.pins_for()):
        pins.update(p.commands if p else {})
    if not pins:
        emit("No command is pinned to a worker", "Pin one with ct-run use <worker> --command <class>; then this compares the workers that ran it.", tag="NOTE", color=YELLOW)
        return 0
    if not rows:
        emit("No finished jobs recorded yet", "Jobs are recorded when you look at them: ct-run jobs or ct-run result <job>.", tag="NOTE", color=YELLOW)
        return 0
    for v in recommend.recommend(rows, pins, ctx.recommend_runs, ctx.recommend_gain):
        def took(w):
            return f"{v['median'][w]:g} s, median of {plural(v['runs'][w], 'run')}"
        if v["verdict"] == "better":
            emit("Another worker runs this command faster", ("Command", v["class"]), ("Pinned to", alias.show(v["pinned"])), ("Pinned took", took(v["pinned"])), ("Faster on", alias.show(v["worker"])), ("It took", took(v["worker"])),
                 f"That is {v['gain_pct']}% shorter.", tag="RECOMMEND", color=CYAN, run_label="Move the pin (nothing has been changed yet)", run=f"ct-run use {v['worker']} --command {v['class']}")
        elif v["verdict"] == "keep":
            emit("This command is fine where it is", ("Command", v["class"]), ("Pinned to", alias.show(v["pinned"])), ("Pinned took", took(v["pinned"])), tag="OK", color=GREEN)
        else:
            for w, n in v["need"].items():
                emit("Not enough runs to compare yet", ("Command", v["class"]), ("Pinned to", alias.show(v["pinned"])), ("Worker", alias.show(w)), ("Successful runs", f"{n} of {ctx.recommend_runs} needed"),
                     tag="NOTE", color=YELLOW)
    return 0


def cmd_jobs(ctx, n=15, cfg=None):
    """Your recent jobs. With several workers: every worker's jobs together, newest first, each with the worker it went to (a worker that cannot be reached is named, not silently left out)."""
    ents = place.entries(cfg) if cfg is not None else []
    if len(ents) < 2:
        results, rows = jobs_of(ctx.repos, ctx.admin)
        where = None
    else:
        from concurrent.futures import ThreadPoolExecutor

        def one(e):
            try:
                return e[0], jobs_of(ctx.repos if e[0] == ents[0][0] else ClientRepos.from_config(e[1]), ctx.admin), None
            except Exception as ex:                                                  # one worker that cannot be read is a note, not the end of the list
                return e[0], ({}, []), (str(getattr(ex, "title", "") or ex).strip().splitlines() or ["no answer"])[0][:100]
        with ThreadPoolExecutor(max_workers=len(ents)) as pool:
            got = list(pool.map(one, ents))
        results, rows, where = {}, [], {}
        for name, (res, rws), problem in got:
            results.update(res)
            rows += rws
            where.update({r_[2]: alias.show(name) for r_ in rws})
            if problem:
                emit("A worker's jobs could not be read", ("Worker", alias.show(name)), ("Why", problem), tag="NOTE", color=YELLOW)
        floor = datetime.min.replace(tzinfo=timezone.utc)
        rows.sort(key=lambda r_: r_[0] or floor, reverse=True)
    note_runs(ctx, results)
    view.render_status(rows, results, ctx.label_of, ctx.hints, ctx.admin, n=n, where=where)
    _ui.related(("Read one result in full", "ct-run result <job>"), ("Send another job", "ct-run submit <class>"), ("Approve a job that is waiting", f"{ctx.admin} approve <job>"))
    return 0


def cmd_classes(ctx, cfg=None, a=None):
    """What can run: with several workers, every class and which workers offer it; `--worker NAME` (or one worker) shows that worker's own list."""
    ents = place.entries(cfg) if cfg is not None else []
    named = alias.resolve(getattr(a, "worker", None), [e[0] for e in ents])
    if named and named not in [e[0] for e in ents] and not (cfg is None):
        raise refuse.error("placement.unknown_worker", ("Name", named), ("Yours", ", ".join(e[0] for e in ents)))
    if len(ents) > 1 and not named:
        return fleet_classes(ctx, ents)
    if named and ents and named != ents[0][0]:
        entry = [e for e in ents if e[0] == named][0]
        other = type(ctx).from_config(entry[1])
        return one_worker_classes(other, named)
    return one_worker_classes(ctx, ctx.product)


def one_worker_classes(ctx, product):
    r = ctx.repos
    r.need_setup()
    r.pull_results()
    r.fetch_queue()
    info = r.registry_info()
    if not info:
        emit("This worker has not published its class list yet", ("Worker", alias.show(product)), "It does this about a minute after an install. Try again shortly.", tag="ERROR", color=RED)
        return 1
    view.render_classes(info, pending=[n for n in off.proposed_names(r) if n not in info["classes"]], excluded=len(off.read_exclusions(r).get("excluded", [])), product=product)
    classes_footer(ctx)
    return 0


def fleet_classes(ctx, ents):
    ctx.repos.need_setup()
    views = place.gather(ents, lambda e: ctx.repos if e[0] == ents[0][0] else ClientRepos.from_config(e[1]), home=ctx.home)
    classes, unreachable = {}, []
    for v in views:
        if not v.reachable:
            unreachable.append((v.name, v.why_not or "no answer"))
            continue                                                  # what an unreachable worker once published is not what it offers now
        for name, c in ((v.registry or {}).get("classes") or {}).items():
            classes.setdefault(name, {"info": c, "offered_by": [], "dedicated": None})["offered_by"].append(v.name)
    if not classes:
        emit("No worker has published its class list yet", "It does this about a minute after an install. Try again shortly.", tag="ERROR", color=RED)
        return 1
    for name, c in classes.items():
        c["dedicated"] = here.command_pin(name)[0]
    pins = here.pins_for()
    view.render_fleet_classes(classes, unreachable, folder_worker=pins.worker if pins else None)
    classes_footer(ctx)
    return 0


def classes_footer(ctx):
    _ui.related(("Run one", "ct-run submit <class>"),
                ("See one worker's own list", "ct-run classes --worker <name>"),
                ("Send this folder's jobs, or one class, to a worker", "ct-run use <worker> [--command <class>]"),
                ("Add a class from a command you have tested", f"{ctx.admin} adopt <profile-job>"),
                ("Add a command that must run as root", f"{ctx.admin} action add <definition file or pack folder>"),
                ("Remove a class", f"{ctx.admin} retire <class>"),
                ("Remove a root-level action and its class", f"{ctx.admin} action remove <name>"),
                ("Test whether a new command suits the worker", "ct-run offload fit -- <command>"))


def cmd_patch(ctx, a):
    """Show the patch a write-capable job returned, and (only when asked, after a look) apply it to the folder you are in. Nothing is applied by the worker, ever."""
    import subprocess
    r = ctx.repos
    r.need_setup()
    r.pull_results()
    res = r.read_result(a.id)
    if res is None:
        raise refuse.error("result.not_ready", ("Job", a.id), example=f"ct-run result {a.id} --wait")
    p = res.get("patch")
    if not p:
        raise refuse.error("patch.none", ("Job", a.id), steps=[f"See what it did: ct-run result {a.id}"], status=res.get("status"))
    text, notes = p.get("text") or "", list(p.get("notes") or [])
    bad = patch_mod.problems(text)
    if a.out:
        Path(a.out).expanduser().write_text(text)
        emit("Patch saved", f"{plural(len(p.get('files') or []), 'file')} in {a.out}. Read it before you apply it.", tag="DONE", color=GREEN, run=f"git apply --check {a.out}")
        return 0
    _ui.document("A patch", text, tag="PATCH", as_diff=True, stream=sys.stdout,
                 notes=[("Job", a.id), ("Files", ", ".join(p.get("files") or []) or "none")] + [("Note", n) for n in notes] + [("Refused", b) for b in bad])
    if not a.apply:
        emit("Nothing was changed", "That was only a look.", tag="NEXT", color=GREEN, run_label="Apply it to this folder", run=f"ct-run patch {a.id} --apply")
        return 0
    if bad:
        raise refuse.error("patch.refused", steps=[f"Save it and read it: ct-run patch {a.id} --out patch.diff"], reasons="; ".join(bad))
    if not text.strip():
        emit("The patch is empty", "The job changed nothing, so there is nothing to apply.", tag="NOTE", color=YELLOW)
        return 0
    check = subprocess.run(["git", "apply", "--check", "-"], input=text, text=True, capture_output=True)
    if check.returncode:
        raise refuse.error("patch.misfit", reason=check.stderr.strip()[-300:] or "git apply --check refused it.")
    if not (a.yes or (ctx.is_tty() and ctx.ask(_ui.confirm_prompt(f"Apply {plural(len(p.get('files') or []), 'file')} of changes to {Path.cwd()}?")).strip().lower() == "y")):
        emit("Not applied", "You answered no, or there was nobody to ask.", tag="STOPPED", color=YELLOW)
        return 1
    done = subprocess.run(["git", "apply", "-"], input=text, text=True, capture_output=True)
    if done.returncode:
        raise refuse.error("patch.apply_failed", steps=[f"Save the patch and read it: ct-run patch {a.id} --out patch.diff"], reason=done.stderr.strip()[-300:])
    emit("Applied", f"{plural(len(p.get('files') or []), 'file')} changed in {Path.cwd()}. Nothing was committed.", tag="DONE", color=GREEN, run="git diff")
    return 0


def cmd_prune(ctx, days, force):
    ctx.repos.need_setup()
    emit("Jobs branch", ctx.repos.prune_jobs(days, force), tag="PRUNE", color=CYAN)
    return 0


# ---- offload: fit, history, exclusions, profile, propose ----
def cmd_fit(ctx, argv):
    r = ctx.repos
    r.need_setup()
    if not argv:
        emit("No command given", "Put the command after a double dash, exactly as you would type it.", tag="ERROR", color=RED, run="ct-run offload fit -- eslint .")
        return 2
    res = off.check_fit(r, argv, ctx.fitlog, by="terminal" if ctx.is_tty() else "captured")
    print_fit(ctx, res, argv)
    return res["rc"]


def print_fit(ctx, res, argv):
    """The one-line answer with its first word coloured. Goes to stdout: it is what a script or Claude reads."""
    word, rest = off.fit_message(res, argv, ctx.product).split(" ", 1)
    color = {"registered": GREEN, "excluded": RED, "retry": YELLOW, "unknown": YELLOW}[res["verdict"]]
    _ui.say(rest, tag=word.upper(), color=color, stream=sys.stdout) if _ui._color_on(sys.stdout) else print(f"{word} {rest}")          # output: a card for a person; the plain words for a script


VERDICT_STYLE = {"registered": GREEN, "excluded": RED, "retry": YELLOW, "unknown": YELLOW}


def cmd_history(ctx, a):
    log, out = ctx.fitlog, sys.stdout
    if a.clear:
        emit("Fit history cleared", ("Removed", f"{log.clear()} checks"), tag="DONE", color=GREEN, stream=out)
        return 0
    rows = log.filtered(a.verdict, a.command)
    if a.json:
        print(json.dumps(rows if a.all else rows[-a.n:], indent=2))                  # output: data (--json)
        return 0
    if not rows:
        emit("No fit checks recorded" + (" for that filter" if a.verdict or a.command else " yet"), "Every `ct-run offload fit` is recorded here, whether you or Claude ran it.",
             tag="NOTE", color=YELLOW, stream=out, run_label="Make one", run="ct-run offload fit -- <command>")
        return 0
    counts = {}
    for r in rows:
        counts[r.get("verdict")] = counts.get(r.get("verdict"), 0) + 1
    first = off.parse_ts(rows[0].get("ts"))
    span = f"since {time.strftime('%Y-%m-%d', time.localtime(first))}" if first else ""
    parts = [f"{counts[k]} {k}" for k in ("registered", "excluded", "retry", "unknown") if counts.get(k)]
    unknown = {}
    for r in rows:
        if r.get("verdict") == "unknown":
            unknown[r["command"]] = unknown.get(r["command"], 0) + 1
    repeated = sorted(((n, c) for c, n in unknown.items() if n >= 2), reverse=True)[:3]
    shown = list(reversed(rows if a.all else rows[-a.n:]))                       # newest first
    table = []
    for r in shown:
        ts = off.parse_ts(r.get("ts"))
        v = r.get("verdict", "?")
        what = r.get("class") or r.get("command", "")
        extra = r["command"][:80] if r.get("class") else (r["reasons"][:100] if r.get("reasons") else "")
        table.append([time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts)) if ts else "?", (v, {"registered": "ok", "excluded": "bad", "retry": "warn", "unknown": "warn"}.get(v, "muted")),
                      r.get("by", "?"), what + (f"   {extra}" if extra else "")])
    _ui.table("Offload history", ["when", "verdict", "by", "class / command"], table, tag="HISTORY", stream=out,
              notes=(f"Every check of whether a command suits {alias.show(ctx.product)}.", f"Kept on this computer only; trimmed to the last {plural(off.FIT_KEEP_DAYS, 'day')} or {off.FIT_KEEP_MAX} checks.", f"{len(rows)} checks {span}  ({', '.join(parts)})",
                     *([f"Worth a look, checked more than once and still unknown: " + "; ".join(f"{n}x {c[:50]}" for n, c in repeated)] if repeated else []),
                     f"Showing {len(shown)} of {len(rows)}, newest first."))
    return 0


def cmd_exclusions(ctx):
    r = ctx.repos
    r.need_setup()
    r.fetch_queue()
    ex, out = off.read_exclusions(r).get("excluded", []), sys.stdout
    if not ex:
        _ui.say("Commands that do not fit this worker", ("Worker", alias.show(ctx.product)), "None yet. Commands that were test-run on it and do not fit are listed here; Claude runs those itself instead.", tag="NONE", color=_ui.GREEN, stream=out)
    for e in ex:
        pr = e.get("profiled", {})
        _ui.say(" ".join(e["argv"]), ("Why", "; ".join(e.get("reasons", []))), ("Limited by", ", ".join(e.get("limiting", [])) or "?"),
                ("Tested", f"{pr.get('date', '?')} on {pr.get('mem_total_mb', '?')} MB RAM (job {pr.get('job', '?')})"), tag="DOES NOT FIT", color=_ui.RED, stream=out)
    _ui.related(("Check whether another command fits", "ct-run offload fit -- <command>"), ("See every check you have made", "ct-run offload history"))
    return 0


def cmd_profile(ctx, a):
    r = ctx.repos
    r.need_setup()
    argv, spec = list(a.argv), {"flags": {}, "packages": []}
    if a.spec:
        spec = off.load_spec(Path(a.spec).expanduser().read_text(), a.spec)
        if spec.get("command") and not argv:
            argv = shlex.split(spec["command"])
    pick = lambda cli_v, key: cli_v if cli_v not in (None, "") else spec.get(key)      # noqa: E731
    what, expect, why = pick(a.what, "what"), pick(a.expect, "expect"), pick(a.why, "why")
    side, watch, name = pick(a.side_effects, "side_effects"), pick(a.watch_for, "watch_for"), pick(a.name, "name")
    timeout_s = a.timeout_s or (int(spec["timeout_s"]) if spec.get("timeout_s") else None)
    est = {}
    for k in off.EST_KEYS:
        v = getattr(a, k)
        if v is None and spec.get(k) not in (None, ""):
            try:
                v = float(spec[k])
            except ValueError:
                raise refuse.error("profile.spec.number", ("File", a.spec), ("Field", k), ("Got", repr(spec[k])))
        est[k] = v
    r.pull_results()
    if not a.retry:
        rc = cmd_fit_quiet(ctx, argv)
        if rc in (0, 3, 5):                                  # registered, excluded, or "excluded before the upgrade: ask the user, then --retry"
            return 0 if rc == 0 else 1
    files = {}
    for f in a.file or []:
        p = Path(f).expanduser()
        files[p.name] = p.read_text()
    worker_cap = int((((r.registry_info() or {}).get("classes") or {}).get("profile-run") or {}).get("timeout_s") or 0)       # the worker's own cap, published: no need to send what it will refuse
    if timeout_s and worker_cap and not 10 <= timeout_s <= worker_cap:
        raise refuse.error("profile.timeout.range", ("Asked for", f"{timeout_s} s"), ("Worker's cap", f"{worker_cap} s"), cap=worker_cap)
    limits = sub.file_limits(r.registry_info())
    left = sub.LeftOut()
    if a.dir:
        files.update(sub.collect_dir(a.dir, limits, a.include, left=left, hold_secret_text=ctx.kind != "direct"))
    show_left_out(left)
    show_summary(ctx, files, left)
    sub.check_complete(left, getattr(a, "send_incomplete", False))
    if files and (len(files) > limits["max_count"] or sum(len(v) for v in files.values()) > limits["max_total_kb"] * 1000):
        raise refuse.error("files.set.profile_too_many", files=plural(limits["max_count"], "file"), kb=limits["max_total_kb"])
    if name:
        bad = off.class_name_problem(name, off.taken_names(r))
        if bad:
            raise refuse.error("class.name.unusable", ("Name", name), example=off.own_command_with(a.raw, "").replace(f"--name {name}", "--name <another-name>"), reason=bad)
    job = off.build_profile_job(argv, what, expect, est, flags={**spec["flags"], **off.parse_flags(a.flag)}, packages=spec["packages"] + (a.package or []), why=why,
                                side_effects=side, watch_for=watch, timeout_s=timeout_s, files=files, retry_example=lambda extra: off.own_command_with(a.raw, extra))
    jid = a.id or sub.new_job_id("profile", r.result_ids())
    cap.check(ctx.fleet(), ctx.max_in_flight, 1)
    sub.push_job(r, jid, job)
    if name:
        ctx.names.remember(jid, name)
    recipe, recipe_note = off.make_recipe(a.dir, a.include, a.file, Path.cwd()) if files else (None, None)
    ctx.commands.remember(jid, list(a.raw))
    if recipe:
        ctx.packs.remember(jid, recipe)
    elif recipe_note:
        emit("How the files were packaged is not saved", recipe_note, "A class adopted from this run will need --dir or --file when you submit.", tag="NOTE", color=YELLOW)
    shown = [("command", " ".join(argv)), ("job", jid)] + ([("name", name)] if name else []) + (
        [("needs", ", ".join(job["packages"]) + "  (fetched into a throwaway folder, deleted unless it fits)")] if job.get("packages") else [])
    if (r.worker_state() or {}).get("profile_approval") == "none":      # this worker starts a profile run without approval (Direct, and only while its jail isolates): nothing to approve, so the card says it was sent
        emit("The profiling run was sent", *[(k.capitalize(), v) for k, v in shown], "This worker starts profile runs without approval, inside its jail.", tag="SENT", color=GREEN,
             steps=[f"Read the result when it is done: ct-run result {jid} --wait"], steps_label="Next")
    else:
        emit("This profiling run needs YOUR approval", *[(k.capitalize(), v) for k, v in shown], tag="APPROVAL", color=YELLOW, steps=[f"Review and approve it on your computer: {ctx.admin} approve {jid}", f"Then watch for the result: ct-run result {jid}"],
             steps_label="What to do", run=[f"{ctx.admin} approve {jid}", f"ct-run result {jid}"], run_label="Run these")
    if not a.wait:                                           # a profiling run always needs approval, usually from another terminal: never block on it
        if not ctx.is_tty():
            print(jid)                                       # output: data (the bare id is for scripts and Claude)
        return 0
    return wait(ctx, jid, a.timeout, False, told=True)


def cmd_fit_quiet(ctx, argv):
    """A fit check made on the way to profiling: logged, but the unknown-case advice (written for Claude) is left out."""
    res = off.check_fit(ctx.repos, argv, ctx.fitlog, source="profile", by="terminal" if ctx.is_tty() else "captured")
    if res["verdict"] != "unknown":
        print_fit(ctx, res, argv)
    return res["rc"]


def cmd_propose(ctx, a):
    r = ctx.repos
    r.need_setup()
    r.pull_results()
    r.fetch_queue()
    res = r.read_result(a.id)
    if res is None:
        raise refuse.error("profile.result.not_ready", ("Job", a.id), example=f"ct-run result {a.id} --wait")
    out = off.propose(r, a.id, res, name=a.name, tier=a.tier, batchable=a.batchable, dry_run=a.dry_run, names=ctx.names, packs=ctx.packs)
    kind = out["kind"]
    if kind == "excluded":
        emit("Recorded: this command does not fit", ("Worker", alias.show(ctx.product)), ("Command", " ".join(out["argv"])), ("Reasons", "; ".join(out["reasons"])), "\n",
             "Claude will not offload it again.", tag="RECORDED", color=YELLOW)
        return 0
    pk = out.get("packages")
    details = [("Cost", out["entry"]["cost"]), ("Tier", out["tier"]), ("Branch", out["branch"] + ("  (already proposed)" if out.get("already") else ""))]
    if pk:
        details += ["\n", ("Installs", f"{', '.join(pk)}  (as root; you see the exact commands first)")]
    if kind == "preview":
        emit("Would register a class", ("Class", out["name"]), ("Runs", " ".join(out["entry"].get("run") or [])), *details, tag="PREVIEW", color=CYAN)
        return 0
    emit("Proposed a class", ("Class", out["name"]), *details, "\n", "Nothing changes on the worker until you review and install it.", tag="PROPOSED", color=GREEN,
         run_label="Review and install it", run=f"{ctx.admin} apply {out['branch']}")
    return 0


def cmd_measure(ctx, a, cfg=None):
    """`ct-run offload measure`: what the person's own log says about offload.repeat_runs and offload.repeat_days. It prints the data and the one command that would apply a
    suggestion; it changes nothing itself."""
    log = command_log(ctx, cfg)
    m = log.measure(a.min_seconds, repeat_runs=ctx.repeat_runs, repeat_days=ctx.repeat_days)
    if not m["records"]:
        emit("Nothing recorded yet", ("Where", "ct-run offload log show"), tag="NOTE", color=YELLOW, steps=["Install the Claude Code hooks: ct-run offload log hook pre|post"])
        return 0
    _ui.table("Candidates at each setting", ["runs needed", *(f"in {plural(d, 'day')}" for d in log.GRID_DAYS)],
              [[str(k), *(str(m["grid"][k][d]) for d in log.GRID_DAYS)] for k in sorted(m["grid"])], tag="MEASURE", stream=sys.stdout,
              notes=(f"From {plural(m['records'], 'command')} recorded over {plural(max(1, round(m['log_days'])), 'day')}; {plural(m['shapes'], 'command shape')} could run on a worker.",
                     f"Now: {plural(ctx.repeat_runs, 'run')} in {plural(ctx.repeat_days, 'day')} gives {plural(m['current'], 'candidate')}."))
    if m["suggested_days"] is None:
        emit("No suggestion yet", f"No command has been run {plural(ctx.repeat_runs, 'time')} yet; the most is {m['most_runs']}.", tag="NOTE", color=YELLOW,
             steps=["Keep working with the hooks installed, then measure again once a command you repeat has reached that many runs."])
        return 0
    d = m["suggested_days"]
    emit("Suggested", f"{plural(m['shapes_enough'], 'command shape')} of yours reached {plural(ctx.repeat_runs, 'run')}: the typical one took {plural(d, 'day')} to do it, so count {plural(ctx.repeat_runs, 'run')} within {plural(d, 'day')}.",
         tag="SUGGESTION", color=CYAN, run_label="Apply it (nothing has been changed yet)", run=f"ct-setup install --set offload.repeat_days={d}")
    return 0


# ---- offload: what is worth running on the worker ----
LOG_HELP = {"hook": ("(called by Claude Code's hooks; you never run this yourself)", [("Installed once in Claude Code's settings", "ct-run offload log hook pre")]),
            "top": ("rank the commands you run most that the worker could take",
                    [("The top 10 candidates", "ct-run offload log top"), ("Include shapes that are not candidates, with the reason", "ct-run offload log top --all"),
                     ("Only commands that usually take over a minute", "ct-run offload log top --min-seconds 60")]),
            "show": ("how many commands are recorded, and where the log is", [("Show the log summary", "ct-run offload log show")]),
            "clear": ("delete the log", [("Start again from nothing", "ct-run offload log clear")])}


def command_log(ctx, cfg=None):
    from .cmdlog import CommandLog
    return CommandLog(ctx.home, own_tools=ctx.own_tools)


def cmd_offload(ctx, a, cfg):
    c = a.offload_cmd
    if c == "fit":
        return cmd_fit(ctx, a.argv)
    if c == "measure":
        return cmd_measure(ctx, a, cfg)
    if c == "recommend":
        return cmd_recommend(ctx, a, cfg)
    if c == "history":
        return cmd_history(ctx, a)
    if c == "exclusions":
        return cmd_exclusions(ctx)
    if c == "profile":
        return cmd_profile(ctx, a)
    if c == "propose":
        return cmd_propose(ctx, a)
    if c != "log" or not a.log_cmd:
        emit("Where to start", ("offload log top", "which of your commands are worth running on the worker"), ("offload fit -- <cmd>", "is it registered, excluded or unknown?"),
             ("offload profile ... -- <cmd>", "test-run it on the worker (you approve it)"), ("offload propose <job>", "turn a profile result into a proposal"),
             ("offload measure", "suggest the repeat settings from your own log"), ("offload recommend", "a better worker for a pinned command"), ("offload history / exclusions", "what was checked, and what does not fit"), tag="HELP", color=CYAN)
        return 0
    log = command_log(ctx, cfg)
    if a.log_cmd == "hook":
        try:
            log.hook(a.phase, sys.stdin.read())
        except Exception:
            pass                                              # a hook must never get in Claude Code's way
        return 0
    if a.log_cmd == "show":
        emit(f"{len(log.read())} commands recorded", ("Log file", str(log.path) + ("" if log.path.exists() else "  (not created yet)")), tag="LOG", color=CYAN)
        return 0
    if a.log_cmd == "clear":
        log.clear()
        emit("Log cleared", f"Deleted {log.path}", tag="DONE", color=GREEN)
        return 0
    recs = log.read()
    if not recs:
        emit("Nothing recorded yet", ("Log file", log.path), tag="NOTE", color=YELLOW, steps=["Install the Claude Code hooks: ct-run offload log hook pre|post"])
        return 0
    rows = log.rank(a.min_seconds, a.all, recs, ctx.repeat_runs, ctx.repeat_days)[:a.top]
    out = sys.stdout
    if not rows:
        emit("No candidates yet", "A candidate is repeated, slow, runs locally, and is not a git, network or package-manager command.", tag="NOTE", color=YELLOW)
        return 0
    _ui.table("All command shapes" if a.all else "Candidates for offloading", ["#", "runs", "median", "total", "command shape"] + ([] if a.all else ["why it pays"]),
              [[str(i), str(r["count"]), "?" if r["median"] is None else f"{r['median']:.0f}s", f"{r['total']:.0f}s", (r["shape"] + (f"   [{r['reason']}]" if r["reason"] else ""), "warn" if r["reason"] else None)]
               + ([] if a.all else [r["headline"] or "-"]) for i, r in enumerate(rows, 1)], tag="CANDIDATES", stream=out,
              notes=(*(() if a.all else (f"For {alias.show(ctx.product)}; median time at least {a.min_seconds:.0f} s, or {plural(ctx.repeat_runs, 'run')} in {plural(ctx.repeat_days, 'day')}.",)), f"From {len(recs)} commands recorded on this computer.", "RUNS is how many times you ran it, MEDIAN the usual time one run takes, TOTAL all the time you spent on it."))
    if a.all:
        verdict = {"met": "met", "not met": "no", "unknown": "?"}
        _ui.table("Every rung, for each command shape", ["#", *(f"rung {n}" for n in sorted(candidacy.NAMES)), "headline"],
                  [[str(i), *(verdict[x["verdict"]] for x in r["rungs"]), r["headline"] or "-"] for i, r in enumerate(rows, 1)], tag="RUNGS", stream=out,
                  notes=("Rungs: " + ", ".join(f"{n} {candidacy.NAMES[n]}" for n in sorted(candidacy.NAMES)) + ".", "met = this reason applies; no = it does not; ? = cannot be judged yet (fewer AI tokens needs the AI layer).",
                         "The headline is the highest-weighted rung that is met (docs/design/OFFLOAD-REASONS.md)."))
    if not a.all:
        _ui.table("Most recent example of each", ["#", "example"], [[str(i), r["example"][:150]] for i, r in enumerate(rows, 1)], tag="EXAMPLES", stream=out)
        emit("Pick the one you like", tag="NEXT", color=CYAN, run_label=f"Check whether {alias.show(ctx.product)} can take it", run="ct-run offload fit -- <the example command>")
    return 0


# ---- parsing ----
def build_parser():
    ap = clihelp.Parser(prog="ct-run", description="ct-run", add_help=False)
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("setup", add_help=False)
    s = sp.add_parser("submit", add_help=False)
    s.add_argument("cls", metavar="class", help="the job class (ct-run classes lists them)")
    s.add_argument("-p", action="append", metavar="name=value", help="a job parameter; repeat for more")
    s.add_argument("--prompt", help="the prompt, for a claude class")
    s.add_argument("--prompt-file", metavar="FILE", help="read the prompt from a file")
    s.add_argument("--file", action="append", metavar="F", help="attach one text file; repeat for more")
    s.add_argument("--dir", metavar="D", help="attach the text files under a folder")
    s.add_argument("--include", action="append", metavar="GLOB", help="with --dir: only these files, for example 'src/**/*.js'")
    s.add_argument("--no-pack", action="store_true", help="do not use the files this class was profiled with")
    s.add_argument("--send-incomplete", action="store_true", help="send the files that can be sent even when some could not be (the job would run on an incomplete set)")
    s.add_argument("--id", help="choose the job id (4 to 64 lowercase letters, digits and dashes)")
    s.add_argument("--wait", action="store_true", help="block until it finishes (the default in a terminal)")
    s.add_argument("--no-wait", action="store_true", help="queue it and return the id")
    s.add_argument("--timeout", type=int, default=900, metavar="SEC", help="how long --wait waits (default %(default)s)")
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    s.add_argument("--worker", metavar="NAME", help="send it to this worker instead of letting ct-run choose (names: ct-admin workers)")
    s.add_argument("--here", action="store_true", help="send it to the worker this configuration is for, without choosing (what the checks that are about one worker use)")
    wh = sp.add_parser("where", add_help=False)
    wh.add_argument("cls", metavar="class", help="the job class (ct-run classes lists them)")
    us = sp.add_parser("use", add_help=False)
    us.add_argument("worker", nargs="?", metavar="worker", help="the worker this folder's jobs go to (names: ct-admin workers); without it, show what is set")
    us.add_argument("--command", metavar="class", help="set only this job class (for example nmap) to that worker, and leave the rest of the folder to the scheduler")
    us.add_argument("--clear", action="store_true", help="remove the setting (with --command: just that one)")
    us.add_argument("--global", dest="glob", action="store_true", help="set it for this whole computer (~/.tenta.toml) instead of this folder")
    us.add_argument("--force", action="store_true", help="with --command: add it even when the folder's worker already covers it")
    us.add_argument("--override", action="store_true", help="with a worker: also send the commands that were set to other workers to this one")
    us.add_argument("--retain", action="store_true", help="with a worker: keep the commands that were set to other workers where they are")
    r = sp.add_parser("result", add_help=False)
    r.add_argument("id", help="the job id")
    r.add_argument("--wait", action="store_true", help="keep checking until it finishes")
    r.add_argument("--timeout", type=int, default=900, metavar="SEC", help="how long --wait waits (default %(default)s)")
    r.add_argument("--json", action="store_true", help="print the result as JSON")
    r.add_argument("--approved", action="store_true", help=argparse.SUPPRESS)          # ct-admin sets it: the job was just approved, so say "approved" while it waits
    pt = sp.add_parser("patch", add_help=False)
    pt.add_argument("id", help="the job id")
    pt.add_argument("--apply", action="store_true", help="apply it to the folder you are in, after showing it and asking (nothing is committed)")
    pt.add_argument("--out", metavar="FILE", help="save the patch to a file instead of showing it")
    pt.add_argument("--yes", action="store_true", help="with --apply: do not ask")
    s = sp.add_parser("jobs", add_help=False)
    s.add_argument("-n", type=int, default=15, metavar="N", help="how many jobs to show (default %(default)s)")
    cl = sp.add_parser("classes", add_help=False)
    cl.add_argument("--worker", metavar="NAME", help="show only what this worker offers, with its limits (names: ct-admin workers)")
    o = sp.add_parser("offload", add_help=False)
    osp = o.add_subparsers(dest="offload_cmd", metavar="command")
    lg = osp.add_parser("log", add_help=False, help="the record of commands you ran in a terminal, to see which are worth offloading")
    lsp = lg.add_subparsers(dest="log_cmd", metavar="command")
    h = lsp.add_parser("hook", add_help=False, help="the shell hook that records each command (pre or post)")
    h.add_argument("phase", choices=["pre", "post"], help="before or after the command")
    t = lsp.add_parser("top", add_help=False, help="the commands most worth offloading, from your own history")
    t.add_argument("--top", type=int, default=10, help="how many to list (default %(default)s)")
    t.add_argument("--min-seconds", type=float, default=30.0, help="only commands whose median run is at least this long (default %(default)s)")
    t.add_argument("--all", action="store_true", help="include shapes that are not candidates, with the reason")
    lsp.add_parser("show", add_help=False, help="where the log is and how many commands it holds")
    lsp.add_parser("clear", add_help=False, help="forget the log")
    osp.add_parser("fit", add_help=False, help="check whether one command suits the worker, from what is known about it (nothing is run)")
    hs = osp.add_parser("history", add_help=False, help="every check of whether a command suits the worker")
    hs.add_argument("-n", type=int, default=15, metavar="N", help="how many checks to show (default %(default)s)")
    hs.add_argument("--all", action="store_true", help="every check that is kept")
    hs.add_argument("--verdict", choices=["registered", "excluded", "retry", "unknown"], help="only checks with this answer")
    hs.add_argument("--command", metavar="TEXT", help="only checks whose command contains this text")
    hs.add_argument("--json", action="store_true", help="print the checks as JSON")
    hs.add_argument("--clear", action="store_true", help="forget the history")
    osp.add_parser("measure", add_help=False, help="suggest how often a command must repeat to count, from your own log (changes nothing)").add_argument(
        "--min-seconds", type=float, default=30.0, help="the usual time a single run must take to count as long (default %(default)s)")
    osp.add_parser("recommend", add_help=False, help="for commands pinned to a worker: has another worker run it clearly faster? (changes nothing)")
    osp.add_parser("exclusions", add_help=False, help="commands that were test-run on the worker and do not fit")
    pf = osp.add_parser("profile", add_help=False, help="describe one exact command and have the worker measure it (needs your approval)")
    pf.add_argument("--spec", metavar="FILE", help="read the description from a file of `key: value` lines")
    pf.add_argument("--name", help="the class name to propose if it fits")
    pf.add_argument("--what", help="what the command does, in plain words")
    pf.add_argument("--expect", help="how long it should take, how much memory, what it prints")
    pf.add_argument("--why"), pf.add_argument("--side-effects", dest="side_effects"), pf.add_argument("--watch-for", dest="watch_for")
    for k in ("est_time_s", "est_mem_mb", "est_cpu_cores", "est_disk_mb"):
        pf.add_argument("--" + k.replace("_", "-"), dest=k, type=float, help="your honest estimate" if k in ("est_time_s", "est_mem_mb") else argparse.SUPPRESS)
    pf.add_argument("--flag", action="append", metavar="-x=meaning", help="explain a flag; one per flag")
    pf.add_argument("--package", action="append", metavar="apt:name", help="software the worker lacks: apt:, npm: or pip:")
    pf.add_argument("--file", action="append", metavar="F"), pf.add_argument("--dir", metavar="D"), pf.add_argument("--include", action="append", metavar="GLOB")
    pf.add_argument("--send-incomplete", action="store_true", help="send the files that can be sent even when some could not be")
    pf.add_argument("--timeout-s", dest="timeout_s", type=int, help="the hard time limit for this run")
    pf.add_argument("--id"), pf.add_argument("--retry", action="store_true", help="profile again even if it was excluded before")
    pf.add_argument("--wait", action="store_true", help="stay until it finishes (needs your approval from another terminal)")
    pf.add_argument("--timeout", type=int, default=3000, metavar="SEC", help="seconds to wait with --wait (default %(default)s)")
    pp = osp.add_parser("propose", add_help=False, help="turn a finished profile run into a proposed class")
    pp.add_argument("id", help="the profile job id (ct-run jobs lists them)")
    pp.add_argument("--name", help="the class name (default: the one chosen when profiling)")
    pp.add_argument("--tier", choices=["auto", "approve"], default="auto", help="auto runs as soon as you submit it; approve waits for your OK each time")
    pp.add_argument("--batchable", action="store_true", help="the input files are independent, so a big set may be split into several jobs")
    pp.add_argument("--dry-run", action="store_true", help="show what would be proposed; push nothing")
    s = sp.add_parser("prune", add_help=False)
    s.add_argument("--days", type=int, default=14, metavar="N", help="keep jobs from the last N days (default %(default)s)")
    s.add_argument("--force", action="store_true", help="squash even if the branch is short")
    return ap, sp


HELP = {"setup": ("clone the two private repos on this computer", [("Do it once", "ct-run setup")]),
        "submit": ("queue a job", [("Queue a job and wait for it", "ct-run submit sys-status --wait"), ("With a parameter", "ct-run submit net-check -p lan_host=192.0.2.1"),
                                   ("Attach the files a command needs", "ct-run submit lint --dir ./src --include 'src/**/*.js'")]),
        "result": ("read a job's result", [("Read it now", "ct-run result job-abc123"), ("Wait for it", "ct-run result job-abc123 --wait")]),
        "jobs": ("your most recent jobs, and the ones waiting for your approval", [("Show 15", "ct-run jobs"), ("Show more", "ct-run jobs -n 40")]),
        "offload": ("find out what is worth running on the worker", [("Which of my commands are worth offloading?", "ct-run offload log top"), ("Where is the log?", "ct-run offload log show")]),
        "classes": ("every class your workers can run, and which workers offer each", [("List them", "ct-run classes"), ("One worker's own list", "ct-run classes --worker tenta3")]),
        "where": ("which of your workers a job of this class would go to, and why each of the others would not (nothing is sent)", [("Explain the choice for a class", "ct-run where sys-status")]),
        "use": ("send this folder's jobs to one worker (or just one command), remembered in a .tenta.toml file you can edit", [("See what is set", "ct-run use"), ("Send this folder to a worker", "ct-run use tenta2"), ("Only one command", "ct-run use tenta2 --command nmap"), ("Remove it", "ct-run use --clear")]),
        "patch": ("show the patch a write-capable job returned, and apply it here after a look", [("Look at it", "ct-run patch <job>"), ("Apply it to this folder", "ct-run patch <job> --apply"), ("Save it to a file", "ct-run patch <job> --out fix.diff")]),
        "prune": ("squash old job files out of the jobs branch", [("Tidy it", "ct-run prune")])}


def updatecheck_banner(cfg, ctx):
    from .. import __version__, updatecheck
    from ..admin import workers
    updatecheck.after_command(cfg.updates, ctx.home, __version__, (workers.read_state(ctx.repos.results) or {}).get("release"))


def main(argv=None, ctx=None, cfg=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    raw, tail = list(argv), []
    if "--" in argv:                                          # everything after -- is the command being asked about, not options
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    ap, sp = build_parser()
    if clihelp.show_cli_help(["ct-run", *argv], "ct-run", "queue jobs on the worker and read their results", sp, HELP, [("See what it can run", "ct-run classes")]):
        return 0
    try:
        a = ap.parse_args(argv)
        a.argv, a.raw = tail, raw
    except (clihelp.UsageError, SystemExit) as e:
        title, why, steps, example, facts = clihelp.usage_problem(e, "ct-run") if isinstance(e, clihelp.UsageError) else ("That is not a command ct-run understands", "ct-run has no command by that name.", ["Run ct-run help to see the commands."], None, [])
        emit(title, *facts, cards.Why(why or ""), tag="ERROR", color=RED, steps=steps, steps_label="Try", run=example, run_label="Did you mean")
        return 2
    try:
        if ctx is None:
            cfg = cfg or config.load()
            ctx = Context.from_config(cfg)
        if a.cmd == "setup":
            return cmd_setup(ctx, cfg or config.load())
        if cfg is not None and a.cmd in ("jobs", "classes", "submit", "result") and not getattr(a, "json", False):
            import atexit
            atexit.register(updatecheck_banner, cfg, ctx)                  # after the command's own output; never during it
        if a.cmd == "use":
            return cmd_use(ctx, cfg, a)
        if a.cmd == "where":
            return cmd_where(ctx, cfg, a)
        if a.cmd == "submit":
            ctx = route_submit(ctx, cfg, a)
            try:
                return cmd_submit(ctx, a)
            finally:
                place.release(getattr(ctx, "home_main", None) or ctx.home, getattr(ctx, "reservation", None))          # the real record replaces the reservation (or the send failed and it goes)
        if a.cmd == "result":
            return cmd_result(place.for_job(ctx, cfg, a.id), a)
        if a.cmd == "jobs":
            return cmd_jobs(ctx, a.n, cfg)
        if a.cmd == "offload":
            return cmd_offload(ctx, a, cfg)
        if a.cmd == "classes":
            return cmd_classes(ctx, cfg, a)
        if a.cmd == "patch":
            return cmd_patch(ctx, a)
        if a.cmd == "prune":
            return cmd_prune(ctx, a.days, a.force)
        emit("No command given", "Run ct-run help to see the commands.", tag="ERROR", color=RED)
        return 2
    except ClientError as e:
        return fail_from(e)
    except config.ConfigError as e:
        emit("The configuration has problems", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
