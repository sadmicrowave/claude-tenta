import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from cw import canarywatch, config, guard  # noqa: E402

SAMPLE = """----
type=PROCTITLE msg=audit(11/14/2026 13:20:01.123:456) : proctitle=cat /home/worker/.aws/credentials
type=PATH msg=audit(11/14/2026 13:20:01.123:456) : item=0 name=/home/worker/.aws/credentials inode=1234 dev=08:01 mode=file,600
type=SYSCALL msg=audit(11/14/2026 13:20:01.123:456) : arch=x86_64 syscall=openat success=yes exit=3 pid=2 auid=unset uid=worker comm=cat exe=/usr/bin/cat key=cw-canary
----
type=PATH msg=audit(11/14/2026 13:20:05.000:457) : item=0 name=/home/worker/.git-credentials inode=9
type=SYSCALL msg=audit(11/14/2026 13:20:05.000:457) : syscall=openat success=yes uid=worker comm=python3 exe=/usr/bin/python3 key=cw-canary
"""


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Parse(unittest.TestCase):
    def test_events_are_extracted(self):
        ev = canarywatch.parse_events(SAMPLE)
        self.assertEqual([e["serial"] for e in ev], ["456", "457"])
        self.assertEqual((ev[0]["file"], ev[0]["exe"], ev[0]["user"]), ("/home/worker/.aws/credentials", "/usr/bin/cat", "worker"))
        s = canarywatch.summarize(ev)
        self.assertIn("read by /usr/bin/cat", s)
        self.assertIn("(+1 more)", s)
        self.assertLessEqual(len(s), 300)

    def test_loading_the_rule_is_not_a_read(self):
        """The first real install tripped the alarm on its own auditctl: those records carry the key but are not opens of a decoy."""
        loading = (
            "----\n"
            "type=PATH msg=audit(11/16/2026 14:46:31.725:26404) : item=0 name=/srv/cwworker/ inode=1 nametype=PARENT\n"
            "type=SYSCALL msg=audit(11/16/2026 14:46:31.725:26404) : arch=x86_64 syscall=sendto success=yes uid=root exe=/usr/sbin/auditctl key=cw-canary\n"
            "----\n"
            "type=SYSCALL msg=audit(11/16/2026 14:46:31.718:26400) : arch=x86_64 syscall=sendto success=yes uid=root exe=/usr/sbin/auditctl key=cw-canary\n")
        self.assertEqual(canarywatch.parse_events(loading), [])

    def test_unrelated_audit_noise_is_ignored(self):
        self.assertEqual(canarywatch.parse_events("----\ntype=SYSCALL msg=audit(1:2) : key=other\n"), [])
        self.assertEqual(canarywatch.parse_events(""), [])


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.decoys = [str(self.tmp / "decoy1"), str(self.tmp / "decoy2")]
        (self.state / "canary.json").write_text(json.dumps({"tokens": ["x"], "files": self.decoys}))
        self.calls, self.rules_loaded, self.ausearch = [], [False], done(1)
        self.clock = [1000.0]

    def fake_run(self, *cmd, **kw):
        self.calls.append(list(cmd))
        name = cmd[0]
        if name == "auditctl" and cmd[1:] == ("-l",):
            return done(0, "".join(f"-w {f} -p r -k cw-canary\n" for f in self.decoys) if self.rules_loaded[0] else "")
        if name == "augenrules":
            self.rules_loaded[0] = True
            return done(0)
        if name == "ausearch":
            return self.ausearch
        if name == "runuser":
            return done(0, "alarm raised\n")
        return done(0)

    def watch(self, **kw):
        return canarywatch.CanaryWatch(self.state, self.tmp / "run" / "status.json", self.tmp / "watch", self.tmp / "audit.rules", "cwworker",
                                       ["env", "X=1", "python3", "-m", "cw.cli", "canary-alarm"], run=kw.pop("run", self.fake_run), clock=lambda: self.clock[0], **kw)

    def status(self):
        return json.loads((self.tmp / "run" / "status.json").read_text())

    def alarms(self):
        return [c for c in self.calls if c[0] == "runuser"]

    def test_it_installs_the_rules_then_stays_quiet_when_nothing_was_read(self):
        w = self.watch()
        w.check()
        self.assertTrue(self.rules_loaded[0])
        self.assertIn("decoy1", (self.tmp / "audit.rules").read_text())
        self.assertEqual(self.status(), {"ts": 1000, "monitoring": True, "reason": ""})
        self.assertEqual(oct((self.tmp / "run" / "status.json").stat().st_mode & 0o777), "0o644")
        self.assertEqual(oct((self.tmp / "watch").stat().st_mode & 0o777), "0o700")                   # its bookkeeping is root-only
        self.assertEqual(self.alarms(), [])

    def test_a_read_raises_the_alarm_once_and_as_the_worker_not_as_root(self):
        self.ausearch = done(0, SAMPLE)
        w = self.watch()
        w.check()
        (alarm,) = self.alarms()
        self.assertEqual(alarm[:4], ["runuser", "-u", "cwworker", "--"])                              # the privileged part does nothing but hand over
        self.assertEqual(alarm[4:10], ["env", "X=1", "python3", "-m", "cw.cli", "canary-alarm"])
        self.assertIn("/home/worker/.aws/credentials", alarm[-1])
        w.check()                                                                                       # the same events again must not re-alarm
        self.assertEqual(len(self.alarms()), 1)

    def test_new_events_after_an_alarm_alarm_again(self):
        self.ausearch = done(0, SAMPLE)
        w = self.watch()
        w.check()
        self.ausearch = done(0, SAMPLE.replace(":456)", ":900)").replace(":457)", ":901)"))
        w.check()
        self.assertEqual(len(self.alarms()), 2)

    def test_the_seen_list_is_bounded(self):
        w = self.watch()
        w.book_dir.mkdir(parents=True, mode=0o700)
        (w.book_dir / "last").write_text(json.dumps({"since": 1, "seen": [f"old{i}" for i in range(500)]}))
        self.ausearch = done(0, SAMPLE)
        w.check()
        self.assertEqual(len(json.loads((w.book_dir / "last").read_text())["seen"]), 200)

    def test_an_ausearch_error_is_not_mistaken_for_no_events(self):
        self.ausearch = done(1, "", "Invalid start date")
        self.watch().check()
        self.assertFalse(self.status()["monitoring"])
        self.assertIn("Invalid start date", self.status()["reason"])
        self.ausearch = done(1, "", "<no matches>")
        self.watch().check()
        self.assertTrue(self.status()["monitoring"])
        self.ausearch = done(2, "", "boom")
        self.watch().check()
        self.assertIn("ausearch failed", self.status()["reason"])

    def test_missing_auditd_is_reported_not_silent(self):
        def no_auditd(*cmd, **kw):
            raise FileNotFoundError("auditctl")
        self.watch(run=no_auditd).check()
        st = self.status()
        self.assertFalse(st["monitoring"])
        self.assertIn("auditd is not installed", st["reason"])
        self.assertEqual(self.alarms(), [])

    def test_rules_that_cannot_be_loaded_are_reported(self):
        def locked(*cmd, **kw):
            return done(0, "") if cmd[:2] == ("auditctl", "-l") else done(1)
        self.watch(run=locked).check()
        self.assertIn("could not load the audit rules", self.status()["reason"])

    def test_the_fallback_to_auditctl_when_augenrules_fails(self):
        loaded = []

        def run(*cmd, **kw):
            if cmd[:2] == ("auditctl", "-l"):
                return done(0, "".join(f"-w {f} -p r -k cw-canary\n" for f in loaded))
            if cmd[0] == "augenrules":
                return done(1)
            if cmd[0] == "auditctl" and cmd[1] == "-w":
                loaded.append(cmd[2])
            return done(1)
        self.watch(run=run).check()
        self.assertEqual(sorted(loaded), sorted(self.decoys))
        self.assertTrue(self.status()["monitoring"])

    def test_no_decoys_yet_is_a_stated_state_not_an_error(self):
        (self.state / "canary.json").unlink()
        self.assertEqual(self.watch().check(), 0)
        self.assertFalse(self.status()["monitoring"])
        self.assertIn("no decoy files planted yet", self.status()["reason"])

    def test_an_older_canary_file_without_a_file_list_falls_back_to_the_workers_home(self):
        (self.state / "canary.json").write_text(json.dumps({"tokens": ["x"]}))
        w = self.watch(worker_home="/home/w")
        self.assertEqual(w.files(), ["/home/w/.aws/credentials", "/home/w/.git-credentials"])
        self.assertEqual(self.watch().files(), [])

    def test_only_absolute_paths_from_the_state_file_are_watched(self):
        (self.state / "canary.json").write_text(json.dumps({"files": ["/ok/path", "relative", 5, "x/y"]}))
        self.assertEqual(self.watch().files(), ["/ok/path"])


