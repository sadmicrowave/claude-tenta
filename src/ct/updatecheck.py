"""Knowing when a newer release exists, and telling the person once, quietly.

Releases are tags named `release-X.Y.Z` in the project repository (`updates.source`). The check is `git ls-remote --tags`: no clone, nothing downloaded. It is
never run while a command waits: the result is cached for a day, a stale cache is refreshed by a detached background process, and a command only ever reads
the cache. A banner is one dim line on stderr after a command, shown only on a terminal, at most once every 12 hours, and never with --json or when
CT_NO_UPDATE_CHECK is set or `updates.check` is off. Nothing is installed here: `ct-admin update` asks first.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TAG_RE = re.compile(r"^release-(\d+)\.(\d+)\.(\d+)$")
CACHE_FILE = "update-check.json"
CHECK_EVERY_S = 86400
BANNER_EVERY_S = 43200


def version_tuple(text):
    m = re.match(r"^(?:release-)?v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def parse_tags(ls_remote_text):
    """{tag: commit} for `release-X.Y.Z` tags from `git ls-remote --tags`; an annotated tag's own line is replaced by the commit it points to (the `^{}` line)."""
    out = {}
    for line in (ls_remote_text or "").splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/"):]
        peeled = tag.endswith("^{}")
        tag = tag[:-3] if peeled else tag
        if TAG_RE.match(tag) and (peeled or tag not in out):
            out[tag] = sha
    return out


def latest(tags):
    """(tag, commit) of the highest release, or None."""
    best = max(tags, key=lambda t: version_tuple(t), default=None)
    return (best, tags[best]) if best else None


def cache_path(home):
    return Path(home) / CACHE_FILE


def load(home):
    try:
        return json.loads(cache_path(home).read_text())
    except (OSError, ValueError):
        return {}


def save(home, data):
    try:
        Path(home).mkdir(parents=True, exist_ok=True)
        cache_path(home).write_text(json.dumps(data))
    except OSError:
        pass                                         # a cache that cannot be written is not worth an error


def check_now(url, run=subprocess.run, now=time.time, timeout=10):
    """Ask the project repository for its release tags. Returns the cache entry: {checked, tag, commit} or {checked, error}."""
    try:
        r = run(["git", "ls-remote", "--tags", url, "release-*"], capture_output=True, text=True, timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"})
    except (OSError, subprocess.SubprocessError) as e:
        return {"checked": int(now()), "error": str(e)[:120]}
    if r.returncode != 0:
        return {"checked": int(now()), "error": (r.stderr or "").strip()[-120:] or "could not reach the release source"}
    found = latest(parse_tags(r.stdout))
    return {"checked": int(now()), **({"tag": found[0], "commit": found[1]} if found else {})}


def due(cache, now=time.time):
    return now() - cache.get("checked", 0) > CHECK_EVERY_S


def background_refresh(home, url):
    """Start the check in a detached process and return at once, so no command ever waits for the network."""
    try:
        subprocess.Popen([sys.executable, "-m", "ct.updatecheck", str(home), url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def pending(cache, local_version, worker_release=None):
    """What is out of date, from the cache: {'tag': ..., 'this_computer': bool, 'worker': bool}, or None when nothing is."""
    tag = cache.get("tag")
    if not tag:
        return None
    newer = (version_tuple(tag) or (0, 0, 0)) > (version_tuple(local_version) or (0, 0, 0))
    worker = bool(worker_release) and bool(cache.get("commit")) and not str(cache["commit"]).startswith(str(worker_release)[:7]) and not str(worker_release).startswith(str(cache["commit"])[:7])
    return {"tag": tag, "this_computer": newer, "worker": worker} if (newer or worker) else None


def banner_text(info, local_version):
    parts = []
    if info["this_computer"]:
        parts.append(f"this computer has {local_version}")
    if info["worker"]:
        parts.append("the worker is on a different release")
    return f"A newer release is available: {info['tag']} ({'; '.join(parts)}). See what changed and update: ct-admin update"


def after_command(updates_cfg, home, local_version, worker_release=None, stream=None, now=time.time, refresh=background_refresh):
    """Called when a command finishes. Prints the one-line banner if it is due; starts a background refresh if the cache is old. Never raises."""
    try:
        stream = stream or sys.stderr
        if not getattr(updates_cfg, "check", True) or os.environ.get("CT_NO_UPDATE_CHECK") or not stream.isatty():
            return False
        url = updates_cfg.source
        cache = load(home)
        if url and due(cache, now):
            refresh(home, url)
        info = pending(cache, local_version, worker_release)
        if not info or now() - cache.get("banner", 0) < BANNER_EVERY_S:
            return False
        from . import ui
        BLUE = ui.PRIMARY
        print(ui.paint("▸ ", BLUE, stream=stream) + ui.paint(banner_text(info, local_version), ui.DIM, stream=stream), file=stream)
        cache["banner"] = int(now())
        save(home, cache)
        return True
    except Exception:
        return False


def main(argv=None):
    """python3 -m ct.updatecheck <home> <url>: the detached refresh."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        return 2
    home, url = argv
    old = load(home)
    entry = check_now(url)
    if "error" in entry:                              # keep the last good answer; only the time moves, so an offline laptop does not retry on every command
        entry = {**old, "checked": entry["checked"], "error": entry["error"]}
    else:
        entry = {**entry, **({"banner": old["banner"]} if "banner" in old else {})}
    save(home, entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
