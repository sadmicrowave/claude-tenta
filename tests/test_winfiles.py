"""The worker's own files on a Windows computer (scope: docs/design/WINDOWS-WORKER-LOOP.md, slice 4d-3a). The PowerShell cannot run here, so its text is checked for the decisions that matter, and the Python around
it is run. The real proof is scripts/win-files-check and the worker loop started as the worker account (docs result)."""
import base64
import re
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ct import config, registry  # noqa: E402
from ct.setup import winfiles, wintools  # noqa: E402


class Config(unittest.TestCase):
    def test_a_windows_configuration_validates_and_uses_forward_slash_folders_under_the_root(self):
        text = winfiles.windows_config("claude-tenta", "192.0.2.10", "me")
        for needle in ('code = "C:/ProgramData/claude-tenta/release"', 'state_root = "C:/ProgramData/claude-tenta/state"', 'config = "C:/ProgramData/claude-tenta/config"', 'kind = "direct"', 'worker_user' if False else 'host = "192.0.2.10"'):
            self.assertIn(needle, text)
        self.assertNotIn("\\", text.replace("\\n", ""))

    def test_it_round_trips_through_the_loader(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "ct.toml").write_text(winfiles.windows_config("claude-tenta", "192.0.2.10", "me", ssh_port=2222))
        c = config.load(d / "ct.toml")
        self.assertEqual((c.server.ssh_port, c.path("queue_clone")), (2222, "C:/ProgramData/claude-tenta/state/queue"))

    def test_a_windows_worker_is_never_given_profile_approval(self):
        self.assertNotIn("profile_approval", winfiles.windows_config("claude-tenta", "192.0.2.10", "me"))

    def test_the_newest_windows_release_loads_the_configuration_this_client_writes(self):
        """Known problem: a release-win tag that predates a setting refuses a config that has it ("transport.profile_approval is not a known setting") and the worker never starts.
        Loads the written file with the config code of the newest release-win tag itself."""
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parent.parent
        tags = subprocess.run(["git", "tag", "--list", "release-win-*"], cwd=root, capture_output=True, text=True).stdout.split()
        if not tags:
            self.skipTest("no release-win tag in this checkout (a shallow clone)")
        tag = max(tags, key=lambda t: [int(x) for x in re.findall(r"\d+", t)])
        d = Path(tempfile.mkdtemp())
        arch = subprocess.run(["git", "archive", tag, "src"], cwd=root, capture_output=True)
        self.assertEqual(arch.returncode, 0, arch.stderr)
        subprocess.run(["tar", "-x", "-C", str(d)], input=arch.stdout, check=True)
        (d / "ct.toml").write_text(winfiles.windows_config("claude-tenta", "192.0.2.10", "me"))
        r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); from ct import config; config.load(sys.argv[2], env={'CT_CONFIG': ''})", str(d / "src"), str(d / "ct.toml")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{tag} cannot load the worker configuration this client writes: {(r.stderr or r.stdout)[-600:]}")

    def test_the_workers_last_error_is_read_from_its_log(self):
        out = 'CT-RESULT {"log": "INFO started\\nERROR The configuration has problems\\n  - transport.profile_approval is not a known setting (known: kind).\\n\\n2026-11-27T20:40:36Z queued job"}'
        said = winfiles.worker_error(lambda argv, text: (0, out))
        self.assertIn("not a known setting", said)
        self.assertNotIn("queued", said)
        self.assertEqual(winfiles.worker_error(lambda argv, text: (255, "no route")), "")

    def test_a_bad_value_is_refused_before_anything_is_sent(self):
        with self.assertRaises(ValueError):
            winfiles.windows_config("claude-tenta", "192.0.2.10", "me", root="C:/Program Files/x")


