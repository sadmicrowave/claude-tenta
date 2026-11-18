"""The environment probes (tests/probes/*.sh, run in a container by scripts/probe) stay well formed. Running them needs Docker and a few seconds each, so that is done by `scripts/probe`, not here."""
import subprocess
import unittest
from pathlib import Path

import support  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


class Probes(unittest.TestCase):
    def test_every_probe_is_valid_shell_says_what_it_proves_and_ends_with_a_count_and_an_exit_status(self):
        probes = sorted((ROOT / "tests" / "probes").glob("*.sh"))
        self.assertTrue(probes)
        for p in probes:
            text = p.read_text()
            self.assertEqual(subprocess.run(["bash", "-n", str(p)], capture_output=True).returncode, 0, p.name)
            self.assertTrue(text.startswith("#!/bin/bash\n# Probe:"), p.name)                          # the first comment says what fact it proves
            self.assertIn('exit "$FAIL"', text, p.name)
            self.assertIn("/src", text, p.name)                                                          # it uses the project's own code, mounted read-only

    def test_the_runner_names_every_probe_and_leaves_nothing_behind(self):
        text = (ROOT / "scripts" / "probe").read_text()
        self.assertIn("docker run --rm", text)
        self.assertIn(":/src:ro", text)


if __name__ == "__main__":
    unittest.main()
