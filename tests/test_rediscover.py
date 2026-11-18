"""Finding a worker by its SSH host key, run over what the real tools print on every supported release (tests/fixtures/real/<release>/)."""
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import rediscover
from ct.setup import discover

REAL = Path(__file__).resolve().parent / "fixtures" / "real"
RELEASES = sorted(p.name for p in REAL.iterdir() if p.is_dir())


def real(release, name):
    return (REAL / release / name).read_text()


class TheRealOutputs(unittest.TestCase):
    def test_what_a_machine_shows_and_what_this_computer_recorded_are_the_same_keys_on_every_release(self):
        for release in RELEASES:
            shown = rediscover.parse_keys(real(release, "keyscan.txt"))
            kinds = {t for t, _ in shown}
            self.assertIn("ssh-ed25519", kinds, release)                        # the comment lines of ssh-keyscan are not keys
            recorded = rediscover.parse_keys(real(release, "keygen-find.txt"))   # a HASHED known_hosts entry, found with ssh-keygen -F
            self.assertTrue(recorded, release)
            self.assertTrue(recorded <= shown, release)                          # what was recorded is one of what the machine shows

    def test_nothing_else_is_taken_for_a_key(self):
        self.assertEqual(rediscover.parse_keys("# 127.0.0.1:22 SSH-2.0-OpenSSH_9.6p1\n\nnot a key line\n"), set())


class TheMatch(unittest.TestCase):
    A = {("ssh-ed25519", "AAAA-one")}
    B = {("ssh-ed25519", "AAAA-two"), ("ssh-rsa", "RSA-two")}

    def test_exactly_one_machine_shows_the_recorded_key(self):
        m = rediscover.match(self.A, {"192.0.2.10": self.B, "192.0.2.11": self.A})
        self.assertEqual((m.kind, m.addresses), ("one", ["192.0.2.11"]))

    def test_a_key_of_another_type_is_not_a_match_and_nothing_matching_is_none(self):
        m = rediscover.match({("ssh-rsa", "AAAA-one")}, {"192.0.2.11": self.A})        # same text, other type
        self.assertEqual(m.kind, "none")

    def test_a_cloned_machine_makes_it_several_and_never_a_guess(self):
        m = rediscover.match(self.A, {"192.0.2.10": self.A, "192.0.2.11": self.A})
        self.assertEqual((m.kind, m.addresses), ("several", ["192.0.2.10", "192.0.2.11"]))

    def test_a_worker_this_computer_never_connected_to_cannot_be_found(self):
        self.assertEqual(rediscover.match(set(), {"192.0.2.11": self.A}).kind, "unknown")


class Locating(unittest.TestCase):
    def test_the_scan_finds_the_worker_by_its_key_and_a_machine_that_only_has_its_name_is_refused(self):
        devs = [discover.Device("192.0.2.20", name="worker-box.example"), discover.Device("192.0.2.21", name="other-box.example")]
        keys = {"192.0.2.20": {("ssh-ed25519", "IMPOSTOR")}, "192.0.2.21": {("ssh-ed25519", "REAL")}}       # the machine with the worker's NAME shows another key
        m, found = rediscover.locate("worker-box.example", 22, [], scan=lambda nets: (devs, []), shown=lambda ip: keys[ip], recorded={("ssh-ed25519", "REAL")})
        self.assertEqual((m.kind, m.addresses), ("one", ["192.0.2.21"]))

    def test_the_recorded_keys_come_from_ssh_keygen_for_the_name_and_the_bracketed_form_for_another_port(self):
        seen = []
        rediscover.recorded_keys("box.example", 22, run=lambda argv, *a: seen.append(argv) or "")
        rediscover.recorded_keys("box.example", 2222, run=lambda argv, *a: seen.append(argv) or "")
        self.assertEqual([a[2] for a in seen], ["box.example", "[box.example]:2222"])


if __name__ == "__main__":
    unittest.main()
