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


class RealMachine(unittest.TestCase):
    def test_the_login_probe_runs_a_command_every_shell_has(self):
        with mock.patch.object(preflight, "_run", return_value=(0, "")) as run:
            preflight._real_problems("u@h")
        self.assertEqual(run.call_args[0][0][-1], "echo ok")   # `true` fails in cmd.exe

    def test_a_key_exchange_warning_is_not_named_as_the_problem(self):
        text = "** WARNING: connection is not using a post-quantum key exchange algorithm.\nu@h: Permission denied (publickey)."
        self.assertIn("Permission denied", preflight._first_line(text))


class Kinds(unittest.TestCase):
    def ok(self, out="", rc=0):
        return mock.Mock(returncode=rc, stdout=out, stderr="")

    def test_probe_needs_a_running_docker_and_names_the_fix(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/bin/x"), mock.patch.object(preflight, "free_gb", return_value=50), \
                mock.patch.object(preflight.subprocess, "run", return_value=self.ok("Cannot connect to the Docker daemon", 1)):
            found = preflight.problems("probe")
        self.assertEqual(len(found), 1)
        self.assertIn("Docker is not running", found[0])
        self.assertIn("Cannot connect", found[0])
        self.assertIn("open -a OrbStack", found[0])

    def test_e2e_needs_orbstack_running_and_room(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/bin/x"), mock.patch.object(preflight, "free_gb", return_value=2), \
                mock.patch.object(preflight.subprocess, "run", return_value=self.ok("arm-e2e\n")):
            found = " ".join(preflight.problems("e2e"))
        self.assertIn("free disk", found)

    def test_real_host_must_answer_with_a_key(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/bin/x"), \
                mock.patch.object(preflight.subprocess, "run", return_value=self.ok("Permission denied (publickey)", 255)):
            found = preflight.problems("real", "mini-host")
        self.assertEqual(len(found), 1)
        self.assertIn("mini-host", found[0])
        self.assertIn("Permission denied", found[0])

    def test_the_docker_failure_shows_the_error_not_the_banner(self):
        real = "Client: Docker Engine - Community\n Version: 29\nfailed to connect to the docker API at unix:///x.sock; daemon running?"
        with mock.patch.object(preflight.shutil, "which", return_value="/bin/x"), mock.patch.object(preflight, "free_gb", return_value=99), \
                mock.patch.object(preflight.subprocess, "run", return_value=self.ok(real, 1)):
            self.assertIn("failed to connect", preflight.problems("probe")[0])

    def test_every_windows_check_script_needs_the_machine_to_answer(self):
        for f in sorted((ROOT / "scripts").glob("win-*-check")):
            self.assertIn('preflight.require("real", argv[0])', f.read_text(), f.name)

    def test_a_healthy_environment_has_no_problems(self):
        with mock.patch.object(preflight.shutil, "which", return_value="/bin/x"), mock.patch.object(preflight, "free_gb", return_value=99), \
                mock.patch.object(preflight.subprocess, "run", return_value=self.ok("arm-e2e\n")):
            for kind, arg in (("probe", None), ("e2e", None), ("real", "h")):
                self.assertEqual(preflight.problems(kind, arg), [], kind)

    def test_every_script_that_starts_a_costly_run_checks_first(self):
        for script, kind in (("probe", "--for probe"), ("e2e-orbstack", "--for e2e"), ("e2e-routes", "--for e2e"), ("e2e-matrix", "--for e2e"), ("e2e-base", "--for e2e")):
            self.assertIn("preflight.py " + kind, (ROOT / "scripts" / script).read_text(), script)
        self.assertIn("preflight.py --for real", (ROOT / "scripts" / "probe").read_text())


if __name__ == "__main__":
    unittest.main()
