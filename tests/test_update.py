import os
import shutil
import stat
import subprocess
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

    def test_subpackages_are_installed_and_stale_files_in_them_are_removed(self):
        r = self.install()
        self.assertTrue(self.at("/opt/homecw/lib/cw/client/cli.py").is_file())
        self.assertIn("/opt/homecw/lib/cw/client/cli.py", r.updated)
        stale = self.at("/opt/homecw/lib/cw/client/gone.py")
        stale.write_text("OLD = 1\n")
        r = self.install(REV2)
        self.assertFalse(stale.exists())
        self.assertIn("/opt/homecw/lib/cw/client/gone.py", r.removed)
        self.assertTrue(self.at("/opt/homecw/lib/cw/client/cli.py").is_file())

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

    def test_ipv6_follows_the_configuration(self):
        sysctls = []
        off = config.load(None, env={"CW_CONFIG": "", "CW_SERVER_ADMIN_USER": "cwadmin", "CW_IDENTITY_NAME": "homecw", "CW_NETWORK_IPV6": "disabled"})
        r = update.install_release(self.release, off, REV, self.prefix, visudo=lambda t: (True, ""), chown=lambda p: None, sysctl=lambda *a: sysctls.append(a))
        f = self.at("/etc/sysctl.d/60-homecw-ipv6.conf")
        self.assertIn("/etc/sysctl.d/60-homecw-ipv6.conf", r.updated)
        self.assertIn("net.ipv6.conf.all.disable_ipv6 = 1", f.read_text())
        self.assertEqual(sysctls, [("--system",)])
        sysctls.clear()
        update.install_release(self.release, off, REV, self.prefix, visudo=lambda t: (True, ""), chown=lambda p: None, sysctl=lambda *a: sysctls.append(a))
        self.assertEqual(sysctls, [])                                                       # unchanged: nothing re-applied
        r = update.install_release(self.release, self.cfg, REV, self.prefix, visudo=lambda t: (True, ""), chown=lambda p: None, sysctl=lambda *a: sysctls.append(a))
        self.assertFalse(f.exists())                                                        # turning it back to "enabled" removes the file ...
        self.assertIn("/etc/sysctl.d/60-homecw-ipv6.conf", r.removed)
        self.assertEqual(sysctls, [("-w", "net.ipv6.conf.all.disable_ipv6=0", "net.ipv6.conf.default.disable_ipv6=0")])   # ... and puts the setting back

    def test_a_default_install_writes_no_ipv6_file(self):
        self.install()
        self.assertFalse(self.at("/etc/sysctl.d/60-homecw-ipv6.conf").exists())

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


def run(*cmd, cwd=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd}: {r.stderr}"
    return r.stdout.strip()


