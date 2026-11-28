"""Seen on a real worker: every ssh connection to it took 30 seconds. The worker's IPv6 was switched off, the network's name service still answered with its two old IPv6 addresses, and ssh waited
out a connect timeout (15 s) for each before falling back to IPv4. When the worker has no IPv6 this computer connects over IPv4 only, everywhere; and a step with nothing to print shows a live line."""
import io
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401
from armada import config, transport
from armada.admin.ssh import Ssh, family_for
from armada.client import view


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
        from armada.setup import client as client_setup
        self.assertIn("network.ipv6", client_setup.ALSO_FOR_THE_CLIENT)


class WordsFollowTheConnection(unittest.TestCase):
    """Seen on a real Direct worker: the round-trip card said 'next look at GitHub' and 'published to GitHub'. A message names what this install passes jobs through."""
    TRIP = (7, 5, 1, 2, 4)

    def rows(self, direct):
        from armada.client import view
        schedule = cfg(kind="direct" if direct else "github").schedule
        view.set_pace(schedule, direct=direct, store=transport.store_for(cfg(kind="direct" if direct else "github")))
        try:
            return " ".join(v for _, v in view.trip_rows(self.TRIP))
        finally:
            view.set_pace(schedule)

    def test_a_direct_worker_is_never_said_to_wait_on_github(self):
        text = self.rows(True)
        self.assertNotIn("GitHub", text)
        self.assertIn("the mailbox", text)

    def test_a_github_worker_still_says_github(self):
        self.assertIn("GitHub", self.rows(False))

    def test_the_worker_status_words_follow_the_connection_too(self):
        import inspect
        from armada import cli
        src = inspect.getsource(cli)
        for fixed in ("pulling new jobs from GitHub", "waiting to be published to GitHub", "(GitHub or the network", "it asks GitHub every"):
            self.assertNotIn(fixed, src, fixed)                                                  # no sentence of the worker's status names GitHub as a literal: it uses what the install uses
        self.assertEqual(transport.store_for(cfg(kind="direct")), "the mailbox")
        self.assertEqual(transport.store_for(cfg(kind="github")), "GitHub")


class TheAdminAndSandboxWordsFollowTheConnection(unittest.TestCase):
    def test_the_sandbox_check_does_not_say_github_for_a_direct_worker(self):
        from armada import sandboxtest
        for store, want in (("GitHub", "GitHub can be reached over SSH"), ("the mailbox", "the mailbox can be reached")):
            names = [c[0] for c in sandboxtest.checks("/home/w", "/var/lib/x", "/var/lib/x/queue", have=lambda p: True, store=store)]
            self.assertIn(want, names)
        self.assertFalse([n for n in [c[0] for c in sandboxtest.checks("/home/w", "/var/lib/x", "/var/lib/x/queue", have=lambda p: True, store="the mailbox")] if "GitHub" in n])

    def test_the_admin_context_carries_the_words_of_its_connection(self):
        from armada.admin import cli as admin_cli
        ctx = admin_cli.Context.from_config(cfg(kind="direct"))
        self.assertEqual(ctx.store, "the mailbox")
        self.assertEqual(admin_cli.Context.from_config(cfg(kind="github")).store, "GitHub")


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
