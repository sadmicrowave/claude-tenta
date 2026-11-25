import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct import registry  # noqa: E402
from ct.client import cli  # noqa: E402
from ct.client import offload as off  # noqa: E402
from ct.client.cli import adopt_tier as cli_tier  # noqa: E402
from ct.client.repo import ClientError  # noqa: E402
from offload_support import OffloadFixture  # noqa: E402
from support import sh  # noqa: E402

INFO = {"mem_total_mb": 8000, "classes": {"echo": {"run": ["/bin/echo", "{word}"], "cost": "light", "tier": "auto"},
                                          "lint": {"run": ["/usr/bin/eslint", ".", "--max-warnings", "0"], "cost": "medium", "tier": "approve"}}}


class AutoAdopt(OffloadFixture):
    """List item 3b: a profile result that fits is adopted and installed without a further question, by default; the owner's own key signs it, on this computer."""

    def fits(self):
        self.push_main_registry()
        jid = self.run_profile_to_a_result()
        self.w.fetch()
        return jid

    def view(self, jid, tty=True, on=True):
        self.admin_calls.clear()
        self.ctx.is_tty, self.ctx.auto_adopt = (lambda: tty), on
        return self.run_cli("result", jid)

    def test_a_result_that_fits_is_adopted_once_with_a_cautious_tier_only_for_heavy_ones(self):
        jid = self.fits()
        self.view(jid)
        self.assertEqual(self.admin_calls, [["adopt", jid, "--tier", "auto", "--yes", "--worker", "testct"]])
        self.view(jid)
        self.assertEqual(self.admin_calls, [])                                                      # shown again: not adopted again

    def test_a_heavy_result_is_adopted_but_waits_for_approval_each_time(self):
        self.assertEqual(cli_tier({"profile": {"cost": "heavy"}}), "approve")
        self.assertEqual(cli_tier({"profile": {"cost": "medium"}}), "auto")
        self.assertEqual(cli_tier({"profile": {"cost": "light"}}), "auto")

    def test_nothing_is_adopted_in_a_pipe_when_switched_off_or_when_it_does_not_fit(self):
        jid = self.fits()
        self.view(jid, tty=False)
        self.assertEqual(self.admin_calls, [])
        self.view(jid, on=False)
        self.assertEqual(self.admin_calls, [])
        res = {"id": "profile-zzz999", "status": "ok", "profile": {"verdict": "needs-longer", "argv": ["x"], "reasons": [], "limiting": ["time"]}}
        cli.auto_adopt(self.ctx, res)
        res["profile"]["verdict"] = "does-not-fit"
        cli.auto_adopt(self.ctx, res)
        self.assertEqual(self.admin_calls, [])


if __name__ == "__main__":
    unittest.main()
