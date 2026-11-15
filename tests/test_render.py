import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cw import config, render  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def cfg(**env):
    base = {"CW_SERVER_ADMIN_USER": "cwadmin", "CW_CONFIG": ""}
    return config.load(None, env={**base, **env})


class Rendering(unittest.TestCase):
    def test_placeholders_are_filled_and_unknown_or_unfilled_ones_are_errors(self):
        self.assertEqual(render.render("a {{name}} b {{ worker_user }}", {"name": "homecw", "worker_user": "runner1"}), "a homecw b runner1")
        with self.assertRaises(render.RenderError) as cm:
            render.render("{{nope}}", {"name": "x"})
        self.assertIn("no value is defined", str(cm.exception))
        with self.assertRaises(render.RenderError):
            render.render("{{name}}", {})

    def test_values_that_could_break_out_of_a_file_are_refused_never_escaped(self):
        for key, bad in (("name", "x\nExecStart=/bin/evil"), ("worker_user", "a b"), ("worker_user", "root; id"), ("state_root", "/var/x y"),
                         ("state_root", "/var/$(id)"), ("config", "/etc/x\nY=1"), ("fetch_s", "30; reboot"), ("alias", "lower")):
            with self.assertRaises(render.RenderError, msg=f"{key}={bad!r}"):
                render.render("{{" + key + "}}", {key: bad})

    def test_only_known_keys_may_be_used_even_when_given(self):
        with self.assertRaises(render.RenderError):
            render.render("{{secret}}", {"secret": "abc"})


class Units(unittest.TestCase):
    def test_every_unit_is_rendered_with_the_installs_names_and_no_placeholder_is_left(self):
        c = cfg(CW_IDENTITY_NAME="homecw", CW_SERVER_WORKER_USER="runner1", CW_SCHEDULE_FETCH_S="45", CW_SCHEDULE_UPDATE_MIN="30")
        units = render.unit_files(c, TEMPLATES)
        self.assertEqual(sorted(units), sorted(f"homecw-{u}" for u in render.UNITS))
        for name, text in units.items():
            self.assertNotIn("{{", text, name)
            self.assertNotIn("claude-worker", text, name)                        # nothing of the original install leaks into another's units
        fetch = units["homecw-fetch.service"]
        for needle in ("User=runner1", "Environment=PYTHONPATH=/opt/homecw/lib", "Environment=CW_CONFIG=/etc/homecw/cw.toml",
                       "ReadWritePaths=/var/lib/homecw /home/runner1 -/run/sudo -/run/sudo-rs", "ExecStart=/usr/bin/python3 -m cw.cli fetch"):
            self.assertIn(needle, fetch)
        self.assertIn("OnUnitInactiveSec=45", units["homecw-fetch.timer"])
        self.assertIn("OnUnitInactiveSec=30min", units["homecw-update.timer"])
        self.assertIn("User=runner1", units["homecw-step.service"])

    def test_the_fetch_and_step_units_carry_the_same_sandbox_and_it_is_the_tested_one(self):
        units = render.unit_files(cfg(), TEMPLATES)
        sandbox_lines = lambda t: [l for l in t.splitlines() if re.match(r"^(Protect|Private|Restrict|ReadWritePaths|LockPersonality|SystemCall)", l)]   # noqa: E731
        a, b = sandbox_lines(units["claude-worker-fetch.service"]), sandbox_lines(units["claude-worker-step.service"])
        self.assertEqual(a, b)
        self.assertIn("ProtectSystem=strict", a)
        keys = {l.split("=")[0] for l in a}
        for banned in ("NoNewPrivileges", "ProtectKernelTunables", "RestrictNamespaces", "SystemCallFilter", "PrivateDevices"):
            self.assertNotIn(banned, keys)                                       # each would break jobs today (see render.SANDBOX)

    def test_the_root_run_units_are_not_sandboxed_and_carry_no_secrets(self):
        units = render.unit_files(cfg(), TEMPLATES)
        for name in ("claude-worker-canary.service", "claude-worker-update.service"):
            self.assertNotIn("ProtectSystem", units[name])
            self.assertNotIn("EnvironmentFile", units[name])
        for name, text in units.items():
            self.assertNotIn("secrets", text, name)                               # secrets are read by the program from its own file, never by systemd

    def test_a_hostile_configuration_value_cannot_reach_a_unit(self):
        c = cfg()
        bad = type("C", (), {})()
        bad.__dict__.update({k: getattr(c, k) for k in ("identity", "server", "paths", "schedule", "access")})
        bad.paths = c.paths.__class__("paths", {**c.paths.as_dict(), "state_root": "/var/x\nExecStartPre=/bin/evil"})
        with self.assertRaises(render.RenderError):
            render.unit_files(bad, TEMPLATES)


class Sudoers(unittest.TestCase):
    def test_read_only_commands_without_a_password_and_nothing_else(self):
        text = render.sudoers_text(cfg(CW_IDENTITY_NAME="homecw", CW_SERVER_WORKER_USER="runner1"), TEMPLATES)
        self.assertIn("Cmnd_Alias HOMECW_READ = ", text)
        self.assertIn("cwadmin ALL=(runner1) NOPASSWD: HOMECW_READ", text)
        commands = re.search(r"Cmnd_Alias \w+ = (.*)", text).group(1).split(", ")
        self.assertEqual(commands, [f"/opt/homecw/bin/homecw {c}" for c in render.READ_ONLY])
        for dangerous in ("approve", "unlock", "kill", "cancel", "canary-alarm", "install-packages", "prune-results", "fetch", "exec", "run"):
            self.assertFalse(any(f" {dangerous}" in c for c in commands), dangerous)       # state-changing commands always need the password
        self.assertNotIn("ALL=(root)", text)
        self.assertNotIn("NOPASSWD: ALL", text)

    def test_no_rule_when_the_admin_prefers_a_password_or_none_is_configured(self):
        self.assertIsNone(render.sudoers_text(cfg(CW_ACCESS_SUDO="password"), TEMPLATES))
        self.assertIsNone(render.sudoers_text(config.load(None, env={"CW_CONFIG": ""}), TEMPLATES))

    def test_the_alias_is_a_valid_sudoers_name(self):
        self.assertRegex(render.alias_for(cfg(CW_IDENTITY_NAME="my-home-cw")), r"^[A-Z][A-Z0-9_]+$")
        self.assertEqual(render.alias_for(cfg(CW_IDENTITY_NAME="my-home-cw")), "MY_HOME_CW_READ")


class Wrappers(unittest.TestCase):
    def test_the_cli_wrapper_pins_its_environment_and_passes_arguments_through(self):
        text = render.cli_wrapper(cfg(CW_IDENTITY_NAME="homecw"))
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("exec env PYTHONPATH='/opt/homecw/lib' CW_CONFIG='/etc/homecw/cw.toml' /usr/bin/python3 -m cw.cli \"$@\"", text)

    def test_the_admin_entry_point_runs_the_cli_as_the_worker_through_sudo(self):
        text = render.sudo_wrapper(cfg(CW_IDENTITY_NAME="homecw", CW_SERVER_WORKER_USER="runner1"))
        self.assertIn("exec sudo -p '[sudo] password for %p to run homecw: ' -u runner1 /opt/homecw/bin/homecw \"$@\"", text)
        self.assertNotIn("NOPASSWD", text)


if __name__ == "__main__":
    unittest.main()
