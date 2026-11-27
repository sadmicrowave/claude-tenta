"""ct-admin: SSH wrapper, worker cards, registry text edits and help text (no git, no keys)."""
import io
from types import SimpleNamespace
import tempfile
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import admin_support  # noqa: F401  (points HOME at a throwaway folder)

from ct import approvals, theme
from ct.admin import cli, regedit
from ct.admin import workers as workers_mod
from ct.admin.ssh import Ssh
from ct.client.repo import ClientError


class SshTests(unittest.TestCase):
    def test_argv_is_key_only_and_short_lived(self):
        a = Ssh("server.example", "admin", 2222, control_dir="/tmp/ct-test-ctl").argv("true", batch=True, tty=False)
        self.assertEqual(a[0], "ssh")
        self.assertIn("BatchMode=yes", a)
        self.assertIn("ControlPersist=60", a)
        self.assertIn("admin" + "@" + "server.example", a)
        self.assertEqual(a[a.index("-p") + 1], "2222")
        self.assertEqual(a[-1], "true")
        self.assertNotIn("-t", a)
        self.assertIn("-t", Ssh("h", control_dir="/tmp/ct-test-ctl").argv("x", tty=True))

    def test_a_session_sends_keep_alives_so_a_stalled_path_is_noticed_or_woken(self):
        a = Ssh("h", control_dir="/tmp/ct-test-ctl").argv("x", tty=True)
        self.assertIn("ServerAliveInterval=10", a)
        self.assertIn("ServerAliveCountMax=6", a)

    def test_the_control_socket_path_fits_a_unix_socket_even_with_macos_style_temp_folders(self):
        import tempfile
        long_tmp = "/var/folders/k7/nb4y713s0dx6nd0d_zvfcqt40000gp/T"
        with unittest.mock.patch.object(tempfile, "gettempdir", return_value=long_tmp), unittest.mock.patch.dict(os.environ, {"TMPDIR": long_tmp}):
            s = Ssh("h", "u", control_dir=None)
            control = [a for a in s.argv("x") if a.startswith("ControlPath=")][0].split("=", 1)[1]
        # %C is 40 characters, and ssh appends a dot and 16 more while it connects
        self.assertLessEqual(len(control.replace("%C", "x" * 40)) + 17, 103, control)

    def test_an_address_that_looks_like_an_option_is_refused(self):
        with self.assertRaises(ClientError):
            Ssh("-oProxyCommand=evil")

    def test_a_failed_connection_is_explained(self):
        s = Ssh("h", run=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "Permission denied (publickey)"), control_dir="/tmp/ct-test-ctl")
        with self.assertRaises(ClientError) as c:
            s.capture("true")
        self.assertIn("Permission denied", c.exception.why)

    def test_arguments_are_quoted(self):
        self.assertEqual(Ssh.quote(["approve", "a b", "x;y"]), "approve 'a b' 'x;y'")

    def test_a_connection_that_fails_under_a_terminal_command_is_a_card_not_raw_ssh_text(self):
        """Found by the output audit: `ct-admin status` and `quarantine` printed ssh's own line ("Could not resolve hostname ...") bare, where `check` drew an error card."""
        said = "ssh: Could not resolve hostname worker2.example: nodename nor servname provided, or not known"
        seen = {}
        s = Ssh("h", run=lambda argv, **k: seen.update(k) or subprocess.CompletedProcess(argv, 255, "", said + "\n"), control_dir="/tmp/ct-test-ctl")
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(ClientError) as c:
            s.interactive("ct-worker status")
        self.assertIn("Could not resolve", c.exception.why)
        self.assertEqual(err.getvalue(), "")                                                   # nothing printed bare
        self.assertEqual(seen.get("stderr"), subprocess.PIPE)

    def test_the_remote_commands_own_failure_and_text_are_passed_through(self):
        s = Ssh("h", run=lambda argv, **k: subprocess.CompletedProcess(argv, 2, "", "usage: ct-worker x\n"), control_dir="/tmp/ct-test-ctl")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(s.interactive("ct-worker x"), 2)
        self.assertIn("usage: ct-worker x", err.getvalue())

    def test_status_for_a_name_that_is_not_a_worker_draws_nothing_before_refusing(self):
        drawn = []
        ctx = SimpleNamespace(targets=lambda name=None: (_ for _ in ()).throw(ClientError("There is no such worker")))
        with unittest.mock.patch.object(cli, "status_overview", side_effect=lambda *a, **k: drawn.append(1)):
            with self.assertRaises(ClientError):
                cli.run_command(ctx, SimpleNamespace(cmd="status", worker="nope", every=None))
        self.assertEqual(drawn, [], "the panel was drawn before the name was checked")


