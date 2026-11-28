"""canary-watch: run as root by a systemd timer. Raises the alarm if anything READS a decoy credential file.

The worker plants fake credentials that nothing legitimate ever opens (guard.plant_canaries) and lists them in <state>/canary.json. This
module asks the Linux audit system (auditd) to log every read of those files, then looks for new events. A hit means something on the
machine went looking for secrets, so it hands the worker a `canary-alarm`: security log, alert, and lockdown.

It runs as root only because reading the audit log needs it, and does nothing else with that power: the alarm itself is raised by the
unprivileged worker account. It writes a small status file (world-readable) that `status` shows, and its own bookkeeping in a root-only
folder. Needs: auditd (`apt install auditd`).
"""
import json
import os
import pwd
import re
import subprocess
import sys
import time
from pathlib import Path

from . import guard

KEY = "arm-canary"
OPENS = {"open", "openat", "openat2", "open_by_handle_at", "read", "pread64", "readv", "mmap"}     # a real read; loading the rule itself also carries the key
SERIAL_RE = re.compile(r"msg=audit\(([^)]*):(\d+)\)")


def parse_events(text):
    """`ausearch -i` output -> [{serial, when, exe, comm, user, file}] (one per audit event)."""
    events = []
    for block in text.split("----"):
        if KEY not in block:
            continue
        ev = {"serial": None, "when": "", "exe": "", "comm": "", "user": "", "file": "", "call": ""}
        for line in block.splitlines():
            m = SERIAL_RE.search(line)
            if m and ev["serial"] is None:
                ev["when"], ev["serial"] = m.group(1), m.group(2)
            if line.startswith("type=SYSCALL"):
                for k, rx in (("call", r"\bsyscall=(\S+)"), ("exe", r"\bexe=(\S+)"), ("comm", r"\bcomm=(\S+)"), ("user", r"\buid=(\S+)")):
                    mm = re.search(rx, line)
                    ev[k] = mm.group(1) if mm else ev[k]
            elif line.startswith("type=PATH") and not ev["file"] and "nametype=PARENT" not in line:
                mm = re.search(r"\bname=(\S+)", line)
                ev["file"] = mm.group(1) if mm else ""
        if ev["serial"] and ev["call"] in OPENS and ev["file"] not in ("", "(null)"):     # not auditctl adding the rule, not a directory lookup
            events.append(ev)
    return events


def summarize(events):
    ev = events[0]
    more = f" (+{len(events) - 1} more)" if len(events) > 1 else ""
    return f"decoy file {ev['file'] or '?'} read by {ev['exe'] or ev['comm'] or '?'} as {ev['user'] or '?'} at {ev['when']}{more}"[:300]


