import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cw import config, firewall  # noqa: E402


def cfg(**env):
    return config.load(None, env={"CW_CONFIG": "", **env})


class Rules(unittest.TestCase):
    def flat(self, rl):
        return [" ".join(r) for r in rl]

    def test_the_defaults_deny_incoming_allow_ssh_from_private_ranges_and_block_the_lan(self):
        r = self.flat(firewall.rules(cfg(), gateway="192.0.2.1"))
        self.assertEqual(r[:2], ["default deny incoming", "default allow outgoing"])
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
            self.assertIn(f"allow in from {net} to any port 22 proto tcp", r)
            self.assertIn(f"deny out to {net}", r)
        self.assertIn("deny out to 169.254.0.0/16", r)
        self.assertIn("allow out to 192.0.2.1 port 53 proto udp", r)
        self.assertIn("allow out to 192.0.2.1 port 53 proto tcp", r)
        self.assertIn("allow out to any port 67 proto udp", r)
        self.assertIn("allow out to 224.0.0.251 port 5353 proto udp", r)

    def test_the_order_cannot_lock_you_out_or_let_the_blanket_denies_win(self):
        r = self.flat(firewall.rules(cfg(CW_NETWORK_LAN_ALLOW_OUT="192.0.2.0/24"), gateway="192.0.2.1"))
        first_allow_in = min(i for i, x in enumerate(r) if x.startswith("allow in"))
        first_deny = min(i for i, x in enumerate(r) if x.startswith("deny out"))
        self.assertLess(first_allow_in, first_deny)                                       # SSH in is allowed before anything is denied
        self.assertLess(r.index("allow out to 192.0.2.0/24"), first_deny)                  # an exception to the LAN block precedes the block
        self.assertLess(r.index("allow out to 192.0.2.1 port 53 proto udp"), first_deny)
        self.assertEqual(self.flat(firewall.rules(cfg(), "192.0.2.1"))[:2], ["default deny incoming", "default allow outgoing"])

    def test_configured_dns_servers_replace_the_gateway_guess(self):
        r = self.flat(firewall.rules(cfg(CW_NETWORK_DNS_SERVERS="192.0.2.53,198.51.100.53"), gateway="192.0.2.1"))
        self.assertIn("allow out to 192.0.2.53 port 53 proto udp", r)
        self.assertIn("allow out to 198.51.100.53 port 53 proto tcp", r)
        self.assertFalse(any("192.0.2.1 " in x for x in r))

    def test_the_ssh_port_and_networks_come_from_the_config(self):
        r = self.flat(firewall.rules(cfg(CW_SERVER_SSH_PORT="2222", CW_NETWORK_SSH_FROM="192.0.2.0/24"), gateway=None))
        self.assertEqual([x for x in r if x.startswith("allow in")], ["allow in from 192.0.2.0/24 to any port 2222 proto tcp"])

    def test_the_lan_block_can_be_turned_off(self):
        r = self.flat(firewall.rules(cfg(CW_NETWORK_LAN_DENY_OUT="false"), gateway="192.0.2.1"))
        self.assertFalse(any(x.startswith("deny out") for x in r))

    def test_ipv6_blocks_unique_local_only_never_link_local(self):
        v6 = self.flat(firewall.rules(cfg(), "192.0.2.1", ipv6=True))
        self.assertIn("deny out to fc00::/7", v6)
        self.assertFalse(any("fe80" in x for x in v6))                                       # neighbour discovery needs link-local
        self.assertFalse(any("fc00" in x for x in self.flat(firewall.rules(cfg(), "192.0.2.1", ipv6=False))))

    def test_the_ipv6_choice_comes_from_the_configuration(self):
        self.assertTrue(firewall.ipv6_enabled(cfg()))                                         # the default keeps IPv6
        self.assertIn("deny out to fc00::/7", self.flat(firewall.rules(cfg(), "192.0.2.1")))
        off = cfg(CW_NETWORK_IPV6="disabled")
        self.assertFalse(firewall.ipv6_enabled(off))
        self.assertFalse(any("fc00" in x for x in self.flat(firewall.rules(off, "192.0.2.1"))))   # nothing to block when it is off
        with self.assertRaises(config.ConfigError):
            cfg(CW_NETWORK_IPV6="maybe")

    def test_ufw_is_told_whether_to_write_ipv6_rules(self):
        self.assertEqual(firewall.ufw_defaults_text("A=1\nIPV6=yes\nB=2\n", False), "A=1\nIPV6=no\nB=2\n")
        self.assertEqual(firewall.ufw_defaults_text("IPV6=no\n", True), "IPV6=yes\n")
        self.assertEqual(firewall.ufw_defaults_text("A=1\n", False), "A=1\nIPV6=no\n")           # added when missing
        self.assertEqual(firewall.ufw_defaults_text("  IPV6 = yes\n", False), "IPV6=no\n")

    def test_every_value_is_validated_so_no_rule_can_carry_extra_arguments(self):
        for r in firewall.rules(cfg(CW_NETWORK_LAN_ALLOW_OUT="192.0.2.0/24"), "192.0.2.1"):
            for part in r:
                self.assertNotRegex(part, r"[;&|$`\s]")                                       # one safe token per argument
        with self.assertRaises(config.ConfigError):
            cfg(CW_NETWORK_LAN_ALLOW_OUT="192.0.2.0/24; ufw disable")


