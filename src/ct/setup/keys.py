"""Moving the server's two public deploy keys onto GitHub, without anyone copying anything.

The server publishes the public halves in a readable file in its configuration folder. This side reads them over SSH and adds each to its repository with
`gh` (queue read-only, results with write access), skipping any that are already there. Without `gh`, the exact manual steps are returned instead.
"""
import json
import os
import tempfile

from ..admin.ssh import Ssh
from .report import sink as _sink

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


def read_only(item, default=True):
    """Whether a key listed by `gh repo deploy-key list` is read-only. This gh prints the API's name (read_only) while its --json option is asked for readOnly; accept either."""
    for name in ("read_only", "readOnly"):
        if name in item:
            return bool(item[name])
    return default


def blob(pub):
    parts = pub.split()
    return parts[1] if len(parts) > 1 else pub


class DeployKeys:
    """`run(argv) -> Result` runs a local command (gh); `which(name)` finds a program."""

    def __init__(self, cfg, run, which, say=None, note=None):
        say = say or _sink()
        self.cfg, self.run, self.which, self.say, self.note = cfg, run, which, say, note
        self.done, self.list_error = {}, ""

    @property
    def available(self):
        return bool(self.which("gh"))

    def slug(self, which):
        g = self.cfg.github
        return f"{g.owner}/{g.queue_repo if which == 'queue' else g.results_repo}"

    def present(self, which):
        """The key texts already on the repository (empty if it cannot be listed)."""
        r = self.run(["gh", "repo", "deploy-key", "list", "-R", self.slug(which), "--json", "id,key,title,readOnly"])
        if not getattr(r, "ok", False):
            self.list_error = (getattr(r, "out", "").strip().splitlines() or ["no answer"])[-1][:160]
            return None
        try:
            return {blob(item.get("key", "")): item for item in json.loads(r.out or "[]")}
        except ValueError:
            return None

    def add(self, which, pub):
        """Add one key. Returns 'added', 'present' or an error text."""
        have, repaired = self.present(which), False
        if have is None:                                                           # GitHub did not answer: adding blindly would only fail the same way, or duplicate a key
            return f"could not check the deploy keys on {self.slug(which)} ({self.list_error}); check your connection and run ct-setup install again"
        if have is not None and blob(pub) in have:
            item = have[blob(pub)]
            if which != "results" or not read_only(item, default=False):
                return "present"
            fixed = self.run(["gh", "repo", "deploy-key", "delete", str(item.get("id", "")), "-R", self.slug(which)]) if item.get("id") else None      # the key is there but cannot write: the worker could never publish a result
            if not fixed or not fixed.ok:
                return "the results key is read-only on GitHub: tick 'Allow write access' on it at github.com/" + self.slug(which) + "/settings/keys"
            repaired = True
        with tempfile.TemporaryDirectory(prefix="ct-key-") as d:
            f = os.path.join(d, f"{which}.pub")
            with open(f, "w") as fh:
                fh.write(pub + "\n")
            argv = ["gh", "repo", "deploy-key", "add", f, "-R", self.slug(which), "-t", TITLE.format(name=self.cfg.identity.name)]
            if which == "results":
                argv.append("--allow-write")
            r = self.run(argv)
        return ("fixed" if repaired else "added") if r.ok else (r.out.strip().splitlines() or ["gh failed"])[-1][:200]

    def stale(self, which, pub):
        """[(id, title)] of keys on this repository with this install's title that are not the server's current key: left by an account or machine that was replaced. They are
        reported, never removed here: another worker of the same install may legitimately hold one."""
        have = self.present(which) or {}
        mine = f"{self.cfg.identity.name}-server"
        return [(item.get("id"), item.get("title")) for b, item in have.items() if b != blob(pub) and item.get("title") == mine and item.get("id")]

    def sync(self, keys):
        """Add every published key not yet handled. Returns [(which, status)] for the ones handled in this call."""
        out = []
        for which, pub in keys.items():
            if which in self.done:
                continue
            status = self.add(which, pub)
            if status in ("added", "present", "fixed"):
                self.done[which] = status
                access = "read and write" if which == "results" else "read-only"
                if self.note:                                                      # the same status column as every other line of the install
                    self.note(f"deploy key for {self.slug(which)}", status, access)
                else:
                    self.say(f"  {status.upper():<9} deploy key for {self.slug(which)}  ({access})")        # output: plain fallback (the caller's printer styles it)
            out.append((which, status))
            if status in ("added", "present", "fixed") and self.note:
                for kid, _ in self.stale(which, keys[which]):
                    self.note(f"stale deploy key on {self.slug(which)}", "found", f"not this server's key; if no other worker uses it: gh repo deploy-key delete {kid} -R {self.slug(which)}")
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
