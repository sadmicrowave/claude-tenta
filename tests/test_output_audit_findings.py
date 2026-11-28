"""Smaller findings of the output audit (see also test_narrow_width, test_review_missing, test_admin_pure)."""
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from armada.client import cli, overview  # noqa: E402


class Findings(unittest.TestCase):
    def lost(self, **k):
        err = io.StringIO()
        with redirect_stderr(err):
            cli.emit_lost("sys-status-9xbjq6", "arm2", "sys-status", "arm2 has published nothing for 6 days", None, **k)
        return err.getvalue()

    def test_a_lost_job_card_tells_you_to_run_jobs_except_inside_jobs_itself(self):
        self.assertIn("arm-run jobs", self.lost())
        self.assertNotIn("RUN THIS", self.lost(run=None))
        self.assertNotIn("arm-run jobs`", self.lost(run=None).replace("(its own row is in arm-run jobs)", ""))

    def test_the_load_bar_says_what_the_second_number_counts(self):
        data = {"workers": [{"name": "arm2", "state": "ok", "bad": False, "load": 0.4, "cores": 2, "running": 0, "age": "5 min ago"}],
                "queue": {"depth": {"high": 0, "normal": 0, "low": 0}, "total": 0, "oldest": None}, "running": [], "recent": []}
        self.assertIn("load 0.4 of 2 cores", "\n".join(overview.lines(data, 60, color=False)))
        data["workers"][0]["cores"] = 1
        self.assertIn("load 0.4 of 1 core", "\n".join(overview.lines(data, 60, color=False)))


if __name__ == "__main__":
    unittest.main()
