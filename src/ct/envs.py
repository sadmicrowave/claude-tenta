"""Shared environments on a worker: one folder of fetched packages per package-set hash, a last-used stamp, a lock held for the life of every job that uses it, and the manifest of
apt packages tenta itself installed (design: docs/design/WORKSPACE-JOBS.md, "Environment lifecycle"). The worker owns it; nothing here needs root.

Layout under `<env_root>/shared/`: `<key>/` (the environment; `.complete` is written only after a build finished, `.last_used` is touched on every use),
`<key>.lock` (flock: shared while a job uses it, exclusive while it is built or removed) and `<key>.pruned.json` (what a removal leaves, so the next build can say it is a rebuild).
The clock is passed in so tests pin it."""
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from .runner import tree_mb
from .words import plural

DEFAULT_KEEP_DAYS = 30
DAY = 86400


def env_key(pkgs, os_release=""):
    """The environment's name: a hash of the sorted pinned package list plus the worker's OS release, so two classes with the same packages share one."""
    blob = json.dumps({"packages": sorted(set(pkgs)), "os": os_release}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


class Lease:
    """A hold on one environment's lock file. Shared (many jobs at once) while in use; exclusive to build or remove. Closing it releases the hold."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

    def exclusive(self, wait=True):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            return True
        except OSError:
            return False

    def share(self):
        fcntl.flock(self.fd, fcntl.LOCK_SH)

    def close(self):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)                                   # closing the descriptor drops the lock
            self.fd = None


class PruneReport:
    def __init__(self):
        self.removed, self.skipped, self.folders, self.freed_mb = [], [], 0, 0.0

    def text(self):
        parts = []
        if self.removed:
            parts.append(f"removed {plural(len(self.removed), 'unused environment')} ({self.freed_mb:.0f} MB)")
        if self.folders:
            parts.append(f"removed {plural(self.folders, 'old throwaway folder')}")
        if self.skipped:
            parts.append(f"skipped {plural(len(self.skipped), 'environment')} in use")
        return "; ".join(parts)


class EnvStore:
    def __init__(self, env_root, clock=time.time):
        self.env_root, self.clock = Path(env_root), clock
        self.root = self.env_root / "shared"

    def path(self, key):
        return self.root / key

    def complete(self, key):
        return (self.path(key) / ".complete").is_file()

    def mark_complete(self, key):
        (self.path(key) / ".complete").write_text("")
        self.touch(key)

    def touch(self, key):
        """Stamp a use. A file of its own: the folder's mtime changes only when files are added, which a running job does not do."""
        stamp = self.path(key) / ".last_used"
        stamp.write_text("")
        now = self.clock()
        os.utime(stamp, (now, now))

    def last_used(self, key):
        try:
            return (self.path(key) / ".last_used").stat().st_mtime
        except OSError:
            return None

    def keys(self):
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []

    def lease(self, key):
        """The USE lock: shared by every job that uses the environment (and by a build in progress), exclusive for whoever removes it."""
        return Lease(self.root / f"{key}.lock")

    def build_lease(self, key):
        """Serialises two builds of the same environment; a different file from the use lock, so a build never waits for (or blocks) a running job."""
        return Lease(self.root / f"{key}.build")

    # ---- what a removal leaves behind ----
    def _tomb(self, key):
        return self.root / f"{key}.pruned.json"

    def tombstone(self, key):
        try:
            return json.loads(self._tomb(key).read_text())
        except (OSError, ValueError):
            return None

    def clear_tombstone(self, key):
        self._tomb(key).unlink(missing_ok=True)

    def _remove(self, key, last):
        try:
            self._tomb(key).write_text(json.dumps({"last_used": last, "pruned": self.clock()}))
        except OSError:
            pass
        shutil.rmtree(self.path(key), ignore_errors=True)

    def _take(self, key):
        """The exclusive lock on `key`, or None when a job holds it (never wait: a held lock means skip and try the next pass)."""
        lease = self.lease(key)
        if lease.exclusive(wait=False):
            return lease
        lease.close()
        return None

    def prune(self, keep_days):
        """Remove every environment unused for more than `keep_days` (exactly that many days is kept). One in use is skipped, never half deleted."""
        rep, limit = PruneReport(), self.clock() - keep_days * DAY
        for key in self.keys():
            last = self.last_used(key)
            if last is None:
                last = self.path(key).stat().st_mtime                  # no stamp (a build that never finished): by its age
            if last >= limit:
                continue
            lease = self._take(key)
            if lease is None:
                rep.skipped.append(key)
                continue
            try:
                if (self.last_used(key) or 0) >= limit:                # a job stamped it between the check and the lock
                    continue
                rep.freed_mb += tree_mb(self.path(key))
                self._remove(key, last)
                rep.removed.append(key)
            finally:
                lease.close()
        return rep

    def evict_lru(self, need_mb):
        """Disk pressure: remove the least recently used environments (never one in use) until `need_mb` is freed. Returns [(key, mb)] in the order removed."""
        gone, freed = [], 0.0
        by_age = sorted(self.keys(), key=lambda k: self.last_used(k) or 0)
        for key in by_age:
            if freed >= need_mb:
                break
            lease = self._take(key)
            if lease is None:
                continue
            try:
                mb = tree_mb(self.path(key))
                self._remove(key, self.last_used(key) or 0)
                freed += mb
                gone.append((key, mb))
            finally:
                lease.close()
        return gone

    def total_mb(self):
        return sum(tree_mb(self.path(k)) for k in self.keys())


class AptManifest:
    """What tenta itself installed with apt, written at install time: one entry per package with who asked for it and its size. A package that was on the machine before tenta
    is never in it, so it is never a candidate for removal. Without the file nothing is known, so nothing is listed."""

    def __init__(self, path):
        self.path = Path(path) if path else None

    def load(self):
        try:
            return (json.loads(self.path.read_text()) or {}).get("packages") or {}
        except (OSError, ValueError, AttributeError):
            return {}

    def save(self, pkgs):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"v": 1, "packages": pkgs}, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    def record(self, installed, wanted_by, sizes, now):
        """`installed`: names tenta just installed. `wanted_by`: {name: [classes]} for every apt package in the registry; entries already in the manifest keep their record but follow the registry."""
        have = self.load()
        for name in installed:
            have[name] = {"classes": sorted(wanted_by.get(name, [])), "mb": sizes.get(name, 0.0), "installed": now}
        for name in have:
            if name in wanted_by:
                have[name]["classes"] = sorted(wanted_by[name])
        self.save(have)

    def leftovers(self, wanted_by):
        """[(name, mb)] of tenta-installed packages no class in the registry lists any more."""
        return sorted((n, float(v.get("mb") or 0)) for n, v in self.load().items() if n not in wanted_by)
