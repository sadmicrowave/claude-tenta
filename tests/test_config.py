import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import config  # noqa: E402


def load_text(text, env=None):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ct.toml"
        p.write_text(text)
        return config.load(p, env=env or {})


def problems(text, env=None):
    try:
        load_text(text, env)
    except config.ConfigError as e:
        return e.problems
    return []


class ProfileApproval(unittest.TestCase):
    def test_on_github_a_profile_run_needs_approval_unless_said_otherwise(self):
        c = config.load(None, env={"CT_CONFIG": ""})
        self.assertEqual(c.transport.profile_approval, "required")

    def test_on_direct_a_profile_run_starts_without_approval_unless_the_owner_said_required(self):
        """The decision (list item 3a): the protections that make an unapproved measurement safe exist (a jail that must isolate, the job ceiling, the disk reserve, the
        estimate-based time limit), so Direct does not ask by default. An owner who wrote `required` keeps it."""
        clean, problems = config.validate({"transport": {"kind": "direct"}})
        self.assertEqual((clean["transport"]["profile_approval"], [p for p in problems if "profile_approval" in p]), ("none", []))
        clean, _ = config.validate({"transport": {"kind": "direct", "profile_approval": "required"}})
        self.assertEqual(clean["transport"]["profile_approval"], "required")
        clean, _ = config.validate({"transport": {"kind": "github"}})
        self.assertEqual(clean["transport"]["profile_approval"], "required")

    def test_running_without_approval_is_a_setting_of_the_direct_connection_only(self):
        clean, problems = config.validate({"transport": {"kind": "github", "profile_approval": "none"}})
        self.assertTrue(any("profile_approval" in p and "direct" in p.lower() for p in problems), problems)
        clean, problems = config.validate({"transport": {"kind": "direct", "profile_approval": "none"}})
        self.assertFalse([p for p in problems if "profile_approval" in p], problems)
        clean, problems = config.validate({"transport": {"kind": "direct", "profile_approval": "sometimes"}})
        self.assertTrue([p for p in problems if "profile_approval" in p], problems)


class Defaults(unittest.TestCase):
    def test_built_in_defaults_are_valid_and_complete(self):
        c = config.load(None, env={"CT_CONFIG": ""})
        self.assertEqual((c.identity.name, c.server.worker_user, c.isolation.require_netns), ("claude-tenta", "ctworker", True))
        self.assertEqual((c.hardware.capacity, c.hardware.max_load, c.hardware.max_temp_c), (4, 3.0, 75))
        self.assertEqual(c.alerts.remind_hours, [1, 6, 24])

    def test_paths_follow_the_name(self):
        c = load_text('[identity]\nname = "homect"\n[server]\nworker_user = "runner1"\n')
        self.assertEqual((c.paths.code, c.paths.state_root, c.paths.config, c.paths.worker_home),
                         ("/opt/homect", "/var/lib/homect", "/etc/homect", "/home/runner1"))
        self.assertEqual(c.path("state"), "/var/lib/homect/state")
        self.assertEqual(c.unit("step.timer"), "homect-step.timer")

    def test_hardware_profile_fills_limits_and_explicit_values_win(self):
        self.assertEqual(load_text('[hardware]\nprofile = "old-laptop"\n').hardware.capacity, 2)
        c = load_text('[hardware]\nprofile = "workstation"\ncapacity = 3\n')
        self.assertEqual((c.hardware.capacity, c.hardware.max_load, c.hardware.max_temp_c), (3, 8.0, 80))

    def test_the_config_is_read_only(self):
        c = config.load(None, env={})
        with self.assertRaises(AttributeError):
            c.server.host = "x"
        with self.assertRaises(AttributeError):
            c.server.nothing