class Gateway(unittest.TestCase):
    def test_detect(self):
        run = lambda cmd: subprocess.CompletedProcess(cmd, 0, "default via 192.0.2.1 dev eth0 proto dhcp\n", "")   # noqa: E731
        self.assertEqual(firewall.detect_gateway(run), "192.0.2.1")
        self.assertIsNone(firewall.detect_gateway(lambda cmd: subprocess.CompletedProcess(cmd, 0, "", "")))
        self.assertIsNone(firewall.detect_gateway(lambda cmd: subprocess.CompletedProcess(cmd, 0, "default via not-an-ip dev x\n", "")))

        def boom(cmd):
            raise OSError("no ip")
        self.assertIsNone(firewall.detect_gateway(boom))


class Applying(unittest.TestCase):
    def run_apply(self, c=None, client="192.0.2.50", fail_on=None, **kw):
        calls, said = [], []

        def run(cmd):
            calls.append(cmd)
            bad = fail_on and " ".join(cmd[1:]).startswith(fail_on)
            return subprocess.CompletedProcess(cmd, 1 if bad else 0, "", "ERROR: something" if bad else "")
        c = c or cfg(CW_NETWORK_SSH_FROM="192.0.2.0/24")
        try:
            n = firewall.apply(c, run=run, gateway="192.0.2.1", client_address=client, out=said.append, **kw)
            return calls, said, n, None
        except firewall.FirewallError as e:
            return calls, said, None, e

    def test_a_dry_run_prints_everything_and_runs_nothing(self):
        calls, said, n, err = self.run_apply(dry_run=True)
        self.assertEqual((calls, err), ([], None))
        text = "\n".join(said)
        self.assertIn("ufw default deny incoming", text)
        self.assertIn("ufw --force enable", text)
        self.assertIn("Nothing was changed", text)
        self.assertIn("gateway: 192.0.2.1", text)

    def test_apply_sets_ufw_ipv6_from_the_configuration(self):
        import tempfile
        d = Path(tempfile.mkdtemp()) / "ufw"
        d.write_text("IPV6=yes\nDEFAULT_INPUT_POLICY=\"DROP\"\n")
        calls, said, n, err = self.run_apply(c=cfg(CW_NETWORK_SSH_FROM="192.0.2.0/24", CW_NETWORK_IPV6="disabled"), dry_run=False, defaults_path=str(d))
        self.assertIsNone(err)
        self.assertEqual(d.read_text(), "IPV6=no\nDEFAULT_INPUT_POLICY=\"DROP\"\n")
        self.assertIn("IPv6 disabled", "\n".join(said))
        self.assertFalse(any("fc00" in " ".join(c) for c in calls))
        calls, said, n, err = self.run_apply(dry_run=False, defaults_path=str(d))
        self.assertIn("IPV6=yes", d.read_text())
        self.assertTrue(any("fc00::/7" in " ".join(c) for c in calls))

    def test_apply_runs_every_rule_in_order_and_enables_last(self):
        calls, said, n, err = self.run_apply(dry_run=False)
        self.assertIsNone(err)
        self.assertEqual(calls[0], ["ufw", "default", "deny", "incoming"])
        self.assertEqual(calls[-1], ["ufw", "--force", "enable"])
        self.assertEqual(len(calls), n + 1)

    def test_it_refuses_when_the_current_ssh_connection_would_be_cut_off(self):
        calls, said, n, err = self.run_apply(client="198.51.100.9", dry_run=False)
        self.assertIn("would lock you out", str(err))
        self.assertIn("198.51.100.9", str(err))
        self.assertEqual(calls, [])                                                          # nothing was touched
        calls, said, n, err = self.run_apply(client="not an address", dry_run=False)
        self.assertIn("could not read your current SSH address", str(err))
        self.assertEqual(self.run_apply(client=None, dry_run=False)[3], None)                 # not over SSH: nothing to protect

    def test_a_failing_rule_stops_before_the_firewall_is_enabled(self):
        calls, said, n, err = self.run_apply(dry_run=False, fail_on="allow out to any port 67")
        self.assertIn("failed", str(err))
        self.assertIn("allow out to any port 67", str(err))
        self.assertNotIn(["ufw", "--force", "enable"], calls)

    def test_failing_to_enable_is_reported(self):
        calls, said, n, err = self.run_apply(dry_run=False, fail_on="--force enable")
        self.assertIn("--force enable", str(err))

    def test_without_dns_information_it_refuses_rather_than_break_name_resolution(self):
        orig = firewall.detect_gateway
        firewall.detect_gateway = lambda run=None: None
        try:
            with self.assertRaises(firewall.FirewallError) as cm:
                firewall.apply(cfg(CW_NETWORK_SSH_FROM="192.0.2.0/24"), run=lambda c: subprocess.CompletedProcess(c, 0, "", ""), client_address=None,
                               dry_run=False, out=lambda s: None)
        finally:
            firewall.detect_gateway = orig
        self.assertIn("DNS would stop working", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
