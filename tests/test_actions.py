import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from cw import actions, registry  # noqa: E402

SHA = "a" * 64
GOOD = {"run": ["/usr/local/sbin/fan-profile", "{profile}"], "params": {"profile": {"enum": ["quiet", "balanced", "performance"]}}, "sha256": SHA,
        "writable": ["/etc/mbpfan.conf"]}


def problems(**over):
    return actions.validate({"fan-profile": {**GOOD, **over}})


class Definition(unittest.TestCase):
    def test_a_purpose_built_program_with_a_pin_and_typed_parameters_is_valid(self):
        self.assertEqual(problems(), [])

    def test_a_shell_or_any_program_that_runs_what_it_is_given_is_refused(self):
        for prog in ("/bin/sh", "/usr/bin/bash", "/usr/bin/python3", "/usr/bin/env", "/usr/bin/find", "/usr/bin/curl", "/usr/bin/systemd-run", "/usr/bin/git"):
            self.assertTrue(any("runs whatever it is given" in p for p in problems(run=[prog, "{profile}"])), prog)

    def test_privilege_tools_are_refused_whatever_the_folder(self):
        self.assertTrue(any("refused by policy" in p for p in problems(run=["/usr/bin/sudo", "{profile}"])))
        self.assertTrue(any("policy" in p for p in actions.validate({"x-y": {**GOOD, "run": ["/usr/local/bin/danger"]}}, deny_commands=["danger"])))

    def test_the_program_must_be_a_plain_absolute_path_in_a_system_folder(self):
        for prog in ("fan-profile", "./fan-profile", "/home/" + "me/fan-profile", "/tmp/fan-profile", "/usr/local/sbin/../../tmp/x", "/opt/tools/fan-profile"):
            self.assertTrue(problems(run=[prog, "{profile}"]), prog)

    def test_a_pin_is_required_and_must_be_a_sha256(self):
        self.assertTrue(any("sha256 is required" in p for p in problems(sha256="short")))
        self.assertTrue(any("sha256 is required" in p for p in actions.validate({"fan-profile": {k: v for k, v in GOOD.items() if k != "sha256"}})))

    def test_a_parameter_is_only_ever_a_whole_argument_and_must_be_declared(self):
        self.assertTrue(any("not part of one" in p for p in problems(run=["/usr/local/sbin/fan-profile", "--mode={profile}"])))
        self.assertTrue(any("no such param" in p for p in problems(run=["/usr/local/sbin/fan-profile", "{other}"])))

    def test_writable_names_what_it_writes_not_whole_system_folders(self):
        for bad in ("/", "/etc", "/usr", "/var", "/home", "/sys", "relative/x", "/etc/../usr"):
            self.assertTrue(problems(writable=[bad]), bad)
        self.assertEqual(problems(writable=["/etc/mbpfan.conf", "/var/lib/fan"]), [])

    def test_unknown_settings_and_a_bad_timeout_are_refused(self):
        self.assertTrue(any("unknown setting 'sudo'" in p for p in problems(sudo=True)))
        for t in (0, 601, "30", True):
            self.assertTrue(any("timeout_s" in p for p in problems(timeout_s=t)), t)


class Registry(unittest.TestCase):
    def reg(self, **cls):
        return {"actions": {"fan-profile": dict(GOOD)}, "classes": {"fan": {"kind": "privileged", "action": "fan-profile", "cost": "light", **cls}}}

    def check(self, reg):
        return registry.validate(reg, "/nonexistent-tools")

    def test_a_privileged_class_that_names_an_installed_action_is_valid(self):
        self.assertEqual(self.check(self.reg()), [])

    def test_it_must_name_an_action_that_exists(self):
        reg = self.reg()
        reg["classes"]["fan"]["action"] = "nope"
        self.assertTrue(any("must name one of the registry's actions" in p for p in self.check(reg)))

    def test_it_can_never_skip_approval_or_carry_its_own_command_line(self):
        self.assertTrue(any("always needs approval" in p for p in self.check(self.reg(tier="auto"))))
        for key, val in (("run", ["/bin/true"]), ("params", {}), ("network", True), ("packages", []), ("accepts_files", True)):
            self.assertTrue(any(f"must not set {key}" in p for p in self.check(self.reg(**{key: val}))), key)

    def test_a_bad_action_makes_the_whole_registry_invalid(self):
        reg = self.reg()
        reg["actions"]["fan-profile"]["run"] = ["/bin/sh", "-c", "x"]
        self.assertTrue(any("runs whatever it is given" in p for p in self.check(reg)))