class RealPlanting(unittest.TestCase):
    def test_the_watcher_follows_exactly_what_the_worker_planted(self):
        state, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        guard.plant_canaries(state, home)
        w = canarywatch.CanaryWatch(state, state / "s.json", state / "w", state / "r", "u", ["x"], run=lambda *c, **k: done(0))
        self.assertEqual(w.files(), [str(home / ".aws" / "credentials"), str(home / ".git-credentials")])


class FromConfig(unittest.TestCase):
    def test_the_alarm_runs_the_installs_own_cli_with_its_own_paths(self):
        cfg = config.load(None, env={"CW_CONFIG": "", "CW_IDENTITY_NAME": "homecw", "CW_SERVER_WORKER_USER": "runner1"})
        w = canarywatch.from_config(cfg)
        self.assertEqual(w.worker_user, "runner1")
        self.assertEqual(w.alarm_argv, ["env", "PYTHONPATH=/opt/homecw/lib", "CW_CONFIG=/etc/homecw/cw.toml", "/usr/bin/python3", "-m", "cw.cli", "canary-alarm"])
        self.assertEqual((str(w.state), str(w.status_file), str(w.rules_file)), ("/var/lib/homecw/state", "/run/homecw/canary-watch.json", "/etc/audit/rules.d/60-homecw.rules"))
        self.assertEqual(str(w.book_dir), "/var/lib/homecw-watch")


if __name__ == "__main__":
    unittest.main()
