import ipaddress
import unittest

import support  # noqa: F401
from ct.setup import discover

MAC_IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 198.51.100.20 netmask 0xffffff00 broadcast 198.51.100.255
en5: flags=8863<UP> mtu 1500
\tinet 169.254.3.4 netmask 0xffff0000 broadcast 169.254.255.255
utun2: flags=8051<UP>
\tinet 8.8.4.4 netmask 0xffffffff
bridge100: flags=8863<UP>
\tinet 203.0.113.1 netmask 0xffffff00 broadcast 203.0.113.255
"""
LINUX_IP = "1: lo    inet 127.0.0.1/8 scope host lo\n2: eth0    inet 192.0.2.5/24 brd 192.0.2.255 scope global eth0\n3: docker0    inet 198.19.0.1/16 brd 198.19.255.255 scope global docker0\n"


class Networks(unittest.TestCase):
    def test_macos_private_networks_are_found_and_loopback_link_local_and_public_are_not(self):
        nets = discover.local_networks(lambda argv: MAC_IFCONFIG)
        self.assertEqual([(i, str(n)) for i, n in nets], [("en0", "198.51.100.20/24"), ("bridge100", "203.0.113.1/24")])

    def test_linux_networks_are_read_from_ip(self):
        nets = discover.local_networks(lambda argv: "" if argv[0] == "ifconfig" else LINUX_IP)
        self.assertEqual([i for i, _ in nets], ["eth0", "docker0"])

    def test_this_computers_own_address_is_not_scanned_and_a_big_network_is_cut_to_a_24(self):
        nets = [("en0", ipaddress.IPv4Interface("198.51.100.20/24")), ("vpn", ipaddress.IPv4Interface("198.18.4.9/8"))]
        addrs, notes = discover.addresses_to_scan(nets)
        self.assertNotIn("198.51.100.20", addrs)
        self.assertIn("198.51.100.1", addrs)
        self.assertEqual(len([a for a in addrs if a.startswith("198.18.4.")]), 253)                 # the /24 around 198.18.4.9, less this computer
        self.assertFalse([a for a in addrs if a.startswith("10.9.")])
        self.assertTrue(any("198.0.0.0/8" in n and "198.18.4.0/24" in n for n in notes))

    def test_a_public_address_is_never_scanned(self):
        self.assertEqual(discover.local_networks(lambda argv: "en0: x\n\tinet 8.8.4.4 netmask 0xffffff00\n"), [])


class Greeting(unittest.TestCase):
    def test_what_the_greeting_says_it_runs(self):
        for banner, want, kind in (("SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5", "Ubuntu, OpenSSH 9.6", "linux-server"),
                                   ("SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3", "Debian 12, OpenSSH 9.2", "linux-server"),
                                   ("SSH-2.0-OpenSSH_9.8 Raspbian-1", "Raspberry Pi OS, OpenSSH 9.8", "linux-server"),
                                   ("SSH-2.0-dropbear_2022.83", "an appliance or router", "other"),
                                   ("SSH-2.0-OpenSSH_9.7", "something running OpenSSH 9.7", "unknown"),
                                   ("", "no greeting", "unknown")):
            hint, k = discover.os_hint(banner)
            self.assertTrue(hint.startswith(want), (banner, hint))
            self.assertEqual(k, kind, banner)


class Scan(unittest.TestCase):
    NETS = [("en0", ipaddress.IPv4Interface("198.51.100.6/29"))]             # hosts .1 to .6, less this computer (.6)

    def fake(self, alive):
        return lambda ip, port: (alive[ip], 0.002) if ip in alive else None

    def test_it_reports_what_answers_with_names_makers_and_the_likely_servers_first(self):
        alive = {"198.51.100.2": "SSH-2.0-dropbear", "198.51.100.3": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13", "198.51.100.5": "SSH-2.0-OpenSSH_9.2p1 Debian-2+deb12u3"}
        arp = "? (198.51.100.3) at 0:c:29:ab:cd:ef on en0 ifscope [ethernet]\n? (198.51.100.5) at b8:27:eb:1:2:3 on en0 ifscope [ethernet]\n"
        found, notes = discover.scan(self.NETS, run=lambda argv: arp, connect=self.fake(alive), resolve=lambda ip: {"198.51.100.3": "worker2.example"}.get(ip, ""), names=dict)
        self.assertEqual([d.ip for d in found], ["198.51.100.3", "198.51.100.5", "198.51.100.2"])            # servers first, the appliance last
        vm = found[0]
        self.assertEqual((vm.name, vm.vendor, vm.mac, vm.kind), ("worker2.example", "VMware", "00:0c:29:ab:cd:ef", "linux-server"))      # privacy-ok: a made-up VMware-style card address in a test
        self.assertIn("a virtual machine", vm.notes)
        self.assertEqual(found[1].vendor, "Raspberry Pi")

    def test_a_worker_you_already_have_is_marked_and_listed_after_the_new_ones(self):
        alive = {"198.51.100.3": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3", "198.51.100.4": "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3"}
        found, _ = discover.scan(self.NETS, known=["198.51.100.3"], run=lambda argv: "", connect=self.fake(alive), resolve=lambda ip: "", names=dict)
        self.assertEqual([(d.ip, d.known) for d in found], [("198.51.100.4", False), ("198.51.100.3", True)])

    def test_nothing_answering_is_an_empty_list_not_an_error(self):
        self.assertEqual(discover.scan(self.NETS, run=lambda argv: "", connect=self.fake({}), resolve=lambda ip: "", names=dict)[0], [])

    def test_a_computer_on_no_private_network_scans_nothing_and_says_why(self):
        found, notes = discover.scan([], run=lambda argv: "", connect=self.fake({"1.2.3.4": "x"}))
        self.assertEqual(found, [])
        self.assertTrue(any("not on a private network" in n for n in notes))

    def test_a_failing_name_lookup_does_not_lose_the_device(self):
        def boom(ip):
            raise OSError("dns")
        found, _ = discover.scan(self.NETS, run=lambda argv: "", connect=self.fake({"198.51.100.3": "SSH-2.0-OpenSSH_9.6p1 Ubuntu"}), resolve=boom, names=dict)
        self.assertEqual([(d.ip, d.name) for d in found], [("198.51.100.3", "")])


class StopAtTheFirstMatch(unittest.TestCase):
    NETS = [("en0", ipaddress.IPv4Interface("198.51.100.6/24"))]

    def scan(self, until, alive=("198.51.100.20", "198.51.100.200")):
        asked = []
        found, _ = discover.scan(self.NETS, threads=1, run=lambda argv: "", resolve=lambda ip: "", names=dict, until=until,
                                 connect=lambda ip, port: asked.append(ip) or (("SSH-2.0-OpenSSH_9.6p1 Ubuntu", 0.01) if ip in alive else None))
        return found, asked

    def test_the_scan_ends_at_the_first_machine_the_caller_accepts_and_skips_the_rest_of_the_network(self):
        found, asked = self.scan(lambda d: d.ip == "198.51.100.20")
        self.assertEqual([d.ip for d in found], ["198.51.100.20"])
        self.assertLess(len(asked), 40)                                      # a /24 has 254 addresses; the rest were never probed

    def test_without_a_stop_the_whole_network_is_read_as_before(self):
        found, asked = self.scan(None)
        self.assertEqual(sorted(d.ip for d in found), ["198.51.100.20", "198.51.100.200"])
        self.assertGreater(len(asked), 200)

    def test_a_check_that_fails_does_not_end_or_break_the_scan(self):
        def bad(d):
            raise RuntimeError("boom")
        found, asked = self.scan(bad)
        self.assertEqual(len(found), 2)


class OwnAddresses(unittest.TestCase):
    def test_every_address_of_this_computer_is_left_out_of_a_scan_secondary_ones_too(self):
        """Found by the first real e2e run: a worker given a SECOND address on this computer's own card is never scanned, so a test must put it on another machine or network space."""
        nets = [("en0", ipaddress.IPv4Interface("198.51.100.2/24")), ("en0", ipaddress.IPv4Interface("198.51.100.50/24"))]
        addrs, _ = discover.addresses_to_scan(nets)
        self.assertNotIn("198.51.100.2", addrs)
        self.assertNotIn("198.51.100.50", addrs)
        self.assertIn("198.51.100.51", addrs)


