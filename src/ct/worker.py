"""The worker: the fetch, step and exec passes that turn a job file in the queue repo into a published result.

  fetch   pull the repos, publish what is pending, snapshot each NEW job file once (edits to the repo copy are ignored), validate it
          against the INSTALLED registry, and either reject it, hold it for approval, or put it in the local queue.
  step    start queued jobs oldest first, only while capacity, load and temperature allow, and never while locked down.
  exec    run one queued job (a separate process), guard its output, and publish the result.

Safety properties kept here: the registry is the installed copy, never the repo's; an approval is bound to the digest of the exact job
file; the job that runs is the validated snapshot; output is shaped, scanned, and withheld (and the worker locked) on a guard hit; a
published result is kept locally until the push succeeds. Every call that touches the machine is passed in, so the whole lifecycle is
tested without root, a network or special hardware.
"""
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger as ledger_mod, patch as patch_mod, approvals, claudeflags, guard, jobs, profiling, publishdiag, registry, results, workerstate
from .gitio import FileLock
from . import refusals
from .jobs import Reject
from .packages import PKG_KEEP_DAYS, PrepError, missing_hint
from .jail import Unshare
from . import plat
from .runner import MAX_TIMEOUT, class_network, kill_group, run_command
from .util import SAFE_PATH, iso, now
from .words import plural


@dataclass
class Settings:
    state: Path
    registry_path: Path
    tools_root: Path
    tool_roots: tuple = ()                  # folders of existing tools a class may name (isolation.tool_roots; only when isolation.mode is relaxed)
    isolation_mode: str = "full"
    isolation_purpose: str = "tenta-only"
    pause_when_logged_in: bool = False
    ignore_users: tuple = ()
    window: str = ""
    capacity: int = 4
    max_load: float = 3.0
    max_temp_c: float = 75.0
    grace_s: int = 180                      # the watchdog fires this long after a job's own timeout
    max_age_s: int = 86400
    product: str = "the worker"
    identity: str = "ct-worker"             # the name decoy credentials are planted under
    approve_note: str = "approve it: ct-admin approve {jid} (on your computer) or ct-approve {jid} (on the worker)"
    exec_argv: list = field(default_factory=list)       # the command that runs one job: exec_argv + [job id]
    claude_command: str = "claude"
    claude_jobs_per_day: int = 0              # the daily Claude limits (see ledger); 0 is no limit
    claude_turns_per_day: int = 0
    prune_days: int = 14
    prune_threshold: int = 200
    pkg_timeout: int = 900
    require_netns: bool = True
    max_job_mb: int = None                  # the largest job file accepted (None: automatic, see Worker.job_ceiling_mb)
    transport: str = "github"               # "direct": the published file limits follow the ceiling instead of the GitHub-era defaults (see Worker.effective_limits)
    disk_reserve: bool = True               # wait instead of starting a job when the disk is nearly full (see Worker.busy_reason)
    profile_approval: str = "required"       # "none" (Direct only): a profile run starts without approval, but only while the jail really isolates jobs (see Worker.profile_runs_unapproved)
    idle_after_s: int = 600                 # smart sleep: nothing to do for this long ...
    idle_fetch_s: int = 90                  # ... and the fetch pass only really runs this often (0: never slows down)
    sleep_after_s: int = 7200               # sleep mode: nothing to do for this long ...
    sleep_fetch_s: int = 300                # ... and the fetch pass only really runs this often (0: no sleep mode)


