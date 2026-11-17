"""Install a release on the worker, and prove it still works afterwards.

The worker fetches the release itself from the project repository (updates.source) and installs it as root; this side chooses WHICH commit,
shows what would change, asks once, then checks the worker is healthy and offers to put the previous version back if it is not.
"""
import re
import subprocess
from pathlib import Path

from ..client.repo import ClientError

HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Files that run as root or define the sandbox: always listed first in a diff, with a warning, because a change here is the dangerous kind.
ROOT_RUN = ("src/ct/update.py", "src/ct/render.py", "src/ct/sandboxtest.py", "src/ct/firewall.py", "src/ct/canarywatch.py", "templates/", "hardening/")
SELFTEST_CLASS = "sys-status"


def short(sha):
    return (sha or "")[:9]


class Source:
    """A bare clone of the project repository kept in the client home, used only to read history and diffs. Nothing in it is ever executed."""

    def __init__(self, path, url, branch="main", run=subprocess.run):
        self.path, self.url, self.branch, self._run = Path(path), url, branch, run

    def git(self, *args, check=True):
        r = self._run(["git", "-C", str(self.path), *args], capture_output=True, text=True)
        if check and r.returncode:
            raise ClientError(f"git {' '.join(args)} failed", (r.stderr or "").strip()[:300])
        return r

    def update(self):
        if not self.url:
            raise ClientError("No release source is configured", "updates.source is empty.", example="ct-setup client")
        if not (self.path / "HEAD").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            r = self._run(["git", "clone", "--bare", "--quiet", self.url, str(self.path)], capture_output=True, text=True)
            if r.returncode:
                raise ClientError("Could not fetch the release source", (r.stderr or "").strip()[:300], example=f"git ls-remote {self.url}")
        else:
            self.git("fetch", "--quiet", "--prune", "--force", "--tags", self.url, "+refs/heads/*:refs/heads/*")

    def resolve(self, rev=None):
        """Full commit id of `rev` (default: the tip of the branch), which must be on the branch: the worker refuses anything else."""
        self.update()
        target = f"{rev}^{{commit}}" if rev else f"refs/heads/{self.branch}"
        r = self.git("rev-parse", "--verify", "-q", target, check=False)
        if r.returncode:
            raise ClientError(f"Commit {rev} was not found in the release source", "Has it been pushed?", example=f"git ls-remote {self.url}")
        sha = r.stdout.strip()
        if self.git("merge-base", "--is-ancestor", sha, f"refs/heads/{self.branch}", check=False).returncode:
            raise ClientError(f"Commit {short(sha)} is not on {self.branch}", "The worker only installs commits that are on the release branch.",
                              ["Merge it first, then try again."])
        return sha

    def changed_files(self, old, new):
        if not old or not HEX_RE.match(old):
            return None
        r = self.git("diff", "--name-status", old, new, check=False)
        if r.returncode:
            return None
        return [tuple(line.split("\t", 1)) for line in r.stdout.splitlines() if "\t" in line]

    def diff_text(self, old, new, paths=()):
        r = self.git("diff", "--no-color", old, new, "--", *paths, check=False)
        return r.stdout


def is_root_run(path):
    return any(path == p or path.startswith(p) for p in ROOT_RUN)


def installed_rev(ssh, name):
    """The commit the worker reports as installed, or None. Read-only; needs no sudo."""
    r = ssh.capture(f"cat /etc/{name}/installed-rev 2>/dev/null")
    out = (r.stdout or "").strip()
    return out if HEX_RE.match(out) else None


def health_command(name, config_dir, code_dir):
    """One read-only shell line that prints key=value facts about the worker (timers, failed units, installed rev, registry validity)."""
    return (f"for t in fetch step canary; do printf 'timer-%s=%s\\n' $t \"$(systemctl is-active {name}-$t.timer)\"; done; "
            f"printf 'failed=%s\\n' \"$(systemctl list-units --failed --no-legend '{name}*' | wc -l)\"; "
            f"printf 'rev=%s\\n' \"$(cat {config_dir}/installed-rev 2>/dev/null)\"; "
            f"PYTHONPATH={code_dir}/lib python3 -m ct.cli check-registry {config_dir}/registry.yaml >/dev/null 2>&1; printf 'registry=%s\\n' $?")


def health_checks(ssh, command, expect_rev):
    """[(what, ok, detail)] from one read-only SSH call. A single failing row means the worker could not be reached."""
    try:
        r = ssh.capture(command)
    except ClientError as e:
        return [("the worker answers over SSH", False, e.why or e.title)]
    kv = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    timers = ("fetch", "step", "canary")
    return [("the three timers are running", all(kv.get(f"timer-{t}") == "active" for t in timers), ", ".join(f"{t} {kv.get(f'timer-{t}', '?')}" for t in timers)),
            ("no service has failed", kv.get("failed") == "0", f"{kv.get('failed', '?')} failed"),
            ("the installed registry is valid", kv.get("registry") == "0", "check-registry exit " + kv.get("registry", "?")),
            ("the installed version is the one just installed", (kv.get("rev") or "").startswith(expect_rev[:9]), (kv.get("rev") or "unknown")[:9])]


def code_changed(source, old, new):
    """True if anything that runs on the worker differs between the two versions; unknown counts as changed."""
    files = source.changed_files(old, new)
    return files is None or any(f[1].startswith(("src/", "templates/", "hardening/", "packs/")) for f in files)