class Strictness(unittest.TestCase):
    def test_unknown_section_and_key_are_errors(self):
        p = " ".join(problems('[servr]\nhost = "x"\n[server]\nhots = "x"\n'))
        self.assertIn("[servr] is not a known section", p)
        self.assertIn("server.hots is not a known setting", p)

    def test_secrets_are_refused_with_a_pointer_to_secrets_env(self):
        p = " ".join(problems('[alerts]\napi_token = "abc"\n'))
        self.assertIn("secrets.env", p)

    def test_values_that_could_reach_a_shell_are_refused(self):
        bad = {
            '[server]\nhost = "a b"\n': "server.host",
            '[server]\nhost = "x; rm -rf /"\n': "server.host",
            '[server]\nadmin_user = "Bob Smith"\n': "server.admin_user",
            '[server]\nadmin_user = "root"\n': "system account",
            '[github]\nowner = "me/../you"\n': "github.owner",
            '[github]\nqueue_repo = ".."\n': "github.queue_repo",
            '[github]\njobs_branch = "-x"\n': "github.jobs_branch",
            '[paths]\ncode = "/opt/x y"\n': "paths.code",
            '[paths]\ncode = "/opt/../etc"\n': "paths.code",
            '[paths]\ncode = "relative/path"\n': "paths.code",
            '[paths]\nstate_root = "/var/lib"\n': "system folder",
            '[paths]\nconfig = "/etc/$(id)"\n': "paths.config",
            '[alerts]\nkind = "ntfy"\nurl = "http://insecure.example.com/x"\n': "https",
            '[alerts]\nkind = "ntfy"\nurl = "https://ex.com/a b"\n': "alerts.url",
            '[network]\nlan_allow_out = ["not-a-network"]\n': "network.lan_allow_out",
            '[network]\ndns_servers = ["1.2.3"]\n': "network.dns_servers",
            '[hardening]\nmodules = ["ok", "bad name"]\n': "hardening.modules",
        }
        for text, needle in bad.items():
            self.assertIn(needle, " ".join(problems(text)), text)

    def test_types_are_strict(self):
        for text, needle in (('[server]\nssh_port = "22"\n', "whole number"), ('[server]\nssh_port = true\n', "whole number"),
                             ('[server]\nssh_port = 70000\n', "between"), ('[isolation]\nrequire_netns = "yes"\n', "true or false"),
                             ('[hardware]\nprofile = "huge"\n', "one of"), ('[alerts]\nremind_hours = [6, 1]\n', "increasing")):
            self.assertIn(needle, " ".join(problems(text)), text)

    def test_cross_checks(self):
        self.assertIn("different accounts", " ".join(problems('[server]\nadmin_user = "same"\nworker_user = "same"\n')))
        self.assertEqual(problems('[alerts]\nkind = "ntfy"\n'), [])                     # the worker loads it: the address is in secrets.env
        clean, errs = config.validate({"alerts": {"kind": "ntfy"}})                       # but the wizard's own check still insists on one
        self.assertIn("alerts.url is required", " ".join(errs))
        self.assertIn("three different folders", " ".join(problems('[paths]\ncode = "/opt/x"\nconfig = "/opt/x"\n')))
        self.assertIn("is inside", " ".join(problems('[paths]\ncode = "/srv/ct"\nstate_root = "/srv/ct/state"\n')))

    def test_isolation_mode_defaults_to_full_and_tool_roots_need_relaxed_and_a_plain_absolute_folder(self):
        self.assertEqual(problems(""), [])
        c = config.load(None, env={})
        self.assertEqual((c.isolation.mode, c.isolation.tool_roots), ("full", []))
        self.assertIn("tool_roots needs isolation.mode", " ".join(problems('[isolation]\ntool_roots = ["/opt/tools"]\n')))
        self.assertEqual(problems('[isolation]\nmode = "relaxed"\ntool_roots = ["/opt/tools", "/srv/pentest"]\n'), [])        # any number
        for bad in ('"~/tools"', '"relative"', '"/etc"', '"/opt/a b"'):
            self.assertTrue(problems('[isolation]\nmode = "relaxed"\ntool_roots = [%s]\n' % bad), bad)
        self.assertTrue(problems('[isolation]\nmode = "loose"\n'))

    def test_availability_windows_and_accounts_are_checked(self):
        self.assertEqual(problems('[availability]\npause_when_logged_in = true\nignore_users = ["agent"]\nwindow = "22:00-07:00"\n'), [])
        self.assertEqual(problems(""), [])
        for bad in ('window = "10pm-7am"', 'window = "25:00-07:00"', 'window = "22:00"', 'ignore_users = ["a b"]', 'pause_when_logged_in = "yes"'):
            self.assertTrue(problems("[availability]\n" + bad + "\n"), bad)
        c = config.load(None, env={})
        self.assertEqual((c.availability.pause_when_logged_in, c.availability.window), (False, ""))

    def test_unsafe_isolation_is_refused_unless_explicitly_overridden(self):
        text = "[isolation]\nrequire_netns = false\n"
        self.assertIn("require_netns = false", " ".join(problems(text)))
        os.environ["CT_ALLOW_UNSAFE"] = "1"
        try:
            self.assertEqual(problems(text), [])
        finally:
            del os.environ["CT_ALLOW_UNSAFE"]

    def test_every_problem_is_reported_at_once(self):
        self.assertGreaterEqual(len(problems('[server]\nhost = "a b"\nssh_port = 0\n[github]\nowner = "x/y"\n')), 3)

    def test_bad_toml_and_missing_file(self):
        self.assertIn("not valid TOML", " ".join(problems("[server\nhost = 1")))
        with self.assertRaises(config.ConfigError):
            config.load("/nonexistent/ct.toml", env={})


