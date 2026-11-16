"""The worker's own commands: the three passes the timers run, and the local admin commands (status, approve, kill, unlock, ...).

  fetch | step | exec <id> | run [--once]        the passes (systemd runs fetch and step; step starts `exec` for each job)
  status [--hours N]                              health, what it is doing, recent use, and the safety checks
  show <id> | review <id>...                      read a job; the approval screen for waiting jobs (asks nothing)
  approve <id> [--sha256 D] [--yes] | --many ID[:SHA]...   approve exactly the job file the worker holds
  cancel <id> | kill <id> [--hard]                stop a job that has not started / is running
  quarantine [id] | unlock | output [id]          withheld results, clearing a lockdown, a failed command's full output
  prune-results [--days N] [--force] | test-alert | canary-alarm <text>
  check-registry FILE | install-packages FILE     used as root by the update tool, before a registry is installed

Every name, path and limit comes from the configuration (cw.toml) through `Context.from_config`; nothing here knows an install.
"""
import hashlib
import json
import os
import shutil
import signal
import sys
import textwrap
import time
from datetime import timedelta
from pathlib import Path

from . import alerts, config, gitio, guard, jobs, packages, perms, profiling, registry, results, review, runner
from . import jail as jail_mod
from . import ui as _ui
from .ui import BOLD, CYAN, DIM, GREEN, RED, YELLOW, paint
from .pkgspec import check_packages
from .util import iso, now, parse_iso
from .worker import Settings, Worker

CW_HELP = (("status [--hours N]", "health, what it is doing, recent use, and the safety checks"),
           ("approve <job>", "approve a job that is waiting for you"),
           ("cancel <job>", "stop a job that has not started yet"),
           ("kill <job> [--hard]", "stop a job that is running"),
           ("show <job>", "print a job file"),
           ("review <job>...", "show what waiting jobs will run, asking nothing"),
           ("quarantine [job]", "list results held back by the output guard, or read one"),
           ("output [job]", "the full output of a failed command (only its error lines were published)"),
           ("unlock", "clear a security lockdown after you have looked at it"),
           ("prune-results [--days N]", "shrink the results repo's history"),
           ("test-alert", "send sample alerts to your phone; nothing is locked"))


class Stop(Exception):
    """A deliberate end of a command (an error, or a 'no'): carries the exit code."""

    def __init__(self, code=1):
        self.code = code


def emit(*a, **k):
    k.setdefault("stream", sys.stdout)
    _ui.say(*a, **k)


def stop(title, *details, code=1, tag="STOPPED", color=YELLOW, **kw):
    emit(title, *details, tag=tag, color=color, **kw)
    raise Stop(code)


def human_dur(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec} s"
    if sec < 5400:
        return f"{round(sec / 60)} min"
    if sec < 172800:
        return f"{sec // 3600} h {sec % 3600 // 60:02d} min"
    return f"{sec // 86400} days {sec % 86400 // 3600} h"


