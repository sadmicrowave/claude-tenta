import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import config, render  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def cfg(**env):
    base = {"CT_SERVER_ADMIN_USER": "ctadmin", "CT_CONFIG": ""}
    return config.load(None, env={**base, **env})


class Rendering(unittest.TestCase):
    def test_placeholders_are_filled_and_unknown_or_unfilled_ones_are_errors(self):
        self.assertEqual(render.render("a {{name}} b {{ worker_user }}", {"name": "homect", "worker_user": "runner1"}), "a homect b runner1")
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
    def test_a_direct_workers_services_may_write_the_mailbox_and_a_github_workers_do_not(self):
        """Found on a real machine: the sandbox stopped the worker pushing results into its own mailbox ('unpacker error'), so no job ever came back."""
        direct = cfg(CT_IDENTITY_NAME="homect", CT_TRANSPORT_KIND="direct")
        for unit in ("homect-fetch.service", "homect-step.service"):
            rw = [l for l in render.unit_files(direct, TEMPLATES)[unit].splitlines() if l.startswith("ReadWritePaths=")][0]
            self.assertIn(" " + direct.path("mailbox") + " ", rw + " ", unit)
        github = render.unit_files(cfg(CT_IDENTITY_NAME="homect"), TEMPLATES)
        self.assertTrue(all("-mail" not in t for t in github.values()))

    def test_every_unit_is_rendered_with_the_installs_names_and_no_placeholder_is_left(self):
        c = cfg(CT_IDENTITY_NAME="homect", CT_SERVER_WORKER_USER="runner1", CT_SCHEDULE_FETCH_S="45", CT_SCHEDULE_UPDATE_MIN="30")
        units = render.unit_files(c, TEMPLATES)
        self.assertEqual(sorted(units), sorted(f"homect-{u}" for u in render.UNITS))
        for name, text in units.items():
            self.assertNotIn("{{", text, name)
            self.assertNotIn("claude-tenta", text, name)                        # nothing of the original install leaks into another's units
        fetch = units["homect-fetch.service"]
        for needle in ("User=runner1", "Environment=PYTHONPATH=/opt/homect/lib", "Environment=CT_CONFIG=/etc/homect/ct.toml",
                       "ReadWritePaths=/var/lib/homect /home/runner1 /tmp -/run/sudo -/run/sudo-rs", "ExecStart=/usr/bin/python3 -m ct.cli fetch"):
            self.assertIn(needle, fetch)
        self.assertIn("OnUnitInactiveSec=45", units["homect-fetch.timer"])
        self.assertIn("OnUnitInactiveSec=30min", units["homect-update.timer"])
        self.assertIn("User=runner1", units["homect-step.service"])

    def test_the_fetch_and_step_units_carry_the_same_sandbox_and_it_is_the_tested_one(self):
        units = render.unit_files(cfg(), TEMPLATES)
        sandbox_lines = lambda t: [l for l in t.splitlines() if re.match(r"^(Protect|Private|Restrict|ReadWritePaths|LockPersonality|SystemCall)", l)]   # noqa: E731
        a, b = sandbox_lines(units["claude-tenta-fetch.service"]), sandbox_lines(units["claude-tenta-step.service"])
        self.assertEqual(a, b)
        self.assertIn("ProtectSystem=strict", a)
        keys = {l.split("=")[0] for l in a}
        for banned in ("NoNewPrivileges", "ProtectKernelTunables", "RestrictNamespaces", "SystemCallFilter", "PrivateDevices"):
            self.assertNotIn(banned, keys)                                       # each would break jobs today (see render.SANDBOX)

    def test_the_root_run_units_are_not_sandboxed_and_carry_no_secrets(self):
        units = render.unit_files(cfg(), TEMPLATES)
        for name in ("claude-tenta-canary.service", "claude-tenta-update.service"):
            self.assertNotIn("ProtectSystem", units[name])
            self.assertNotIn("EnvironmentFile", units[name])
        for name, text in units.items():
            self.assertNotIn("secrets", text, name)                               # secrets are read by the program from its own file, never by systemd

    def test_a_hostile_configuration_value_cannot_reach_a_unit(self):
        c = cfg()
        bad = type("C", (), {})()
        bad.__dict__.update({k: getattr(c, k) for k in ("identity", "server", "paths", "schedule", "access", "claude")})
        bad.paths = c.paths.__class__("paths", {**c.paths.as_dict(), "state_root": "/var/x\nExecStartPre=/bin/evil"})
        with self.assertRaises(render.RenderError):
            render.unit_files(bad, TEMPLATES)


