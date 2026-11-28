"""The Direct transport's mailbox on a Windows worker (scope: docs/design/WINDOWS-WORKER-LOOP.md, slice 4d-2). The PowerShell cannot run here, so its text is checked for the decisions that matter, and the
Python around it is run. The real proof is scripts/win-mailbox-check against a Windows computer."""
import re
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from armada.setup import winmailbox, wintools  # noqa: E402

KEY = "ssh-" + "ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAIHydw7kP9aelENfu8kRyX7cP0ynpRXnqVcLiyRjZ75AD" + " mailbox-check"          # built in pieces: this file must not itself look like a leak


class Script(unittest.TestCase):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        s = winmailbox.ensure_script(key=KEY)
        r = winmailbox.remove_script()

    def test_the_key_may_run_only_the_launcher_with_every_other_ssh_feature_off(self):
        self.assertIn("'restrict,command=\"' + $launcher + '\" ' + $key", self.s)

    def test_the_launcher_puts_the_tools_git_on_a_path_of_its_own_and_starts_the_mail_shell_with_the_mailbox_named(self):
        for needle in (r"tools\git\cmd", "%SystemRoot%\\system32", "PYTHONPATH", "ARM_MAILBOX", "-m armada.mailshell"):
            self.assertIn(needle, self.s)

    def test_the_mail_account_is_standard_cannot_log_on_at_a_desktop_and_is_never_an_administrator(self):
        self.assertIn("Remove-LocalGroupMember -SID 'S-1-5-32-544'", self.s)
        self.assertIn("SeDenyInteractiveLogonRight", self.s)
        self.assertIn("SeDenyRemoteInteractiveLogonRight", self.s)
        self.assertNotIn("SeDenyNetworkLogonRight", self.s)                           # ssh logons must keep working

    def test_the_mail_account_writes_the_queue_and_only_reads_the_results_and_the_worker_the_other_way_round(self):
        self.assertIn('Acl (Join-Path $box \'queue.git\') @("${u}:(OI)(CI)M", "${w}:(OI)(CI)RX")', self.s)
        self.assertIn('Acl (Join-Path $box \'results.git\') @("${w}:(OI)(CI)M", "${u}:(OI)(CI)RX")', self.s)

    def test_the_mail_account_can_read_only_the_tools_the_release_and_its_own_folder_not_the_workers_state_or_config(self):
        self.assertIn("foreach ($d in @('tools', 'release'))", self.s)
        self.assertNotIn("'config'", self.s.replace("run the layout step", ""))
        self.assertNotIn("state')", self.s.replace("state-mail", ""))

    def test_sshd_config_is_edited_between_markers_backed_up_checked_with_sshd_t_and_put_back_when_it_fails(self):
        for needle in (winmailbox.BEGIN, winmailbox.END, ".arm-backup", "sshd.exe') -t", "put back", "Restart-Service sshd", "AuthorizedKeysFile"):
            self.assertIn(needle, self.s)
        self.assertLess(self.s.index("sshd.exe') -t"), self.s.index("Restart-Service sshd"))

    def test_git_is_told_to_trust_exactly_the_two_repositories_in_its_own_system_configuration(self):
        self.assertIn("config --system --add safe.directory", self.s)
        self.assertNotIn("safe.directory '*'", self.s)
        self.assertNotIn("--global", self.s)

    def test_a_second_run_changes_nothing_because_each_step_compares_before_it_writes(self):
        for needle in ("(Sddl $path) -ne $before", "-notcontains $p", "(Get-Content $launcher -Raw) -ne $text", "-ne $line", "if ($new -ne $cur)"):
            self.assertIn(needle, self.s)

    def test_remove_undoes_the_account_the_block_and_the_trust_but_keeps_the_mailbox_contents(self):
        for needle in ("Remove-LocalUser", "$rx.Replace($cur, '')", "--unset-all safe.directory", "mailbox_kept"):
            self.assertIn(needle, self.r)
        self.assertNotIn("Remove-Item", self.r[self.r.index("try {"):])                    # the mailbox folder and its contents are not deleted

    def test_every_script_ends_with_a_blank_line_prints_one_result_line_and_never_prints_a_password(self):
        for text in (self.s, self.r):
            self.assertTrue(text.endswith("}\n\n"))
            self.assertEqual(text.count("CT-RESULT "), 1)
            self.assertNotIn("Write-Output $pw", text)

    def test_no_double_quoted_string_puts_a_colon_right_after_a_variable_name(self):
        for text in (self.s, self.r):
            for line in text.splitlines():
                self.assertIsNone(re.search(r'"[^"\n]*\$(?!env:)[A-Za-z_]\w*:', line), line)


class Inputs(unittest.TestCase):
    def test_the_mail_account_name_must_fit_a_windows_account(self):
        self.assertEqual(winmailbox.mail_user("armada"), "armada-mail")
        with self.assertRaises(ValueError):
            winmailbox.mail_user("a-very-long-install-name")

    def test_only_one_plain_public_key_line_gets_into_the_script(self):
        for bad in ("ssh-ed25519", "ssh-ed25519 AAAA", KEY + "'; calc", KEY + "\nssh-ed25519 " + "A" * 40, 'command="x" ' + KEY, "ssh-dss " + "A" * 40, KEY.replace("mailbox-check", "a b")):
            with self.assertRaises(ValueError, msg=bad):
                winmailbox.ensure_script(key=bad)
        winmailbox.ensure_script(key=KEY)
        winmailbox.ensure_script(key=KEY.rsplit(" ", 1)[0])                             # a key without a comment

    def test_a_plain_folder_and_account_only(self):
        with self.assertRaises(ValueError):
            winmailbox.ensure_script(root="C:\\x'y")
        with self.assertRaises(ValueError):
            winmailbox.ensure_script(worker="Bad Name")


class Calls(unittest.TestCase):
    def test_ensure_sends_the_script_on_standard_input_and_reads_the_result(self):
        seen = []

        def runner(argv, text):
            seen.append((argv, text))
            return 0, 'CT-RESULT {"changed": true, "user": "armada-mail"}'
        self.assertTrue(winmailbox.ensure(runner, key=KEY)["changed"])
        self.assertEqual(seen[0][0], wintools.POWERSHELL)
        self.assertIn(KEY, seen[0][1])

    def test_a_failure_keeps_the_real_text(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            winmailbox.ensure(lambda a, t: (1, 'CT-ERROR {"what": "sshd", "detail": "sshd_config did not pass sshd -t, so it was put back: line 3"}'), key=KEY)
        self.assertEqual((e.exception.what, "line 3" in e.exception.detail), ("sshd", True))

class RemoveAfterTheToolsAreGone(unittest.TestCase):
    def test_taking_out_the_git_trust_is_skipped_when_git_is_already_gone_so_a_second_removal_does_not_fail(self):
        s = winmailbox.remove_script()
        self.assertLess(s.index("Test-Path -LiteralPath $git"), s.index("config --system --get-all"))


if __name__ == "__main__":
    unittest.main()

