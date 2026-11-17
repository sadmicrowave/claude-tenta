import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import alerts, guard, util  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.sent, self.logged = [], []
        self.notifier = alerts.Notifier(url="https://ntfy.example/topic", token="", opener=self.sent.append, log=self.logged.append, syslog=False,
                                        product="myinstall")
        self.seclog = guard.SecurityLog(self.state / "security.log")
        self.clock = [util.now()]
        self.lock = alerts.Lockdown(self.state, self.notifier, self.seclog, product="myinstall", clock=lambda: self.clock[0])

    def titles(self):
        return [r.get_header("Title") for r in self.sent]

    def bodies(self):
        return [r.data.decode() for r in self.sent]


class Notify(Fixture):
    def test_a_push_is_multiline_text_with_headers(self):
        self.notifier.send("T", "one line", fields=[("What", "x"), ("Job", "j-1"), ("Class", None)], tags="warning")
        self.assertEqual(self.bodies(), ["What: x\nJob: j-1"])                     # empty fields left out
        r = self.sent[0]
        self.assertEqual((r.get_header("Title"), r.get_header("Priority"), r.get_header("Tags")), ("T", "urgent", "warning"))
        self.assertIsNone(r.get_header("Authorization"))
        self.assertIn("SECURITY ALERT: T: one line", self.logged[0])

    def test_a_token_is_sent_as_bearer_auth(self):
        n = alerts.Notifier(url="https://x.example/t", token="tk_example", opener=self.sent.append, syslog=False)
        n.send("t", "b")
        self.assertEqual(self.sent[0].get_header("Authorization"), "Bearer tk_example")

    def test_only_https_endpoints_are_used_and_failures_never_raise(self):
        for url in ("", "http://insecure.example/t", "ftp://x"):
            n = alerts.Notifier(url=url, opener=self.sent.append, syslog=False)
            self.assertFalse(n.send("t", "b"))
        self.assertEqual(self.sent, [])

        def boom(req):
            raise OSError("down")
        n = alerts.Notifier(url="https://x.example/t", opener=boom, log=self.logged.append, syslog=False)
        self.assertFalse(n.send("t", "b"))
        self.assertTrue(any("alert push failed" in m for m in self.logged))

    def test_non_ascii_titles_are_made_safe_for_a_header(self):
        self.notifier.send("café alert", "b")
        self.assertEqual(self.titles(), ["caf? alert"])


