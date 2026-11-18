"""Seen on a real worker: every ssh connection to it took 30 seconds. The worker's IPv6 was switched off, the network's name service still answered with its two old IPv6 addresses, and ssh waited
out a connect timeout (15 s) for each before falling back to IPv4. When the worker has no IPv6 this computer connects over IPv4 only, everywhere; and a step with nothing to print shows a live line."""
import io
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from ct import config, transport
from ct.admin.ssh import Ssh, family_for
from ct.client import view


def cfg(ipv6=None, kind="direct"):
    nested = {"server": {"admin_user": "me", "host": "worker.example"}, "github": {"owner": "o"}, "transport": {"kind": kind}}
    if ipv6:
        nested["network"] = {"ipv6": ipv6}
    clean, problems = config.validate(nested)
    assert not problems, problems
    return config.Config(clean)


class Family(unittest.TestCase):
    def test_only_a_worker_with_ipv6_off_is_reached_over_ipv4_only(self):
        self.assertEqual(family_for(cfg("disabled")), "inet")
        self.assertIsNone(family_for(cfg("enabled")))
        self.assertIsNone(family_for(cfg()))

    def test_the_admin_connection_carries_it(self):
        ssh = Ssh.from_config(cfg("disabled"), control_dir=tempfile.mkdtemp())
        argv = ssh.argv("true", batch=True)
        self.assertIn("AddressFamily=inet", argv)
        self.assertNotIn("AddressFamily", " ".join(Ssh.from_config(cfg("enabled"), control_dir=tempfile.mkdtemp()).argv("true")))

    def test_the_mailbox_connection_carries_it(self):
        d = transport.TRANSPORTS["direct"]
        self.assertIn("-o AddressFamily=inet", d.client_ssh_command(cfg("disabled")))
        self.assertNotIn("AddressFamily", d.client_ssh_command(cfg("enabled")))

    def test_this_computers_own_configuration_for_a_worker_records_the_setting(self):
        """The IPv6 question is the server's, so it used to be missing from this computer's file and the default (enabled) applied: the fix did nothing."""
        from ct.setup import client as client_setup
        self.assertIn("network.ipv6", client_setup.ALSO_FOR_THE_CLIENT)


class BusyLine(unittest.TestCase):
    class Tty(io.StringIO):
        def isatty(self):
            return True

        def fileno(self):
            raise OSError

    def test_a_step_with_nothing_to_print_shows_what_it_is_doing_and_the_line_is_gone_afterwards(self):
        out = self.Tty()
        import time
        with view.Busy("sending: the job to the worker", stream=out, every=0.01):
            time.sleep(0.1)
        text = out.getvalue()
        self.assertIn("SENDING", text.upper())
        self.assertTrue(text.endswith("\r\x1b[K"), repr(text[-20:]))                       # cleared, so the next output starts on a clean line
        self.assertGreater(text.count("SENDING") + text.count("sending"), 1)                  # and it was redrawn while it lasted

    def test_without_a_terminal_it_prints_nothing_and_never_gets_in_the_way(self):
        out = io.StringIO()
        with view.Busy("sending: the job", stream=out):
            pass
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
