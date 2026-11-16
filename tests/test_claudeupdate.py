import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from cw import claudeupdate  # noqa: E402


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Updates(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / ".local" / "bin").mkdir(parents=True)

    def install(self, kind):
        if kind == "native":
            real = self.home / ".local" / "share" / "claude" / "versions" / "2.1.325"
        else:
            real = self.home / ".local" / "node_modules" / ".bin" / "claude"
        real.parent.mkdir(parents=True)
        real.write_text("x")
        os.symlink(real, self.home / ".local" / "bin" / "claude")

    def runner(self, versions, update_rc=0):
        calls = []
        seq = list(versions)

        def run(argv):
            calls.append(argv[1:])
            if argv[1:] == ["--version"]:
                return done(0, f"{seq.pop(0)} (Claude Code)\n")
            return done(update_rc, "", "boom" if update_rc else "")
        run.calls = calls
        return run

    def test_a_native_install_is_updated_and_the_new_version_recorded(self):
        self.install("native")
        run = self.runner(["2.1.325", "2.1.400"])
        res = claudeupdate.update(self.home, run=run, clock=lambda: 100)
        self.assertEqual((res["kind"], res["before"], res["after"], res["ok"]), ("native", "2.1.325", "2.1.400", True))
        self.assertIn(["update"], run.calls)

    def test_a_failed_update_is_a_result_not_a_crash(self):
        self.install("native")
        res = claudeupdate.update(self.home, run=self.runner(["2.1.325", "2.1.325"], update_rc=1))
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["note"])

    def test_the_older_release_is_never_pushed_to_a_newer_one(self):
        """npm's latest is a native program that cannot run on the processor that needed the older release, so nothing is installed."""
        self.install("legacy")
        run = self.runner(["2.1.112"])
        res = claudeupdate.update(self.home, run=run)
        self.assertEqual((res["kind"], res["ok"]), ("legacy", False))
        self.assertIn("cannot be updated here", res["note"])
        self.assertEqual(run.calls, [["--version"]])

    def test_a_missing_tool_is_reported(self):
        res = claudeupdate.update(self.home, run=self.runner([]))
        self.assertEqual((res["kind"], res["ok"]), ("missing", False))

    def test_results_round_trip(self):
        d = Path(tempfile.mkdtemp())
        self.assertIsNone(claudeupdate.load(d))
        claudeupdate.save(d, {"ts": 1, "kind": "native"})
        self.assertEqual(claudeupdate.load(d), {"ts": 1, "kind": "native"})


if __name__ == "__main__":
    unittest.main()