class Environment(unittest.TestCase):
    def test_environment_overrides_the_file_and_is_validated(self):
        c = load_text('[server]\nhost = "from-file.example.com"\nssh_port = 22\n',
                      env={"CT_SERVER_HOST": "from-env.example.com", "CT_SERVER_SSH_PORT": "2222", "CT_HARDWARE_MAX_LOAD": "2.5",
                           "CT_ALERTS_REMIND_HOURS": "2,12", "CT_NETWORK_LAN_DENY_OUT": "false"})
        self.assertEqual((c.server.host, c.server.ssh_port, c.hardware.max_load, c.alerts.remind_hours, c.network.lan_deny_out),
                         ("from-env.example.com", 2222, 2.5, [2.0, 12.0], False))
        self.assertIn("server.host", " ".join(problems("", env={"CT_SERVER_HOST": "bad host!"})))


class Requirements(unittest.TestCase):
    def test_require_names_everything_missing(self):
        c = config.load(None, env={})
        with self.assertRaises(config.ConfigError) as cm:
            c.require("server.host", "github.owner")
        self.assertEqual(len(cm.exception.problems), 2)
        with self.assertRaises(config.ConfigError):
            c.queue_url
        c2 = load_text('[github]\nowner = "alice"\n')
        self.assertEqual(c2.queue_url, "git@github.com:alice/claude-tenta-queue.git")
        self.assertEqual(c2.results_url, "git@github.com:alice/claude-tenta-results.git")


