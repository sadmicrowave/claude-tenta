import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from armada import config, render  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def cfg(**env):
    base = {"ARM_SERVER_ADMIN_USER": "ctadmin", "ARM_CONFIG": ""}
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
    def test_no_setting_in_a_unit_has_a_comment_after_it_on_the_same_line(self):
        """Found by running the real systemd on every release: systemd has no inline comments, so `AccuracySec=1s  # why` is an invalid value, the whole line is ignored and the timer fires up to a
        minute late (the very thing the comment says the setting prevents). A comment goes on its own line."""
        import re
        units = render.unit_files(cfg(ARM_IDENTITY_NAME="homect", ARM_TRANSPORT_KIND="direct"), TEMPLATES)
        for name, text in units.items():
            for n, line in enumerate(text.splitlines(), 1):
                if re.match(r"^[A-Za-z]+=", line):
                    self.assertNotRegex(line, r"\s#", f"{name}:{n}: {line}")
        for path in sorted((TEMPLATES / "systemd").glob("*.in")):                                          # and the templates themselves, before anything is filled in
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if re.match(r"^[A-Za-z]+=", line):
                    self.assertNotRegex(line, r"\s#", f"{path.name}:{n}: {line}")

    def test_the_services_that_start_jobs_do_not_set_restrict_suid_sgid(self):
        """Found on Debian 13: with it the jail (bubblewrap 0.12) cannot open /usr and no job runs. A test jail check on the real machine is what caught it."""
        for unit, text in render.unit_files(cfg(ARM_IDENTITY_NAME="homect"), TEMPLATES).items():
            self.assertNotIn("RestrictSUIDSGID", text, unit)

    def test_a_direct_workers_services_may_write_the_mailbox_and_a_github_workers_do_not(self):
        """Found on a real machine: the sandbox stopped the worker pushing results into its own mailbox ('unpacker error'), so no job ever came back."""
        direct = cfg(ARM_IDENTITY_NAME="homect", ARM_TRANSPORT_KIND="direct")
        for unit in ("homect-fetch.service", "homect-step.service"):
            rw = [l for l in render.unit_files(direct, TEMPLATES)[unit].splitlines() if l.startswith("ReadWritePaths=")][0]
            self.assertIn(" " + direct.path("mailbox") + " ", rw + " ", unit)
        github = render.unit_files(cfg(ARM_IDENTITY_NAME="homect"), TEMPLATES)
        self.assertTrue(all("-mail" not in t for t in github.values()))

    def test_a_shared_computers_limits_are_a_dropin_of_their_own_and_nothing_set_writes_none(self):
        """The jobs run in the step service's cgroup, so its limits keep them from taking a computer other people use. They are a drop-in FILE, never a placeholder in the unit template (see the upgrade guard below)."""
        plain = cfg(ARM_IDENTITY_NAME="homect")
        self.assertIsNone(render.step_dropin(plain))
        self.assertNotIn("CPUWeight", render.unit_files(plain, TEMPLATES)["homect-step.service"])
        shared = cfg(ARM_IDENTITY_NAME="homect", ARM_AVAILABILITY_CPU_WEIGHT="20", ARM_AVAILABILITY_IO_WEIGHT="20", ARM_AVAILABILITY_MEMORY_MAX_PERCENT="50", ARM_AVAILABILITY_NICE="10")
        text = render.step_dropin(shared)
        self.assertIn("[Service]\nCPUWeight=20\nIOWeight=20\nMemoryMax=50%\nNice=10\n", text)
        self.assertEqual(render.step_dropin_path(shared), "/etc/systemd/system/homect-step.service.d/resources.conf")
        self.assertNotIn("CPUWeight", render.unit_files(shared, TEMPLATES)["homect-step.service"])        # the unit itself is byte for byte what it always was
        self.assertIn("Nice=19\n", render.step_dropin(cfg(ARM_IDENTITY_NAME="homect", ARM_AVAILABILITY_NICE="19")))

    # What the updater already installed on a worker knows how to fill. A release is installed by the OLD updater (the installed code renders the NEW templates), so a template that
    # uses a placeholder outside this set cannot be installed on any worker that runs an older release: seen on the first `arm-admin apply` of {{resources}}, on all three workers
    # ("the template uses {{resources}} but no value is defined for it"). Add to this set only with a plan for the workers that cannot be upgraded in one step.
    UPGRADE_SAFE_PLACEHOLDERS = {"name", "worker_user", "admin_user", "alias", "state_root", "worker_home", "lib", "config", "code", "fetch_s", "step_s", "canary_s", "update_min",
                                 "registry_sync_min", "commands", "mailbox_rw", "sandbox"}      # release f4c9ae706

    def test_no_template_uses_a_placeholder_an_older_updater_cannot_fill(self):
        import re
        used = {}
        for t in sorted(TEMPLATES.rglob("*.in")):
            for key in re.findall(r"\{\{(\w+)\}\}", t.read_text()):
                used.setdefault(key, []).append(t.name)
        extra = {k: v for k, v in used.items() if k not in self.UPGRADE_SAFE_PLACEHOLDERS}
        self.assertEqual(extra, {}, "a new template placeholder breaks `arm-admin apply` on workers that run an older release (use a file of its own instead)")

    def test_every_unit_is_rendered_with_the_installs_names_and_no_placeholder_is_left(self):
        c = cfg(ARM_IDENTITY_NAME="homect", ARM_SERVER_WORKER_USER="runner1", ARM_SCHEDULE_FETCH_S="45", ARM_SCHEDULE_UPDATE_MIN="30")
        units = render.unit_files(c, TEMPLATES)
        self.assertEqual(sorted(units), sorted(f"homect-{u}" for u in render.UNITS))
        for name, text in units.items():
            self.assertNotIn("{{", text, name)
            self.assertNotIn("armada", re.sub(r"armada(\.[a-z_]+|/)", "", text), name)                        # nothing of the original install leaks into another's units
        fetch = units["homect-fetch.service"]
        for needle in ("User=runner1", "Environment=PYTHONPATH=/opt/homect/lib", "Environment=ARM_CONFIG=/etc/homect/armada.toml",
                       "ReadWritePaths=/var/lib/homect /home/runner1 /tmp -/run/sudo -/run/sudo-rs", "ExecStart=/usr/bin/python3 -m armada.cli fetch"):
            self.assertIn(needle, fetch)
        self.assertIn("OnUnitInactiveSec=45", units["homect-fetch.timer"])
        self.assertIn("OnUnitInactiveSec=30min", units["homect-update.timer"])
        self.assertIn("User=runner1", units["homect-step.service"])

    def test_the_fetch_and_step_units_carry_the_same_sandbox_and_it_is_the_tested_one(self):
        units = render.unit_files(cfg(), TEMPLATES)
        sandbox_lines = lambda t: [l for l in t.splitlines() if re.match(r"^(Protect|Private|Restrict|ReadWritePaths|LockPersonality|SystemCall)", l)]   # noqa: E731
        a, b = sandbox_lines(units["armada-fetch.service"]), sandbox_lines(units["armada-step.service"])
        self.assertEqual(a, b)
        self.assertIn("ProtectSystem=strict", a)
        keys = {l.split("=")[0] for l in a}
        for banned in ("NoNewPrivileges", "ProtectKernelTunables", "RestrictNamespaces", "SystemCallFilter", "PrivateDevices"):
            self.assertNotIn(banned, keys)                                       # each would break jobs today (see render.SANDBOX)

    def test_the_root_run_units_are_not_sandboxed_and_carry_no_secrets(self):
        units = render.unit_files(cfg(), TEMPLATES)
        for name in ("armada-canary.service", "armada-update.service"):
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
        text = render.sudoers_text(cfg(ARM_IDENTITY_NAME="homect", ARM_SERVER_WORKER_USER="runner1"), TEMPLATES)
        self.assertIn("Cmnd_Alias HOMECT_READ = ", text)
        self.assertIn("ctadmin ALL=(runner1) NOPASSWD: HOMECT_READ", text)
        commands = re.search(r"Cmnd_Alias \w+ = (.*)", text).group(1).split(", ")
        self.assertEqual(commands, [f"/opt/homect/bin/homect {c}" for c in render.READ_ONLY])
        for dangerous in ("approve", "unlock", "kill", "cancel", "canary-alarm", "install-packages", "prune-results", "fetch", "exec", "run"):
            self.assertFalse(any(f" {dangerous}" in c for c in commands), dangerous)       # state-changing commands always need the password
        self.assertNotIn("ALL=(root)", text)
        self.assertNotIn("NOPASSWD: ALL", text)

    def test_no_rule_when_the_admin_prefers_a_password_or_none_is_configured(self):
        self.assertIsNone(render.sudoers_text(cfg(ARM_ACCESS_SUDO="password"), TEMPLATES))
        self.assertIsNone(render.sudoers_text(config.load(None, env={"ARM_CONFIG": ""}), TEMPLATES))

    def test_the_alias_is_a_valid_sudoers_name(self):
        self.assertRegex(render.alias_for(cfg(ARM_IDENTITY_NAME="my-home-ct")), r"^[A-Z][A-Z0-9_]+$")
        self.assertEqual(render.alias_for(cfg(ARM_IDENTITY_NAME="my-home-ct")), "MY_HOME_CT_READ")


class Wrappers(unittest.TestCase):
    def test_the_cli_wrapper_pins_its_environment_and_passes_arguments_through(self):
        text = render.cli_wrapper(cfg(ARM_IDENTITY_NAME="homect"))
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("exec env PYTHONPATH='/opt/homect/lib' ARM_CONFIG='/etc/homect/armada.toml' /usr/bin/python3 -m armada.cli \"$@\"", text)

    def test_the_admin_entry_point_runs_the_cli_as_the_worker_through_sudo(self):
        text = render.sudo_wrapper(cfg(ARM_IDENTITY_NAME="homect", ARM_SERVER_WORKER_USER="runner1"))
        self.assertIn("exec sudo -p '[sudo] password for %p to run arm-worker: ' -u runner1 /opt/homect/bin/homect \"$@\"", text)
        self.assertNotIn("NOPASSWD", text)


if __name__ == "__main__":
    unittest.main()
