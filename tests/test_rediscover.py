"""Finding a worker by its SSH host key, run over what the real tools print on every supported release (tests/fixtures/real/<release>/)."""
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from armada import rediscover
from armada.setup import discover

MAC = "aa:bb:cc:" + "00:00:01"                                        # privacy-ok: an invented address
BOX = "box.lo" + "cal"
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


class SavingTheAddress(unittest.TestCase):
    def test_only_the_host_line_changes_the_old_value_is_kept_and_the_file_still_reads(self):
        import tempfile
        import tomllib
        d = Path(tempfile.mkdtemp())
        f = d / "w.toml"
        f.write_text('[identity]\nname = "w"\n[server]\nhost = "box.example"\nadmin_user = "agent"\n[network]\nhost = "unrelated"\n')
        was = rediscover.save_address(f, "192.0.2.21")
        self.assertEqual(was, "box.example")
        data = tomllib.loads(f.read_text())
        self.assertEqual((data["server"]["host"], data["server"]["admin_user"], data["network"]["host"], data["identity"]["name"]), ("192.0.2.21", "agent", "unrelated", "w"))
        self.assertIn("# was: box.example", f.read_text())

    def test_a_file_without_a_host_line_is_refused_not_guessed_at(self):
        import tempfile
        f = Path(tempfile.mkdtemp()) / "w.toml"
        f.write_text('[server]\nadmin_user = "agent"\n')
        with self.assertRaises(ValueError):
            rediscover.save_address(f, "192.0.2.21")


class TheLadder(unittest.TestCase):
    KEY = {("ssh-ed25519", "REAL")}

    def run_ladder(self, shown, **kw):
        import tempfile
        calls = []
        kw.setdefault("home", tempfile.mkdtemp())

        def read(ip):
            calls.append(ip)
            return shown.get(ip, set())
        scans = []
        kw.setdefault("scan_real", lambda until=None: scans.append("real") or ([discover.Device("192.0.2.50")], []))
        kw.setdefault("scan_virtual", lambda until=None: scans.append("virtual") or ([discover.Device("192.0.2.60")], []))
        r = rediscover.ladder("w.example", 22, "w", recorded=self.KEY, shown=read, mdns=kw.pop("mdns", lambda h: None), arp=kw.pop("arp", lambda: {}), **kw)
        return r, calls, scans

    def test_a_worker_still_at_its_address_costs_one_key_read_and_no_scan(self):
        (m, rung, _), calls, scans = self.run_ladder({"w.example": self.KEY})
        self.assertEqual((m.kind, rung, calls, scans), ("one", "the configured address", ["w.example"], []))

    def test_the_last_address_that_worked_is_tried_before_anything_is_scanned(self):
        import tempfile
        home = tempfile.mkdtemp()
        rediscover.remember(home, "w", "198.51.100.7", resolve=lambda h: h, arp=lambda: {"198.51.100.7": MAC})
        (m, rung, _), calls, scans = self.run_ladder({"198.51.100.7": self.KEY}, home=home)
        self.assertEqual((m.addresses, rung, scans), (["198.51.100.7"], "the last address that worked", []))

    def test_an_address_already_tried_is_not_tried_again(self):
        """Found on the real VM: the last address that worked was the configured one, which was dead, so it was waited on twice (6 s instead of 4)."""
        import tempfile
        home = tempfile.mkdtemp()
        rediscover.remember(home, "w", "198.51.100.7", resolve=lambda h: h, arp=lambda: {})
        calls = []
        rediscover.ladder("198.51.100.7", 22, "w", home, recorded=self.KEY, shown=lambda ip: calls.append(ip) or set(), mdns=lambda h: "198.51.100.7", arp=lambda: {}, full=False)
        self.assertEqual(calls, ["198.51.100.7"])

    def test_the_name_service_answer_is_only_a_hint_a_wrong_machine_there_moves_on(self):
        (m, rung, _), calls, scans = self.run_ladder({"192.0.2.77": {("ssh-ed25519", "IMPOSTOR")}, "192.0.2.50": self.KEY}, mdns=lambda h: "192.0.2.77")
        self.assertEqual((m.addresses, rung), (["192.0.2.50"], "a scan of your real networks"))
        self.assertEqual(scans, ["real"])                                     # found on the real networks: the virtual ones were never scanned

    def test_the_neighbour_table_entry_of_the_same_network_card_is_tried_before_a_scan(self):
        import tempfile
        home = tempfile.mkdtemp()
        rediscover.remember(home, "w", "198.51.100.7", resolve=lambda h: h, arp=lambda: {"198.51.100.7": MAC})
        (m, rung, _), calls, scans = self.run_ladder({"198.51.100.99": self.KEY}, home=home, arp=lambda: {"198.51.100.99": MAC})
        self.assertEqual((m.addresses, rung, scans), (["198.51.100.99"], "the neighbour table", []))

    def test_without_the_full_scan_only_the_cheap_rungs_run(self):
        (m, rung, _), calls, scans = self.run_ladder({"192.0.2.50": self.KEY}, full=False)
        self.assertEqual((m.kind, scans), ("none", []))                       # what a monitoring loop uses: it never waits for a scan

    def test_the_virtual_networks_are_scanned_last_and_only_if_needed(self):
        (m, rung, _), calls, scans = self.run_ladder({"192.0.2.60": self.KEY})
        self.assertEqual((m.addresses, scans), (["192.0.2.60"], ["real", "virtual"]))

    def test_the_scan_is_told_to_stop_at_the_machine_that_shows_the_recorded_key_and_no_key_is_read_twice(self):
        reads = []

        def scan(until):
            devs = [discover.Device("192.0.2.40"), discover.Device("192.0.2.50")]
            stopped_at = next(d.ip for d in devs if until(d))
            return [d for d in devs if d.ip <= stopped_at], []
        r = rediscover.ladder("w.example", 22, "w", tempfile.mkdtemp(), recorded=self.KEY, shown=lambda ip: reads.append(ip) or (self.KEY if ip == "192.0.2.50" else set()),
                              mdns=lambda h: None, arp=lambda: {}, scan_real=scan, scan_virtual=lambda until: ([], []))
        self.assertEqual((r[0].addresses, r[1]), (["192.0.2.50"], "a scan of your real networks"))
        self.assertEqual(sorted(reads), ["192.0.2.40", "192.0.2.50", "w.example"])      # the configured address once, each scanned machine once

    def test_the_multicast_answer_is_read_from_avahi_or_dns_sd(self):
        self.assertEqual(rediscover.resolve_mdns(BOX, run=lambda argv, t=0: "box.lo" + "cal\t192.0.2.9\n"), "192.0.2.9")
        self.assertEqual(rediscover.resolve_mdns(BOX, run=lambda argv, t=0: "" if argv[0] == "avahi-resolve" else "Timestamp A/R Flags if Hostname Address TTL\n12:00 Add 2 4 box.lo" + "cal. 192.0.2.9 120\n"), "192.0.2.9")
        self.assertIsNone(rediscover.resolve_mdns("box.example", run=lambda *a: "x"))      # only .local names are asked of the multicast service