ANSI_ = __import__("re").compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class WorkerCards(unittest.TestCase):
    ROW = {"name": "testct", "os": "ubuntu 26.04", "cpu": "2 cores", "memory": "14.0 of 14.9 GB free", "state": "jail ON",
           "release": "b3861ea", "load": "0.31 / 52 C", "age": "3 min ago", "locked": False, "stale": False, "packages": 1, "classes": 2, "class_names": ["alpha", "beta"]}

    def test_the_card_names_the_platform_only_when_it_is_not_linux(self):
        win = workers_mod.cards([{**self.ROW, "platform": "windows"}], width=90, color=False)
        self.assertTrue(any(l.startswith("  PLATFORM") and "windows" in l for l in win), win)
        for usual in ("", "linux"):
            self.assertFalse(any("PLATFORM" in l for l in workers_mod.cards([{**self.ROW, "platform": usual}], width=90, color=False)))

    def test_a_held_shared_computer_says_why_in_its_card(self):
        plain = workers_mod.cards([{**self.ROW, "purpose": "shared with other uses", "held": "a person is using this computer (alice)"}], width=90, color=False)
        self.assertTrue(any(l.startswith("  AVAILABILITY") and "held: a person is using this computer (alice)" in l for l in plain))
        self.assertFalse(any("AVAILABILITY" in l for l in workers_mod.cards([self.ROW], width=90, color=False)))

    def test_what_the_computer_is_used_for_is_a_row_and_a_relaxed_jail_is_said_in_the_state(self):
        doc = dict(self.DOC) if hasattr(self, "DOC") else None
        row = workers_mod.row_for("lab", {**(doc or {"name": "lab", "hardware": {}, "observed": {}, "os": {}}), "isolation": {"ready": True, "mode": "relaxed", "purpose": "special-use"}})
        self.assertEqual((row["state"], row["purpose"]), ("jail ON, relaxed", "special use"))
        plain = workers_mod.cards([{**self.ROW, "purpose": "special use"}], width=80, color=False)
        self.assertTrue(any(l.startswith("  PURPOSE") and l.endswith("special use") for l in plain))
        self.assertFalse(any("PURPOSE" in l for l in workers_mod.cards([self.ROW], width=80, color=False)))          # an older release says nothing about it: no row

    def test_each_worker_is_a_card_with_a_rail_a_chip_and_labelled_rows_in_the_wizards_style(self):
        lines = workers_mod.cards([self.ROW], width=80, color=False)                       # plain (a pipe): the name, then labelled rows
        self.assertEqual(lines[0], "TESTCT")
        for label in ("OS", "CPU", "MEMORY", "STATE", "RELEASE", "LOAD", "READING", "CLASSES", "PACKAGES"):
            self.assertTrue(any(l.startswith("  " + label) for l in lines), label)
        self.assertTrue(any(l.startswith("  CLASSES") and l.endswith("alpha") for l in lines))                   # the classes by name, one per line
        self.assertTrue(any(l.strip() == "beta" for l in lines))
        self.assertTrue(any(l.startswith("  PACKAGES") and l.endswith("1 package") for l in lines))
        width = workers_mod.HANG                                                                                  # the label column, whatever its width
        values = {l[2:2 + width].strip(): l[2 + width:] for l in lines if l.startswith("  ") and len(l) > 2 + width}
        self.assertEqual(values["MEMORY"], "14.0 of 14.9 GB free")
        framed = [ANSI_.sub("", x) for x in workers_mod.cards([self.ROW], width=80, color=True)]            # on a terminal: one framed card per worker
        self.assertTrue(any(x.startswith("╭─ testct") for x in framed))
        self.assertTrue(any(x.startswith("│ CLASSES") for x in framed))

    def test_no_line_is_longer_than_the_card_width_so_nothing_wraps(self):
        long = dict(self.ROW, memory="x" * 200)
        lines = workers_mod.cards([long], width=60, color=False)
        self.assertTrue(all(len(l) <= 60 for l in lines), [len(l) for l in lines])
        self.assertTrue(any("…" in l for l in lines))

    def test_cards_are_stacked_with_a_blank_line_between_and_a_locked_one_says_so(self):
        locked = dict(self.ROW, name="studio", locked=True)
        lines = workers_mod.cards([self.ROW, locked], width=80, color=False)
        self.assertIn("", lines)
        self.assertIn("STUDIO (LOCKED)", lines)

    def test_colour_gives_the_frame_the_colour_of_its_state(self):
        blue = "\n".join(workers_mod.cards([self.ROW], width=80, color=True))
        self.assertIn(theme.code("primary"), blue)
        self.assertIn("38;5;203", "\n".join(workers_mod.cards([dict(self.ROW, locked=True)], width=80, color=True)))          # red while locked
        self.assertIn(theme.code("warn"), "\n".join(workers_mod.cards([dict(self.ROW, stale=True)], width=80, color=True)))    # yellow while its reading is stale


