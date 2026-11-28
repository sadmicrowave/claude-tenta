"""Part 1 of 2 of the test_worker_jobs tests (Guard)."""
import unittest
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))      # shared fixtures, however the tests are started
from armada import guard  # noqa: E402
from support import REGISTRY, IP, Base, sh  # noqa: E402


class Guard(Base):
    def test_a_leak_is_withheld_quarantined_and_locks_the_worker(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["output"]), ("quarantined", ""))
        self.assertNotIn(IP, json.dumps(res))
        self.assertIn("private_ip", res["note"])
        q = self.state / "quarantine" / "job-0001.json"
        self.assertIn(IP, q.read_text())                                                     # the full text stays on the worker
        self.assertEqual(oct(q.stat().st_mode & 0o777), "0o600")
        self.assertTrue(self.lockdown.locked())
        self.assertEqual([r.get_header("Title") for r in self.pushes], ["testct QUARANTINE", "testct LOCKDOWN"])
        self.assertTrue(json.loads(sh("git", "show", "main:security.json", cwd=self.tmp / "results.git"))["locked"])
        self.assertNotIn(IP, "".join(r.data.decode() for r in self.pushes))
        self.assertEqual(self.seclog.verify()[0], True)

    def test_nothing_new_runs_while_locked_until_it_is_cleared(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()
        self.runner.reply = ("ok", 0, "fine", {}, "")
        self.job("job-0002")
        self.run_all()
        self.assertIsNone(self.result("job-0002"))                                           # not even looked at
        self.assertEqual(len(self.runner.calls), 1)
        self.lockdown.clear()
        self.run_all()
        self.assertEqual(self.result("job-0002")["status"], "ok")

    def test_a_class_may_allow_what_it_legitimately_prints(self):
        self.registry_path.write_text(REGISTRY.replace("  keyed:\n", "  netcheck:\n    tier: auto\n    cost: light\n    run: [/bin/echo, x]\n    output_allow: [private_ip]\n  keyed:\n"))
        self.job("job-0001", "netcheck")
        self.runner.reply = ("ok", 0, f"gateway {IP}\n", {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "ok")
        self.assertFalse(self.lockdown.locked())

    def test_the_hostname_rule_uses_the_configured_name(self):
        self.job("job-0001")
        self.runner.reply = ("ok", 0, "running on zzhost today\n", {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "quarantined")
        self.assertIn("hostname", self.result("job-0001")["note"])

    def test_a_decoy_credential_in_output_is_a_breach(self):
        self.w.fetch()                                                                        # plants the decoys
        token = guard.canary_tokens(self.state)[0]
        self.job("job-0001")
        self.runner.reply = ("ok", 0, f"found {token}\n", {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "quarantined")
        self.assertEqual([r.get_header("Title") for r in self.pushes][0], "testct BREACH")

    def test_declared_output_keys_only_are_published(self):
        self.job("job-0001", "keyed")
        self.runner.reply = ("ok", 0, json.dumps({"ok": 1, "secret": "x"}), {}, "")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual(json.loads(res["output"]), {"ok": 1})
        self.assertIn("undeclared", res["note"])

    def test_a_failed_command_sends_only_its_error_lines_and_keeps_the_rest(self):
        self.job("job-0001")
        trace = 'Traceback (most recent call last):\n  File "/srv/x/t.py", line 3, in go\n    boom()\nValueError: bad input\n'
        self.runner.reply = ("error", 1, trace, {}, "")
        self.run_all()
        res = self.result("job-0001")
        self.assertEqual((res["status"], res["output"]), ("error", "ValueError: bad input"))
        self.assertIn("only the error lines were sent", res["note"])
        self.assertIn("/srv/x/t.py", self.keeper.read("job-0001"))

    def test_a_decoy_hidden_in_a_trace_is_caught_even_though_it_was_not_going_to_be_sent(self):
        self.w.fetch()
        token = guard.canary_tokens(self.state)[1]
        self.job("job-0001")
        self.runner.reply = ("error", 1, f'  File "x", line 1\n    token = "{token}"\nValueError: bad\n', {}, "")
        self.run_all()
        self.assertEqual(self.result("job-0001")["status"], "quarantined")


if __name__ == "__main__":
    unittest.main()
