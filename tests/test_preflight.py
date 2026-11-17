"""The tests refuse to start without what they need, with one message that says what to do."""
import importlib.metadata
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401

import preflight

ROOT = Path(__file__).resolve().parents[1]


class Preflight(unittest.TestCase):
    def test_nothing_is_missing_where_the_tests_are_running(self):
        self.assertEqual(preflight.problems(), [])

    def test_a_missing_package_or_tool_is_named_with_what_to_do(self):
        with mock.patch.object(importlib.metadata, "version", side_effect=importlib.metadata.PackageNotFoundError("x")), mock.patch.object(preflight.shutil, "which", return_value=None):
            found = preflight.problems()
        self.assertIn("PyYAML", " ".join(found))
        self.assertIn("git", " ".join(found))
        with mock.patch.object(preflight, "problems", return_value=["the Python package PyYAML is not installed for /usr/bin/python3"]):
            with self.assertRaises(SystemExit) as e:
                preflight.require()
        self.assertEqual(e.exception.code, 1)

    def test_every_way_of_running_the_tests_checks_first(self):
        self.assertIn("preflight", (ROOT / "scripts" / "t").read_text())
        self.assertIn("preflight", (ROOT / "scripts" / "release").read_text())
        self.assertIn("preflight.require()", (ROOT / "tests" / "support.py").read_text())


if __name__ == "__main__":
    unittest.main()
