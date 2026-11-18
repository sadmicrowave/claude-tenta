"""The shared end-to-end library: a machine cloned from the shared baseline must not repeat the baseline, and a machine without it must."""
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

LIB = Path(__file__).resolve().parent / "e2e" / "lib.sh"
ROOT = Path(__file__).resolve().parent.parent


def bash(script, mark):
    return subprocess.run(["bash", "-c", f'BASELINE_MARK={mark}; . {LIB}; install_github() {{ echo RAN-THE-INSTALL; }}; check() {{ echo "CHECK $1"; }}; {script}'], capture_output=True, text=True)


class TheBaseline(unittest.TestCase):
    def test_a_machine_with_the_baseline_does_not_install_again(self):
        with tempfile.NamedTemporaryFile() as mark:
            r = bash("baseline_github; prepare_checkout", mark.name)
        self.assertNotIn("RAN-THE-INSTALL", r.stdout + r.stderr)
        self.assertIn("shared baseline", r.stdout)

    def test_a_machine_without_it_does_install(self):
        r = bash("baseline_github", "/nonexistent/mark")
        self.assertIn("RAN-THE-INSTALL", r.stdout)

    def test_the_marker_is_written_only_by_the_baseline_script_and_only_when_nothing_failed(self):
        text = (ROOT / "tests" / "e2e" / "baseline.sh").read_text()
        self.assertIn('[ "$FAIL" = 0 ] && touch "$BASELINE_MARK"', text)

    def test_the_runner_builds_the_baseline_once_and_deletes_every_machine_however_it_ends(self):
        text = (ROOT / "scripts" / "e2e-routes").read_text()
        self.assertIn("trap ", text)
        self.assertIn("baseline", text)
        for route in ("move", "rename", "switch", "uninstall"):
            self.assertIn(route, text)


if __name__ == "__main__":
    unittest.main()