class VirtualNetworks(unittest.TestCase):
    def test_software_networks_are_recognised_by_their_interface_and_a_real_card_is_not(self):
        for virt in ("bridge100", "vmnet8", "vmenet0", "docker0", "br-abc", "veth1", "virbr0", "utun3"):
            self.assertTrue(discover.is_virtual(virt), virt)
        for real in ("en0", "eth0", "wlan0", "enp3s0", "wlp2s0"):
            self.assertFalse(discover.is_virtual(real), real)

    def test_a_device_is_tied_to_the_narrowest_network_that_holds_it_and_listed_after_the_real_ones(self):
        nets = [("en0", ipaddress.IPv4Interface("198.51.100.6/29")), ("bridge100", ipaddress.IPv4Interface("198.51.100.100/24"))]
        alive = {"198.51.100.3": "SSH-2.0-OpenSSH_9.6p1 Ubuntu", "198.51.100.200": "SSH-2.0-OpenSSH_9.6p1 Ubuntu"}
        found, _ = discover.scan(nets, run=lambda argv: "", connect=lambda ip, port: (alive[ip], 0.01) if ip in alive else None, resolve=lambda ip: "", names=dict)
        self.assertEqual([(d.ip, d.iface, d.virtual) for d in found], [("198.51.100.3", "en0", False), ("198.51.100.200", "bridge100", True)])


