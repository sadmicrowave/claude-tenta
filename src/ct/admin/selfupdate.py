"""Updating this computer's own copy of the tool, after the person said yes.

Two ways it is installed: from a checkout of the project repository (pip's editable install, which is what `ct-setup install` is run from), or as a normal
pip package. A checkout is moved forward only by a fast-forward to the release tag's commit, so nothing the person committed is ever overwritten, and never
when it has uncommitted changes. A pip package is reinstalled from the tag.
"""
import subprocess
import sys
from pathlib import Path

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
        raise ClientError("Your checkout has uncommitted changes", f"{root} was not touched.", ["Commit or stash them, then run ct-admin update again."])
    r = git("fetch", "--quiet", url, f"refs/tags/{tag}:refs/tags/{tag}")
    if r.returncode:
        raise ClientError("Could not fetch the release", (r.stderr or "").strip()[-200:], [f"Check that you can reach {url}"], facts=[("Release", str(tag))])
    r = git("merge", "--ff-only", f"{tag}^{{commit}}")
    if r.returncode:
        raise ClientError("Your checkout cannot be fast-forwarded to that release", "It has commits the release does not (or the history differs), so nothing was changed.",
                          [f"Merge it yourself: git -C {root} merge {tag}"])
    return git("rev-parse", "HEAD").stdout.strip()


def pip_argv(url, tag):
    return [sys.executable, "-m", "pip", "install", "--upgrade", f"git+{url}@{tag}"]


def update_package(url, tag, run=subprocess.run):
    r = run(pip_argv(url, tag), capture_output=True, text=True)
    if r.returncode:
        raise ClientError("pip could not install that release", ((r.stderr or "") + (r.stdout or "")).strip()[-300:], [" ".join(pip_argv(url, tag))])