class RegistryTextEdits(unittest.TestCase):
    def test_entries_go_into_an_existing_section_or_make_one(self):
        from ct.admin import regedit
        text = "classes:\n  a:\n    kind: command\n"
        out = regedit.add_entries(text, "actions", ["  x:", "    run: []"])
        self.assertTrue(out.endswith("actions:\n  x:\n    run: []\n"))
        out = regedit.add_entries(out, "actions", ["  y:", "    run: []"])
        self.assertEqual(regedit.action_names(out), ["x", "y"])
        self.assertEqual(regedit.add_entries("actions: {}\nclasses:\n", "actions", ["  x:"]).splitlines()[:2], ["actions:", "  x:"])

    def test_a_pin_is_replaced_in_the_right_action_only(self):
        from ct.admin import regedit
        text = 'actions:\n  x:\n    sha256: "' + "1" * 64 + '"\n  y:\n    sha256: "' + "2" * 64 + '"\nclasses:\n'
        out = regedit.set_action_pin(text, "y", "3" * 64)
        self.assertIn("1" * 64, out)
        self.assertNotIn("2" * 64, out)
        self.assertIsNone(regedit.set_action_pin(text, "z", "3" * 64))


class FollowAfterApproving(unittest.TestCase):
    def test_the_command_ct_admin_runs_to_follow_a_job_is_one_the_client_accepts(self):
        """ct-admin follows an approved job with `result <id> --wait --approved`; the client's parser once refused that flag and the person saw a usage error."""
        from ct.client import cli as client_cli
        ap, _ = client_cli.build_parser()
        a = ap.parse_args(["result", "job-1", "--wait", "--approved"])
        self.assertTrue(a.approved and a.wait)
        import re
        src = Path(cli.__file__).read_text()
        self.assertIn('"result", jid, "--wait", "--approved"', src)                       # the call this test mirrors