class Context:
    """Everything a command needs: the worker, and the names that appear in messages."""

    def __init__(self, worker, *, product="the worker", admin="cw-admin", installed_rev=None, notifier=None, canary_status=None,
                 ask=input, hardware=None, uptime=None):
        self.w, self.product, self.admin = worker, product, admin
        self.installed_rev, self.notifier = installed_rev, notifier
        self.canary_status = canary_status or Path(f"/run/{product}/canary-watch.json")
        self.ask = ask
        self.hardware = hardware or {}
        self.uptime = uptime or self._uptime

    @staticmethod
    def _uptime():
        try:
            return float(Path("/proc/uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    @classmethod
    def from_config(cls, cfg, env=None):
        """Build the real worker from cw.toml and the secrets file. Nothing is read from the repos here."""
        env = os.environ if env is None else env
        name = cfg.identity.name
        secrets_path = cfg.path("secrets")
        secrets = config.read_env_file(secrets_path)
        problem = config.secrets_file_problem(secrets_path)
        state = Path(cfg.path("state"))
        settings = Settings(state=state, registry_path=Path(cfg.path("registry")), tools_root=Path(cfg.path("tools")), capacity=cfg.hardware.capacity,
                            max_load=cfg.hardware.max_load, max_temp_c=cfg.hardware.max_temp_c, product=name, identity=name,
                            exec_argv=[sys.executable, "-m", "cw.cli", "exec"], require_netns=cfg.isolation.require_netns)
        gitio.harden_git_env(os.environ)
        io = gitio.GitIO(cfg.path("queue_clone"), cfg.path("results_clone"), state, queue_branch=cfg.github.main_branch, jobs_branch=cfg.github.jobs_branch,
                         committer=(name, f"{name}@localhost"), log=_log)
        url = cfg.alerts.url if cfg.alerts.kind != "none" else ""
        notifier = alerts.Notifier(url=url or "", token=secrets.get("CW_ALERT_TOKEN", ""), log=_log, product=name)
        seclog = guard.SecurityLog(state / "security.log")
        lock = alerts.Lockdown(state, notifier, seclog, product=name, remind_at=cfg.alerts.remind_hours, repeat_h=cfg.alerts.remind_repeat_h)
        keeper = results.OutputKeeper(state)
        sensors = runner.LinuxSensors()
        pk = packages.Packages(cfg.path("tools"), state / "profile-env", product=name)
        w = Worker(settings, io, lock, keeper, pk, seclog=seclog, sensors=sensors, log=_log, home=cfg.paths.worker_home, api_key=secrets.get("ANTHROPIC_API_KEY") if cfg.claude.auth == "api-key" else None,
                   oauth_token=secrets.get("CLAUDE_CODE_OAUTH_TOKEN") if cfg.claude.auth == "account" else None,       # only the chosen sign-in is ever given to a job
                   hostname=None, jail=jail_mod.from_config(cfg))
        if problem:
            _log(f"WARNING: {problem}")
        return cls(w, product=name, installed_rev=Path(cfg.path("installed_rev")), notifier=notifier, canary_status=Path(cfg.path("run_dir")) / "canary-watch.json")


def _log(msg):
    print(f"{iso(now())} {msg}", flush=True)


# ---- the passes ----------------------------------------------------------------------------------------------------------------
def cmd_exec(ctx, jid):
    if not jobs.ID_RE.match(jid or ""):
        stop("That is not a job id", "Job ids are lowercase letters, digits and dashes.", tag="ERROR", color=RED, code=2)
    runner.install_term_handler()
    try:
        ctx.w.execute(jid)
    except runner.Killed:
        _log(f"{jid}: stopped")
    return 0


def cmd_run(ctx, once=False, poll=20):
    while True:
        ctx.w.fetch()
        ctx.w.step()
        if once:
            return 0
        time.sleep(poll)


# ---- reading and approving jobs -------------------------------------------------------------------------------------------------
def describe_job(ctx, raw):
    """The approval screen for a profile run; '' for anything else."""
    try:
        job = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(job, dict) or job.get("class") != "profile-run" or not isinstance(job.get("argv"), list):
        return ""
    w = ctx.w
    facts = profiling.command_facts(job["argv"], find_program=w.find_program)
    try:
        reg = w.load_registry()
        rules = registry.profiling_rules(reg)
        cls = (reg.get("classes") or {}).get("profile-run") or {}
        table = profiling.glance(job, rules, profiling.profile_limits(rules, w.mem_total()), w.snapshot(), jobs.job_timeout(cls, job), facts, ctx.product)
    except Exception:                                    # the numbers are extra; never block an approval on them
        table = ""
    try:
        specs = check_packages(job.get("packages"))
    except ValueError:
        specs = []
    text = w.pkgs.plan_text(w.pkgs.plan(specs, "<job id>")) if specs else None
    return review.review_text({**job, "packages": specs}, facts, text, table, product=ctx.product)


def cmd_show(ctx, jid):
    raw = ctx.w.io.job_bytes(jid) if jobs.ID_RE.match(jid or "") else None
    if raw is None:
        stop(f"No job called {jid}", "Its file is not in the queue repo (has it been pushed to the jobs branch?).", tag="ERROR", color=RED)
    print(raw.decode("utf-8", "replace"))
    return 0


def cmd_review(ctx, ids):
    ctx.w.io.sync()
    for jid in ids:
        raw = ctx.w.io.job_bytes(jid) if jobs.ID_RE.match(jid) else None
        print(paint(f"\n=== {jid} " + "=" * max(4, 60 - len(jid)), BOLD, CYAN, stream=sys.stdout))
        if raw is None:
            print(paint(f"  {ctx.product} has no copy of this job.", RED, stream=sys.stdout))
            continue
        text = describe_job(ctx, raw)
        print(review.color_review(text, ctx.product) if text else raw.decode("utf-8", "replace"))
    return 0


def cmd_approve(ctx, jid, sha=None, yes=False):
    if not jobs.ID_RE.match(jid or ""):
        stop("That is not a job id", "Job ids are lowercase letters, digits and dashes.", tag="ERROR", color=RED, code=2)
    ctx.w.io.sync()
    raw = ctx.w.io.job_bytes(jid)
    if raw is None:
        stop(f"No job called {jid}", "Its file is not in the queue repo (has it been pushed to the jobs branch?).", tag="ERROR", color=RED)
    digest = hashlib.sha256(raw).hexdigest()
    if sha and not yes and digest != sha.lower():
        stop("NOT approved: the job changed", f"{ctx.product}'s copy has digest {digest[:12]}..., not the {sha[:12]}... you reviewed.",
             "Look at it again before approving.", tag="REFUSED", color=RED)
    if not yes and not sha:
        text = describe_job(ctx, raw)
        print(review.color_review(text, ctx.product) if text else raw.decode("utf-8", "replace"))
        if ctx.ask(paint("\nApprove this exact job? [y/N] ", BOLD, YELLOW, stream=sys.stdout)).strip().lower() != "y":
            stop("Not approved", "You answered no. The job stays waiting.")
    ok, why = ctx.w.approve(jid, sha=None if yes else sha)
    if not ok:
        stop("NOT approved", why, tag="REFUSED", color=RED)
    emit("Approved", f"Job {jid}", f"{ctx.product} picks it up within a minute.", tag="APPROVED", color=GREEN, run_label="Watch for the result", run=f"cw-run result {jid}")
    return 0


def cmd_approve_many(ctx, items):
    """ID[:SHA256] ...: approve every job the client says was viewed, each pinned to what was viewed."""
    ctx.w.io.sync()
    bad = 0
    for item in items:
        jid, _, sha = item.partition(":")
        ok, why = ctx.w.approve(jid, sha=sha or None)
        bad += not ok
        word, color = ("APPROVED", GREEN) if ok else ("REFUSED", RED)
        print(f"{paint(f'{word:<9}', BOLD, color, stream=sys.stdout)}{jid}" + ("" if ok else f"  {why}"))
    print()
    stop(f"{len(items) - bad} approved, {bad} refused", f"{ctx.product} runs the approved ones within a minute, one at a time.",
         tag="DONE" if not bad else "PARTLY", color=GREEN if not bad else YELLOW, code=1 if bad else 0)


def cmd_cancel(ctx, jid):
    ok, why = ctx.w.cancel(jid)
    if not ok:
        stop(f"Cannot cancel {jid}", why.capitalize() + ".", tag="ERROR", color=RED, run_label="If it is running, stop it with", run="cw-kill <job>")
    emit("Cancelled", f"Job {jid} will not run.", tag="DONE", color=GREEN)
    return 0


def cmd_kill(ctx, jid, hard=False):
    if not jobs.ID_RE.match(jid or ""):
        stop("That is not a job id", tag="ERROR", color=RED, code=2)
    if not ctx.w.kill_job(jid, hard=hard):
        stop(f"{jid} is not running", "It may have finished, or it was never started.", tag="NOTE", color=YELLOW)
    emit("Stopping" if not hard else "Killed", f"Job {jid}", "It publishes a 'killed' result." if not hard else "The whole process tree was killed.", tag="DONE", color=GREEN)
    return 0


# ---- security ------------------------------------------------------------------------------------------------------------------
def cmd_quarantine(ctx, jid=None):
    qd = ctx.w.state / "quarantine"
    files = sorted(qd.glob("*.json")) if qd.exists() else []
    if jid:
        f = qd / f"{jid}.json"
        if f.exists() and jobs.ID_RE.match(jid):
            print(f.read_text())
        else:
            emit(f"No quarantined result {jid}", tag="NOTE", color=YELLOW)
        return 0
    state = ctx.w.lock.locked()
    if state:
        emit(f"{ctx.product} is LOCKED", ("What happened", state.get("kind")), ("Job", state.get("job")),
             ("Rules", ", ".join(state.get("rules") or []) or "n/a"), tag="LOCKED", color=RED)
    else:
        emit("Not locked", tag="OK", color=GREEN)
    if files:
        print(paint("Withheld results", BOLD, stream=sys.stdout) + paint("  (they never left this machine)", DIM, stream=sys.stdout) + "\n")
        for f in files:
            print(f"  {f.stem}")
        emit("Read one in full", tag="NEXT", color=CYAN, run=["cw-worker quarantine <job>", "cw-worker unlock"], run_label="Read one, then unlock when you are satisfied")
    else:
        emit("Nothing in quarantine", tag="OK", color=GREEN)
    return 0


def cmd_unlock(ctx):
    cleared = ctx.w.lock.clear()
    if not cleared:
        emit("Not locked", "There is nothing to unlock.", tag="OK", color=GREEN)
        return 0
    ctx.w.publish_security()
    emit("Unlocked", "The worker processes jobs again on its next pass.", tag="DONE", color=GREEN)
    return 0


def cmd_canary_alarm(ctx, text):
    """Called by the root-run canary watcher when auditd saw a decoy credential file being read."""
    text = "".join(c if " " <= c <= "~" else "?" for c in (text or ""))[:300]
    ctx.w.lock.engage("breach", "(decoy file read)", {"canary_access"}, detail=text)
    ctx.w.publish_security()
    emit("Canary alarm raised", "The worker is now locked.", tag="LOCKED", color=RED)
    return 0


def cmd_output(ctx, jid=None):
    keeper = ctx.w.keeper
    if jid:
        text = keeper.read(jid)
        if text is None:
            emit(f"No kept output for {jid}", f"Only failed commands keep their full output, for {keeper.keep_days} days.", tag="NOTE", color=YELLOW)
        else:
            print(text)
        return 0
    rows = keeper.listing()
    if not rows:
        emit("No kept output", f"Failed commands keep their full output here, for {keeper.keep_days} days.", tag="OK", color=GREEN)
        return 0
    print(paint("Full output of failed commands", BOLD, stream=sys.stdout) + paint("  (newest first; only the error lines were published)", DIM, stream=sys.stdout) + "\n")
    for name, kb, age in rows:
        print(f"  {name:<22}{kb:>6} KB  {human_dur(age)} ago")
    emit("Read one", tag="NEXT", color=CYAN, run="cw-output <job>")
    return 0


def cmd_prune_results(ctx, days=14, force=False):
    emit("Results repo", ctx.w.io.prune_results(days, ctx.w.s.prune_threshold, force=force), tag="PRUNE", color=CYAN)
    return 0


def cmd_test_alert(ctx):
    n = ctx.notifier
    if not n or not n.url.startswith("https://"):
        emit("No phone alert address is set", "alerts.url is empty in the configuration.", tag="NOTE", color=YELLOW)
        return 0
    why = guard.why_text(["private_ip"])
    n.send(f"{ctx.product} TEST", "test alert", fields=[("What", "this is only a test of the phone alert"), ("Why", why), ("Job", "test-0001")], tags="white_check_mark")
    remind = "STILL LOCKED test: this is how a reminder looks (nothing is locked)"
    n.send(f"{ctx.product} STILL LOCKED (test)", remind, fields=[("State", remind)], tags="white_check_mark", priority="default")
    emit("Test alerts sent", "Check your phone: one quarantine alert and one reminder. Nothing was locked.", tag="DONE", color=GREEN)
    return 0


# ---- status --------------------------------------------------------------------------------------------------------------------
def finished_jobs(ctx, since=None):
    """[(finished time, result)] from the published results, oldest first."""
    out = []
    folder = ctx.w.io.results / "results"
    for f in folder.glob("*.json") if folder.is_dir() else []:
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        t = parse_iso(r.get("finished"))
        if t and r.get("status") not in ("awaiting_approval", "running") and (since is None or t >= since):
            out.append((t, r))
    return sorted(out, key=lambda x: x[0])


def waiting_for_approval(ctx):
    folder = ctx.w.io.results / "results"
    out = []
    for f in folder.glob("*.json") if folder.is_dir() else []:
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if r.get("status") == "awaiting_approval" and not (ctx.w.state / "done" / r.get("id", "")).exists() and not (ctx.w.state / "queued" / f"{r.get('id')}.json").exists():
            out.append(r)
    return sorted(out, key=lambda r: r.get("id", ""))


def job_seconds(r):
    a, b = parse_iso(r.get("started")), parse_iso(r.get("finished"))
    return (b - a).total_seconds() if a and b else 0


def cmd_status(ctx, hours=12):
    w, out, at = ctx.w, sys.stdout, now()
    cols = shutil.get_terminal_size((100, 24)).columns

    def put(cells, text, indent):
        lines = textwrap.wrap(str(text), max(30, cols - indent - 1)) or [""]
        print(cells + lines[0])
        for extra in lines[1:]:
            print(" " * indent + extra)
    kv = lambda k, v, wd=14: put(f"  {paint(k.ljust(wd), BOLD, stream=out)}", v, 2 + wd)             # noqa: E731
    head = lambda t, note="": print("\n" + paint(t, BOLD, CYAN, stream=out) + (f"  {paint(note, DIM, stream=out)}" if note else ""))  # noqa: E731
    mark = lambda ok: paint(f"{'OK' if ok else 'WARN':<5}", BOLD, GREEN if ok else YELLOW, stream=out)       # noqa: E731
    print(f"\n{paint('status', BOLD, GREEN, stream=out)}  {paint(ctx.product + ', ' + time.strftime('%a %Y-%m-%d %H:%M %Z'), BOLD, stream=out)}")
    head("WORKER")
    up = ctx.uptime()
    kv("Up for", human_dur(up) if up else "unknown")
    load = os.getloadavg()
    kv("Load", f"{load[0]:.2f} now, {load[1]:.2f} over 5 min, {load[2]:.2f} over 15 min")
    kv("Capacity", f"{w.s.capacity} capacity points (a heavy job takes 4)")
    try:
        kv("Installed", f"{ctx.installed_rev.read_text().split()[0][:7]}  ({len(w.load_registry().get('classes') or {})} job classes)")
    except (OSError, IndexError, ValueError, AttributeError):
        pass
    last = finished_jobs(ctx)
    kv("Last job", f"{last[-1][1].get('id')}  {last[-1][1].get('status')}, {human_dur((at - last[-1][0]).total_seconds())} ago, took {human_dur(job_seconds(last[-1][1]))}" if last else "none yet")
    rows = []
    for dname in ("queued", "fetching", "running", "finished"):
        for f in sorted((w.state / dname).glob("*")):
            jid = f.stem if f.suffix == ".json" else f.name
            if dname == "queued" and any((w.state / x / jid).exists() for x in ("running", "fetching")):
                continue
            extra = {"queued": "waiting for capacity", "finished": "finished; waiting to be published to GitHub"}.get(dname, "")
            if dname in ("running", "fetching"):
                info = w._marker(f)
                alive = "alive" if dname == "fetching" or w.is_job_process(info.get("pid"), jid) else "PROCESS GONE"
                extra = f"getting packages: {info.get('step')}" if dname == "fetching" else f"pid {info.get('pid')} {alive} since {info.get('started')}"
            rows.append((dname, jid, extra))
    rows.sort(key=lambda r: (r[0] != "running", r[1]))
    head("JOBS RIGHT NOW")
    if rows:
        wd = max(len(r[1]) for r in rows)
        color = {"queued": YELLOW, "fetching": CYAN, "running": CYAN, "finished": GREEN}
        print(paint(f"  {'STATE':<10}{'JOB':<{wd}}  DETAILS", BOLD, stream=out))
        for dname, jid, extra in rows:
            put(f"  {paint(f'{dname:<10}', BOLD, color[dname], stream=out)}{jid:<{wd}}  ", extra, 2 + 10 + wd + 2)
    else:
        print(paint(f"  Nothing queued or running; {ctx.product} is idle.", DIM, stream=out))
    waiting = waiting_for_approval(ctx)
    if waiting:
        head("WAITING FOR YOU", "these will not run until you approve them")
        for r in waiting[:6]:
            print(f"  {paint(r.get('id', '?'), BOLD, YELLOW, stream=out)}  {r.get('class') or ''}")
        print("\n" + paint("Approve one", BOLD, stream=out) + "\n")
        print(paint(f"{ctx.admin} approve {waiting[0].get('id')}", BOLD, GREEN, stream=out))
    recent = finished_jobs(ctx, at.replace(microsecond=0) - timedelta(hours=hours))
    head(f"LAST {hours} HOURS")
    if not recent:
        print(paint("  No jobs finished in this time.", DIM, stream=out))
    else:
        by = {}
        for _, r in recent:
            by[r.get("status")] = by.get(r.get("status"), 0) + 1
        colr = {"ok": GREEN, "error": RED, "limit": YELLOW, "rejected": RED, "killed": YELLOW, "cancelled": DIM, "quarantined": RED}
        parts = [f"{by[k]} {paint(k, colr.get(k, BOLD), stream=out)}" for k in ("ok", "error", "limit", "rejected", "killed", "cancelled", "quarantined") if by.get(k)]
        busy = sum(job_seconds(r) for _, r in recent)
        kv("Jobs", f"{len(recent)} finished  ({', '.join(parts)})")
        kv("Busy time", f"{human_dur(busy)} of work in {hours} h  ({busy / (hours * 3600) * 100:.1f}% of one slot)")
        longest = max(recent, key=lambda x: job_seconds(x[1]))
        kv("Longest job", f"{human_dur(job_seconds(longest[1]))}  ({longest[1].get('id')})")
    head("CHECKS")
    sw = 24

    def line(ok, name, stat, why):
        put(f"  {mark(ok)} {paint(name.ljust(11), BOLD, stream=out)}{paint(stat.ljust(sw), BOLD, GREEN if ok else RED, stream=out)}", why, 2 + 6 + 11 + sw)
    iso_on = w.jail.ready()
    line(iso_on, "Isolation", "ON" if iso_on else "OFF", w.jail.describe() if iso_on else f"jobs are REFUSED until it works: {w.jail.problem()}")
    cw = ctx.canary_status
    try:
        st = json.loads(Path(cw).read_text())
        ok = bool(st.get("monitoring")) and time.time() - st.get("ts", 0) < 300
        why = "watching trap files that reveal a job snooping for secrets" if ok else (st.get("reason") or "canary-watch has not reported for over 5 minutes")
    except (OSError, ValueError):
        ok, why = False, "canary-watch has not reported"
    line(ok, "Canary", "ON" if ok else "OFF", why)
    try:
        passes = json.loads((Path(w.state) / PASS_FILE).read_text())
    except (OSError, ValueError):
        passes = {}
    for which, label, what in (("fetch", "Fetch", "pulling new jobs from GitHub"), ("step", "Step", "starting queued jobs")):
        p = passes.get(which)
        if not p:
            line(False, label, "NOT RUN YET", f"the timer has not run the {which} pass yet ({what})")
        elif not p["ok"]:
            line(False, label, "FAILED", f"{time.strftime('%H:%M:%S', time.localtime(p['ts']))}: {p.get('error') or 'see the journal'}  (nothing is {what})")
        elif time.time() - p["ts"] > 300:
            line(False, label, "STALLED", f"last ran {int((time.time() - p['ts']) / 60)} min ago; the timer may have stopped")
        else:
            line(True, label, "OK", f"last pass {int(time.time() - p['ts'])} s ago ({what})")
    total, avail = w.sensors.meminfo() if w.sensors else (0, None)
    if total and avail is not None:
        good = avail > total * 0.1
        line(good, "Memory", f"{avail / 1024:.1f} GB free of {total / 1024:.1f} GB", "enough for the next job" if good else "under 10% free; jobs may hit their memory limit")
    temp = w.sensors.temp() if w.sensors else None
    if temp is not None:
        line(temp < w.s.max_temp_c, "Heat", f"{temp:.0f} C", "normal" if temp < w.s.max_temp_c else f"hot; no job starts above {w.s.max_temp_c:.0f} C")
    else:
        line(False, "Heat", "unreadable", "no temperature sensor answered")
    try:
        du = shutil.disk_usage(w.state)
        line(du.free / 1e9 > 5, "Disk", f"{du.free / 1e9:.0f} GB free of {du.total / 1e9:.0f} GB", "plenty of room" if du.free / 1e9 > 5 else "under 5 GB free")
    except OSError:
        pass
    state = w.lock.locked()
    if state:
        line(False, "Security", "LOCKED", f"{ctx.product} runs nothing until you review and unlock it")
        emit("SECURITY LOCKDOWN", ("What happened", state.get("kind")), ("Job", state.get("job")), tag="LOCKED", color=RED,
             run_label="Look at it, then unlock", run=["cw-worker quarantine", "cw-worker unlock"])
    else:
        line(True, "Security", "NOT LOCKED", "the output guard has not tripped; jobs are being accepted")
        print()
    return 0


# ---- the timer-driven passes: what happened last time, so `status` can say why nothing is moving -----------------------------
PASS_FILE = "pass-status.json"


def record_pass(state, which, ok, error=""):
    f = Path(state) / PASS_FILE
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        data = {}
    data[which] = {"ts": int(time.time()), "ok": ok, "error": error[:300]}
    try:
        f.write_text(json.dumps(data))
    except OSError:
        pass                                    # reporting must never be the reason a pass fails


def run_pass(ctx, which):
    try:
        getattr(ctx.w, which)()
    except Exception as e:
        record_pass(ctx.w.state, which, False, f"{type(e).__name__}: {e}")
        raise
    record_pass(ctx.w.state, which, True)
    return 0


# ---- root-run helpers ----------------------------------------------------------------------------------------------------------
def cmd_check_registry(path, tools_root):
    try:
        errs = registry.validate(registry.parse(Path(path).read_text()), tools_root)
    except Exception as e:
        errs = [f"not valid YAML: {e}"]
    for e in errs:
        print(f"registry problem: {e}", file=sys.stderr)
    return 1 if errs else 0


def cmd_install_packages(ctx, path):
    reg = registry.parse(Path(path).read_text())
    return ctx.w.pkgs.install_registry_packages(reg)


# ---- dispatch ------------------------------------------------------------------------------------------------------------------
def usage():
    emit("Commands", *[(c, d) for c, d in CW_HELP], tag="HELP", color=CYAN)
    return 0


def _flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def _value(args, name, default, cast):
    if name in args:
        i = args.index(name)
        try:
            v = cast(args[i + 1])
        except (IndexError, ValueError):
            stop(f"{name} needs a number", tag="ERROR", color=RED, code=2)
        del args[i:i + 2]
        return v
    return default


def main(argv=None, ctx=None, cfg=None):
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args.pop(0) if args else "help"
    try:
        if cmd in ("help", "-h", "--help"):
            return usage()
        if cmd == "check-registry" and len(args) == 1:                    # run as root, before any state is touched
            cfg = cfg or config.load()
            return cmd_check_registry(args[0], cfg.path("tools"))
        if ctx is None:
            cfg = cfg or config.load()
            if cmd == "install-packages":
                ctx = Context.from_config(cfg)
                return cmd_install_packages(ctx, args[0])
            os.umask(perms.STATE_UMASK)                                    # after the root-run commands: root's tools folder must stay readable
            ctx = Context.from_config(cfg)
            perms.tighten_modes([ctx.w.state, ctx.w.io.queue, ctx.w.io.results], ctx.w.state / perms.MODES_MARKER)
        if cmd in ("fetch", "step"):
            return run_pass(ctx, cmd)
        if cmd == "exec" and len(args) == 1:
            return cmd_exec(ctx, args[0])
        if cmd == "run":
            return cmd_run(ctx, once=_flag(args, "--once"))
        if cmd == "status":
            return cmd_status(ctx, hours=min(168, max(1, _value(args, "--hours", 12, int))))
        if cmd == "show" and len(args) == 1:
            return cmd_show(ctx, args[0])
        if cmd == "review" and args:
            return cmd_review(ctx, args)
        if cmd == "approve":
            if args[:1] == ["--many"] and len(args) >= 2:
                return cmd_approve_many(ctx, args[1:])
            yes, sha = _flag(args, "--yes"), _value(args, "--sha256", None, str)
            if len(args) == 1:
                return cmd_approve(ctx, args[0], sha=sha, yes=yes)
        if cmd == "cancel" and len(args) == 1:
            return cmd_cancel(ctx, args[0])
        if cmd == "kill":
            hard = _flag(args, "--hard")
            if len(args) == 1:
                return cmd_kill(ctx, args[0], hard=hard)
        if cmd == "quarantine" and len(args) <= 1:
            return cmd_quarantine(ctx, args[0] if args else None)
        if cmd == "unlock" and not args:
            return cmd_unlock(ctx)
        if cmd == "output" and len(args) <= 1:
            return cmd_output(ctx, args[0] if args else None)
        if cmd == "prune-results":
            force = _flag(args, "--force")
            return cmd_prune_results(ctx, days=_value(args, "--days", 14, int), force=force)
        if cmd == "test-alert" and not args:
            return cmd_test_alert(ctx)
        if cmd == "canary-alarm" and len(args) == 1:
            return cmd_canary_alarm(ctx, args[0])
        emit(f"Unknown or incomplete command: {cmd}", "Run it with no arguments to see the commands.", tag="ERROR", color=RED)
        return 2
    except Stop as s:
        return s.code
    except config.ConfigError as e:
        emit("The configuration has problems", *[f"- {p}" for p in e.problems], tag="ERROR", color=RED)
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main())