class Fetch(unittest.TestCase):
    """A real 'public repository' with a main branch, SSH-signed release tags (one trusted key, one not) and an unmerged side branch."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        self.upstream = t / "upstream.git"
        work = t / "work"
        run("git", "init", "-q", "-b", "main", str(work))
        shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "templates", work / "templates")
        self.git_env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.work = work
        for name in ("good", "other"):
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(t / name), "-C", name)
        (t / "etc").mkdir()
        pub = (t / "good.pub").read_text().split()
        (t / "etc" / "allowed_signers").write_text(f'releases namespaces="git" {pub[0]} {pub[1]}\n')
        self.c1 = self.commit("one")
        self.c2 = self.commit("two")
        self.c3 = self.commit("three")
        run("git", "clone", "-q", "--bare", str(work), str(self.upstream))
        self.cfg = config.load(None, env={"CW_CONFIG": "", "CW_PATHS_STATE_ROOT": str(t / "var"), "CW_PATHS_CONFIG": str(t / "etc"),
                                           "CW_PATHS_CODE": str(t / "code"), "CW_UPDATES_SOURCE": "https://example.com/project.git"})
        self.said = []

    def commit(self, text):
        (self.work / "VERSION").write_text(text + "\n")
        run("git", "add", "-A", cwd=self.work, env=self.git_env)
        run("git", "commit", "-q", "-m", text, cwd=self.work, env=self.git_env)
        return run("git", "rev-parse", "HEAD", cwd=self.work)

    def tag(self, name, commit, key, when):
        run("git", "-c", "gpg.format=ssh", "-c", f"user.signingkey={self.tmp / key}", "tag", "-s", "-m", name, name, commit, cwd=self.work,
            env={**self.git_env, "GIT_COMMITTER_DATE": when})

    def publish(self):
        run("git", "push", "-q", "--force", str(self.upstream), "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*", cwd=self.work)

    def git(self, *args, cwd=None):
        return update.run_git("-c", f"url.{self.upstream}.insteadOf=https://example.com/project.git", *args, cwd=cwd)

    def fetch(self, **kw):
        return update.fetch_release(self.cfg, git=self.git, workdir=self.tmp / "out", out=self.said.append, **kw)

    def test_verified_installs_the_newest_tag_signed_by_a_trusted_key_and_skips_the_rest(self):
        self.tag("release-1", self.c1, "good", "2026-01-01T00:00:00")
        run("git", "tag", "release-2", self.c2, cwd=self.work, env=self.git_env)                  # lightweight: not signed at all
        self.tag("release-3", self.c3, "other", "2026-01-03T00:00:00")                              # signed, but by a key nobody trusts
        self.publish()
        src, rev, label = self.fetch(verified=True)
        self.assertEqual((label, rev), ("release-1", self.c1))
        self.assertEqual((src / "VERSION").read_text(), "one\n")
        self.assertIn("ignoring release-3 (not signed by a trusted key)", " ".join(self.said))
        self.assertIn("ignoring release-2", " ".join(self.said))

    def test_the_newest_trusted_tag_wins(self):
        self.tag("release-1", self.c1, "good", "2026-01-01T00:00:00")
        self.tag("release-2", self.c2, "good", "2026-01-02T00:00:00")
        self.publish()
        self.assertEqual(self.fetch(verified=True)[2], "release-2")

    def test_with_no_trusted_tag_nothing_is_installed(self):
        self.tag("release-3", self.c3, "other", "2026-01-03T00:00:00")
        self.publish()
        with self.assertRaises(update.InstallError) as cm:
            self.fetch(verified=True)
        self.assertIn("no release tag signed by a trusted key", str(cm.exception))

    def test_without_a_signers_file_automatic_updates_are_off(self):
        (self.tmp / "etc" / "allowed_signers").unlink()
        with self.assertRaises(update.InstallError) as cm:
            self.fetch(verified=True)
        self.assertIn("allowed_signers is missing", str(cm.exception))

    def test_an_explicit_revision_must_be_on_the_configured_branch(self):
        run("git", "checkout", "-q", "-b", "side", self.c1, cwd=self.work, env=self.git_env)
        side = self.commit("only on a side branch")
        run("git", "checkout", "-q", "main", cwd=self.work, env=self.git_env)
        self.publish()
        src, rev, label = self.fetch(rev=self.c2[:10])
        self.assertEqual((rev, label, (src / "VERSION").read_text()), (self.c2, self.c2[:10], "two\n"))
        with self.assertRaises(update.InstallError) as cm:
            self.fetch(rev=side)
        self.assertIn("is not on main", str(cm.exception))

    def test_bad_and_unknown_revisions_are_refused(self):
        self.publish()
        for bad in (None, "", "main", "xyz", "../x", "a" * 41):
            with self.assertRaises(update.InstallError, msg=str(bad)):
                self.fetch(rev=bad)
        with self.assertRaises(update.InstallError) as cm:
            self.fetch(rev="0" * 40)
        self.assertIn("was not found", str(cm.exception))

    def test_the_export_has_no_git_folder_hooks_or_config(self):
        self.publish()
        src, rev, label = self.fetch(rev=self.c3)
        self.assertEqual([p for p in src.rglob("*") if p.name in (".git", "hooks", "config") and p.parent.name != "cw"], [])
        self.assertFalse((src / ".git").exists())

    def test_the_source_must_be_configured_and_an_unreachable_one_is_explained(self):
        c = config.load(None, env={"CW_CONFIG": "", "CW_PATHS_STATE_ROOT": str(self.tmp / "var2")})
        with self.assertRaises(config.ConfigError):
            update.fetch_release(c, rev=self.c1)
        with self.assertRaises(update.InstallError) as cm:
            update.fetch_release(self.cfg, rev=self.c1, git=lambda *a, cwd=None: (128, "fatal: unable to access"), workdir=self.tmp / "o2")
        self.assertIn("could not fetch https://example.com/project.git", str(cm.exception))

    def test_a_fetched_release_installs_end_to_end(self):
        self.tag("release-1", self.c1, "good", "2026-01-01T00:00:00")
        self.publish()
        src, rev, label = self.fetch(verified=True)
        c2 = config.load(None, env={"CW_CONFIG": "", "CW_IDENTITY_NAME": "homecw", "CW_SERVER_ADMIN_USER": "cwadmin"})
        r = update.install_release(src, c2, rev, self.tmp / "root", visudo=lambda t: (True, ""), chown=lambda p: None)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(update.installed_rev(c2, self.tmp / "root"), self.c1)


class Command(unittest.TestCase):
    def test_usage(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(update.main(["--bogus"]), 2)
        self.assertIn("usage", err.getvalue())


if __name__ == "__main__":
    unittest.main()