class Pack(unittest.TestCase):
    def test_the_windows_pack_is_a_valid_registry_for_a_windows_worker(self):
        reg = registry.parse(winfiles.pack_text())
        self.assertEqual(registry.validate(reg, "C:/ProgramData/claude-tenta/release/tools", platform="windows"), [])
        self.assertEqual(sorted(reg["classes"]), ["win-echo", "win-status"])

    def test_every_class_names_its_program_by_full_path_allows_the_network_flag_a_windows_worker_needs_and_needs_no_approval(self):
        for name, c in registry.parse(winfiles.pack_text())["classes"].items():
            self.assertTrue(c["run"][0].startswith("C:\\Windows\\System32\\"), name)
            self.assertIs(c["network"], True, name)
            self.assertEqual(c["tier"], "auto", name)

    def test_the_echo_text_parameter_cannot_carry_a_shell_character(self):
        rx = re.compile(registry.parse(winfiles.pack_text())["classes"]["win-echo"]["params"]["text"]["regex"])
        for ok in ("hello", "a b.c-d_e"):
            self.assertTrue(rx.fullmatch(ok), ok)
        for bad in ("a&b", "a|b", "a>b", "%PATH%", "a^b", 'a"b', "a;b", "", "x" * 61, "a\nb"):
            self.assertIsNone(rx.fullmatch(bad), bad)


class Script(unittest.TestCase):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg, reg = winfiles.windows_config("claude-tenta", "192.0.2.10", "me"), winfiles.pack_text()
        s = winfiles.ensure_script(cfg, reg)

    def test_the_file_texts_travel_as_base64_and_come_out_exactly(self):
        for text in (self.cfg, self.reg):
            self.assertIn(base64.b64encode(text.encode("utf-8")).decode("ascii"), self.s)

    def test_a_file_is_written_only_when_its_bytes_differ_and_without_a_byte_order_mark(self):
        self.assertIn("-eq [BitConverter]::ToString($bytes))) { return $false }", self.s)
        self.assertIn("WriteAllBytes", self.s)
        self.assertNotIn("Set-Content", self.s[self.s.index("try {"):])                    # Set-Content in PowerShell 5.1 would write a different encoding

    def test_the_clones_are_made_once_and_git_is_told_to_trust_exactly_those_two_in_its_own_system_configuration(self):
        self.assertIn("-notcontains $p", self.s)
        self.assertIn("config --system --add safe.directory", self.s)
        self.assertNotIn("--global", self.s)
        self.assertIn("if (-not (Test-Path (Join-Path $dest '.git')))", self.s)

    def test_git_warnings_do_not_stop_the_script_and_a_real_failure_does_with_gits_own_words(self):
        self.assertIn("$ErrorActionPreference = 'Continue'", self.s)
        self.assertIn("git clone of $($c[1]) failed: $o", self.s)

    def test_it_stops_with_a_plain_sentence_when_an_earlier_step_has_not_been_run(self):
        for needle in ("run the tools installer first", "run the layout step first", "run the mailbox step first"):
            self.assertIn(needle, self.s)

    def test_it_never_deletes_anything_and_ends_with_a_blank_line_and_one_result_line(self):
        self.assertNotIn("Remove-Item", self.s[self.s.index("try {"):])
        self.assertTrue(self.s.endswith("}\n\n"))
        self.assertEqual(self.s.count("CT-RESULT "), 1)

    def test_no_double_quoted_string_puts_a_colon_right_after_a_variable_name(self):
        for line in self.s.splitlines():
            self.assertIsNone(re.search(r'"[^"\n]*\$(?!env:)[A-Za-z_]\w*:', line), line)


class Calls(unittest.TestCase):
    def test_ensure_sends_the_script_on_standard_input(self):
        seen = []

        def runner(argv, text):
            seen.append((argv, text))
            return 0, 'CT-RESULT {"changed": false}'
        self.assertFalse(winfiles.ensure(runner, "a = 1\n", "classes: {}\n")["changed"])
        self.assertEqual(seen[0][0], wintools.POWERSHELL)

    def test_a_failure_keeps_the_real_text(self):
        with self.assertRaises(wintools.WinToolsError) as e:
            winfiles.ensure(lambda a, t: (1, 'CT-ERROR {"what": "clone", "detail": "git clone of queue.git failed: fatal: x"}'), "a = 1\n", "classes: {}\n")
        self.assertEqual((e.exception.what, "fatal: x" in e.exception.detail), ("clone", True))


if __name__ == "__main__":
    unittest.main()
