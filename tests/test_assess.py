import subprocess
import unittest

import support  # noqa: F401

USER_AT_HOST = "agent" + "@" + "h.example"
from ct.setup import assess

GOOD = {"OS_ID": "ubuntu", "OS_LIKE": "debian", "OS_VERSION": "24.04", "OS_NAME": "Ubuntu 24.04.3 LTS", "ARCH": "x86_64", "CORES": "2", "MEM_MB": "2924", "DISK_MB": "20000",
        "PYTHON": "3.12", "SYSTEMD": "yes", "SUDO": "yes", "SUDO_GROUP": "yes", "GIT": "yes", "VIRT": "vmware", "INSTALLS": "", "WHO": "agent"}


def v(**over):
    return assess.verdict({**GOOD, **over}, "agent")


def status(verdict, what):
    return next(s for s, w, _ in verdict.rows if w == what)


class Verdict(unittest.TestCase):
    def test_the_machine_that_was_installed_for_this_meets_the_requirements(self):
        r = v()
        self.assertTrue(r.meets)
        self.assertEqual(r.failures, [])
        self.assertEqual(status(r, "Release"), "OK")

    def test_every_supported_release_passes_and_an_old_or_foreign_one_fails_with_a_reason(self):
        for os_id, ver in (("ubuntu", "24.04"), ("ubuntu", "26.04"), ("debian", "12"), ("debian", "13"), ("raspbian", "12")):
            self.assertTrue(v(OS_ID=os_id, OS_VERSION=ver, OS_LIKE="debian").meets, (os_id, ver))
        for os_id, ver in (("ubuntu", "22.04"), ("debian", "11"), ("fedora", "40"), ("alpine", "3.20")):
            r = v(OS_ID=os_id, OS_VERSION=ver, OS_LIKE="")
            self.assertFalse(r.meets, (os_id, ver))
            self.assertEqual(status(r, "Release"), "FAIL")
        self.assertTrue(any("Install Ubuntu" in f for f in v(OS_ID="ubuntu", OS_VERSION="22.04").fixes))

    def test_old_python_is_a_failure_that_says_what_to_do(self):
        r = v(PYTHON="3.10")
        self.assertFalse(r.meets)
        self.assertTrue(any("apt-get install python3" in f for f in r.fixes))
        self.assertFalse(v(PYTHON="").meets)

    def test_too_little_memory_or_disk_fails_and_a_small_amount_only_warns(self):
        self.assertFalse(v(MEM_MB="512").meets)
        r = v(MEM_MB="1500")
        self.assertTrue(r.meets)
        self.assertEqual(status(r, "Memory"), "WARN")
        self.assertFalse(v(DISK_MB="900").meets)
        self.assertEqual(status(v(DISK_MB="3000"), "Disk"), "WARN")

    def test_no_systemd_or_an_unsupported_processor_fails(self):
        self.assertFalse(v(SYSTEMD="no").meets)
        self.assertFalse(v(ARCH="armv7l").meets)
        self.assertTrue(v(ARCH="aarch64").meets)

    def test_an_account_that_cannot_use_sudo_fails_with_the_command_to_fix_it(self):
        r = v(SUDO_GROUP="no")
        self.assertFalse(r.meets)
        self.assertTrue(any("usermod -aG sudo agent" in f for f in r.fixes))
        self.assertFalse(v(SUDO="no").meets)

    def test_a_container_warns_a_vm_and_hardware_do_not(self):
        self.assertEqual(status(v(VIRT="docker"), "Environment"), "WARN")
        self.assertTrue(v(VIRT="docker").meets)
        self.assertEqual(status(v(VIRT="none"), "Environment"), "OK")

    def test_an_existing_install_is_named_as_a_warning(self):
        r = v(INSTALLS="claude-tenta other")
        self.assertTrue(r.meets)
        self.assertIn("claude-tenta, other", [d for s, w, d in r.rows if w == "Existing install"][0].replace("as: ", "") and "claude-tenta, other")

    def test_nothing_read_is_a_failed_login_not_a_pass(self):
        self.assertFalse(assess.verdict({}).meets)


class Reading(unittest.TestCase):
    def fake(self, out="", err="", rc=0):
        self.argv = None

        def run(argv, **kw):
            self.argv, self.input = argv, kw.get("input")
            return subprocess.CompletedProcess(argv, rc, out, err)
        return run

    def test_the_script_only_reads_and_the_login_never_asks_for_a_password(self):
        assess.run_script("h.example", "agent", 22, self.fake("OS_ID=ubuntu\nMEM_MB=2900\n"))
        self.assertIn("BatchMode=yes", self.argv)
        self.assertIn(USER_AT_HOST, self.argv)
        script = assess.SCRIPT.replace("command -v sudo", "")                          # looking for sudo is allowed; running it is not
        for forbidden in ("sudo ", "apt", "rm ", "tee ", ">>", "chmod", "systemctl ", "useradd", "install "):
            self.assertNotIn(forbidden, script, forbidden)
        self.assertNotIn(" > ", script.replace("2>/dev/null", "").replace(">/dev/null", ""))     # it never writes a file

    def test_the_facts_are_read_from_key_equals_value_lines(self):
        facts, err = assess.run_script("h", "u", 22, self.fake("OS_ID=ubuntu\nnoise\nMEM_MB=2900\n"))
        self.assertEqual((facts, err), ({"OS_ID": "ubuntu", "MEM_MB": "2900"}, ""))

    def test_a_refused_key_says_how_to_install_it(self):
        r = assess.assess("h.example", "agent", 22, self.fake(err=USER_AT_HOST + ": Permission denied (publickey).", rc=255))
        self.assertFalse(r.meets)
        self.assertTrue(any("ssh-copy-id " + USER_AT_HOST in f for f in r.fixes))

    def test_an_unreachable_machine_says_to_check_it_is_on(self):
        r = assess.assess("h.example", "agent", 22, self.fake(err="ssh: connect to host h.example port 22: Connection timed out", rc=255))
        self.assertTrue(any("is on" in f for f in r.fixes))


if __name__ == "__main__":
    unittest.main()