class SystemBranchesAreNotProposals(unittest.TestCase):
    """Seen on the real fleet: the branch that holds the signed approvals was listed as '9 to review', and `merge --all` or `tidy` would have treated it as a change."""

    def test_the_approvals_branch_is_reserved_like_main_and_jobs(self):
        from ct import approvals
        from ct.admin import proposals
        self.assertIn(approvals.BRANCH, proposals.RESERVED)
        p = proposals.Proposals(SimpleNamespace(git=lambda repo, *a, **k: SimpleNamespace(stdout="main jobs approvals feature-x registry/ssh-version\n")))
        p.repos.queue = "q"
        self.assertEqual(p.branches(), ["feature-x", "registry/ssh-version"])
        with self.assertRaises(ClientError):
            p.check_name("approvals")


class RegistryEditTests(unittest.TestCase):
    def test_a_class_and_its_comments_are_removed_and_nothing_else(self):
        text = "classes:\n  # keep\n  a:\n    tier: auto\n\n  # about b\n  b:\n    tier: auto\n    run: [x]\n\nlimits:\n  x: 1\n"
        out = regedit.without_class(text, "b")
        self.assertNotIn("about b", out)
        self.assertNotIn("  b:", out)
        self.assertIn("  a:", out)
        self.assertIn("limits:", out)
        self.assertIsNone(regedit.without_class(text, "zzz"))
        self.assertEqual(regedit.class_names(text), ["a", "b"])

    def test_profile_classes_are_recognised(self):
        self.assertTrue(regedit.is_profile_kind("classes:\n  profile-run:\n    kind: profile\n    cost: heavy\n", "profile-run"))
        self.assertFalse(regedit.is_profile_kind("classes:\n  a:\n    tier: auto\n", "a"))


class HelpTests(unittest.TestCase):
    def test_help_groups_the_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["help"])
        text = out.getvalue()
        self.assertEqual(code, 0)
        for heading in ("WORKER", "JOBS", "SECURITY", "REGISTRY", "RELEASE"):
            self.assertIn(heading, text)
        self.assertLess(text.index("WORKER"), text.index("JOBS"))
        self.assertLess(text.index("REGISTRY"), text.index("RELEASE"))
        for c in ("status", "approve", "unlock", "proposals", "apply"):
            self.assertIn(c, text)
        for gone in ("login", "logout", "branches", "tidy "):
            self.assertNotIn(f"\n  {gone}", text)

    def test_every_command_has_help_and_is_in_a_group(self):
        grouped = [c for _, names in cli.GROUPS for c in names]
        self.assertEqual(sorted(grouped), sorted(cli.HELP))
        self.assertEqual(sorted(cli.DISPATCH), sorted(cli.HELP))

    def test_per_command_help_and_unknown_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["help", "apply"]), 0)
        self.assertIn("--no-verify", out.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.main(["login"]), 2)


class RegeditSetFields(unittest.TestCase):
    TEXT = "classes:\n  # about a\n  a:\n    tier: approve\n    run: [/bin/a]\n\n  b:\n    tier: auto\n    tools:\n      - /opt/x\n      - /opt/y\n    run: [/bin/b]\n"

    def test_a_line_is_replaced_in_place_a_new_one_is_added_at_the_end_and_comments_stay(self):
        out = regedit.set_class_fields(self.TEXT, "a", {"tier": "auto", "network": True})
        self.assertIn("  # about a\n  a:\n    tier: auto\n    run: [/bin/a]\n    network: true\n", out)
        self.assertIn("  b:\n    tier: auto", out)                                       # the other class is untouched

    def test_a_list_written_under_the_setting_is_replaced_whole_and_none_removes_it(self):
        out = regedit.set_class_fields(self.TEXT, "b", {"tools": ["/opt/z"]})
        self.assertIn("    tools: [/opt/z]\n    run: [/bin/b]\n", out)
        self.assertNotIn("- /opt/x", out)
        self.assertNotIn("tools", regedit.set_class_fields(self.TEXT, "b", {"tools": None}))
        self.assertIsNone(regedit.set_class_fields(self.TEXT, "nope", {"tier": "auto"}))


if __name__ == "__main__":
    unittest.main()
