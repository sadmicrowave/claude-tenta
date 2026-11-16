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
from cw import config  # noqa: E402


def load_text(text, env=None):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cw.toml"
        p.write_text(text)
        return config.load(p, env=env or {})


def problems(text, env=None):
    try:
        load_text(text, env)
    except config.ConfigError as e:
        return e.problems
    return []


class Defaults(unittest.TestCase):
    def test_built_in_defaults_are_valid_and_complete(self):
        c = config.load(None, env={"CW_CONFIG": ""})
        self.assertEqual((c.identity.name, c.server.worker_user, c.isolation.require_netns), ("claude-ballast", "cwworker", True))
        self.assertEqual((c.hardware.capacity, c.hardware.max_load, c.hardware.max_temp_c), (4, 3.0, 75))
        self.assertEqual(c.alerts.remind_hours, [1, 6, 24])

    def test_paths_follow_the_name(self):
        c = load_text('[identity]\nname = "homecw"\n[server]\nworker_user = "runner1"\n')
        self.assertEqual((c.paths.code, c.paths.state_root, c.paths.config, c.paths.worker_home),
                         ("/opt/homecw", "/var/lib/homecw", "/etc/homecw", "/home/runner1"))
        self.assertEqual(c.path("state"), "/var/lib/homecw/state")
        self.assertEqual(c.unit("step.timer"), "homecw-step.timer")

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
        self.assertIn("alerts.url is required", " ".join(problems('[alerts]\nkind = "ntfy"\n')))
        self.assertIn("three different folders", " ".join(problems('[paths]\ncode = "/opt/x"\nconfig = "/opt/x"\n')))
        self.assertIn("is inside", " ".join(problems('[paths]\ncode = "/srv/cw"\nstate_root = "/srv/cw/state"\n')))

    def test_unsafe_isolation_is_refused_unless_explicitly_overridden(self):
        text = "[isolation]\nrequire_netns = false\n"
        self.assertIn("require_netns = false", " ".join(problems(text)))
        os.environ["CW_ALLOW_UNSAFE"] = "1"
        try:
            self.assertEqual(problems(text), [])
        finally:
            del os.environ["CW_ALLOW_UNSAFE"]

    def test_every_problem_is_reported_at_once(self):
        self.assertGreaterEqual(len(problems('[server]\nhost = "a b"\nssh_port = 0\n[github]\nowner = "x/y"\n')), 3)

    def test_bad_toml_and_missing_file(self):
        self.assertIn("not valid TOML", " ".join(problems("[server\nhost = 1")))
        with self.assertRaises(config.ConfigError):
            config.load("/nonexistent/cw.toml", env={})


class Environment(unittest.TestCase):
    def test_environment_overrides_the_file_and_is_validated(self):
        c = load_text('[server]\nhost = "from-file.example.com"\nssh_port = 22\n',
                      env={"CW_SERVER_HOST": "from-env.example.com", "CW_SERVER_SSH_PORT": "2222", "CW_HARDWARE_MAX_LOAD": "2.5",
                           "CW_ALERTS_REMIND_HOURS": "2,12", "CW_NETWORK_LAN_DENY_OUT": "false"})
        self.assertEqual((c.server.host, c.server.ssh_port, c.hardware.max_load, c.alerts.remind_hours, c.network.lan_deny_out),
                         ("from-env.example.com", 2222, 2.5, [2.0, 12.0], False))
        self.assertIn("server.host", " ".join(problems("", env={"CW_SERVER_HOST": "bad host!"})))


class Requirements(unittest.TestCase):
    def test_require_names_everything_missing(self):
        c = config.load(None, env={})
        with self.assertRaises(config.ConfigError) as cm:
            c.require("server.host", "github.owner")
        self.assertEqual(len(cm.exception.problems), 2)
        with self.assertRaises(config.ConfigError):
            c.queue_url
        c2 = load_text('[github]\nowner = "alice"\n')
        self.assertEqual(c2.queue_url, "git@github.com:alice/claude-ballast-queue.git")
        self.assertEqual(c2.results_url, "git@github.com:alice/claude-ballast-results.git")


class ExampleFile(unittest.TestCase):
    path = ROOT / "config" / "cw.example.toml"

    def test_the_example_loads_cleanly(self):
        c = config.load(self.path, env={})
        self.assertEqual(c.server.admin_user, "cwadmin")

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
            p.write_text('# comment\nCW_ALERT_TOKEN="tk_x"\n\nANTHROPIC_API_KEY=abc=def\nnot a pair\n')
            self.assertEqual(config.read_env_file(p), {"CW_ALERT_TOKEN": "tk_x", "ANTHROPIC_API_KEY": "abc=def"})
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
            bad = Path(d) / "cw.toml"
            bad.write_text('[server]\nhost = "a b"\n')
            self.assertEqual(config.main(["check", str(bad)]), 1)
        self.assertIn("server.host", err.getvalue())


if __name__ == "__main__":
    unittest.main()
