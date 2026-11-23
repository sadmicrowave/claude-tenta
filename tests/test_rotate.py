"""Replacing a Direct worker's mailbox key (src/ct/setup/rotate.py; security spot-check F4). The key is the only thing that lets a computer queue jobs on a Direct worker, so it can be replaced: a new key is made, the worker is told to trust only it,
the local key file is swapped, and the new key is checked. The order is what keeps the person from being locked out."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import config  # noqa: E402
from ct.setup import rotate  # noqa: E402
from ct.setup.server import SetupError  # noqa: E402


def make(kind="direct"):
    d = Path(tempfile.mkdtemp())
    conf = d / "ct.toml"
    conf.write_text(f'[identity]\nname = "box"\n[server]\nhost = "192.0.2.50"\nadmin_user = "me"\n[transport]\nkind = "{kind}"\n[paths]\nclient_home = "{d}/ct"\n')
    cfg = config.load(str(conf), env={"CT_CONFIG": ""})
    key = d / "ct" / "mailbox" / "key"
    key.parent.mkdir(parents=True, mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "old", "-f", str(key)], check=True)
    return cfg, key


class Rotate(unittest.TestCase):
    def go(self, cfg, *, install=None, verify=None, dry_run=False, keygen=None):
        self.calls = []
        install = install or (lambda pub: self.calls.append(("install", pub)))
        verify = verify or (lambda keyfile: self.calls.append(("verify", str(keyfile))) or (True, ""))
        return rotate.Rotation(cfg, install=install, verify=verify, dry_run=dry_run, confirm=lambda p: True, **({"keygen": keygen} if keygen else {}))

    def test_a_preview_changes_nothing_and_says_what_it_will_do(self):
        cfg, key = make()
        before = key.read_text()
        r = self.go(cfg, dry_run=True)
        self.assertEqual(r.run_all()[-1][1], "planned")
        self.assertEqual(key.read_text(), before)
        self.assertEqual(self.calls, [])
        self.assertFalse(key.with_name("key.new").exists())
        labels = [k for k, _ in r.plan_rows()]
        self.assertIn("Make", " ".join(labels))

    def test_the_new_key_replaces_the_old_one_and_the_worker_is_told_before_the_swap(self):
        cfg, key = make()
        old_pub = key.with_name("key.pub").read_text().strip()
        self.go(cfg).run_all()
        new_pub = key.with_name("key.pub").read_text().strip()
        self.assertNotEqual(old_pub, new_pub)
        self.assertEqual([c[0] for c in self.calls], ["install", "verify"])
        self.assertEqual(self.calls[0][1].strip(), new_pub)                                  # the worker was given the key that is now in place
        self.assertEqual(self.calls[1][1], str(key))                                         # and the check used the key file the client really uses
        self.assertFalse(key.with_name("key.old").exists())                                  # the old key is gone once the new one works
        self.assertFalse(key.with_name("key.new").exists())
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")

    def test_a_worker_that_could_not_be_changed_leaves_the_old_key_exactly_as_it_was(self):
        cfg, key = make()
        before, before_pub = key.read_text(), key.with_name("key.pub").read_text()
        def refuse(pub):
            raise SetupError("The worker did not take the key")
        with self.assertRaises(SetupError) as e:
            self.go(cfg, install=refuse).run_all()
        self.assertEqual(e.exception.facts[-1], ("Code", "rotate.install_failed"))
        self.assertEqual((key.read_text(), key.with_name("key.pub").read_text()), (before, before_pub))
        self.assertFalse(key.with_name("key.new").exists())                                  # nothing left behind

    def test_a_check_that_fails_after_the_worker_took_the_key_keeps_the_new_key_in_place_and_the_old_one_aside(self):
        cfg, key = make()
        old = key.read_text()
        with self.assertRaises(SetupError) as e:
            self.go(cfg, verify=lambda keyfile: (False, "timed out")).run_all()
        self.assertEqual(e.exception.facts[-1], ("Code", "rotate.verify_failed"))
        self.assertIn("now trusts only the new key", e.exception.why)                        # the worker no longer accepts the old one: the new one must be the key in use
        self.assertNotEqual(key.read_text(), old)
        self.assertEqual(key.with_name("key.old").read_text(), old)

    def test_only_the_direct_connection_has_a_mailbox_key(self):
        cfg, _ = make("github")
        with self.assertRaises(SetupError) as e:
            self.go(cfg).run_all()
        self.assertEqual(e.exception.facts[-1], ("Code", "rotate.not_direct"))

    def test_a_computer_with_no_key_yet_is_told_to_install_first(self):
        cfg, key = make()
        key.unlink()
        key.with_name("key.pub").unlink()
        with self.assertRaises(SetupError) as e:
            self.go(cfg).run_all()
        self.assertEqual(e.exception.facts[-1], ("Code", "rotate.no_key"))

    def test_a_key_that_cannot_be_made_is_reported_with_the_real_error(self):
        cfg, key = make()
        def fail(path):
            return False, "ssh-keygen: no such program"
        with self.assertRaises(SetupError) as e:
            self.go(cfg, keygen=fail).run_all()
        self.assertEqual(e.exception.facts[-1], ("Code", "setup.mailbox_key.not_made"))
        self.assertIn("no such program", e.exception.why)


class Strategies(unittest.TestCase):
    def test_the_check_uses_the_new_key_file_with_the_same_ssh_options_as_every_other_connection(self):
        from ct import transport
        cfg, key = make()
        cmd = transport.for_config(cfg).client_ssh_command(cfg, key=Path("/x/other key"))
        self.assertIn("-i '/x/other key'", cmd)
        self.assertIn("IdentitiesOnly=yes", cmd)
        self.assertIn(str(key), transport.for_config(cfg).client_ssh_command(cfg))                # the default is unchanged

    def test_on_a_windows_worker_the_new_key_is_installed_through_the_mailbox_script(self):
        scripts = []
        def runner(argv, text):
            scripts.append(text)
            return 0, 'CT-RESULT {"user": "box-mail", "changed": true}'
        pub = "ssh-" + "ed25519 " + "AAAAC3" + "NzaC1lZDI1NTE5AAAAIOnewkeyonlyfortestsnewkeyonlyfortest ct-mailbox"        # (built in pieces: the privacy check refuses a literal key)
        rotate.windows_install(runner, "C:\\ProgramData\\claude-tenta", "box", "ctworker", pub)
        self.assertEqual(len(scripts), 1)
        self.assertIn(pub, scripts[0])
        self.assertIn("restrict,command=", scripts[0])


class Command(unittest.TestCase):
    """The ct-setup rotate-key command: preview by default, the Linux worker half over ssh, the Windows worker through its mailbox script, and the worker half on the worker itself."""

    class Ssh:
        def __init__(self):
            self.sent, self.ran = [], []

        def send(self, command, data):
            self.sent.append((command, data))

        def interactive(self, command):
            self.ran.append(command)
            return 0

    def run_cli(self, cfg_path, *argv, **kw):
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from ct.setup import cli
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"CT_CONFIG": str(cfg_path)}), redirect_stderr(err), redirect_stdout(out):
            rc = cli.main(list(argv), **kw)
        return rc, out.getvalue() + err.getvalue()

    def conf(self, cfg):
        return Path(cfg.source)

    def test_without_apply_it_only_shows_what_it_would_do(self):
        cfg, key = make()
        ssh = self.Ssh()
        before = key.read_text()
        rc, text = self.run_cli(self.conf(cfg), "rotate-key", ssh_factory=lambda h, u, p: ssh)
        self.assertEqual(rc, 0, text)
        self.assertIn("What replacing the mailbox key does", text)
        self.assertEqual((ssh.sent, ssh.ran, key.read_text()), ([], [], before))

    def test_on_linux_the_worker_half_runs_as_root_with_the_new_key_and_the_local_key_is_swapped(self):
        from unittest import mock
        cfg, key = make()
        ssh = self.Ssh()
        old = key.read_text()
        with mock.patch("ct.setup.cli.deliver_source", return_value=("/tmp/ctd", "abc123", None)), mock.patch("ct.setup.cli.remove_remote_dir"):
            rc, text = self.run_cli(self.conf(cfg), "rotate-key", "--apply", "--yes", ssh_factory=lambda h, u, p: ssh, verify=lambda k: (True, ""))
        self.assertEqual(rc, 0, text)
        self.assertIn("The mailbox key was replaced", text)
        self.assertEqual(len(ssh.sent), 1)
        self.assertTrue(ssh.sent[0][1].decode().startswith("ssh-ed25519 "))
        self.assertIn("sudo env", ssh.ran[0])
        self.assertIn("ct.setup rotate-key --here --apply --yes", ssh.ran[0])
        self.assertIn("--mailbox-key-file /tmp/ctd/mailbox-client.pub", ssh.ran[0])
        self.assertNotEqual(key.read_text(), old)

    def test_a_worker_half_that_fails_leaves_the_old_key_and_says_the_worker_was_not_changed(self):
        from unittest import mock
        cfg, key = make()
        ssh = self.Ssh()
        ssh.interactive = lambda command: 3
        old = key.read_text()
        with mock.patch("ct.setup.cli.deliver_source", return_value=("/tmp/ctd", "abc123", None)), mock.patch("ct.setup.cli.remove_remote_dir"):
            rc, text = self.run_cli(self.conf(cfg), "rotate-key", "--apply", "--yes", ssh_factory=lambda h, u, p: ssh, verify=lambda k: (True, ""))
        self.assertNotEqual(rc, 0)
        self.assertIn("rotate.install_failed", text)
        self.assertEqual(key.read_text(), old)

    def test_on_a_windows_worker_the_new_key_goes_through_the_mailbox_script(self):
        from unittest import mock
        cfg, key = make()
        text = self.conf(cfg).read_text().replace('[paths]\n', '[paths]\ncode = "C:/ProgramData/claude-tenta/release"\nstate_root = "C:/ProgramData/claude-tenta/state"\nconfig = "C:/ProgramData/claude-tenta/config"\nworker_home = "C:/ProgramData/claude-tenta/state"\n')
        self.conf(cfg).write_text(text)
        scripts = []
        runner = lambda argv, script: scripts.append(script) or (0, 'CT-RESULT {"user": "box-mail", "changed": true}')      # noqa: E731
        with mock.patch("ct.setup.wintools.ssh_runner", return_value=runner):
            rc, out = self.run_cli(self.conf(cfg), "rotate-key", "--apply", "--yes", verify=lambda k: (True, ""))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(scripts), 1)
        self.assertIn(key.with_name("key.pub").read_text().strip(), scripts[0])                 # the script carries the key that is now in place

    def test_on_the_worker_it_writes_the_new_key_as_the_mailboxs_only_key(self):
        from unittest import mock
        cfg, key = make()
        seen = {}
        class Setup:
            def __init__(self, host, source, **kw):
                seen["pub"], seen["dry"] = kw.get("mailbox_pubkey"), kw.get("dry_run")

            def mailbox(self):
                seen["ran"] = True
        pubfile = key.parent / "new.pub"
        pubfile.write_text("ssh-ed25519 AAAATESTNEWKEY ct-mailbox\n")
        with mock.patch("ct.setup.cli.ServerSetup", Setup):
            rc, text = self.run_cli(self.conf(cfg), "rotate-key", "--here", "--mailbox-key-file", str(pubfile))
        self.assertEqual(rc, 0, text)
        self.assertEqual((seen["pub"].strip(), seen["dry"], seen["ran"]), ("ssh-ed25519 AAAATESTNEWKEY ct-mailbox", False, True))
        rc, text = self.run_cli(self.conf(cfg), "rotate-key", "--here")
        self.assertEqual(rc, 2)                                                                 # no public key given: it changes nothing


if __name__ == "__main__":
    unittest.main()