class CanaryWatch:
    def __init__(self, state, status_file, book_dir, rules_file, worker_user, alarm_argv, *, run=None, clock=time.time, worker_home=None):
        self.state, self.status_file, self.book_dir = Path(state), Path(status_file), Path(book_dir)
        self.rules_file, self.worker_user, self.alarm_argv = Path(rules_file), worker_user, list(alarm_argv)
        self._run = run or (lambda *cmd, **kw: subprocess.run(list(cmd), capture_output=True, text=True, timeout=60, **kw))
        self.clock, self.worker_home = clock, worker_home

    def write_status(self, monitoring, reason=""):
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(json.dumps({"ts": int(self.clock()), "monitoring": monitoring, "reason": reason}))
        self.status_file.chmod(0o644)

    def files(self):
        files = guard.canary_files(self.state)
        if files or not self.worker_home:
            return files
        home = Path(self.worker_home)                                # an older canary.json without a file list
        return [str(home / ".aws" / "credentials"), str(home / ".git-credentials")]

    def ensure_rules(self, files):
        """Make sure auditd is logging reads of every decoy file. Returns (ok, reason)."""
        rule = lambda f: re.compile(rf"-w {re.escape(f)} -p [a-z]*r[a-z]* -k {KEY}")      # noqa: E731
        try:
            listed = self._run("auditctl", "-l").stdout
        except (OSError, subprocess.SubprocessError):
            return False, "auditd is not installed (apt install auditd): decoy files are NOT being watched"
        missing = [f for f in files if not rule(f).search(listed)]
        if not missing:
            return True, ""
        self.rules_file.parent.mkdir(parents=True, exist_ok=True)
        self.rules_file.write_text("".join(f"-w {f} -p r -k {KEY}\n" for f in files))
        if self._run("augenrules", "--load").returncode != 0:
            for f in missing:
                self._run("auditctl", "-w", f, "-p", "r", "-k", KEY)
        listed = self._run("auditctl", "-l").stdout
        if all(rule(f).search(listed) for f in files):
            return True, ""
        return False, "could not load the audit rules (is the audit configuration locked with -e 2?)"

    def alarm(self, text):
        """Hand the alarm to the UNPRIVILEGED worker account: it writes the security log, alerts, and locks down."""
        return self._run("runuser", "-u", self.worker_user, "--", *self.alarm_argv, text)

    def check(self):
        files = self.files()
        if not files:
            self.write_status(False, "no decoy files planted yet (the worker plants them on its first pass)")
            return 0
        ok, reason = self.ensure_rules(files)
        if not ok:
            self.write_status(False, reason)
            print(reason, file=sys.stderr)                                              # output: log (the journal)
            return 0
        self.book_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        last = self.book_dir / "last"
        try:
            book = json.loads(last.read_text())
        except (OSError, ValueError):
            book = {"since": int(self.clock()), "seen": []}
        try:
            r = self._run("ausearch", "-k", KEY, "-i", "-ts", "recent", env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError) as e:
            self.write_status(False, f"ausearch could not run: {e}")
            return 0
        msg = (r.stdout + r.stderr).strip()
        # exit 1 means "no matching events", but also what it returns for a bad argument, so look at what it said
        if r.returncode not in (0, 1) or (r.returncode == 1 and msg and "no matches" not in msg.lower()):
            self.write_status(False, f"ausearch failed: {msg[:150]}")
            print(f"ausearch failed: {msg[:300]}", file=sys.stderr)                      # output: log (the journal)
            return 0
        events = [e for e in parse_events(r.stdout) if e["serial"] not in book["seen"]]
        self.write_status(True)
        if events:
            out = self.alarm(summarize(events))
            print(out.stdout + out.stderr)                                                   # output: log (the journal)
            book["seen"] = (book["seen"] + [e["serial"] for e in events])[-200:]
        book["since"] = int(self.clock()) - 2                      # a small overlap; the seen list de-duplicates
        last.write_text(json.dumps(book))
        return 0


def from_config(cfg, **kw):
    """The real watcher for this install. The alarm command is the worker's own CLI, run as the worker account."""
    name = cfg.identity.name
    alarm = ["env", f"PYTHONPATH={cfg.paths.code}/lib", f"ARM_CONFIG={cfg.paths.config}/armada.toml", "/usr/bin/python3", "-m", "armada.cli", "canary-alarm"]
    return CanaryWatch(cfg.path("state"), Path(cfg.path("run_dir")) / "canary-watch.json", cfg.path("watch_state"), f"/etc/audit/rules.d/60-{name}.rules",
                       cfg.server.worker_user, alarm, worker_home=cfg.paths.worker_home, **kw)


def main(argv=None):
    from . import config
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = config.load()
    if argv == ["status"]:
        f = Path(cfg.path("run_dir")) / "canary-watch.json"
        print(f.read_text() if f.exists() else "no status yet")                      # output: data (the watcher's status file)
        return 0
    try:
        pwd.getpwnam(cfg.server.worker_user)
    except KeyError:
        print(f"canary-watch: the worker account {cfg.server.worker_user!r} does not exist", file=sys.stderr)       # output: log (the journal)
        return 1
    return from_config(cfg).check()


if __name__ == "__main__":
    sys.exit(main())
