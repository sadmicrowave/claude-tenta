import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cw import config, render, update  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REV = "a" * 40
REV2 = "b" * 40


def mode(p):
    return stat.S_IMODE(Path(p).stat().st_mode)


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.release = self.tmp / "release"
        shutil.copytree(ROOT / "src", self.release / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "templates", self.release / "templates")
        self.prefix = self.tmp / "root"
        self.cfg = config.load(None, env={"CW_CONFIG": "", "CW_SERVER_ADMIN_USER": "cwadmin", "CW_IDENTITY_NAME": "homecw"})
        self.calls, self.sandboxed = [], []
        self.said = []

    def install(self, rev=REV, sandbox_result=0, visudo_ok=True, cfg=None, **kw):
        def sandbox(unit_text):
            self.sandboxed.append(unit_text)
            return sandbox_result
        return update.install_release(self.release, cfg or self.cfg, rev, self.prefix, sandbox=sandbox, systemctl=lambda *a: self.calls.append(a),
                                      visudo=lambda text: (visudo_ok, "" if visudo_ok else "parse error near line 3"), chown=lambda p: None,
                                      out=self.said.append, **kw)

    def at(self, path):
        return self.prefix / path.lstrip("/")

    def test_a_first_install_writes_code_wrappers_units_sudoers_and_the_revision(self):
        r = self.install()
        self.assertTrue(r.ok, r.errors)
        lib = self.at("/opt/homecw/lib/cw")
        self.assertTrue((lib / "config.py").is_file() and (lib / "cli.py").is_file())
        self.assertEqual((lib / "cli.py").read_bytes(), (self.release / "src" / "cw" / "cli.py").read_bytes())
        self.assertEqual(mode(lib / "cli.py"), 0o644)
        self.assertEqual(mode(self.at("/opt/homecw/bin/homecw")), 0o755)
        self.assertEqual(mode(self.at("/usr/local/bin/homecw")), 0o755)
        self.assertIn("-u cwworker /opt/homecw/bin/homecw", self.at("/usr/local/bin/homecw").read_text())
        for unit in render.UNITS:
            self.assertEqual(mode(self.at(f"/etc/systemd/system/homecw-{unit}")), 0o644)
        self.assertEqual(mode(self.at("/etc/sudoers.d/homecw")), 0o440)
        self.assertIn("NOPASSWD: HOMECW_READ", self.at("/etc/sudoers.d/homecw").read_text())
        self.assertEqual(self.at("/etc/homecw/installed-rev").read_text(), REV + "\n")
        self.assertEqual(update.installed_rev(self.cfg, self.prefix), REV)
        self.assertEqual(self.calls, [("daemon-reload",)])
        self.assertEqual(len(self.sandboxed), 1)
        self.assertIn("ExecStart=/usr/bin/python3 -m cw.cli step", self.sandboxed[0])           # the step unit is what gets sandbox-tested

    def test_a_second_install_of_the_same_release_changes_nothing_and_does_not_retest_or_reload(self):
        self.install()
        self.calls.clear()
        self.sandboxed.clear()
        r = self.install()
        self.assertEqual((r.updated, r.removed, r.units_changed), ([], [], False))
        self.assertGreater(r.unchanged, 10)
        self.assertEqual((self.calls, self.sandboxed), ([], []))

    def test_a_changed_unit_is_tested_and_kept_when_it_passes(self):
        self.install()
        self.calls.clear()
        self.sandboxed.clear()
        (self.release / "templates" / "systemd" / "step.timer.in").write_text((self.release / "templates" / "systemd" / "step.timer.in").read_text().replace("OnBootSec=70", "OnBootSec=80"))
        r = self.install(REV2)
        self.assertTrue(r.ok)
        self.assertEqual(r.updated, ["/etc/systemd/system/homecw-step.timer"])
        self.assertIn("OnBootSec=80", self.at("/etc/systemd/system/homecw-step.timer").read_text())
        self.assertEqual((self.calls, len(self.sandboxed)), ([("daemon-reload",)], 1))
        self.assertEqual(update.installed_rev(self.cfg, self.prefix), REV2)

    def test_a_failing_sandbox_puts_the_old_units_back_and_records_nothing(self):
        self.install()
        old_unit = self.at("/etc/systemd/system/homecw-fetch.service").read_text()
        old_timer = self.at("/etc/systemd/system/homecw-step.timer").read_text()
        (self.release / "templates" / "systemd" / "fetch.service.in").write_text("[Service]\nProtectSystem=strict\nExecStart=/bin/true\n")
        (self.release / "templates" / "systemd" / "step.timer.in").write_text("[Timer]\nOnBootSec=1\n")
        (self.release / "src" / "cw" / "newmodule.py").write_text("X = 1\n")
        self.calls.clear()
        r = self.install(REV2, sandbox_result=1)
        self.assertTrue(r.rolled_back)
        self.assertFalse(r.ok)
        self.assertIn("a sandbox check failed", r.errors[0])
        self.assertEqual(self.at("/etc/systemd/system/homecw-fetch.service").read_text(), old_unit)       # the previous units are back
        self.assertEqual(self.at("/etc/systemd/system/homecw-step.timer").read_text(), old_timer)
        self.assertEqual(self.calls, [])                                                                  # and systemd was not reloaded
        self.assertEqual(update.installed_rev(self.cfg, self.prefix), REV)                                # the new revision is NOT recorded
        self.assertTrue(self.at("/opt/homecw/lib/cw/newmodule.py").exists())                              # the code itself stays installed

    def test_a_unit_that_did_not_exist_before_is_removed_on_rollback(self):
        r = self.install(sandbox_result=1)
        self.assertTrue(r.rolled_back)
        self.assertFalse(self.at("/etc/systemd/system/homecw-fetch.service").exists())
        self.assertIsNone(update.installed_rev(self.cfg, self.prefix))

    def test_modules_a_release_no_longer_has_are_removed(self):
        self.install()
        stale = self.at("/opt/homecw/lib/cw/oldmodule.py")
        stale.write_text("OLD = 1\n")
        (self.at("/opt/homecw/lib/cw") / "notes.txt").write_text("not python: left alone")
        r = self.install(REV2)
        self.assertFalse(stale.exists())
        self.assertIn("/opt/homecw/lib/cw/oldmodule.py", r.removed)
        self.assertTrue((self.at("/opt/homecw/lib/cw") / "notes.txt").exists())

    def test_an_invalid_sudoers_rule_is_never_installed_but_the_rest_is(self):
        r = self.install(visudo_ok=False)
        self.assertFalse(self.at("/etc/sudoers.d/homecw").exists())
        self.assertIn("NOT installed: parse error near line 3", r.errors[0])
        self.assertTrue(self.at("/opt/homecw/lib/cw/cli.py").exists())

    def test_a_sudoers_rule_from_an_earlier_release_goes_when_the_config_stops_asking_for_it(self):
        self.install()
        self.assertTrue(self.at("/etc/sudoers.d/homecw").exists())
        c2 = config.load(None, env={"CW_CONFIG": "", "CW_SERVER_ADMIN_USER": "cwadmin", "CW_IDENTITY_NAME": "homecw", "CW_ACCESS_SUDO": "password"})
        r = self.install(REV2, cfg=c2)
        self.assertFalse(self.at("/etc/sudoers.d/homecw").exists())
        self.assertIn("/etc/sudoers.d/homecw", r.removed)

    def test_the_sudoers_file_is_checked_before_it_is_written(self):
        seen = []
        update.install_release(self.release, self.cfg, REV, self.prefix, sandbox=lambda t: 0, systemctl=lambda *a: None, chown=lambda p: None,
                               visudo=lambda text: (seen.append(text) or True, ""), out=self.said.append)
        self.assertEqual(len(seen), 1)
        self.assertIn("Cmnd_Alias HOMECW_READ", seen[0])

    def test_nothing_is_written_when_the_release_cannot_be_rendered(self):
        (self.release / "templates" / "systemd" / "fetch.service.in").write_text("{{unknown_value}}\n")
        with self.assertRaises(render.RenderError):
            self.install()
        self.assertFalse(self.prefix.exists())                                                            # fail closed: not a single file

    def test_a_directory_that_is_not_a_release_is_refused(self):
        with self.assertRaises(update.InstallError):
            update.install_release(self.tmp, self.cfg, REV, self.prefix)
        shutil.rmtree(self.release / "templates")
        with self.assertRaises(update.InstallError):
            self.install()

    def test_the_revision_must_look_like_a_commit_id(self):
        for bad in ("main", "xyz", "a" * 41, "", "../etc"):
            with self.assertRaises(update.InstallError, msg=bad):
                self.install(bad)

    def test_files_are_replaced_atomically_with_no_temp_files_left(self):
        self.install()
        leftovers = [p for p in self.prefix.rglob("*") if p.name.endswith(".new")]
        self.assertEqual(leftovers, [])

    def test_a_file_whose_mode_was_loosened_is_put_right(self):
        self.install()
        p = self.at("/opt/homecw/lib/cw/cli.py")
        os.chmod(p, 0o666)
        r = self.install()
        self.assertIn("/opt/homecw/lib/cw/cli.py", r.updated)
        self.assertEqual(mode(p), 0o644)

    def test_no_systemctl_or_sandbox_means_units_are_just_written(self):
        r = update.install_release(self.release, self.cfg, REV, self.prefix, visudo=lambda t: (True, ""), chown=lambda p: None)
        self.assertTrue(r.ok)

    def test_the_installed_files_never_name_the_original_install(self):
        self.install()
        for p in self.prefix.rglob("*"):
            if p.is_file() and p.suffix in ("", ".service", ".timer"):
                self.assertNotIn("claude-worker", p.read_text(errors="ignore"), str(p))


class Command(unittest.TestCase):
    def test_usage(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(update.main([]), 2)
        self.assertIn("usage", err.getvalue())


if __name__ == "__main__":
    unittest.main()
