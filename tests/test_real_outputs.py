"""The parsers the project depends on, run over what the real tools print on every supported release (tests/fixtures/real/<release>/, recorded by scripts/record-fixtures on Ubuntu 24.04 and
26.04 and Debian 12 and 13). A parser tested only against its author's idea of a tool's output passes while the real output differs; these cannot."""
import re
import unittest
from pathlib import Path

import support  # noqa: F401
from armada import clonediag
from armada.setup import assess, detect
from armada.setup.host import Result
from test_setup import FakeHost

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
RELEASES = {"ubuntu-24.04": ("ubuntu", "24.04", "sudo"), "ubuntu-26.04": ("ubuntu", "26.04", "sudo-rs"), "debian-12": ("debian", "12", "sudo"), "debian-13": ("debian", "13", "sudo")}


def real(release, name):
    return (REAL / release / name).read_text()


def ver(text):
    return tuple(int(x) for x in re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text).groups(0)[:2])


class EveryReleaseIsRecorded(unittest.TestCase):
    def test_all_four_supported_releases_have_their_real_outputs(self):
        for release in RELEASES:
            for name in ("os-release.txt", "sudo-version.txt", "git-version.txt", "python-version.txt", "assess-output.txt", "git-dubious-ownership.txt"):
                self.assertTrue((REAL / release / name).is_file(), f"{release}/{name}: run scripts/record-fixtures")


class Detection(unittest.TestCase):
    def facts(self, release):
        h = FakeHost()
        h.put("/etc/os-release", real(release, "os-release.txt"))
        h.replies["sudo --version"] = Result(0, real(release, "sudo-version.txt"))
        return detect.detect(h)

    def test_the_system_and_its_sudo_are_recognised_on_every_release(self):
        for release, (os_id, version, sudo) in RELEASES.items():
            f = self.facts(release)
            self.assertEqual((f.os_id, f.os_version, f.sudo), (os_id, version, sudo), release)             # sudo-rs on Ubuntu 26.04 is what the real output says
            self.assertIsNone(f.os_problem(), release)

    def test_the_candidacy_check_reads_the_real_script_output_of_every_release(self):
        for release, (os_id, version, _) in RELEASES.items():
            facts = assess.parse(real(release, "assess-output.txt"))
            self.assertEqual((facts["OS_ID"], facts["OS_VERSION"]), (os_id, version), release)
            rows = {what: status for status, what, _ in assess.verdict(facts, "agent").rows}
            self.assertEqual((rows["Release"], rows["Python"]), ("OK", "OK"), release)                  # the machine is supported, and the parser agrees on the real text


class Tools(unittest.TestCase):
    def test_what_the_project_relies_on_is_present_and_new_enough_on_every_release(self):
        for release in RELEASES:
            self.assertGreaterEqual(ver(real(release, "git-version.txt")), (2, 32), release)           # GIT_CONFIG_GLOBAL, used by the history copy
            self.assertGreaterEqual(ver(real(release, "python-version.txt")), (3, 11), release)        # tomllib
            self.assertRegex(real(release, "bwrap-version.txt"), r"bubblewrap \d+\.\d+", release)
            self.assertRegex(real(release, "ssh-version.txt"), r"OpenSSH_\d+\.\d+", release)

    def test_the_jobs_sandbox_directives_exist_in_the_systemd_of_every_release(self):
        for release in RELEASES:
            self.assertGreaterEqual(ver(real(release, "systemd-version.txt")), (249, 0), release)


class GitRefusesAnotherAccountsRepository(unittest.TestCase):
    def test_where_git_refuses_the_message_is_recognised_and_its_path_is_found(self):
        refused = 0
        for release in RELEASES:
            text = real(release, "git-dubious-ownership.txt")
            if "dubious ownership" not in text:
                continue
            refused += 1
            cause, meaning, steps = clonediag.classify(text, admin="agent" + "@" + "w.example")
            self.assertEqual(cause, "dubious-ownership", release)
            self.assertIn("/home/worker/r.git", " ".join(steps), release)                               # the path comes out of git's own message
        self.assertGreaterEqual(refused, 3)

    def test_the_releases_that_do_not_refuse_are_recorded_as_such(self):
        """Found by recording: git 2.53 (Ubuntu 26.04) served the repository to another account instead of refusing it, so the mailbox bug could not show there. If a release stops refusing, the
        explanation for that release is this recording, not a guess."""
        for release in RELEASES:
            text = real(release, "git-dubious-ownership.txt")
            if "dubious ownership" not in text:
                self.assertRegex(text, r"^[0-9a-f]{4}", release)                                          # a normal reference advertisement


if __name__ == "__main__":
    unittest.main()