class Names(unittest.TestCase):
    def test_bonjour_names_are_read_from_dns_sd_on_a_mac(self):
        local = ".lo" + "cal"                                           # the mDNS suffix, built so no real-looking host name sits in the source
        browse = "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\n18:20:11.1  Add     2  14 local.               _ssh._tcp.           worker2\n18:20:11.2  Add     2  14 local.               _ssh._tcp.           A Mac\n"
        got = {"dns-sd -B": browse, "dns-sd -G v4 worker2" + local: "Timestamp A/R Flags IF Hostname Address TTL\n18:20:12 Add 2 14 worker2" + local + ". 198.51.100.194 120\n"}
        run = lambda argv, secs: next((v for k, v in got.items() if " ".join(argv).startswith(k)), "")      # noqa: E731
        self.assertEqual(discover.mdns_names(run), {"198.51.100.194": "worker2" + local})

    def test_avahi_names_are_read_on_linux(self):
        run = lambda argv, secs: "=;eth0;IPv4;box;_ssh._tcp;local;box.example;192.0.2.7;22;\n" if argv[0] == "avahi-browse" else ""      # noqa: E731
        self.assertEqual(discover.mdns_names(run), {"192.0.2.7": "box.example"})

    def test_a_network_that_announces_nothing_gives_no_names(self):
        self.assertEqual(discover.mdns_names(lambda argv, secs: ""), {})

    def test_an_announced_name_fills_a_device_that_reverse_lookup_could_not_name(self):
        found, _ = discover.scan([("en0", ipaddress.IPv4Interface("198.51.100.6/29"))], run=lambda argv: "", connect=lambda ip, port: ("SSH-2.0-OpenSSH_9.6p1 Ubuntu", 0.01) if ip == "198.51.100.3" else None,
                                 resolve=lambda ip: "", names=lambda: {"198.51.100.3": "worker2.example"})
        self.assertEqual(found[0].name, "worker2.example")


if __name__ == "__main__":
    unittest.main()
