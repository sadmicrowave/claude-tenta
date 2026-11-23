"""Updating this computer's own copy of the tool, after the person said yes.

Two ways it is installed: from a checkout of the project repository (pip's editable install, which is what `ct-setup install` is run from), or as a normal
pip package. A checkout is moved forward only by a fast-forward to the release tag's commit, so nothing the person committed is ever overwritten, and never
when it has uncommitted changes. A pip package is reinstalled from the tag.
"""
import subprocess
import sys
from pathlib import Path

from ..client import refuse
from ..client.repo import ClientError


def checkout_root():
    """The folder of the project checkout this code runs from, or None when it is an ordinary installed package."""
    import ct
    root = Path(ct.__file__).resolve().parents[2]
    return root if (root / ".git").exists() and (root / "pyproject.toml").exists() else None


def update_checkout(root, url, tag, run=subprocess.run):
    """Fast-forward the checkout to the release tag. Returns the new HEAD commit."""
    def git(*a):
        return run(["git", "-C", str(root), *a], capture_output=True, text=True)
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise refuse.error("update.dirty", root=root)
    r = git("fetch", "--quiet", url, f"refs/tags/{tag}:refs/tags/{tag}")
    if r.returncode:
        raise refuse.error("update.fetch_failed", ("Release", str(tag)), steps=[f"Check that you can reach {url}"], reason=(r.stderr or "").strip()[-200:])
    r = git("merge", "--ff-only", f"{tag}^{{commit}}")
    if r.returncode:
        raise refuse.error("update.not_fast_forward", steps=[f"Merge it yourself: git -C {root} merge {tag}"])
    return git("rev-parse", "HEAD").stdout.strip()


def pip_argv(url, tag):
    return [sys.executable, "-m", "pip", "install", "--upgrade", f"git+{url}@{tag}"]


def update_package(url, tag, run=subprocess.run):
    r = run(pip_argv(url, tag), capture_output=True, text=True)
    if r.returncode:
        raise refuse.error("update.pip_failed", steps=[" ".join(pip_argv(url, tag))], reason=((r.stderr or "") + (r.stdout or "")).strip()[-300:])
