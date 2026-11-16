"""Moving the server's two public deploy keys onto GitHub, without anyone copying anything.

The server publishes the public halves in a readable file in its configuration folder. This side reads them over SSH and adds each to its repository with
`gh` (queue read-only, results with write access), skipping any that are already there. Without `gh`, the exact manual steps are returned instead.
"""
import json
import os
import tempfile

from ..admin.ssh import Ssh

TITLE = "{name}-server"


def published(ssh, config_dir):
    """{'queue': 'ssh-ed25519 AAAA... comment', 'results': ...} for whichever of the two the server has published so far."""
    out = {}
    for which in ("queue", "results"):
        r = ssh.capture(f"cat {Ssh.quote([f'{config_dir}/deploy-keys/{which}.pub'])} 2>/dev/null")
        text = (r.stdout or "").strip()
        if r.returncode == 0 and text.startswith("ssh-"):
            out[which] = text
    return out


def blob(pub):
    parts = pub.split()
    return parts[1] if len(parts) > 1 else pub


class DeployKeys:
    """`run(argv) -> Result` runs a local command (gh); `which(name)` finds a program."""

    def __init__(self, cfg, run, which, say=print):
        self.cfg, self.run, self.which, self.say = cfg, run, which, say
        self.done = {}

    @property
    def available(self):
        return bool(self.which("gh"))

    def slug(self, which):
        g = self.cfg.github
        return f"{g.owner}/{g.queue_repo if which == 'queue' else g.results_repo}"

    def present(self, which):
        """The key texts already on the repository (empty if it cannot be listed)."""
        r = self.run(["gh", "repo", "deploy-key", "list", "-R", self.slug(which), "--json", "key,title,readOnly"])
        if not r.ok:
            return None
        try:
            return {blob(item.get("key", "")): item for item in json.loads(r.out or "[]")}
        except ValueError:
            return None

    def add(self, which, pub):
        """Add one key. Returns 'added', 'present' or an error text."""
        have = self.present(which)
        if have is not None and blob(pub) in have:
            return "present"
        with tempfile.TemporaryDirectory(prefix="cw-key-") as d:
            f = os.path.join(d, f"{which}.pub")
            with open(f, "w") as fh:
                fh.write(pub + "\n")
            argv = ["gh", "repo", "deploy-key", "add", f, "-R", self.slug(which), "-t", TITLE.format(name=self.cfg.identity.name)]
            if which == "results":
                argv.append("--allow-write")
            r = self.run(argv)
        return "added" if r.ok else (r.out.strip().splitlines() or ["gh failed"])[-1][:200]

    def sync(self, keys):
        """Add every published key not yet handled. Returns [(which, status)] for the ones handled in this call."""
        out = []
        for which, pub in keys.items():
            if which in self.done:
                continue
            status = self.add(which, pub)
            if status in ("added", "present"):
                self.done[which] = status
                self.say(f"  {'ADDED' if status == 'added' else 'PRESENT':<9} deploy key for {self.slug(which)}" + ("  (read and write)" if which == "results" else "  (read-only)"))
            out.append((which, status))
        return out

    def manual_steps(self, keys):
        rows = []
        for which, pub in keys.items():
            rows.append(f"Add this key to https://github.com/{self.slug(which)}/settings/keys" + (" with 'Allow write access' ticked" if which == "results" else " (leave write access OFF)")
                        + f":\n    {pub}")
        return rows

    @property
    def complete(self):
        return {"queue", "results"} <= set(self.done)