class ExampleFile(unittest.TestCase):
    path = ROOT / "config" / "ct.example.toml"

    def test_the_example_loads_cleanly(self):
        c = config.load(self.path, env={})
        self.assertEqual(c.server.admin_user, "ctadmin")

    def test_every_setting_is_documented_in_the_example(self):
        text = self.path.read_text()
        sections = set(re.findall(r"^\[(\w+)\]", text, re.M))
        self.assertEqual(sections, set(config.SCHEMA))
        for section, fields in config.SCHEMA.items():
            block = re.search(rf"^\[{section}\](.*?)(?=^\[|\Z)", text, re.M | re.S).group(1)
            for key in fields:
                self.assertRegex(block, rf"(?m)^#?\s*{key}\s*=", f"{section}.{key} is missing from the example")

    def test_the_example_holds_no_real_looking_details(self):
        text = self.path.read_text()
        generic = text.replace("10.0.0.0/8", "").replace("172.16.0.0/12", "").replace("192.168.0.0/16", "")   # the three standard private ranges
        self.assertNotRegex(generic, r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+\.\d+\b")


class Secrets(unittest.TestCase):
    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "secrets.env"
            p.write_text('# comment\nCT_ALERT_TOKEN="tk_x"\n\nANTHROPIC_API_KEY=abc=def\nnot a pair\n')
            self.assertEqual(config.read_env_file(p), {"CT_ALERT_TOKEN": "tk_x", "ANTHROPIC_API_KEY": "abc=def"})
            self.assertEqual(config.read_env_file(Path(d) / "missing"), {})

    def test_a_world_readable_secrets_file_is_a_problem(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "secrets.env"
            p.write_text("A=b\n")
            p.chmod(0o644)
            self.assertIn("chmod 640", config.secrets_file_problem(p))
            p.chmod(0o640)
            self.assertIsNone(config.secrets_file_problem(p))


class Cli(unittest.TestCase):
    def test_check_and_show(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(config.main(["check", str(ExampleFile.path)]), 0)
            self.assertEqual(config.main(["show", str(ExampleFile.path)]), 0)
        self.assertIn("OK", out.getvalue())
        self.assertIn("[server]", out.getvalue())
        with tempfile.TemporaryDirectory() as d, redirect_stderr(err):
            bad = Path(d) / "ct.toml"
            bad.write_text('[server]\nhost = "a b"\n')
            self.assertEqual(config.main(["check", str(bad)]), 1)
        self.assertIn("server.host", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class LegacyInstall(unittest.TestCase):
    """An install made before the project was renamed keeps working: its settings are in cw.toml in the old folder, its clones and keys in ~/.cw."""

    def test_the_old_settings_file_is_found_and_its_client_folder_is_the_old_one(self):
        legacy = config.legacy_config_paths()[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('[identity]\nname = "claude-ballast"\n')
        try:
            self.assertEqual(config.default_config_path({}), legacy)
            c = config.load(None, env={})
            self.assertEqual((c.identity.name, c.paths.client_home), ("claude-ballast", "~/.cw"))
            new = Path.home() / ".config" / "claude-tenta" / "ct.toml"
            new.parent.mkdir(parents=True, exist_ok=True)
            new.write_text('[identity]\nname = "claude-tenta"\n')
            self.assertEqual(config.default_config_path({}), new)                        # the new place wins once it exists
            self.assertEqual(config.load(None, env={}).paths.client_home, "~/.ct")
        finally:
            for p in (legacy, Path.home() / ".config" / "claude-tenta" / "ct.toml"):
                p.unlink(missing_ok=True)

    def test_an_old_file_that_never_named_the_install_means_the_old_default_name_and_its_repositories(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cw.toml"
            p.write_text('[github]\nowner = "someone"\n')
            c = config.load(p, env={})
            self.assertEqual((c.identity.name, c.github.queue_repo, c.github.results_repo, c.paths.code),
                             ("claude-ballast", "claude-ballast-queue", "claude-ballast-results", "/opt/claude-ballast"))
            q = Path(d) / "ct.toml"
            q.write_text('[github]\nowner = "someone"\n')
            self.assertEqual(config.load(q, env={}).identity.name, "claude-tenta")          # a current file keeps the new default

    def test_an_old_file_that_never_named_the_worker_account_means_the_account_it_had(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cw.toml"
            p.write_text('[github]\nowner = "someone"\n')
            self.assertEqual(config.load(p, env={}).server.worker_user, "cwworker")
            q = Path(d) / "ct.toml"
            q.write_text('[github]\nowner = "someone"\n')
            self.assertEqual(config.load(q, env={}).server.worker_user, "ctworker")

    def test_an_explicit_client_folder_in_an_old_file_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cw.toml"
            p.write_text('[paths]\nclient_home = "~/elsewhere"\n')
            self.assertEqual(config.load(p, env={}).paths.client_home, "~/elsewhere")