class ClaudeUpdateUnit(unittest.TestCase):
    def test_the_weekly_update_unit_runs_as_the_worker_under_the_same_sandbox(self):
        units = render.unit_files(cfg(CT_CLAUDE_UPDATE_DAYS="7"), TEMPLATES)
        svc, timer = units["claude-tenta-claude-update.service"], units["claude-tenta-claude-update.timer"]
        self.assertIn("ExecStart=/usr/bin/python3 -m ct.cli claude-update", svc)
        self.assertIn("User=ctworker", svc)
        self.assertIn("ProtectSystem=strict", svc)
        self.assertIn("OnUnitInactiveSec=7d", timer)


class Sudoers(unittest.TestCase):
    def test_read_only_commands_without_a_password_and_nothing_else(self):
        text = render.sudoers_text(cfg(CT_IDENTITY_NAME="homect", CT_SERVER_WORKER_USER="runner1"), TEMPLATES)
        self.assertIn("Cmnd_Alias HOMECT_READ = ", text)
        self.assertIn("ctadmin ALL=(runner1) NOPASSWD: HOMECT_READ", text)
        commands = re.search(r"Cmnd_Alias \w+ = (.*)", text).group(1).split(", ")
        self.assertEqual(commands, [f"/opt/homect/bin/homect {c}" for c in render.READ_ONLY])
        for dangerous in ("approve", "unlock", "kill", "cancel", "canary-alarm", "install-packages", "prune-results", "fetch", "exec", "run"):
            self.assertFalse(any(f" {dangerous}" in c for c in commands), dangerous)       # state-changing commands always need the password
        self.assertNotIn("ALL=(root)", text)
        self.assertNotIn("NOPASSWD: ALL", text)

    def test_no_rule_when_the_admin_prefers_a_password_or_none_is_configured(self):
        self.assertIsNone(render.sudoers_text(cfg(CT_ACCESS_SUDO="password"), TEMPLATES))
        self.assertIsNone(render.sudoers_text(config.load(None, env={"CT_CONFIG": ""}), TEMPLATES))

    def test_the_alias_is_a_valid_sudoers_name(self):
        self.assertRegex(render.alias_for(cfg(CT_IDENTITY_NAME="my-home-ct")), r"^[A-Z][A-Z0-9_]+$")
        self.assertEqual(render.alias_for(cfg(CT_IDENTITY_NAME="my-home-ct")), "MY_HOME_CT_READ")


class Wrappers(unittest.TestCase):
    def test_the_cli_wrapper_pins_its_environment_and_passes_arguments_through(self):
        text = render.cli_wrapper(cfg(CT_IDENTITY_NAME="homect"))
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("exec env PYTHONPATH='/opt/homect/lib' CT_CONFIG='/etc/homect/ct.toml' /usr/bin/python3 -m ct.cli \"$@\"", text)

    def test_the_admin_entry_point_runs_the_cli_as_the_worker_through_sudo(self):
        text = render.sudo_wrapper(cfg(CT_IDENTITY_NAME="homect", CT_SERVER_WORKER_USER="runner1"))
        self.assertIn("exec sudo -p '[sudo] password for %p to run ct-worker: ' -u runner1 /opt/homect/bin/homect \"$@\"", text)
        self.assertNotIn("NOPASSWD", text)


if __name__ == "__main__":
    unittest.main()