class Engage(Fixture):
    def test_a_quarantine_sends_why_then_a_separate_lockdown_alert(self):
        ev = self.lock.engage("quarantine", "job-0001", {"private_ip"}, cls="leaky")
        self.assertEqual(self.titles(), ["myinstall QUARANTINE", "myinstall LOCKDOWN"])
        quarantine, lockdown = self.bodies()
        self.assertIn("Why: Withheld because it contained a private network (LAN) IP address.", quarantine)
        self.assertIn("Job: job-0001", quarantine)
        self.assertNotIn("leaky", quarantine + lockdown)                              # the class stays in the log
        self.assertNotIn("private_ip", quarantine + lockdown)                         # and so do rule names
        self.assertLessEqual(len(quarantine.splitlines()), 4)
        self.assertIn("State: LOCKED DOWN", lockdown)
        self.assertIn("no new jobs will start", lockdown)
        self.assertIn("ct-admin unlock", lockdown)
        self.assertEqual(self.sent[1].get_header("Tags"), "lock")
        self.assertEqual(ev, {"kind": "quarantine", "job": "job-0001", "class": "leaky", "rules": ["private_ip"]})
        self.assertEqual(self.lock.locked()["job"], "job-0001")
        self.assertEqual(self.seclog.verify(), (True, 1))

    def test_a_second_event_while_locked_does_not_repeat_the_lockdown_alert_or_restart_the_clock(self):
        self.lock.engage("quarantine", "job-0001", {"mac"}, cls="c")
        since = self.lock.locked()["since"]
        self.clock[0] += timedelta(hours=3)
        self.sent.clear()
        self.lock.engage("quarantine", "job-0002", {"token"}, cls="c")
        self.assertEqual(self.titles(), ["myinstall QUARANTINE"])
        self.assertEqual(self.lock.locked()["since"], since)
        self.assertEqual(self.lock.locked()["job"], "job-0002")

    def test_a_decoy_file_read_is_a_breach_with_no_path_on_the_phone(self):
        self.lock.engage("breach", "(decoy file read)", {"canary_access"}, detail="decoy file /home/w/.aws/credentials read by /usr/bin/cat")
        self.assertEqual(self.titles(), ["myinstall BREACH", "myinstall LOCKDOWN"])
        self.assertIn("something read a decoy credential file", self.bodies()[0])
        self.assertNotIn("/home/w", "".join(self.bodies()))
        self.assertIn("/home/w", self.seclog.path.read_text())                        # the detail is in the log
        self.assertIn("decoy", self.lock.locked()["detail"])

    def test_a_leaked_decoy_in_output_is_a_breach_with_the_job(self):
        self.lock.engage("breach", "job-0009", {"canary"}, cls="c")
        self.assertIn("decoy credential was exposed", self.bodies()[0])
        self.assertEqual(self.sent[0].get_header("Tags"), "rotating_light")

    def test_unlock_clears_the_lock_and_the_reminders_and_is_logged(self):
        self.assertIsNone(self.lock.clear())                                            # not locked: nothing to do
        self.lock.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.clock[0] += timedelta(hours=2)
        self.lock.remind_if_due()
        self.assertTrue((self.state / "lockdown-reminders").exists())
        self.assertEqual(self.lock.clear()["job"], "job-0001")
        self.assertIsNone(self.lock.locked())
        self.assertFalse((self.state / "lockdown-reminders").exists())
        self.assertIn('"kind": "unlock"', self.seclog.path.read_text())
        self.assertEqual(self.seclog.verify()[0], True)

    def test_public_status_holds_no_details(self):
        self.assertFalse(self.lock.public_status()["locked"])
        self.lock.engage("breach", "job-0001", {"canary"}, cls="c", detail="secret detail")
        doc = self.lock.public_status()
        self.assertTrue(doc["locked"])
        self.assertNotIn("detail", doc)
        self.assertNotIn("secret", json.dumps(doc))

    def test_an_unreadable_lock_file_still_counts_as_locked(self):
        (self.state / "lockdown").write_text("{ not json")
        self.assertTrue(self.lock.locked())


class Reminders(Fixture):
    def test_schedule(self):
        due = lambda h: alerts.reminders_due(h, [1, 6, 24], 24)   # noqa: E731
        self.assertEqual([due(h) for h in (0.5, 1, 5.9, 6, 23, 24, 47.9, 48, 72, 100)], [0, 1, 1, 2, 2, 3, 3, 4, 5, 6])
        self.assertEqual(alerts.reminders_due(100, [], 24), 0)
        self.assertEqual(alerts.reminders_due(100, [1], 0), 1)

    def test_sent_once_each_at_normal_priority_and_not_before_due(self):
        self.lock.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.sent.clear()
        self.clock[0] += timedelta(minutes=30)
        self.assertFalse(self.lock.remind_if_due())
        self.clock[0] += timedelta(hours=6.5)                                          # 7 h in: two reminders are due
        self.assertTrue(self.lock.remind_if_due())
        self.assertFalse(self.lock.remind_if_due())                                    # the second is NOT sent on the very next pass
        self.assertEqual(len(self.sent), 1)                                            # one per call; marker says 2 sent
        self.assertEqual(self.titles(), ["myinstall STILL LOCKED"])
        self.assertEqual(self.sent[0].get_header("Priority"), "default")
        self.assertIn("still LOCKED DOWN after 7 h", self.bodies()[0])
        self.clock[0] += timedelta(hours=18)                                           # 25 h in: the 24 h reminder
        self.assertTrue(self.lock.remind_if_due())
        self.assertFalse(self.lock.remind_if_due())

    def test_nothing_when_not_locked_or_the_start_time_is_unknown(self):
        self.assertFalse(self.lock.remind_if_due())
        (self.state / "lockdown").write_text(json.dumps({"kind": "quarantine", "job": "x"}))
        self.assertFalse(self.lock.remind_if_due())

    def test_a_new_lockdown_restarts_the_reminders(self):
        self.lock.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.clock[0] += timedelta(hours=2)
        self.assertTrue(self.lock.remind_if_due())
        self.lock.clear()
        self.lock.engage("quarantine", "job-0002", {"mac"}, cls="c")
        self.clock[0] += timedelta(hours=1.5)
        self.assertTrue(self.lock.remind_if_due())                                      # the new lockdown's first reminder is due


if __name__ == "__main__":
    unittest.main()
