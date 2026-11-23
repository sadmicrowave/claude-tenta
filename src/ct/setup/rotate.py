"""ct-setup rotate-key: replace a Direct worker's mailbox key (security spot-check F4; docs/security/DIRECT-PROFILE-APPROVAL.md).

The mailbox key is the only thing that lets a computer queue jobs on a Direct worker. If it is lost or copied it must be replaceable without touching anything else. The ORDER is what keeps the person from being locked out:
  1. make a NEW key beside the old one (the old one is untouched);
  2. tell the worker to trust ONLY the new one (it takes the old one away): if this fails, the new key is deleted and nothing changed;
  3. swap the local key file: from here the worker accepts only the new key, so it is the one in use whatever happens next;
  4. check the new key works; if so delete the old key; if not, keep the old key aside (key.old) and say so.
The worker side is the existing mailbox step (Linux: `ct-setup rotate-key --here` on the worker as root; Windows: winmailbox.ensure with the new key)."""
import os
import shlex
import subprocess
from pathlib import Path

from .. import transport
from . import refuse, winmailbox


def keygen(path):
    """(ok, detail): an ed25519 key with no passphrase (the mailbox key has no other use), mode 0600 from ssh-keygen."""
    r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ct-mailbox", "-f", str(path)], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()


def git_verify(cfg):
    """A check function for Rotation: can the key at `keyfile` read the worker's queue? (git ls-remote through the same ssh options every connection uses.)"""
    def verify(keyfile):
        t = transport.for_config(cfg)
        env = {**os.environ, "GIT_SSH_COMMAND": t.client_ssh_command(cfg, key=keyfile), "GIT_TERMINAL_PROMPT": "0"}
        try:
            r = subprocess.run(["git", "ls-remote", t.client_url(cfg, "queue")], env=env, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)
        return r.returncode == 0, (r.stderr or "").strip()[-300:]
    return verify


def windows_install(runner, root, name, worker, pub):
    """Give a Windows worker's mailbox account the new key (it keeps only this one): the same idempotent step the setup uses."""
    return winmailbox.ensure(runner, root=root, name=name, worker=worker, key=pub)


class Rotation:
    def __init__(self, cfg, *, install, verify, say=None, dry_run=True, confirm=None, keygen=keygen):
        self.cfg, self.install, self.verify, self.say = cfg, install, verify, say or (lambda m: None)
        self.dry_run, self.confirm, self.keygen = dry_run, confirm, keygen
        self.key = Path(transport.for_config(cfg).client_key(cfg)) if cfg.transport.kind == "direct" else None
        self.report = []

    def paths(self):
        return self.key, self.key.with_name(self.key.name + ".new"), self.key.with_name(self.key.name + ".old")

    def plan_rows(self):
        key, new, old = self.paths()
        return [("Make", f"a new key beside the old one ({new.name})"), ("Install", f"on {self.cfg.server.host}: the worker will trust only the new key"),
                ("Swap", f"the new key becomes {key}"), ("Check", "that the new key can reach the worker's mailbox"), ("Delete", "the old key, once the check passes")]

    def check(self):
        if self.cfg.transport.kind != "direct":
            raise refuse.error("rotate.not_direct", ("Connection", str(self.cfg.transport.kind)))
        key = self.paths()[0]
        if not (key.exists() and key.with_name(key.name + ".pub").exists()):
            raise refuse.error("rotate.no_key", ("Key file", str(key)))

    def run_all(self):
        self.check()
        key, new, old = self.paths()
        if self.dry_run:
            self.report.append(("rotation", "planned", ""))
            return self.report
        if self.confirm and not self.confirm(f"Replace the mailbox key for {self.cfg.server.host}? The old key stops working."):
            self.report.append(("rotation", "declined", ""))
            return self.report
        pub_new = new.with_name(new.name + ".pub")
        for p in (new, pub_new):
            p.unlink(missing_ok=True)
        ok, detail = self.keygen(new)
        if not ok:
            raise refuse.error("setup.mailbox_key.not_made", reason=detail or "ssh-keygen did not make it.")
        pub = pub_new.read_text().strip()
        try:
            self.install(pub)                                                                      # the worker now trusts ONLY the new key
        except Exception as e:                                                                     # it did not take it: nothing changed, the new key is thrown away
            for p in (new, pub_new):
                p.unlink(missing_ok=True)
            why = str(getattr(e, "why", None) or getattr(e, "title", None) or e)
            raise refuse.error("rotate.install_failed", ("Computer", self.cfg.server.host), reason=why) from e
        key_pub = key.with_name(key.name + ".pub")
        old.unlink(missing_ok=True)
        os.replace(key, old)                                                                       # from here the new key is the one in use, whatever happens next
        os.replace(new, key)
        old_pub = old.with_name(old.name + ".pub")
        os.replace(key_pub, old_pub)
        os.replace(pub_new, key_pub)
        self.report.append(("key", "replaced", str(key)))
        ok, detail = self.verify(key)
        if not ok:
            raise refuse.error("rotate.verify_failed", ("Computer", self.cfg.server.host), ("Old key kept as", str(old)), reason=detail or "no answer")
        for p in (old, old_pub):
            p.unlink(missing_ok=True)
        self.report.append(("check", "ok", "the new key reached the worker's mailbox"))
        self.report.append(("rotation", "done", ""))
        return self.report


def linux_install(ssh, cfg, source, deliver, cleanup):
    """The install function for a Linux worker: send the source and the new public key, then run the worker half as root (`ct-setup rotate-key --here`), which rewrites the mailbox key."""
    def install(pub):
        d, rev, _ = deliver(ssh, source)
        try:
            ssh.send(f"cat > {d}/mailbox-client.pub", (pub + "\n").encode())
            base = (f"env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={d}/src CT_CONFIG={cfg.paths.config}/ct.toml python3 -m ct.setup rotate-key --here --apply --yes "
                    f"--source {shlex.quote(d)} --rev {rev} --mailbox-key-file {d}/mailbox-client.pub")
            rc = ssh.interactive(f"sudo {base}")
        finally:
            cleanup(ssh, d)
        if rc:
            raise RuntimeError(f"the worker half exited with {rc}")
    return install
