"""A worker's configuration with Windows paths (docs/design/WINDOWS-WORKER-LOOP.md): drive letters with forward slashes are accepted, system folders are not, and the Linux rules are unchanged."""
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import config

WIN = '[identity]\nname = "claude-tenta"\n[server]\nhost = "192.0.2.10"\nadmin_user = "me"\nworker_user = "ctworker"\n[paths]\ncode = "C:/ProgramData/claude-tenta/release"\nstate_root = "C:/ProgramData/claude-tenta/state"\nconfig = "C:/ProgramData/claude-tenta/config"\nworker_home = "C:/ProgramData/claude-tenta/state"\n[transport]\nkind = "direct"\n'


def load(text):
    d = Path(tempfile.mkdtemp())
    (d / "ct.toml").write_text(text)
    return config.load(d / "ct.toml")


class Paths(unittest.TestCase):
    def test_a_windows_configuration_loads_and_its_folders_are_the_ones_written(self):
        c = load(WIN)
        self.assertEqual(c.paths.state_root, "C:/ProgramData/claude-tenta/state")
        self.assertEqual(c.path("state"), "C:/ProgramData/claude-tenta/state/state")
        self.assertEqual(c.path("queue_clone"), "C:/ProgramData/claude-tenta/state/queue")
        self.assertEqual(c.path("registry"), "C:/ProgramData/claude-tenta/config/registry.yaml")
        self.assertEqual(c.path("config_file"), "C:/ProgramData/claude-tenta/config/ct.toml")

    def test_the_run_folder_and_the_helper_socket_are_under_the_state_folder_not_slash_run(self):
        c = load(WIN)
        self.assertEqual(c.path("run_dir"), "C:/ProgramData/claude-tenta/state/run")
        self.assertTrue(c.path("helper_socket").startswith("C:/ProgramData/claude-tenta/state/"))

    def test_on_linux_the_run_folder_is_still_slash_run(self):
        c = load(WIN.replace("C:/ProgramData/claude-tenta/release", "/opt/claude-tenta").replace("C:/ProgramData/claude-tenta/state", "/var/lib/claude-tenta").replace("C:/ProgramData/claude-tenta/config", "/etc/claude-tenta"))
        self.assertEqual(c.path("run_dir"), "/run/claude-tenta")

    def test_a_system_folder_a_space_a_backslash_or_dotdot_is_refused_with_the_reason(self):
        for bad in ("C:/Windows", "c:/programdata", "D:/Users", "C:/Program Files/x", "C:\\x", "C:/a/../b", "C:/a b"):
            v, why = config._path(bad)
            self.assertIsNone(v, bad)
            self.assertTrue(why, bad)

    def test_a_folder_of_its_own_on_any_drive_is_accepted(self):
        for ok in ("D:/tenta/state", "C:/ProgramData/claude-tenta", "c:/ct"):
            self.assertEqual(config._path(ok)[0], ok)

    def test_the_posix_rules_are_unchanged(self):
        self.assertEqual(config._path("/opt/x")[0], "/opt/x")
        self.assertIsNone(config._path("/opt")[0])
        self.assertIsNone(config._path("/a b")[0])
        self.assertEqual(config._path("~/.ct")[0], "~/.ct")

    def test_code_state_and_config_must_still_be_three_different_folders(self):
        with self.assertRaises(Exception):
            load(WIN.replace('config = "C:/ProgramData/claude-tenta/config"', 'config = "C:/ProgramData/claude-tenta/state"'))


if __name__ == "__main__":
    unittest.main()