class Executable(unittest.TestCase):
    """The program on disk is checked by owner, mode, folders and pinned hash. A fake lstat stands in for root-owned files."""

    def setUp(self):
        self.files = {}
        self.sha = "f" * 64

    def st(self, mode, uid=0):
        return os.stat_result((mode, 0, 0, 1, uid, 0, 10, 0, 0, 0))

    def lstat(self, path):
        if path not in self.files:
            raise FileNotFoundError(2, "No such file", path)
        return self.files[path]

    def put(self, path, mode, uid=0):
        self.files[path] = self.st(mode, uid)

    def good(self):
        self.put("/usr", stat.S_IFDIR | 0o755)
        self.put("/usr/local", stat.S_IFDIR | 0o755)
        self.put("/usr/local/sbin", stat.S_IFDIR | 0o755)
        self.put("/usr/local/sbin/fan-profile", stat.S_IFREG | 0o755)

    def check(self, sha=None):
        return actions.check_executable("/usr/local/sbin/fan-profile", sha or self.sha, lstat=self.lstat, sha=lambda p: self.sha)

    def test_a_root_owned_program_with_the_pinned_hash_passes(self):
        self.good()
        self.assertEqual(self.check(), (True, ""))

    def test_a_different_program_than_the_one_approved_is_refused(self):
        self.good()
        ok, why = self.check(sha="0" * 64)
        self.assertFalse(ok)
        self.assertIn("not the program that was approved", why)

    def test_a_program_someone_else_could_have_changed_is_refused(self):
        self.good()
        self.put("/usr/local/sbin/fan-profile", stat.S_IFREG | 0o775)
        self.assertIn("writable by nobody else", self.check()[1])
        self.put("/usr/local/sbin/fan-profile", stat.S_IFREG | 0o755, uid=1000)
        self.assertIn("owned by root", self.check()[1])

    def test_a_folder_in_the_path_that_others_can_write_would_let_them_swap_the_program(self):
        self.good()
        self.put("/usr/local/sbin", stat.S_IFDIR | 0o777)
        ok, why = self.check()
        self.assertFalse(ok)
        self.assertIn("swap the program", why)

    def test_a_link_a_folder_a_missing_file_and_a_non_executable_are_refused(self):
        self.good()
        self.put("/usr/local/sbin/fan-profile", stat.S_IFLNK | 0o777)
        self.assertIn("not a regular file", self.check()[1])
        self.put("/usr/local/sbin/fan-profile", stat.S_IFREG | 0o644)
        self.assertIn("not executable", self.check()[1])
        del self.files["/usr/local/sbin/fan-profile"]
        self.assertIn("cannot be read", self.check()[1])

    def test_the_real_hash_function_hashes_the_file(self):
        d = Path(tempfile.mkdtemp())
        (d / "p").write_bytes(b"abc")
        import hashlib
        self.assertEqual(actions.sha256_of(str(d / "p")), hashlib.sha256(b"abc").hexdigest())


class Sandbox(unittest.TestCase):
    def test_nothing_is_open_that_the_action_did_not_declare(self):
        props = actions.sandbox_properties({"writable": ["/etc/mbpfan.conf"], "timeout_s": 20}, "/s")
        for need in ("ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "NoNewPrivileges=yes", "PrivateNetwork=yes", "RuntimeMaxSec=20",
                     "ReadWritePaths=/etc/mbpfan.conf", "RestrictAddressFamilies=AF_UNIX"):
            self.assertIn(need, props)

    def test_the_network_is_only_there_when_declared(self):
        props = actions.sandbox_properties({"network": True}, "/s")
        self.assertNotIn("PrivateNetwork=yes", props)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", props)
        self.assertFalse(any(p.startswith("ReadWritePaths") for p in props))


if __name__ == "__main__":
    unittest.main()
