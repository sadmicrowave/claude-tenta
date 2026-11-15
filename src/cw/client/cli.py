"""cw-run: queue a job, read its result, list your jobs and what the worker can run.

  setup                          clone the two private repos on this computer
  submit <class> [options]       queue a job (-p name=value, --prompt, --file, --dir/--include); waits for the result in a terminal
  result <job> [--wait]          read a job's result; --wait keeps checking until it finishes
  jobs [-n N]                    your most recent jobs, and the ones waiting for your approval
  classes                        what the worker can run today, as it published it
  prune [--days N] [--force]     squash old job files out of the jobs branch
  offload log hook|top|show|clear   learn which of your commands are worth running on the worker (more under offload: fit, profile, ...)

Everything (the two repo addresses, the folders, the names in messages) comes from the configuration; nothing here knows an install.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from .. import clihelp, config
from .. import ui as _ui
from ..ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, paint
from . import submit as sub
from . import view
from .repo import PENDING, ClientError, ClientRepos


def emit(*a, **k):
    """Status and error messages go to stderr; stdout carries only what a script may want (a job id, a result, JSON)."""
    k.setdefault("stream", sys.stderr)
    _ui.say(*a, **k)


class Context:
    def __init__(self, repos, *, product="the worker", admin="cw-admin", hints=None, label_of=None, ask=input, sleep=time.sleep, clock=time.time, run_admin=None,
                 is_tty=None, own_tools=()):
        self.repos, self.product, self.admin, self.own_tools = repos, product, admin, tuple(own_tools)
        self.hints = hints or [("on your computer", f"{admin} approve {{}}")]
        self.label_of = label_of or (lambda jid, cls: "-" if cls in ("", "profile-run") else cls)
        self.ask, self.sleep, self.clock, self.run_admin = ask, sleep, clock, run_admin
        self.is_tty = is_tty or (lambda: sys.stdin.isatty() and sys.stdout.isatty())

    @classmethod
    def from_config(cls, cfg):
        repos = ClientRepos(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch)
        name = cfg.identity.name
        return cls(repos, product=name, admin=f"{name}-admin", hints=[("on your computer", f"{name}-admin approve {{}}"), (f"on {name}", f"{name} approve {{}}")],
                   own_tools=("cw-run", "cw-admin", "cw-setup", "cw-help", name, f"{name}-admin"))


def fail_from(e):
    """Show a ClientError the way every other error is shown, and return its exit code."""
    emit(e.title, e.why, tag="ERROR", color=RED, steps=e.steps or None, steps_label="How to fix it", run=e.example.split("\n") if e.example else None)
    return 2


# ---- commands ----
def cmd_setup(ctx, cfg):
    rows = ctx.repos.clone_all(cfg.paths.client_home)
    emit("This computer is set up", *[(what, path) for what, path in rows], tag="DONE", color=GREEN, run=["cw-run classes", "cw-run jobs"], run_label="Try it")
    return 0


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
    if a.dir:
        files.update(sub.collect_dir(a.dir, limits, a.include, note=lambda n, why: emit(f"Skipped {n}", why, tag="NOTE", color=YELLOW)))
    batches = [files]
    if files:
        batches = sub.plan_batches(files, bool(((info or {}).get("classes") or {}).get(a.cls, {}).get("batchable")), limits)
    job = sub.build_job(a.cls, params, prompt)
    base = a.id or sub.new_job_id(re.sub("[^a-z0-9-]", "-", a.cls.lower()), r.result_ids())
    ids = sub.job_ids(base, len(batches))
    if len(batches) > 1:
        emit(f"{len(files)} files split into {len(batches)} independent jobs", *ids, tag="NOTE", color=YELLOW)
    for jid, chunk in zip(ids, batches):
        sub.push_job(r, jid, {**job, **({"files": chunk} if chunk else {})})
        emit(f"Queued {jid}", f"{ctx.product} picks it up within a minute.", tag="QUEUED", color=CYAN)
    try:
        r.prune_jobs()
    except ClientError:
        pass
    if not should_wait(ctx, a):
        print("\n".join(ids))
        return 0
    return max(wait(ctx, jid, a.timeout, a.json) for jid in ids)


def should_wait(ctx, a):
    """A person typing in a terminal wants to watch it finish; a captured run (Claude, a script) does not want to block. --wait / --no-wait override."""
    if getattr(a, "no_wait", False):
        return False
    return bool(a.wait) or ctx.is_tty()


def awaiting_approval_screen(ctx, jid):
    """`result` on a job nobody has approved yet. A person at a terminal can approve it right here; a captured run only gets the instructions."""
    if ctx.is_tty() and ctx.run_admin:
        print(f"\n{paint('STATUS', BOLD, CYAN)}  {paint('awaiting_approval', BOLD, YELLOW)}", file=sys.stderr)
        try:
            yes = ctx.ask(paint("\nReview it and approve it now? [y/N] ", BOLD, YELLOW, stream=sys.stdout)).strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            yes = False
        if yes:
            return ctx.run_admin(["approve", jid])
    emit("Not approved yet", f"{ctx.product} has not started this job. If you have not approved it yet, run this:", tag="WAITING", color=YELLOW,
         run=f"{ctx.admin} approve {jid}", run_label="Approve it", stream=sys.stderr)
    return 0


def wait(ctx, jid, timeout, as_json, told=False, approved=False):
    """Poll until the job has a final result. Returns the exit code (0 only for status ok; 3 if the worker is locked; 1 on timeout)."""
    r = ctx.repos
    r.need_setup()
    told = told or approved
    end, prog, res = ctx.clock() + timeout, view.Progress(clock=ctx.clock), None
    try:
        while ctx.clock() < end:
            prog.clear()
            locked = r.pull_results()
            res = r.read_result(jid)
            if res and res["status"] not in PENDING:
                return view.print_result(res, as_json, admin=ctx.admin)
            if locked:
                emit("Stopped waiting: the worker is locked", f"{jid} will not run until you unlock it.", "It stays queued and runs after you unlock.", tag="LOCKED",
                     color=RED, run=f"{ctx.admin} unlock", stream=sys.stderr)
                return 3
            if res and res["status"] == "awaiting_approval" and not told:
                emit("Waiting for your approval", tag="WAITING", color=YELLOW, run_label="Approve it by running this", run=f"{ctx.admin} approve {jid}", stream=sys.stderr)
                told = True
            if res and res["status"] == "running" and told != "running" and not prog.on:
                emit(f"Running on {ctx.product} now", "Waiting for it to finish. Ctrl-C stops waiting, not the job.", tag="RUNNING", color=CYAN, stream=sys.stderr)
                told = "running"
            text, color = view.phase_of(res, jid, ctx.admin, approved, ctx.product)
            for _ in range(25):                               # redraw ~5 times a second; ask GitHub again every ~5 s
                prog.draw(text, color)
                ctx.sleep(0.2)
        prog.clear()
        emit(f"Stopped waiting after {timeout} s", "The job may still run.", tag="TIMEOUT", color=YELLOW, run_label="Check on it later", run=f"cw-run result {jid}", stream=sys.stderr)
        return 1
    finally:
        prog.clear()


def cmd_result(ctx, a):
    r = ctx.repos
    r.need_setup()
    if a.wait:
        return wait(ctx, a.id, a.timeout, a.json)
    r.pull_results()
    res = r.read_result(a.id)
    if res is None:
        if not r.job_known(a.id):
            emit(f"No job called {a.id}", "Check the id (cw-run status lists your jobs).", tag="ERROR", color=RED)
            return 1
        emit(f"{a.id} has no result yet", f"{ctx.product} has not picked it up. It looks about once a minute.", tag="QUEUED", color=YELLOW, run=f"cw-run result {a.id} --wait")
        return 0
    if res["status"] == "awaiting_approval":
        return awaiting_approval_screen(ctx, a.id)
    return view.print_result(res, a.json, admin=ctx.admin)


def cmd_jobs(ctx, n=15):
    r = ctx.repos
    r.need_setup()
    r.fetch_queue()
    r.pull_results()
    ids = r.result_ids()
    results = {jid: res for jid in ids if (res := r.read_result(jid))}
    listing = []
    out = r.git(r.queue, "ls-tree", "--name-only", f"origin/{r.jobs_branch}", "jobs/", check=False).stdout
    for path in filter(None, out.split("\n")):
        try:
            listing.append((Path(path).stem, json.loads(r.show_file(f"origin/{r.jobs_branch}", path) or "")))
        except ValueError:
            pass

    def created_of(jid):
        return next((view.parse_when(j.get("created", "")) for i, j in listing if i == jid), None)
    rows = view.status_rows(results, created_of, None, view.queued_rows(ids, listing, admin=ctx.admin))
    view.render_status(rows, results, ctx.label_of, ctx.hints, ctx.admin, n=n)
    return 0


def cmd_classes(ctx):
    r = ctx.repos
    r.need_setup()
    r.pull_results()
    r.fetch_queue()
    info = r.registry_info()
    if not info:
        emit(f"{ctx.product} has not published its class list yet", "It does this about a minute after an install. Try again shortly.", tag="ERROR", color=RED)
        return 1
    view.render_classes(info, product=ctx.product)
    return 0


def cmd_prune(ctx, days, force):
    ctx.repos.need_setup()
    emit("Jobs branch", ctx.repos.prune_jobs(days, force), tag="PRUNE", color=CYAN)
    return 0


# ---- offload: what is worth running on the worker ----
LOG_HELP = {"hook": ("(called by Claude Code's hooks; you never run this yourself)", [("Installed once in Claude Code's settings", "cw-run offload log hook pre")]),
            "top": ("rank the commands you run most that the worker could take",
                    [("The top 10 candidates", "cw-run offload log top"), ("Include shapes that are not candidates, with the reason", "cw-run offload log top --all"),
                     ("Only commands that usually take over a minute", "cw-run offload log top --min-seconds 60")]),
            "show": ("how many commands are recorded, and where the log is", [("Show the log summary", "cw-run offload log show")]),
            "clear": ("delete the log", [("Start again from nothing", "cw-run offload log clear")])}


def command_log(ctx, cfg=None):
    from .cmdlog import CommandLog
    home = Path(cfg.paths.client_home).expanduser() if cfg else Path("~/.cw").expanduser()
    return CommandLog(home, own_tools=ctx.own_tools)


def cmd_offload(ctx, a, cfg):
    if a.offload_cmd != "log" or not a.log_cmd:
        emit("Where to start", ("offload log top", "which of your commands are worth running on the worker"), ("offload log show", "where the log is, and how big"),
             tag="HELP", color=CYAN)
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
        emit("Nothing recorded yet", ("Log file", log.path), tag="NOTE", color=YELLOW, steps=["Install the Claude Code hooks: cw-run offload log hook pre|post"])
        return 0
    rows = log.rank(a.min_seconds, a.all, recs)[:a.top]
    out = sys.stdout
    print(paint(("All command shapes" if a.all else f"Candidates for {ctx.product} (median time at least {a.min_seconds:.0f} s)") + ".", BOLD, stream=out))
    print(paint(f"From {len(recs)} commands recorded on this computer.", DIM, stream=out))
    print(f"\n{paint('RUNS', BOLD, stream=out)}     how many times you ran it\n{paint('MEDIAN', BOLD, stream=out)}   the usual time one run takes\n"
          f"{paint('TOTAL', BOLD, stream=out)}    all the time you spent on it\n")
    if not rows:
        emit("No candidates yet", "A candidate is repeated, slow, runs locally, and is not a git, network or package-manager command.", tag="NOTE", color=YELLOW)
        return 0
    print(paint(f"{'#':>2}  {'RUNS':>5}  {'MEDIAN':>7}  {'TOTAL':>7}  COMMAND SHAPE", BOLD, stream=out))
    for i, r in enumerate(rows, 1):
        med = "?" if r["median"] is None else f"{r['median']:.0f}s"
        print(f"{i:>2}  {r['count']:>5}  {med:>7}  {r['total']:>6.0f}s  {r['shape']}" + (paint(f"   [{r['reason']}]", _ui.ITALIC, YELLOW, stream=out) if r["reason"] else ""))
    if not a.all:
        print("\n" + paint("Most recent example of each", BOLD, stream=out) + "\n")
        for i, r in enumerate(rows, 1):
            print(f"{i:>2}  {r['example'][:150]}")
        emit("Pick the one you like", tag="NEXT", color=CYAN, run_label=f"Check whether {ctx.product} can take it", run="cw-run offload fit -- <the example command>")
    return 0


# ---- parsing ----
def build_parser():
    ap = argparse.ArgumentParser(description="cw-run", add_help=False)
    sp = ap.add_subparsers(dest="cmd")
    sp.add_parser("setup", add_help=False)
    s = sp.add_parser("submit", add_help=False)
    s.add_argument("cls", metavar="class", help="the job class (cw-run classes lists them)")
    s.add_argument("-p", action="append", metavar="name=value", help="a job parameter; repeat for more")
    s.add_argument("--prompt", help="the prompt, for a claude class")
    s.add_argument("--prompt-file", metavar="FILE", help="read the prompt from a file")
    s.add_argument("--file", action="append", metavar="F", help="attach one text file; repeat for more")
    s.add_argument("--dir", metavar="D", help="attach the text files under a folder")
    s.add_argument("--include", action="append", metavar="GLOB", help="with --dir: only these files, for example 'src/**/*.js'")
    s.add_argument("--id", help="choose the job id (4 to 64 lowercase letters, digits and dashes)")
    s.add_argument("--wait", action="store_true", help="block until it finishes (the default in a terminal)")
    s.add_argument("--no-wait", action="store_true", help="queue it and return the id")
    s.add_argument("--timeout", type=int, default=900, metavar="SEC", help="how long --wait waits (default %(default)s)")
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    r = sp.add_parser("result", add_help=False)
    r.add_argument("id", help="the job id")
    r.add_argument("--wait", action="store_true", help="keep checking until it finishes")
    r.add_argument("--timeout", type=int, default=900, metavar="SEC", help="how long --wait waits (default %(default)s)")
    r.add_argument("--json", action="store_true", help="print the result as JSON")
    s = sp.add_parser("jobs", add_help=False)
    s.add_argument("-n", type=int, default=15, metavar="N", help="how many jobs to show (default %(default)s)")
    sp.add_parser("classes", add_help=False)
    o = sp.add_parser("offload", add_help=False)
    osp = o.add_subparsers(dest="offload_cmd")
    lg = osp.add_parser("log", add_help=False)
    lsp = lg.add_subparsers(dest="log_cmd")
    h = lsp.add_parser("hook", add_help=False)
    h.add_argument("phase", choices=["pre", "post"], help="before or after the command")
    t = lsp.add_parser("top", add_help=False)
    t.add_argument("--top", type=int, default=10, help="how many to list (default %(default)s)")
    t.add_argument("--min-seconds", type=float, default=30.0, help="only commands whose median run is at least this long (default %(default)s)")
    t.add_argument("--all", action="store_true", help="include shapes that are not candidates, with the reason")
    lsp.add_parser("show", add_help=False)
    lsp.add_parser("clear", add_help=False)
    s = sp.add_parser("prune", add_help=False)
    s.add_argument("--days", type=int, default=14, metavar="N", help="keep jobs from the last N days (default %(default)s)")
    s.add_argument("--force", action="store_true", help="squash even if the branch is short")
    return ap, sp


HELP = {"setup": ("clone the two private repos on this computer", [("Do it once", "cw-run setup")]),
        "submit": ("queue a job", [("Queue a job and wait for it", "cw-run submit sys-status --wait"), ("With a parameter", "cw-run submit net-check -p lan_host=192.0.2.1"),
                                   ("Attach the files a command needs", "cw-run submit lint --dir ./src --include 'src/**/*.js'")]),
        "result": ("read a job's result", [("Read it now", "cw-run result job-abc123"), ("Wait for it", "cw-run result job-abc123 --wait")]),
        "jobs": ("your most recent jobs, and the ones waiting for your approval", [("Show 15", "cw-run jobs"), ("Show more", "cw-run jobs -n 40")]),
        "offload": ("find out what is worth running on the worker", [("Which of my commands are worth offloading?", "cw-run offload log top"), ("Where is the log?", "cw-run offload log show")]),
        "classes": ("what the worker can run today", [("List them", "cw-run classes")]),
        "prune": ("squash old job files out of the jobs branch", [("Tidy it", "cw-run prune")])}


def main(argv=None, ctx=None, cfg=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap, sp = build_parser()
    if clihelp.show_cli_help(["cw-run", *argv], "cw-run", "queue jobs on the worker and read their results", sp, HELP, [("See what it can run", "cw-run classes")]):
        return 0
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        emit("That is not a command cw-run understands", "Run cw-run help to see the commands.", tag="ERROR", color=RED)
        return 2
    try:
        if ctx is None:
            cfg = cfg or config.load()
            ctx = Context.from_config(cfg)
        if a.cmd == "setup":
            return cmd_setup(ctx, cfg or config.load())
        if a.cmd == "submit":
            return cmd_submit(ctx, a)
        if a.cmd == "result":
            return cmd_result(ctx, a)
        if a.cmd == "jobs":
            return cmd_jobs(ctx, a.n)
        if a.cmd == "offload":
            return cmd_offload(ctx, a, cfg)
        if a.cmd == "classes":
            return cmd_classes(ctx)
        if a.cmd == "prune":
            return cmd_prune(ctx, a.days, a.force)
        emit("No command given", "Run cw-run help to see the commands.", tag="ERROR", color=RED)
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
