import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from armada.setup import cli


def commands():
    return set(cli.build_parser()._subparsers._group_actions[0].choices)


class EverySetupCommandKnowsWhichComputerItActsOn(unittest.TestCase):
    """The standing rule: an arm-setup command that acts on a machine never assumes which one. Every command is classified here, so a new one cannot be added without saying."""

    def test_every_command_is_in_exactly_one_class(self):
        classes = [set(cli.PICKS), set(cli.PICK_COMMANDS), set(cli.NEW_COMMANDS), set(cli.LOCAL_COMMANDS)]
        self.assertEqual(set().union(*classes), commands(), "classify the new command in setup/cli.py")
        self.assertEqual(sum(len(c) for c in classes), len(commands()), "a command is in two classes")

    def test_the_commands_that_act_on_a_worker_take_it_by_name(self):
        sub = cli.build_parser()._subparsers._group_actions[0].choices
        for n in cli.PICKS + cli.PICK_COMMANDS:
            self.assertTrue(any("--worker" in x.option_strings for x in sub[n]._actions), n)

    def test_a_command_about_the_computer_it_runs_on_refuses_a_worker_name(self):
        for n in cli.LOCAL_COMMANDS + cli.NEW_COMMANDS:
            with mock.patch("armada.setup.cli.emit") as e:
                self.assertEqual(cli.main([n, "--worker", "x"]), 2, n)
            self.assertIn("has no --worker", e.call_args[0][0])

    def test_a_worker_name_that_is_nobodys_stops_uninstall_rename_and_transport_before_anything_runs(self):
        with mock.patch("armada.setup.cli.aim_at_worker", return_value=2) as aim:
            for n in cli.PICK_COMMANDS:
                self.assertEqual(cli.main([n, "--worker", "x"]), 2, n)
            self.assertEqual(aim.call_count, len(cli.PICK_COMMANDS))


class InstallGivesTheInstallerTheComputerYouMeantFixture(unittest.TestCase):
    """The glue in main(): the ordinary installer must be handed the picked computer's address, not whatever the saved answers of another machine say."""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.conf = d / "armada.toml"
        self.conf.write_text('[identity]\nname = "armada"\n[server]\nhost = "mini.example"\nadmin_user = "me"\n[paths]\nclient_home = "%s"\n' % (d / ".armada"))
        env = mock.patch.dict(os.environ, {"ARM_CONFIG": str(self.conf), "HOME": str(d)})
        env.start()
        self.addCleanup(env.stop)

    def installer_given(self, argv):
        seen = {}

        class Fake:
            def __init__(inner, *a, **kw):
                seen.update(kw["given"])

            def run_all(inner):
                return [("a job went through the whole loop", "ok", "")]
        with mock.patch("armada.setup.cli.Installer", Fake), mock.patch("armada.setup.cli.emit"):
            rc = cli.main(argv)
        return rc, seen


class InstallGivesTheInstallerTheComputerYouMeant(InstallGivesTheInstallerTheComputerYouMeantFixture):
    def test_the_first_install_named_with_worker_is_installed_at_its_own_address(self):
        rc, given = self.installer_given(["install", "--worker", "armada", "--yes"])
        self.assertEqual((rc, given.get("server.host")), (0, "mini.example"))

    def test_an_address_that_is_the_first_install_is_the_same(self):
        rc, given = self.installer_given(["install", "mini.example", "--yes"])
        self.assertEqual((rc, given.get("server.host")), (0, "mini.example"))

    def test_an_answers_file_that_names_a_host_is_still_taken_as_it_is(self):
        f = Path(tempfile.mkdtemp()) / "a.toml"
        f.write_text('[server]\nhost = "other.example"\n')
        rc, given = self.installer_given(["install", "--answers", str(f), "--yes"])
        self.assertEqual(given.get("server.host"), "other.example")


class ANewComputerIsAskedHowItPassesJobs(InstallGivesTheInstallerTheComputerYouMeantFixture):
    """Seen on a real run: a new computer silently took the first install's transport (GitHub after that was changed), so it asked for a GitHub account the earlier Direct installs never needed, and the answer
    given there (the server's login name) made the repository step fail. The first install's choice is the DEFAULT, never an answer given on the person's behalf."""

    def test_the_new_computers_installer_is_not_given_the_transport_it_is_only_suggested_the_first_ones(self):
        import sys as _sys
        from armada.setup import addworker, assess
        seen = {}

        class Fake:
            def __init__(inner, *a, **kw):
                seen["given"], seen["suggest"], inner.conf = dict(kw["given"]), dict(kw["suggest"]), kw["config_path"]

            def run_all(inner):
                Path(inner.conf).write_text('[identity]\nname = "box"\n[server]\nhost = "192.0.2.77"\n')
                return [("a job went through the whole loop", "ok", "")]
        good = assess.verdict({"OS_ID": "debian", "OS_LIKE": "", "OS_VERSION": "13", "OS_NAME": "Debian 13", "ARCH": "x86_64", "CORES": "2", "MEM_MB": "4000", "DISK_MB": "20000", "PYTHON": "3.13", "SYSTEMD": "yes",
                               "SUDO": "yes", "SUDO_GROUP": "yes", "VIRT": "vmware", "INSTALLS": "", "WHO": "agent", "HOSTNAME": "box"}, "agent")
        self.conf.write_text(self.conf.read_text() + '[transport]\nkind = "github"\n')
        with mock.patch("armada.setup.cli.Installer", Fake), mock.patch("armada.setup.cli.emit"), mock.patch.object(addworker.assess_mod, "assess", lambda h, u, p=22: good), \
                mock.patch("armada.setup.addworker.notice", create=True):
            cli.main(["add-worker", "192.0.2.77", "--yes"])
        self.assertNotIn("transport.kind", seen["given"])                         # so the wizard asks (or, with --yes, takes the default)
        self.assertEqual(seen["suggest"]["transport.kind"], "github")             # and the first install's is what it offers


if __name__ == "__main__":
    unittest.main()
