import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from armada import results  # noqa: E402

TRACE = ("." * 50 + "Exception in thread Thread-2 (<lambda>):\nTraceback (most recent call last):\n"
         '  File "/usr/lib/python3.14/urllib/request.py", line 1321, in do_open\n    h.request()\n'
         "OSError: [Errno 101] Network is unreachable\n\nDuring handling of the above exception, another exception occurred:\n\n"
         'Traceback (most recent call last):\n  File "/var/lib/example/state/work/x/t.py", line 37, in <lambda>\n    go()\n'
         "urllib.error.URLError: <urlopen error [Errno 101] Network is unreachable>\nE\nRan 12 tests in 1.2s\n\nFAILED (errors=2)\n")


class Shaping(unittest.TestCase):
    def test_declared_json_keys_only(self):
        cls = {"output_keys": ["ok", "load"]}
        text, note = results.shape_output(cls, json.dumps({"ok": 1, "load": 0.2, "secret": "x"}), 100_000)
        self.assertEqual(json.loads(text), {"ok": 1, "load": 0.2})
        self.assertIn("1 undeclared output field not published", note)
        text, note = results.shape_output(cls, "not json", 100_000)
        self.assertEqual((text, "declared JSON shape" in note), ("", True))
        text, note = results.shape_output(cls, "[1, 2]", 100_000)                              # valid JSON, but not an object
        self.assertEqual(text, "")
        self.assertEqual(results.shape_output(cls, json.dumps({"ok": 1}), 100_000)[1], "")        # nothing dropped: no note

    def test_length_caps(self):
        text, _ = results.shape_output({"output_max_chars": 10}, "x" * 50, 100_000)
        self.assertEqual(text, "x" * 10 + "\n[truncated]")
        text, _ = results.shape_output({}, "x" * 100_001, 100_000)                           # the registry-wide default applies
        self.assertTrue(text.endswith("[truncated]"))
        self.assertEqual(results.shape_output({}, "short", 100_000), ("short", ""))


class FailureSummary(unittest.TestCase):
    def test_only_the_top_level_messages_are_kept_never_the_frames(self):
        out = results.failure_summary(TRACE)
        self.assertEqual(out.split("\n"), ["OSError: [Errno 101] Network is unreachable",
                                           "urllib.error.URLError: <urlopen error [Errno 101] Network is unreachable>",
                                           "Ran 12 tests in 1.2s", "FAILED (errors=2)"])
        for leaked in ("Traceback", "/var/lib/example", "/usr/lib/python", "File "):
            self.assertNotIn(leaked, out)

    def test_build_errors_without_a_traceback(self):
        self.assertEqual(results.failure_summary("compiling...\nld: cannot find -lfoo\nmake: *** [all] Error 1\n"),
                         "ld: cannot find -lfoo\nmake: *** [all] Error 1")

    def test_when_nothing_matches_the_last_lines_are_sent(self):
        self.assertEqual(results.failure_summary("a\nb\nc\nd\ne\nf\ng\n"), "c\nd\ne\nf\ng")
        self.assertEqual(results.failure_summary(""), "")
        self.assertEqual(results.failure_summary(None), "")

    def test_a_long_list_is_cut_and_says_how_many_were_left_out(self):
        text = "\n".join(f"ValueError: problem number {i}" for i in range(12))
        out = results.failure_summary(text, max_lines=8, where="in the kept output")
        self.assertEqual(len(out.splitlines()), 9)
        self.assertTrue(out.endswith("[4 more error lines in the kept output]"))

    def test_lines_are_capped_and_repeats_dropped(self):
        out = results.failure_summary("ValueError: " + "x" * 500 + "\nValueError: same\nValueError: same\n", max_len=40)
        self.assertEqual(out.splitlines()[0], ("ValueError: " + "x" * 500)[:40])
        self.assertEqual(out.count("ValueError: same"), 1)


class Keeping(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.keeper = results.OutputKeeper(self.state, keep_days=30, keep_chars=1000)

    def test_full_output_is_kept_privately_and_capped(self):
        self.keeper.keep("job-keep1", TRACE * 3)
        f = self.state / "output" / "job-keep1.txt"
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.state / "output").stat().st_mode & 0o777), "0o700")
        self.assertEqual(len(f.read_text()), 1000)
        self.assertEqual(self.keeper.read("job-keep1"), (TRACE * 3)[:1000])

    def test_reading_cannot_wander_outside_the_folder(self):
        self.keeper.keep("job-keep1", "x")
        (self.state / "secret.txt").write_text("nope")
        for bad in ("../secret", "job-keep1/../../secret", "", None, "JOB", "a"):
            self.assertIsNone(self.keeper.read(bad), repr(bad))
        self.assertIsNone(self.keeper.read("job-nokept"))

    def test_listing_newest_first_and_pruning_by_age(self):
        self.keeper = results.OutputKeeper(self.state)                                         # default cap: a 2 KB file stays 2 KB
        for jid, age_days in (("job-old1", 40), ("job-new1", 1), ("job-mid1", 10)):
            self.keeper.keep(jid, "x" * 2048)
            t = time.time() - age_days * 86400
            os.utime(self.state / "output" / f"{jid}.txt", (t, t))
        names = [n for n, *_ in self.keeper.listing()]
        self.assertEqual(names, ["job-new1", "job-mid1", "job-old1"])
        self.assertEqual(self.keeper.listing()[0][1], 2)                                      # size in KB
        self.assertEqual(self.keeper.prune(), 1)
        self.assertEqual([n for n, *_ in self.keeper.listing()], ["job-new1", "job-mid1"])

    def test_empty_folder(self):
        self.assertEqual(self.keeper.listing(), [])
        self.assertEqual(self.keeper.prune(), 0)


if __name__ == "__main__":
    unittest.main()
