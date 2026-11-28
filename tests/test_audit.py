"""The audit trail (R5): every placement and outcome is kept in an append-only 0600 file, trimmed by age, and outlives placements.json."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401
from armada.client import audit, cli, place, queue, retry
from test_health import LostBase
from test_retry import INFO, iso

NOW = 1_800_000_000


class Ctx:
    def __init__(self, home, keep=90):
        self.home, self.audit_keep_days = Path(home), keep


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ctx = Ctx(self.tmp)


class File(Base):
    def test_a_line_is_appended_with_mode_0600_and_nothing_is_rewritten(self):
        audit.append(self.tmp, "placed", "a-1", NOW, worker="w1", rows=[["w1", "CHOSEN", "idle"]])
        audit.append(self.tmp, "outcome", "a-1", NOW + 5, status="ok")
        f = self.tmp / "audit.jsonl"
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        rows = audit.read(self.tmp)
        self.assertEqual([r["event"] for r in rows], ["placed", "outcome"])
        self.assertEqual(rows[0]["rows"], [["w1", "CHOSEN", "idle"]])

    def test_a_line_cut_short_is_skipped_not_an_error(self):
        audit.append(self.tmp, "placed", "a-1", NOW)
        with open(self.tmp / "audit.jsonl", "a") as fh:
            fh.write('{"at": 5, "event": "pla')
        self.assertEqual([r["job"] for r in audit.read(self.tmp)], ["a-1"])

    def test_lines_older_than_keep_days_are_dropped_by_the_hourly_trim_only(self):
        old = NOW - 100 * 86400
        audit.append(self.tmp, "placed", "old-1", old, keep=90)
        audit.append(self.tmp, "placed", "old-2", old + 60, keep=90)       # less than an hour after the last trim: nothing is trimmed
        audit.append(self.tmp, "placed", "new-1", old + 120, keep=1)       # (a short keep proves it: old-1 is already "too old", yet stays)
        self.assertEqual([r["job"] for r in audit.read(self.tmp)], ["old-1", "old-2", "new-1"])
        audit.append(self.tmp, "placed", "new-2", NOW, keep=90)
        self.assertEqual([r["job"] for r in audit.read(self.tmp)], ["new-2"])

    def test_a_trail_that_cannot_be_written_never_raises(self):
        (self.tmp / "audit.jsonl").mkdir()                              # a directory where the file should be
        audit.append(self.tmp, "placed", "a-1", NOW)

    def test_the_default_keeps_a_line_longer_than_placements_json_does(self):
        self.assertGreater(audit.KEEP_DAYS, 14)


class Placed(Base):
    def test_the_explanation_of_every_worker_is_kept_with_all_its_reasons(self):
        ch = SimpleNamespace(worker="w1", lines=[("w1", "CHOSEN", "idle"), ("w2", "left out", "x; y")], reasons={"w2": [("a.b", "busy"), ("c.d", "silent")]})
        audit.placed(self.ctx, ["j-1", "j-2"], "scan", "w1", ch, "high", now=NOW)
        a, b = audit.read(self.tmp)
        self.assertEqual((a["job"], b["job"], a["worker"], a["priority"], a["class"]), ("j-1", "j-2", "w1", "high", "scan"))
        self.assertEqual(a["rows"], [["w1", "CHOSEN", "idle"], ["w2", "left out", "busy", "silent"]])

    def test_a_named_worker_says_so_instead_of_an_explanation(self):
        audit.placed(self.ctx, ["j-1"], "scan", "w1", None, now=NOW)
        self.assertIn("named", audit.read(self.tmp)[0]["why"])

    def test_it_is_written_to_the_main_home_also_from_another_workers_context(self):
        other = Ctx(self.tmp / "other")
        other.home_main = self.tmp
        audit.placed(other, ["j-1"], "scan", "w1", None, now=NOW)
        self.assertEqual(len(audit.read(self.tmp)), 1)


class Outcomes(Base):
    def test_an_outcome_is_written_once_and_only_for_a_job_that_was_placed(self):
        audit.placed(self.ctx, ["j-1"], "scan", "w1", None, now=NOW)
        res = {"j-1": {"status": "ok", "class": "scan"}, "other": {"status": "ok"}, "j-2": {"status": "running"}}
        self.assertEqual(audit.outcomes(self.ctx, res, NOW + 1), 1)
        self.assertEqual(audit.outcomes(self.ctx, res, NOW + 2), 0)
        self.assertEqual([r["event"] for r in audit.read(self.tmp)], ["placed", "outcome"])

    def test_a_running_or_waiting_result_is_not_an_outcome(self):
        audit.placed(self.ctx, ["j-1"], "scan", "w1", None, now=NOW)
        self.assertEqual(audit.outcomes(self.ctx, {"j-1": {"status": "awaiting_approval"}}), 0)

    def test_any_status_that_is_not_pending_is_an_outcome_even_one_not_seen_before(self):
        audit.placed(self.ctx, ["j-1", "j-2"], "scan", "w1", None, now=NOW)
        self.assertEqual(audit.outcomes(self.ctx, {"j-1": {"status": "refused"}, "j-2": {"status": "queued"}}), 1)

    def test_it_survives_the_fortnight_placements_json_forgets(self):
        audit.placed(self.ctx, ["j-1"], "scan", "w1", None, now=time.time() - 20 * 86400)
        place.record(self.tmp, ["j-2"], "w1", "scan")                    # the record's own prune drops anything older than 14 days
        place._update(self.tmp, lambda d: d.update({"j-1": {"worker": "w1", "at": int(time.time() - 20 * 86400)}}))
        place.record(self.tmp, ["j-3"], "w1", "scan")
        self.assertNotIn("j-1", json.loads((self.tmp / "placements.json").read_text()))
        self.assertEqual(audit.outcomes(self.ctx, {"j-1": {"status": "ok"}}), 1)
        self.assertEqual([r["event"] for r in audit.chain(audit.read(self.tmp), "j-1")], ["placed", "outcome"])


class Chain(unittest.TestCase):
    def test_root_strips_again_and_try_tails_however_many(self):
        for jid in ("scan-1", "scan-1-again", "scan-1-again2", "scan-1-try2", "scan-1-try22", "scan-1-again-try2"):
            self.assertEqual(audit.root(jid), "scan-1")
        self.assertEqual(audit.root("scan-again-x"), "scan-again-x")

    def test_chain_follows_a_job_across_its_new_ids_from_any_of_them(self):
        rows = [{"at": 1, "event": "placed", "job": "a-1"}, {"at": 2, "event": "lost", "job": "a-1", "again": "a-1-again"}, {"at": 3, "event": "placed", "job": "a-1-again"},
                {"at": 4, "event": "placed", "job": "b-1"}]
        for start in ("a-1", "a-1-again"):
            self.assertEqual([r["at"] for r in audit.chain(rows, start)], [1, 2, 3])


class Hooks(LostBase):
    def test_a_lost_job_is_recorded_with_the_id_it_was_sent_again_as(self):
        self.quiet("vmx")
        self.sent("scan-1")
        self.sweep()
        r, = [r for r in audit.read(self.main.home) if r["event"] == "lost"]
        self.assertEqual((r["job"], r["worker"], r["again"]), ("scan-1", "vmx", "scan-1-again"))
        self.assertIn("published nothing", r["why"])

    def test_a_retry_is_recorded_with_the_attempt(self):
        self.sent("scan-1", age=100)
        retry._update(self.main.home, lambda d: d.update({"scan-1": {"attempt": 1, "of": 3, "delay": 120}}))
        d = self.tmp / "r-vmx" / "results"
        d.mkdir(exist_ok=True)
        (d / "scan-1.json").write_text(json.dumps({"status": "killed", "finished": iso(NOW - 500)}))
        retry.sweep(self.main, self.cfgs["mini"], entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], now=NOW)
        r, = [r for r in audit.read(self.main.home) if r["event"] == "retry"]
        self.assertEqual((r["job"], r["next"], r["attempt"], r["of"]), ("scan-1", "scan-1-try2", 2, 3))

    def test_a_job_sent_from_the_waiting_list_records_its_priority_and_the_explanation(self):
        ch = SimpleNamespace(worker="vmx", lines=[("vmx", "CHOSEN", "idle")], reasons={})
        audit.placed(self.main, ["q-1"], "sys-status", "vmx", ch, "high", "no worker had room")
        r, = audit.read(self.main.home)
        self.assertEqual((r["priority"], r["why"]), ("high", "no worker had room"))


class Command(Base):
    def call(self, ctx, **kw):
        shown = []
        a = SimpleNamespace(id=kw.get("id"), n=kw.get("n", 15), json=kw.get("json", False))
        with mock.patch.object(cli, "emit", lambda *x, **k: shown.append((x, k))):
            rc = cli.cmd_audit(ctx, a)
        return rc, shown

    def test_nothing_recorded_is_a_note_and_an_unknown_job_is_an_error(self):
        self.assertEqual(self.call(self.ctx)[0], 0)
        self.assertEqual(self.call(self.ctx, id="nope-1")[0], 1)

    def test_the_list_shows_each_placed_job_newest_first_with_its_outcome(self):
        audit.placed(self.ctx, ["j-1"], "scan", "w1", None, now=NOW)
        audit.placed(self.ctx, ["j-2"], "scan", "w2", None, now=NOW + 5)
        audit.outcomes(self.ctx, {"j-1": {"status": "ok"}}, NOW + 9)
        rc, shown = self.call(self.ctx)
        self.assertEqual(rc, 0)
        jobs = [dict(x for x in s[0][1:] if isinstance(x, tuple))["Job"] for s in shown]
        self.assertEqual(jobs, ["j-2", "j-1"])
        self.assertIn(("Outcome", "ok"), shown[1][0])
        self.assertIn(("Outcome", "no result seen yet"), shown[0][0])

    def test_one_job_shows_its_whole_chain(self):
        audit.placed(self.ctx, ["j-1"], "scan", "w1", None, now=NOW)
        audit.lost(self.ctx, "j-1", "w1", "scan", "went silent", "j-1-again", NOW + 1)
        audit.placed(self.ctx, ["j-1-again"], "scan", "w2", None, now=NOW + 2)
        rc, shown = self.call(self.ctx, id="j-1-again")
        self.assertEqual((rc, len(shown)), (0, 3))


if __name__ == "__main__":
    unittest.main()
