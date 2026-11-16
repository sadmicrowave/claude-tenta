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
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import guard, jobs, profiling, registry, results
from .gitio import FileLock
from .jobs import Reject
from .packages import PKG_KEEP_DAYS, PrepError, missing_hint
from .jail import Unshare
from .runner import MAX_TIMEOUT, class_network, kill_group, run_command
from .util import SAFE_PATH, iso, now


@dataclass
class Settings:
    state: Path
    registry_path: Path
    tools_root: Path
    capacity: int = 4
    max_load: float = 3.0
    max_temp_c: float = 75.0
    grace_s: int = 180                      # the watchdog fires this long after a job's own timeout
    max_age_s: int = 86400
    product: str = "the worker"
    identity: str = "cw-worker"             # the name decoy credentials are planted under
    approve_note: str = "approve it: cw-admin approve {jid} (on your computer) or cw-approve {jid} (on the worker)"
    exec_argv: list = field(default_factory=list)       # the command that runs one job: exec_argv + [job id]
    claude_command: str = "claude"
    prune_days: int = 14
    prune_threshold: int = 200
    pkg_timeout: int = 900
    require_netns: bool = True


class Worker:
    def __init__(self, settings, io, lockdown, keeper, packages, *, seclog=None, sensors=None, runner=run_command, log=None, clock=now,
                 home=None, api_key=None, hostname=None, jail=None, is_job_process=None, spawn=None, find_program=None, loadavg=None):
        self.s, self.io, self.lock, self.keeper, self.pkgs = settings, io, lockdown, keeper, packages
        self.sensors = sensors
        self.runner, self.clock = runner, clock
        self.log = log or (lambda msg: None)
        self.home, self.api_key, self.hostname = str(home or Path.home()), api_key, hostname
        self.jail = jail if jail is not None else Unshare()
        self.loadavg = loadavg or (lambda: os.getloadavg()[0])
        self.is_job_process = is_job_process or self._pid_is_job
        self._spawn = spawn or self._default_spawn
        self.find_program = find_program or (lambda name: shutil.which(name, path=SAFE_PATH))
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
        try:
            return jid in Path(f"/proc/{pid}/cmdline").read_text()
        except Exception:
            return False

    def _default_spawn(self, jid):
        # The job outlives the short unit that started it. That unit has a private /tmp which systemd deletes when the unit stops, so a job left running
        # inside it would find no usable temporary folder: it gets one of its own under the state folder instead.
        tmp = self.state / "tmp"
        tmp.mkdir(mode=0o700, exist_ok=True)
        return subprocess.Popen([*self.s.exec_argv, jid], start_new_session=True, stdin=subprocess.DEVNULL, env={**os.environ, "TMPDIR": str(tmp)}).pid

    def load_registry(self):
        reg = registry.parse(self.s.registry_path.read_text())
        self.limits = registry.limits_of(reg)
        self.policy = registry.policy_of(reg)
        return reg

    def mem_total(self):
        return self.sensors.meminfo()[0] if self.sensors else 0

    def snapshot(self):
        total, avail = self.sensors.meminfo() if self.sensors else (0, None)
        try:
            st = os.statvfs(self.state)
            free_gb = round(st.f_bavail * st.f_frsize / 1e9, 1)
        except OSError:
            free_gb = None
        return {"at": iso(self.clock()), "cores": os.cpu_count(), "mem_total_mb": total, "mem_available_mb": avail,
                "load_1m": round(self.loadavg(), 2), "temp_c": self.sensors.temp() if self.sensors else None, "disk_free_gb": free_gb,
                "running": len(list(self._p("running").glob("*"))), "waiting": len(list(self._p("queued").glob("*.json")))}

    def busy_reason(self):
        """Why the worker should not start another job right now (None = fine)."""
        load = self.loadavg()
        if load > self.s.max_load:
            return f"load {load:.2f} > {self.s.max_load}"
        t = self.sensors.temp() if self.sensors else None
        if t is not None and t > self.s.max_temp_c:
            return f"core temp {t:.0f}C > {self.s.max_temp_c:.0f}C"
        return None

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
        self._p("approved", jid).write_text(digest)
        return True, ""

    def _approved(self, jid, digest):
        m = self._p("approved", jid)
        return m.exists() and m.read_text().strip() == digest

    # ---- the guard's reaction ----
    def trip(self, res, rules):
        """A job's output matched the guard: quarantine it locally, withhold it, alert, and lock down."""
        kind = "breach" if "canary" in rules else "quarantine"
        q = self._p("quarantine", f"{res['id']}.json")
        q.write_text(json.dumps(res, indent=2))
        q.chmod(0o600)
        self.lock.engage(kind, res["id"], set(rules), cls=res.get("class"))
        res.update(status="quarantined", output="", note=f"output withheld by the security guard ({', '.join(sorted(rules))}); "
                                                          "the full text is on the worker only (cw-admin quarantine)")
        res.pop("facts", None)

    def publish_security(self):
        st = self.lock.public_status()
        self.io.publish_file("security.json", json.dumps(st, indent=2) + "\n", "security: " + ("LOCKED" if st["locked"] else "clear"))

    # ---- validation (runs at intake and again at exec, on the snapshot) ----
    def _validate(self, jid, raw, reg, res):
        jobs.check_id(jid)
        try:
            job = json.loads(raw)
        except ValueError:
            raise Reject("job file is not valid JSON")
        if not isinstance(job, dict) or job.get("id") != jid:
            raise Reject("id missing or does not match filename")
        cname = job.get("class")
        res["class"] = cname if isinstance(cname, str) else None
        cls = (reg.get("classes") or {}).get(cname)
        if cls is None:
            raise Reject(f"class not in installed registry: {cname!r}")
        jobs.check_age(job, self.s.max_age_s, self.clock)
        params = job.get("params") or {}
        if not isinstance(params, dict):
            raise Reject("params must be an object")
        kind = cls.get("kind", "command")
        argv = prompt = None
        files, meta = {}, {}
        try:
            if kind == "command":
                argv = jobs.build_argv(cls, params, self.policy)
                files = jobs.check_files(job.get("files"), cls, self.limits)
            elif kind == "profile":
                if params:
                    raise ValueError("profile-run takes argv, not params")
                argv, files, meta = jobs.build_profile(job, cls, self.limits, find_program=self.find_program, policy=self.policy)
            elif kind == "claude":
                prompt = job.get("prompt")
                if not isinstance(prompt, str) or not 1 <= len(prompt) <= 8000:
                    raise ValueError("prompt must be 1-8000 characters")
                files = jobs.check_files(job.get("files"), cls, self.limits)
            else:
                raise ValueError(f"unknown kind {kind!r}")
        except ValueError as e:
            raise Reject(str(e))
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
            if (kind == "profile" or cls.get("tier", "approve") != "auto") and not self._approved(jid, digest):
                if not self._p("notified", jid).exists():
                    if kind == "profile":
                        res["facts"] = profiling.command_facts(argv, find_program=self.find_program)
                        rules = registry.profiling_rules(reg)
                        res["glance"] = profiling.glance(job, rules, profiling.profile_limits(rules, self.mem_total()), self.snapshot(),
                                                         jobs.job_timeout(cls, job), res["facts"], self.s.product)
                        if meta.get("packages"):
                            plan = self.pkgs.plan(meta["packages"], jid)
                            if plan["problems"]:
                                raise Reject("; ".join(plan["problems"]))
                            res["package_text"] = self.pkgs.plan_text(plan)
                    res.update(status="awaiting_approval", note=self.s.approve_note.format(jid=jid))
                    self.io.publish(res)
                    self._p("notified", jid).touch()
                return
            if not run:
                self._p("queued", f"{jid}.json").write_bytes(raw)          # the snapshot that will execute is exactly what was validated
                self.log(f"queued {jid} ({cls.get('cost', 'medium')})")
                return
            self._execute(jid, job, cls, kind, argv, prompt, files, meta, reg, res)
        except Reject as e:
            res.update(status="rejected", note=str(e))
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
            self._p("running", jid).unlink(missing_ok=True)

    def _command_for(self, jid, cls, kind, argv, prompt, meta, reg, res):
        """(argv to run, extra environment, limits, refusal or None)."""
        limits = {k: cls[k] for k in ("max_rss_mb", "max_temp_c", "max_disk_mb") if cls.get(k)}
        if kind == "claude":
            return ([self.s.claude_command, "-p", prompt, "--output-format", "json", "--max-turns", str(cls.get("max_turns", 10)),
                     "--allowedTools", ",".join(cls["allowed_tools"])], None, limits, None)
        run_argv, extra, prep_fail = argv, cls.get("env"), None
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

    def _execute(self, jid, job, cls, kind, argv, prompt, files, meta, reg, res):
        wd = self._p("work", jid)
        wd.mkdir(parents=True, exist_ok=True)
        res["started"] = iso(self.clock())
        for name, content in files.items():
            target = wd / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.io.publish({**res, "status": "running", "note": f"approved; running on {self.s.product} now"})   # so the client can tell it was picked up
        run_argv, extra, limits, prep_fail = self._command_for(jid, cls, kind, argv, prompt, meta, reg, res)
        if prep_fail:
            st, rc, out, metrics, note = "error", None, "", {}, prep_fail
        else:
            st, rc, out, metrics, note = self.runner(run_argv, jobs.job_timeout(cls, job), wd, home=self.home, claude=(kind == "claude"),
                                                     marker=self._p("running", jid), limits=limits, network=class_network(cls), extra_env=extra,
                                                     api_key=self.api_key, sensors=self.sensors, require_netns=self.s.require_netns, jail=self.jail)
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
                    pv["packages_note"] = (f"the packages are in a throwaway folder on {self.s.product} (deleted after {PKG_KEEP_DAYS} days); "
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
                kill_group(info["child_pgid"], signal.SIGKILL)
            kill_group(info["pid"], signal.SIGKILL)
            info["watchdog"] = "hard-killed"
            m.write_text(json.dumps(info))
        else:
            os.kill(info["pid"], signal.SIGTERM)
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
                        os.kill(info["pid"], signal.SIGTERM)
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
            if self.io.publish(res):
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
            running = {m.name: self._marker(m).get("weight", 2) for m in self._p("running").glob("*")}
            used = sum(running.values())
            for p in queued:
                jid = p.stem
                if jid in running:
                    continue
                try:
                    job = json.loads(p.read_bytes())
                except ValueError:
                    job = {}
                cls = (reg.get("classes") or {}).get(job.get("class")) or {}
                w = min(self.s.capacity, registry.COST_W.get(cls.get("cost", "medium"), 2))
                if used + w > self.s.capacity:
                    self.log(f"waiting: {jid} needs {w}, {self.s.capacity - used} of {self.s.capacity} free")
                    break                                    # strict FIFO so a big job cannot be starved by a stream of small ones
                why = self.busy_reason()
                if why:
                    self.log(f"waiting: {jid}: {why}")
                    break
                pid = self._spawn(jid)
                limit = min(jobs.job_timeout(cls, job), MAX_TIMEOUT) + (self.s.pkg_timeout if job.get("packages") else 0)
                self._p("running", jid).write_text(json.dumps({"pid": pid, "weight": w, "started": iso(self.clock()), "deadline": time.time() + limit + self.s.grace_s}))
                used += w
                started.append(jid)
                self.log(f"started {jid} (weight {w}, {used}/{self.s.capacity} in use)")
        return started

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
        stamp, mark = f"{digest}:{total}:{isolated}:{self.jail.name}", self._p("registry.sha")
        if mark.exists() and mark.read_text() == stamp:
            return False
        classes = {}
        for name, c in (reg.get("classes") or {}).items():
            kind = c.get("kind", "command")
            classes[name] = {k: v for k, v in {
                "kind": kind, "cost": c.get("cost", "medium"), "tier": "approve" if kind == "profile" else c.get("tier", "approve"),
                "run": c.get("run"), "params": c.get("params"), "accepts_files": c.get("accepts_files"), "batchable": c.get("batchable"),
                "packages": c.get("packages"), "timeout_s": c.get("timeout_s") or c.get("max_timeout_s"), "max_rss_mb": c.get("max_rss_mb"),
                "max_temp_c": c.get("max_temp_c"), "max_disk_mb": c.get("max_disk_mb"), "network": class_network(c)}.items() if v is not None}
        info = {"updated": iso(self.clock()), "registry_sha256": digest, "mem_total_mb": total, "capacity": self.s.capacity, "classes": classes,
                "profiling": registry.profiling_rules(reg), "limits": registry.limits_of(reg), "job_network_isolation": isolated, "job_jail": self.jail.name}
        if self.io.publish_file("registry.json", json.dumps(info, indent=2) + "\n", "registry info"):
            mark.write_text(stamp)
            return True
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

    def fetch(self):
        """The fetch pass. Returns the ids that were looked at."""
        self.io.sync()
        self.publish_pending()
        reg = self.load_registry()
        self.publish_registry_info(reg)
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
                raw = self.io.git_bytes(self.io.queue, "show", f"{ref}:{name}")
                if raw is None:
                    continue
                inbox.write_bytes(raw)
            self.process(inbox, reg)
            seen.append(jid)
        for inbox in self.orphan_inbox(listing):
            self.process(inbox, reg)
            seen.append(inbox.stem)
        self.maybe_prune_results()
        self.step()                                          # start new jobs right away instead of waiting for the next step tick
        return seen