class Worker:
    def __init__(self, settings, io, lockdown, keeper, packages, *, seclog=None, sensors=None, runner=run_command, log=None, clock=now,
                 home=None, api_key=None, oauth_token=None, signed_approval=None, approvers_file=None, state_facts=None, privileged_call=None, hostname=None, jail=None, is_job_process=None, spawn=None, find_program=None, loadavg=None):
        self.s, self.io, self.lock, self.keeper, self.pkgs = settings, io, lockdown, keeper, packages
        self.claude_flags = claudeflags.flags
        self.sensors = sensors
        self.runner, self.clock = runner, clock
        self.log = log or (lambda msg: None)
        self.home, self.api_key, self.hostname = str(home or Path.home()), api_key, hostname
        self.oauth_token = oauth_token
        self.approvers_file = approvers_file
        self.idle_now = False
        self.mode = "active"                       # active (checks every fetch_s) | idle (every idle_fetch_s) | sleep (every sleep_fetch_s)
        self.privileged_call = privileged_call            # (exact job file, signed approval text, timeout) -> the root helper's answer; the worker never runs a root-level action itself
        self.state_facts = state_facts            # () -> the facts only the install knows (name, release, OS, Claude tool...) for worker-state.json
        self.signed_approval = signed_approval            # (job id, digest) -> bool: is there a valid approval, signed on the administrator's own computer?
        self.jail = jail if jail is not None else Unshare()
        self.loadavg = loadavg or plat.loadavg
        self.is_job_process = is_job_process or self._pid_is_job
        self._spawn = spawn or self._default_spawn
        self.find_program = find_program or (lambda name: shutil.which(name, path=SAFE_PATH))
        self.ledger = ledger_mod.Ledger(settings.state, settings.claude_jobs_per_day, settings.claude_turns_per_day, clock=clock)
        self.limits = registry.limits_of({})
        self.policy = registry.policy_of({})
        self.state = Path(settings.state)
        for d in ("done", "approved", "notified", "work", "queued", "running", "fetching", "finished", "inbox", "quarantine", "output"):
            (self.state / d).mkdir(parents=True, exist_ok=True)
        (self.state / "quarantine").chmod(0o700)
        (self.state / "output").chmod(0o700)

    # ---- small helpers ----
    def _p(self, *parts):
        return self.state.joinpath(*parts)

    def _pid_is_job(self, pid, jid):
        return jid in plat.process_cmdline(pid)

    def _default_spawn(self, jid):
        return subprocess.Popen([*self.s.exec_argv, jid], stdin=subprocess.DEVNULL, **plat.new_session()).pid

    def job_ceiling_mb(self):
        """The largest job file this worker accepts, in MB: the owner's hardware.max_job_mb, or automatically the smallest of a quarter of its free disk, a quarter of its free memory and 1024 MB (a job's files travel inside
        one JSON file that is staged in memory, so memory bounds it until files are streamed to disk). A reading that is missing does not limit."""
        if self.s.max_job_mb:
            return int(self.s.max_job_mb)
        snap = self.snapshot()
        terms = [1024.0]
        if snap.get("disk_free_gb") is not None:
            terms.append(snap["disk_free_gb"] * 1000 / 4)
        if snap.get("mem_available_mb") is not None:
            terms.append(snap["mem_available_mb"] / 4)
        return int(max(1, min(terms)))

    def disk_reserve_gb(self, total_gb=None):
        """Free disk kept back: 10% of the volume, never less than 2 GB. Below it the worker starts no job."""
        if total_gb is None:
            total_gb = shutil.disk_usage(self.state).total / 1e9
        return max(2.0, round(total_gb * 0.10, 1))

    def time_limit(self, cls, job, reg):
        """A job's time limit: the class's own, or for a profile run the person's estimate x speed factor x margin (the owner's ceiling only where the computer is shared)."""
        profile = cls.get("kind") == "profile"
        return jobs.job_timeout(cls, job, rules=registry.profiling_rules(reg) if profile else None, shared=self.shared(), factor=self.speed()[0])

    def shared(self):
        return self.s.isolation_purpose == "shared"

    def speed(self):
        """(factor, basis): how slow this worker is against the shipped reference (2.0 = twice as slow), from the install's own timings; 1.0 until measured."""
        from . import calibration, calibration_reference
        got = calibration.load(self._p("calibration.json")).get("phases")
        return calibration.speed_factor(got, calibration_reference.REFERENCE) if isinstance(got, dict) else (1.0, "none yet")

    def effective_limits(self, reg):
        """The registry's limits, except that on the Direct connection the file limits follow this worker's ceiling (what it can really hold) instead of the GitHub-era defaults, unless the owner set them in the registry."""
        lim = registry.limits_of(reg)
        if self.s.transport == "direct":
            given = ((reg or {}).get("limits") or {}).get("files") or {}
            kb = self.job_ceiling_mb() * 1000
            for key, value in (("max_total_kb", kb), ("max_file_kb", kb), ("max_count", 100000)):
                if key not in given:
                    lim["files"][key] = value
        return lim

    def _blob_size(self, ref, name):
        """The size in bytes of a job file in the local clone, asked of git without reading its content (no network: the clone already has it); None when it cannot be told."""
        out = self.io.git_bytes(self.io.queue, "cat-file", "-s", f"{ref}:{name}")
        try:
            return int((out or b"").strip())
        except ValueError:
            return None

    def _reject_unread(self, jid, size, ceiling_mb):
        res = {"id": jid, "class": None, "status": "rejected", "started": None, "finished": None, "exit_code": None, "output": "",
               "note": str(refusals.refuse("job.too_big_for_worker", size=f"{size / 1e6:.1f}", ceiling=ceiling_mb)), "refusal": "job.too_big_for_worker"}
        self._finish(res, False)

    def load_registry(self):
        reg = registry.parse(self.s.registry_path.read_text())
        self.limits = self.effective_limits(reg)
        self.policy = registry.policy_of(reg)
        return reg

    def tool_folders(self, tools):
        """([real folders], refusal or None) for a class's `tools`: each must exist here and, once symbolic links are followed, still be inside a folder the owner allowed (isolation.tool_roots). The jail then
        shows them read-only and puts them first on the job's PATH. Checked at EVERY run, whatever the registry said when it was installed."""
        if self.s.isolation_mode != "relaxed":
            return [], ("this worker is in full isolation, so a class's tool folders are not shown to jobs. The owner can change that: isolation.mode = \"relaxed\" and the folder in isolation.tool_roots "
                        "(see ct-worker status)")
        roots = [os.path.realpath(r) for r in self.s.tool_roots]
        real = []
        for t in tools:
            r = os.path.realpath(t)
            if not os.path.isdir(r):
                return [], f"the tool folder {t} does not exist on this worker, so the job was not run"
            if not registry.inside(r, roots):
                return [], f"the tool folder {t} leads outside the folders this worker allows (isolation.tool_roots), so the job was not run"
            real.append(r)
        return real, None

    def mem_total(self):
        return self.sensors.meminfo()[0] if self.sensors else 0

    def snapshot(self):
        total, avail = self.sensors.meminfo() if self.sensors else (0, None)
        free_gb = plat.disk_free_gb(self.state)
        return {"at": iso(self.clock()), "cores": os.cpu_count(), "mem_total_mb": total, "mem_available_mb": avail,
                "load_1m": round(self.loadavg(), 2), "temp_c": self.sensors.temp() if self.sensors else None, "disk_free_gb": free_gb,
                "running": len(list(self._p("running").glob("*"))), "waiting": len(list(self._p("queued").glob("*.json"))), "held": self.availability_reason()}

    def availability_reason(self):
        """Why this computer is not available for new work right now because of how it is shared (None = available): a person is logged in, or it is outside its working hours."""
        if self.s.window:
            start, _, end = self.s.window.partition("-")
            t = self.clock().astimezone()
            mins = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:5])                      # noqa: E731
            now, lo, hi = t.hour * 60 + t.minute, mins(start), mins(end)
            inside = lo <= now < hi if lo < hi else (now >= lo or now < hi)
            if not inside:
                return f"outside its working hours ({self.s.window}, now {t.strftime('%H:%M %Z')})"                      # a time on screen says its zone
        if self.s.pause_when_logged_in:
            who = getattr(self.sensors, "logged_in", None)
            people = sorted(set(who()) - set(self.s.ignore_users)) if who else []
            if people:
                return "a person is using this computer (" + ", ".join(people[:3]) + ")"
        return None

    def busy_reason(self):
        """Why the worker should not start another job right now (None = fine)."""
        load = self.loadavg()
        if load > self.s.max_load:
            return f"load {load:.2f} > {self.s.max_load}"
        t = self.sensors.temp() if self.sensors else None
        if t is not None and t > self.s.max_temp_c:
            return f"core temp {t:.0f}C > {self.s.max_temp_c:.0f}C"
        if self.s.disk_reserve:
            try:
                usage = shutil.disk_usage(self.state)
            except OSError:
                usage = None
            if usage is not None and usage.free / 1e9 < self.disk_reserve_gb(usage.total / 1e9):
                return f"disk nearly full ({usage.free / 1e9:.1f} GB free, {self.disk_reserve_gb(usage.total / 1e9)} GB kept in reserve)"
        return self.availability_reason()

    # ---- approval ----
    def approve(self, jid, sha=None):
        """Approve exactly the job file the worker holds. With `sha`, only if its digest is the one the person reviewed.
        Returns (True, '') or (False, why)."""
        raw = self.io.job_bytes(jid) if jobs.ID_RE.match(jid or "") else None
        if raw is None:
            return False, "no copy of that job on the worker"
        digest = hashlib.sha256(raw).hexdigest()
        if sha and digest != sha.lower():
            return False, f"the job changed since you viewed it ({digest[:12]} is not {sha[:12]})"
        try:
            kind = ((self.load_registry().get("classes") or {}).get(json.loads(raw).get("class")) or {}).get("kind")
        except Exception:
            kind = None
        if kind == "privileged":
            return False, "this class runs as root, so it can only be approved with a signature from your computer (ct-admin approve), not on the worker"
        self._p("approved", jid).write_text(digest)
        return True, ""

    def apply_controls(self):
        """Carry out the orders (unlock, cancel, kill) the administrator signed on their own computer. Each order is acted on at most once, whatever the outcome:
        it is marked done BEFORE it is carried out, so a crash cannot repeat it. An order that is unsigned, signed by another key, or stale does nothing."""
        if not self.approvers_file or not os.path.isfile(self.approvers_file):
            return 0
        done_dir = self.state / "control-done"
        done_dir.mkdir(exist_ok=True)
        acted = 0
        for name in self.io.control_listing():
            key = Path(name).stem
            if (done_dir / key).exists():
                continue
            raw = self.io.control_bytes(name)
            if raw is None:
                continue
            ok, why, rec = approvals.check_control(raw.decode("utf-8", "replace"), self.approvers_file)
            (done_dir / key).write_text(why or "accepted")
            if not ok:
                self.log(f"an order ({key}) was found but not accepted: {why}")
                continue
            acted += 1
            self._carry_out(rec)
        return acted

    def _carry_out(self, rec):
        action = rec["action"]
        if action == "cancel":
            ok, why = self.cancel(rec["job"])
            self.log(f"signed order: cancel {rec['job']}: " + ("done" if ok else why))
        elif action == "kill":
            ok = self.kill_job(rec["job"], hard=bool(rec.get("hard")))
            self.log(f"signed order: kill {rec['job']}: " + ("done" if ok else "it was not running"))
        elif action == "unlock":
            st = self.lock.locked()
            if not st:
                self.log("signed order: unlock: the worker was not locked")
            elif rec.get("lock") != st.get("since"):
                self.log("signed order: unlock refused: it was signed for an earlier lockdown, not the current one")
            else:
                self.lock.clear()
                self.publish_security()
                self.log("signed order: unlocked")

    def profile_runs_unapproved(self):
        """True when this worker starts a profile run without approval: the owner set transport.profile_approval = none (Direct only) AND the job really is confined here (a jail that isolates and is ready, in full isolation).
        Anywhere else the setting changes nothing: a worker whose jail is missing, broken or only a Windows Job Object still waits for approval."""
        return self.s.profile_approval == "none" and self.s.isolation_mode == "full" and bool(getattr(self.jail, "isolates", False)) and bool(self.jail.ready())

    def _approved(self, jid, digest, signed_only=False):
        """Is there an approval for exactly this file? `signed_only` (root-level jobs): only a signature made on the administrator's own computer counts, never the
        worker's own approval marker, which anything running as the worker could write."""
        m = self._p("approved", jid)
        if signed_only:
            return bool(self.signed_approval is not None and self.signed_approval(jid, digest))
        if m.exists() and m.read_text().strip() == digest:
            return True
        if self.signed_approval is not None and self.signed_approval(jid, digest):
            m.write_text(digest)                               # remembered as any approval is: it is for this exact file
            self.log(f"approved {jid} (signed on the administrator's computer)")
            return True
        return False

    # ---- the guard's reaction ----
    def trip(self, res, rules):
        """A job's output matched the guard: quarantine it locally, withhold it, alert, and lock down."""
        kind = "breach" if "canary" in rules else "quarantine"
        q = self._p("quarantine", f"{res['id']}.json")
        q.write_text(json.dumps(res, indent=2))
        q.chmod(0o600)
        self.lock.engage(kind, res["id"], set(rules), cls=res.get("class"))
        res.update(status="quarantined", output="", note=f"output withheld by the security guard ({', '.join(sorted(rules))}); "
                                                          "the full text is on the worker only (ct-admin quarantine)")
        res.pop("facts", None)
        res.pop("patch", None)                                  # a patch is output too: withheld with the rest

    def publish_security(self):
        st = self.lock.public_status()
        self.io.publish_file("security.json", json.dumps(st, indent=2) + "\n", "security: " + ("LOCKED" if st["locked"] else "clear"))

    # ---- validation (runs at intake and again at exec, on the snapshot) ----
    def _validate(self, jid, raw, reg, res):
        jobs.check_id(jid)
        try:
            job = json.loads(raw)
        except ValueError:
            raise refusals.refuse("job.json.invalid")
        if not isinstance(job, dict) or job.get("id") != jid:
            raise refusals.refuse("job.id.mismatch")
        cname = job.get("class")
        res["class"] = cname if isinstance(cname, str) else None
        cls = (reg.get("classes") or {}).get(cname)
        if cls is None:
            raise refusals.refuse("job.class.unknown", name=cname)
        jobs.check_age(job, self.s.max_age_s, self.clock)
        params = job.get("params") or {}
        if not isinstance(params, dict):
            raise refusals.refuse("job.params.not_object")
        kind = cls.get("kind", "command")
        argv = prompt = None
        files, meta = {}, {}
        try:
            if kind == "command":
                argv = jobs.build_argv(cls, params, self.policy)
                files = jobs.check_files(job.get("files"), cls, self.limits)
            elif kind == "profile":
                if params:
                    raise refusals.refuse("job.profile.params")
                argv, files, meta = jobs.build_profile(job, cls, self.limits, find_program=self.find_program, policy=self.policy, shared=self.shared())
            elif kind == "privileged":
                action = (reg.get("actions") or {}).get(cls.get("action"))
                if not isinstance(action, dict):
                    raise refusals.refuse("job.action.missing")
                if job.get("files") or job.get("prompt"):
                    raise refusals.refuse("job.root.params_only")
                from . import actions as actions_mod
                argv = actions_mod.build_argv(action, params)               # only to validate the parameters now; the helper builds it again from the root-owned registry
            elif kind == "claude":
                prompt = job.get("prompt")
                if not isinstance(prompt, str) or not 1 <= len(prompt) <= 8000:
                    raise refusals.refuse("job.prompt.length")
                files = jobs.check_files(job.get("files"), cls, self.limits)
            else:
                raise refusals.refuse("job.kind.unknown", kind=kind)
        except refusals.Refused:
            raise                                                           # already a catalog refusal: its code goes on to the result
        except ValueError as e:                                             # a validator that has no entry of its own yet (actions.build_argv): the generic one, with its text
            raise refusals.refuse("job.invalid", detail=str(e))
        return job, cls, kind, argv, prompt, files, meta

    # ---- one job ----
    def process(self, path, reg, run=False):
        """run=False: validate and put the job in the local queue (or reject it, or hold it for approval).
        run=True: execute a queued snapshot."""
        path = Path(path)
        jid = path.stem
        if self._p("done", jid).exists():
            return
        if not run and any(x.exists() for x in (self._p("queued", f"{jid}.json"), self._p("running", jid), self._p("finished", f"{jid}.json"))):
            return
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        res = {"id": jid, "class": None, "status": "rejected", "started": None, "finished": None, "exit_code": None, "output": "", "note": ""}
        try:
            job, cls, kind, argv, prompt, files, meta = self._validate(jid, raw, reg, res)
            # a profile run executes a command nobody has seen before, so it ALWAYS needs approval
            if (kind in ("profile", "privileged") or cls.get("tier", "approve") != "auto") and not (kind == "profile" and self.profile_runs_unapproved()) \
                    and not self._approved(jid, digest, signed_only=(kind == "privileged")):
                if not self._p("notified", jid).exists():
                    if kind == "profile":
                        res["facts"] = profiling.command_facts(argv, find_program=self.find_program)
                        rules = registry.profiling_rules(reg)
                        res["glance"] = profiling.glance(job, rules, profiling.profile_limits(rules, self.mem_total()), self.snapshot(),
                                                         self.time_limit(cls, job, reg), res["facts"], self.s.product)
                        if meta.get("packages"):
                            plan = self.pkgs.plan(meta["packages"], jid)
                            if plan["problems"]:
                                raise refusals.refuse("job.packages.plan", problems="; ".join(plan["problems"]))
                            res["package_text"] = self.pkgs.plan_text(plan)
                    res.update(status="awaiting_approval", note=self.s.approve_note.format(jid=jid))
                    if kind == "privileged":
                        res["note"] = (f"this runs as root ({' '.join(argv)}), so it needs your signed approval: ct-admin approve {jid} (from your computer, "
                                       "with your approval key); an approval typed on the worker is not accepted")
                    self.io.publish(res)
                    self._p("notified", jid).touch()
                return
            if not run:
                self._p("queued", f"{jid}.json").write_bytes(raw)          # the snapshot that will execute is exactly what was validated
                self._p("queued", f"{jid}.meta").write_text(json.dumps({"class": job.get("class"), "packages": bool(job.get("packages")), "timeout_s": job.get("timeout_s"), "est_time_s": job.get("est_time_s")}))      # what step needs, so it never parses the whole job
                self.log(f"queued {jid} ({cls.get('cost', 'medium')})")
                return
            self._execute(jid, job, cls, kind, argv, prompt, files, meta, reg, res)
        except Reject as e:
            res.update(status="rejected", note=str(e))
            if getattr(e, "code", None):
                res["refusal"] = e.code                                    # machine-readable: which rule refused it (shown as the last row, and counted later)
        except Exception as e:                                              # one bad job must never kill the loop
            res.update(status="error", note=f"worker error: {e}")
        self._finish(res, run)

    def _finish(self, res, run):
        jid = res["id"]
        res["finished"] = iso(self.clock())
        fin = self._p("finished", f"{jid}.json")
        fin.write_text(json.dumps(res))                      # keep the result locally until the push succeeds
        if self.io.publish(res):
            self._p("done", jid).touch()
            fin.unlink(missing_ok=True)
            if res.get("status") == "quarantined":
                self.publish_security()
        if run:
            self._p("queued", f"{jid}.json").unlink(missing_ok=True)
            self._p("queued", f"{jid}.meta").unlink(missing_ok=True)
            self._p("running", jid).unlink(missing_ok=True)

    def _command_for(self, jid, cls, kind, argv, prompt, meta, reg, res):
        """(argv to run, extra environment, limits, refusal or None)."""
        limits = {k: cls[k] for k in ("max_rss_mb", "max_temp_c", "max_disk_mb") if cls.get(k)}
        if kind == "claude":
            have = self.claude_flags(self.s.claude_command)
            names = [registry.tool_name(t) for t in cls["allowed_tools"]]
            if "--tools" not in have and "--disallowedTools" not in have and "--disallowed-tools" not in have:
                return ([], None, limits, "the installed Claude cannot restrict its tools (no --tools or --disallowedTools), so a Claude job is not run: it could start helper agents or use tools its class did not name")
            argv = [self.s.claude_command, "-p", prompt, "--output-format", "json", "--max-turns", str(cls.get("max_turns", 10)), "--allowedTools", ",".join(cls["allowed_tools"])]
            if "--tools" in have:
                argv += ["--tools", ",".join(dict.fromkeys(names))]                  # exactly the named tools exist for this job: no helper agent, nothing else
            if "--disallowedTools" in have or "--disallowed-tools" in have:
                argv += ["--disallowedTools", ",".join(registry.HELPER_TOOLS)]       # and the helper-starting tools are named as forbidden, whatever the release does with --tools
            return (argv, None, limits, None)
        run_argv, extra, prep_fail = argv, cls.get("env"), None
        if cls.get("tools") and kind == "command":
            real, prep_fail = self.tool_folders(cls["tools"])
            if real:
                extra = {**(extra or {}), "PATH": ":".join(real + ([extra["PATH"]] if extra and extra.get("PATH") else []))}
        if kind == "profile":
            rules = registry.profiling_rules(reg)
            limits = profiling.profile_limits(rules, self.mem_total())
            search, fetched_path = SAFE_PATH, []
            if meta.get("packages"):
                self.io.publish({**res, "status": "running", "note": "approved; fetching packages on the worker"})
                try:
                    rt = self.pkgs.prepare(jid, meta["packages"], on_step=lambda text: self._p("fetching", jid).write_text(json.dumps({"step": text})))
                    fetched_path, search = rt["path"], ":".join(rt["path"] + [SAFE_PATH])
                    extra = {"PATH": ":".join(rt["path"]), **rt["vars"]}
                except PrepError as e:
                    prep_fail = f"package preparation failed: {e}"
                finally:
                    self._p("fetching", jid).unlink(missing_ok=True)
            # run the very binary that was validated (the system path plus the throwaway folder), not whatever the worker's PATH says
            found = shutil.which(argv[0], path=search) if "/" not in argv[0] else argv[0]
            if not found and not prep_fail:
                prep_fail = f"{argv[0]!r} was not found, even after fetching the packages." + missing_hint(fetched_path)
            run_argv = [found or argv[0]] + argv[1:]
        return run_argv, extra, limits, prep_fail

    def _run_privileged(self, jid, cls, reg):
        """Ask the root helper to run the class's action. The helper decides: it is given the exact job file and the signed approval and believes nothing else.
        Returns the same tuple a runner does."""
        if self.privileged_call is None:
            return "error", None, "", {}, "this worker has no root helper configured, so a root-level job cannot run"
        raw = self._p("inbox", f"{jid}.json").read_bytes()
        approval = (self.io.approval_bytes(jid) or b"").decode("utf-8", "replace")
        timeout = int(((reg.get("actions") or {}).get(cls.get("action")) or {}).get("timeout_s", 30)) + 30
        ans = self.privileged_call(raw, approval, timeout)
        status = {"ok": "ok", "timeout": "timeout"}.get(ans.get("status"), "error")
        self.log(f"root helper answered {jid}: {ans.get('status')}" + (f" ({ans.get('note')})" if ans.get("note") else ""))
        return status, ans.get("exit_code"), ans.get("output") or "", {}, ans.get("note") or ""

    def _execute(self, jid, job, cls, kind, argv, prompt, files, meta, reg, res):
        wd = self._p("work", jid)
        wd.mkdir(parents=True, exist_ok=True)
        res["started"] = iso(self.clock())
        res["created"] = job.get("created")                 # when the client sent it, so the result can say how long the whole round trip took
        try:                                                # when this worker first saw it (the inbox copy is written then): splits "waiting" into noticing and starting
            res["seen"] = iso(datetime.fromtimestamp(self._p("inbox", f"{jid}.json").stat().st_mtime, timezone.utc))
        except OSError:
            pass
        base = wd.resolve()
        for name, content in files.items():
            target = wd / name
            if not target.resolve().is_relative_to(base):                       # the second defence, on the path actually written: the name check (jobs.file_name_problem) is the first
                raise refusals.refuse("files.name.path", name=name, reason="it would be written outside the job's folder")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        returns_patch = kind == "claude" and cls.get("returns") == "patch"
        before, before_notes = patch_mod.snapshot(wd) if returns_patch else ({}, [])      # what the folder held when the job began: the patch is the difference from this
        self.io.publish({**res, "status": "running", "note": f"approved; running on {self.s.product} now"})   # so the client can tell it was picked up
        if kind == "claude" and files:                    # Claude is not told by the files themselves that they exist: "this file" would have nothing to point at
            prompt = f"{prompt}\n\n[Attached files, in the current directory: {', '.join(sorted(files))}. Use the Read tool to look at them.]"
        if kind == "privileged":
            st, rc, out, metrics, note = self._run_privileged(jid, cls, reg)
        else:
            run_argv, extra, limits, prep_fail = self._command_for(jid, cls, kind, argv, prompt, meta, reg, res)
            if prep_fail:
                st, rc, out, metrics, note = "error", None, "", {}, prep_fail
            else:
                st, rc, out, metrics, note = self.runner(run_argv, self.time_limit(cls, job, reg), wd, home=self.home, claude=(kind == "claude"),
                                                         marker=self._p("running", jid), limits=limits, network=class_network(cls), extra_env=extra,
                                                         api_key=self.api_key, oauth_token=self.oauth_token, sensors=self.sensors, require_netns=self.s.require_netns, jail=self.jail)
        if kind == "claude":
            self.ledger.finish(jid, job.get("class"), (metrics or {}).get("claude"), st)
        patch_text = ""
        if returns_patch and st == "ok":
            after, after_notes = patch_mod.snapshot(wd)
            patch_text, changed, patch_note = patch_mod.make(before, after, int(cls.get("patch_max_kb", 200)) * 1000)
            res["patch"] = {"files": changed, "text": patch_text, "notes": sorted(set(before_notes + after_notes + ([patch_note] if patch_note else [])))}
        shaped, shape_note = results.shape_output(cls, out, self.limits["output_max_chars"])
        summarised = st == "error" and kind != "claude" and not cls.get("output_keys") and bool(out)
        if summarised:
            self.keeper.keep(jid, out)                       # a failed command: send back only its error messages, not the stack traces
            shaped = results.failure_summary(out, where=f"on {self.s.product}")
            shape_note = "; ".join(x for x in (shape_note, f"only the error lines were sent; the full output is on {self.s.product}") if x)
        res.update(status=st, exit_code=rc, output=guard.scrub(shaped, self.limits["output_max_chars"]), metrics=metrics)
        if note or shape_note:
            res["note"] = "; ".join(x for x in (note, shape_note) if x)
        allow, tokens = set(cls.get("output_allow") or []), guard.canary_tokens(self.state)
        scan = lambda text: guard.scan(text, allow=allow, hostname=self.hostname, canary_tokens=tokens)   # noqa: E731
        hits = scan(shaped if summarised or cls.get("output_keys") else out)       # what is sent is what the guard judges
        if patch_text:
            hits = sorted(set(hits) | set(scan(patch_text)))                        # the patch is published too, so the guard reads it like any output
        if summarised and "canary" not in hits and any(t and t in (out or "") for t in tokens):
            hits.append("canary")                            # a decoy credential in the full output is a breach even if it was not going to be sent
        if hits:
            self.trip(res, hits)
        if kind == "profile":
            rules = registry.profiling_rules(reg)
            res["profile"] = {"argv": argv, **meta, "files": sorted(files),
                              **profiling.profile_verdict(st, rc, note, metrics, run_argv, files, rules, self.s.product)}
            shutil.rmtree(wd, ignore_errors=True)            # a profiled command may leave a lot behind
            if meta.get("packages"):
                pv, env_dir = res["profile"], self.pkgs.env_root / jid
                if pv["verdict"] == "fits":
                    pv["entry"]["run"] = [self.pkgs.promote_path(x, env_dir) if i == 0 else x for i, x in enumerate(pv["entry"]["run"])]
                    pv["entry"]["packages"] = self.pkgs.resolve(env_dir, meta["packages"])      # pinned to what was just fetched
                    if any(p.startswith("pip:") for p in meta["packages"]):
                        pv["entry"]["env"] = {"PYTHONPATH": str(self.s.tools_root / "pip")}
                    pv["packages_note"] = (f"the packages are in a throwaway folder on {self.s.product} (deleted after {plural(PKG_KEEP_DAYS, 'day')}); "
                                           "applying the registry entry installs them for real")
                else:
                    self.pkgs.drop_env(jid)
                    pv["packages_note"] = "the throwaway folder with the packages was deleted; nothing was installed"

    # ---- watchdog, kill, cancel ----
    @staticmethod
    def _marker(m):
        try:
            return json.loads(m.read_text())
        except Exception:
            return {}

    def kill_job(self, jid, hard=False):
        """Stop a running job: SIGTERM lets it publish a 'killed' result; hard also SIGKILLs the whole tree."""
        m = self._p("running", jid)
        info = self._marker(m)
        if not info or not self.is_job_process(info.get("pid"), jid):
            return False
        if hard:
            if info.get("child_pgid"):
                kill_group(info["child_pgid"], plat.KILL)
            kill_group(info["pid"], plat.KILL)
            info["watchdog"] = "hard-killed"
            m.write_text(json.dumps(info))
        else:
            plat.send_signal(info["pid"], plat.TERM)
        return True

    def reap(self):
        """Watchdog and cleanup. A job past its deadline is asked to stop, then hard-killed; a job whose process vanished without a result
        (crash, reboot) gets an error result."""
        for m in sorted(self._p("running").glob("*")):
            jid, info = m.name, self._marker(m)
            if info and self.is_job_process(info.get("pid"), jid):
                if time.time() > info.get("deadline", float("inf")):
                    if not info.get("term_sent"):
                        self.log(f"watchdog: {jid} exceeded its time limit; asking it to stop")
                        info["term_sent"] = time.time()
                        m.write_text(json.dumps(info))
                        plat.send_signal(info["pid"], plat.TERM)
                    elif time.time() > info["term_sent"] + 30:
                        self.log(f"watchdog: {jid} ignored SIGTERM; killing it")
                        self.kill_job(jid, hard=True)
                continue
            if self._p("done", jid).exists() or self._p("finished", f"{jid}.json").exists():
                m.unlink(missing_ok=True)
                continue
            why = "killed by the watchdog (exceeded its time limit)" if info.get("watchdog") else "job process ended without a result (crash or reboot)"
            res = {"id": jid, "class": None, "status": "killed" if info.get("watchdog") else "error", "started": info.get("started"),
                   "finished": iso(self.clock()), "exit_code": None, "output": "", "note": why}
            self._p("finished", f"{jid}.json").write_text(json.dumps(res))
            m.unlink(missing_ok=True)
            self._p("queued", f"{jid}.json").unlink(missing_ok=True)
            self._p("queued", f"{jid}.meta").unlink(missing_ok=True)

    def cancel(self, jid):
        """Cancel a job that has not started: one waiting in the queue, or one still waiting for approval. Returns (ok, why)."""
        if not jobs.ID_RE.match(jid or ""):
            return False, "that is not a job id"
        snap = self._p("queued", f"{jid}.json")
        prior = self.io.published_result(jid) or {}
        waiting_for_approval = (prior.get("status") == "awaiting_approval" and not snap.exists()
                                and not self._p("running", jid).exists() and not self._p("done", jid).exists())
        if self._p("running", jid).exists() or not (snap.exists() or waiting_for_approval):
            return False, "it is already running, finished, or unknown"
        snap.unlink(missing_ok=True)
        self._p("queued", f"{jid}.meta").unlink(missing_ok=True)
        self._p("approved", jid).unlink(missing_ok=True)         # an approval of a cancelled job must not survive
        res = {"id": jid, "class": prior.get("class"), "status": "cancelled", "started": None, "finished": iso(self.clock()), "exit_code": None,
               "output": "", "note": "cancelled before it was approved" if waiting_for_approval else f"cancelled on {self.s.product} before it started"}
        self._p("finished", f"{jid}.json").write_text(json.dumps(res))
        if self.io.publish(res):
            self._p("done", jid).touch()
            self._p("finished", f"{jid}.json").unlink(missing_ok=True)
        return True, ""

    def publish_pending(self):
        for f in sorted(self._p("finished").glob("*.json")):
            res = json.loads(f.read_text())
            if self._p("running", res["id"]).exists():
                continue
            ok = self.io.publish(res)
            publishdiag.record(self.state, ok, getattr(self.io, "last_push_error", "") if not ok else "")
            if ok:
                self._p("done", res["id"]).touch()
                f.unlink(missing_ok=True)

    # ---- the passes ----
    def step(self):
        """Start queued jobs, oldest first, while capacity and machine health allow. Returns the ids started."""
        started = []
        with FileLock(self._p("step.lock"), wait=0) as lk:
            if not lk.got:
                return started
            self.reap()
            if self.lock.locked():
                self.lock.remind_if_due()
                return started
            queued = sorted(self._p("queued").glob("*.json"), key=lambda p: p.stat().st_mtime)
            if not queued:
                return started
            reg = self.load_registry()
            markers = {m.name: self._marker(m) for m in self._p("running").glob("*")}
            running = {n: mk.get("weight", 2) for n, mk in markers.items()}
            used = sum(running.values())
            for p in queued:
                jid = p.stem
                if jid in running:
                    continue
                try:
                    job = json.loads(self._p("queued", f"{jid}.meta").read_text())              # the small sidecar written when the job was queued
                except (OSError, ValueError):
                    try:
                        job = json.loads(p.read_bytes())                                         # a job queued before sidecars existed
                    except ValueError:
                        job = {}
                cls = (reg.get("classes") or {}).get(job.get("class")) or {}
                w = min(self.s.capacity, registry.COST_W.get(cls.get("cost", "medium"), 2))
                if used + w > self.s.capacity:
                    self.log(f"waiting: {jid} needs {w}, {self.s.capacity - used} of {self.s.capacity} free")
                    break                                    # strict FIFO so a big job cannot be starved by a stream of small ones
                cap = cls.get("max_running") or (1 if cls.get("kind") == "claude" else None)     # a Claude class runs one job at a time unless it says otherwise: usage is spent one job at a time
                if cap and sum(1 for mk in markers.values() if mk.get("class") == job.get("class")) >= cap:
                    self.log(f"waiting: {jid}: {cap} {plural(job.get('class'), 'job')} already running")
                    continue
                if cls.get("kind") == "claude":
                    spent = self.ledger.exceeded()
                    if spent:
                        self.log(f"waiting: {jid}: {spent}")
                        self._announce_pause(spent)
                        continue                                 # a pause for Claude jobs only: everything else behind it still runs
                why = self.busy_reason()
                if why:
                    self.log(f"waiting: {jid}: {why}")
                    break
                pid = self._spawn(jid)
                if cls.get("kind") == "claude":
                    self.ledger.start(jid, job.get("class"))
                limit = min(self.time_limit(cls, job, reg), MAX_TIMEOUT) + (self.s.pkg_timeout if job.get("packages") else 0)
                markers[jid] = {"pid": pid, "weight": w, "class": job.get("class"), "started": iso(self.clock()), "deadline": time.time() + limit + self.s.grace_s}
                self._p("running", jid).write_text(json.dumps(markers[jid]))
                used += w
                started.append(jid)
                self.log(f"started {jid} (weight {w}, {used}/{self.s.capacity} in use)")
        return started

    def _announce_pause(self, why):
        """Say once per UTC day that new Claude jobs are paused (a quiet notice, not an alarm: nothing is wrong)."""
        mark = self._p("ledger-paused")
        day = self.ledger.day()
        try:
            if mark.read_text().strip() == day:
                return
        except OSError:
            pass
        mark.write_text(day)
        try:
            self.lock.notifier.send(f"{self.s.product} PAUSED CLAUDE JOBS", f"{why}. They start again tomorrow or when the limit is raised.",
                                    fields=[("State", "new Claude jobs are paused; other jobs still run"), ("Why", why), ("Next", "ct-admin status shows today's use; raise claude.jobs_per_day or turns_per_day to continue now")],
                                    tags="hourglass", priority="default")
        except Exception as e:
            self.log(f"pause notice: {e}")

    def execute(self, jid):
        """The exec pass: run one queued job in this process. A stop signal is turned into a 'killed' result by the runner."""
        jobs.check_id(jid)
        self.process(self._p("queued", f"{jid}.json"), self.load_registry(), run=True)

    def clean_work(self, days=3):
        cutoff = time.time() - days * 86400
        for old in self._p("inbox").glob("*.json"):          # finished jobs' snapshots; unfinished ones are kept
            try:
                if old.stat().st_mtime < cutoff and self._p("done", old.stem).exists():
                    old.unlink()
            except OSError:
                pass
        self.pkgs.prune_old_envs()
        self.keeper.prune()
        for d in self._p("work").iterdir():
            try:
                if d.stat().st_mtime < cutoff and not self._p("running", d.name).exists():
                    shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink()
            except OSError:
                pass

    def orphan_inbox(self, listing):
        """Jobs seen once and still to deal with whose files have since left the queue repo (the jobs branch is squashed now and then):
        their own first-seen copy is what gets approved and run."""
        return [f for f in sorted(self._p("inbox").glob("*.json"))
                if f.stem not in listing and jobs.ID_RE.match(f.stem) and not self._p("done", f.stem).exists()]

    def publish_registry_info(self, reg):
        """Publish what is INSTALLED (classes, limits, RAM) as registry.json whenever the installed registry changes, so a client can read
        what the worker can run today."""
        try:
            raw = self.s.registry_path.read_bytes()
        except OSError:
            return False
        digest, total = hashlib.sha256(raw).hexdigest(), self.mem_total()
        isolated = self.jail.ready()
        try:
            approvers = approvals.fingerprints(Path(self.approvers_file).read_text()) if self.approvers_file else []
        except OSError:
            approvers = []
        stamp, mark = f"{digest}:{total}:{isolated}:{self.jail.name}:{','.join(approvers)}:{self.job_ceiling_mb() // 64}:{self.s.transport}:{self.s.isolation_purpose}:{self.speed()}", self._p("registry.sha")      # (the ceiling moves with free disk: a coarse step, so it is republished when it really changes)
        if mark.exists() and mark.read_text() == stamp:
            return False
        classes = {}
        for name, c in (reg.get("classes") or {}).items():
            kind = c.get("kind", "command")
            act = (reg.get("actions") or {}).get(c.get("action")) if kind == "privileged" else None
            if act:                                                # what a person must be able to read before approving something that runs as root
                c = {**c, "run": act.get("run"), "params": act.get("params"), "privileged": True, "writable": act.get("writable"), "network": bool(act.get("network")),
                     "timeout_s": act.get("timeout_s", 30)}
            classes[name] = {k: v for k, v in {
                "privileged": c.get("privileged"), "writable": c.get("writable"),
                "kind": kind, "cost": c.get("cost", "medium"), "tier": "approve" if kind in ("profile", "privileged") else c.get("tier", "approve"),
                "run": c.get("run"), "params": c.get("params"), "accepts_files": c.get("accepts_files"), "batchable": c.get("batchable"), "submit": c.get("submit"),
                "packages": c.get("packages"), "tools": c.get("tools"), "timeout_s": c.get("timeout_s") or c.get("max_timeout_s"), "max_rss_mb": c.get("max_rss_mb"),
                "max_temp_c": c.get("max_temp_c"), "max_disk_mb": c.get("max_disk_mb"), "network": class_network(c)}.items() if v is not None}
        when, since_file = iso(self.clock()), self._p("registry.since")
        try:
            known = json.loads(since_file.read_text())
        except (OSError, ValueError):
            known = {}
        for name in classes:                                      # when each class first appeared on this worker (a class that was already here when this was added gets that day)
            classes[name]["installed"] = known.get(name) or when
        info = {"updated": when, "registry_sha256": digest, "mem_total_mb": total, "capacity": self.s.capacity, "classes": classes,
                "profiling": registry.profiling_rules(reg), "limits": self.effective_limits(reg), "job_network_isolation": isolated, "job_jail": self.jail.name,
                "profile_time": {"ceiling_s": int((reg.get("classes") or {}).get("profile-run", {}).get("max_timeout_s", 1800)) if self.shared() else None,
                                 "factor": self.speed()[0], "basis": self.speed()[1], "shared": self.shared()},       # what a profile run's time limit is built from (the client checks a request against it)
                "approvers": approvers}              # fingerprints of the keys whose signed approvals this worker accepts
        if self.io.publish_file("registry.json", json.dumps(info, indent=2) + "\n", "registry info"):
            mark.write_text(stamp)
            since_file.write_text(json.dumps({n: c["installed"] for n, c in classes.items()}))
            return True
        return False

    def publish_worker_state(self, reg):
        """Publish worker-state.json (see workerstate): when the environment changed, and otherwise at most every half hour so the observed readings stay recent.
        Never breaks a pass."""
        if self.state_facts is None:
            return False
        try:
            env, doc = workerstate.build(snapshot=self.snapshot(), registry=reg, extra={**self.state_facts(), "job_ceiling_mb": self.job_ceiling_mb(), "profile_approval": "none" if self.profile_runs_unapproved() else "required", "claude_budget": self.ledger.summary()}, jail=self.jail, locked=bool(self.lock.locked()), now_iso=iso(self.clock()))
            stamp, key = self._p("worker-state.stamp"), workerstate.digest(env)
            try:
                last = json.loads(stamp.read_text())
            except (OSError, ValueError):
                last = {}
            if last.get("hash") == key and time.time() - last.get("at", 0) < workerstate.HEARTBEAT_S:
                return False
            if self.io.publish_file("worker-state.json", json.dumps(doc, indent=2) + "\n", "worker state"):
                stamp.write_text(json.dumps({"hash": key, "at": time.time()}))
                return True
        except Exception as e:                                # a snapshot that cannot be taken is not worth a failed pass
            self.log(f"worker state: {e}")
        return False

    def maybe_prune_results(self):
        """At most once a day, squash the results history if it has grown past the threshold."""
        stamp = self._p("prune.stamp")
        try:
            if stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
                return None
            stamp.touch()
            msg = self.io.prune_results(self.s.prune_days, self.s.prune_threshold)
            if "no prune needed" not in msg:
                self.log(msg)
            return msg
        except Exception as e:                               # housekeeping must never break a pass
            self.log(f"prune error: {e}")

    # ---- smart sleep: a worker with nothing to do stops asking GitHub every half minute ----
    def has_work(self):
        """True while a job is queued, running, or still waiting (for approval, say): such a worker keeps checking at the normal pace."""
        if any(self._p("queued").glob("*.json")) or any(self._p("running").glob("*")):
            return True
        return any(not self._p("done", p.stem).exists() for p in self._p("inbox").glob("*.json"))

    def idle_skip(self):
        """Should this pass do nothing because the worker has been idle? It still runs when work exists, when idle_fetch_s is 0, or when it is time for the slow check.
        Any activity ends the idleness at once."""
        s = self.s
        if not s.idle_fetch_s:
            self.idle_now, self.mode = False, "active"
            return False
        activity, last = self._p("activity.stamp"), self._p("last-sync.stamp")
        now = time.time()
        if self.has_work() or self.lock.locked():
            activity.touch()
            self.idle_now, self.mode = False, "active"
            return False
        try:
            idle_for = now - activity.stat().st_mtime
        except OSError:
            activity.touch()
            self.idle_now, self.mode = False, "active"
            return False
        self.idle_now = idle_for >= s.idle_after_s
        if not self.idle_now:
            self.mode = "active"
            return False
        asleep = bool(s.sleep_fetch_s) and idle_for >= s.sleep_after_s and s.sleep_fetch_s > s.idle_fetch_s
        self.mode = "sleep" if asleep else "idle"
        try:
            return now - last.stat().st_mtime < (s.sleep_fetch_s if asleep else s.idle_fetch_s)
        except OSError:
            return False

    def fetch(self):
        """The fetch pass. Returns the ids that were looked at."""
        if self.idle_skip():
            return []
        self._p("last-sync.stamp").touch()
        self.io.sync()
        try:
            self.apply_controls()                                      # before the lockdown check: a signed unlock must work while locked
        except Exception as e:                                         # an order that cannot be read must never break a pass
            self.log(f"signed orders: {e}")
        self.publish_pending()
        reg = self.load_registry()
        self.publish_registry_info(reg)
        self.publish_worker_state(reg)
        self.clean_work()
        guard.plant_canaries(self.state, self.home, identity=self.s.identity)
        seen = []
        if self.lock.locked():
            self.log("locked down after a security event: not processing jobs")
            return seen
        listing = self.io.job_listing()
        for jid, (ref, name) in sorted(listing.items()):
            if not jobs.ID_RE.match(jid):
                self.log(f"ignoring job file with a bad name: {name!r}")
                continue
            if self._p("done", jid).exists():
                continue
            inbox = self._p("inbox", f"{jid}.json")
            if not inbox.exists():                           # first sight: snapshot the exact bytes; later edits to the repo copy are ignored
                size, ceiling = self._blob_size(ref, name), self.job_ceiling_mb()
                if size is not None and size > ceiling * 1_000_000:       # too big for what this worker can hold: said, and never read
                    self._reject_unread(jid, size, ceiling)
                    seen.append(jid)
                    continue
                raw = self.io.git_bytes(self.io.queue, "show", f"{ref}:{name}")
                if raw is None:
                    continue
                inbox.write_bytes(raw)
            self.process(inbox, reg)
            seen.append(jid)
        for inbox in self.orphan_inbox(listing):
            self.process(inbox, reg)
            seen.append(inbox.stem)
        if seen and any(not self._p("done", j).exists() for j in seen) or self.has_work():
            self._p("activity.stamp").touch()             # something new to do: the slow pace ends now
        self.maybe_prune_results()
        self.step()                                          # start new jobs right away instead of waiting for the next step tick
        return seen

