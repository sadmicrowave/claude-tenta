"""The worker's store of file contents by hash (design: docs/design/WORKSPACE-JOBS.md, "Built: delta sync"). The worker owns it; a job never writes to it (a job's own folder gets copies).

A blob is stored under the SHA-256 the worker computed itself, never under a name the sender chose, so nobody can plant content under another file's hash. Every read is checked again, so an entry changed on disk is
dropped and treated as missing instead of served. The folder is private (0700) and keeps a random `epoch` that changes when the folder is made again, which tells a client the worker was reset."""
import os
import secrets
import time
from pathlib import Path

from . import refusals, snapshot


class FileCache:
    def __init__(self, root, max_mb, clock=time.time):
        self.root, self.max_mb, self.clock = Path(root), max_mb, clock
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        marker = self.root / ".epoch"
        try:
            self.epoch = marker.read_text().strip()
        except OSError:
            self.epoch = ""
        if not self.epoch:
            self.epoch = secrets.token_hex(8)
            marker.write_text(self.epoch)

    @property
    def max_bytes(self):
        return int(self.max_mb * 1_000_000)

    @max_bytes.setter
    def max_bytes(self, value):
        self.max_mb = value / 1_000_000

    def _entries(self):
        return [p for p in self.root.iterdir() if snapshot.HASH_RE.fullmatch(p.name)]

    def put(self, text, claimed=None):
        """Store a text under the hash of its own bytes and return the hash. A claimed hash that is not that one is refused and nothing is stored."""
        h = snapshot.blob_hash(text)
        if claimed is not None and claimed != h:
            raise refusals.refuse("files.cache.hash_mismatch", claimed=str(claimed)[:16], actual=h[:16])
        target = self.root / h
        if not target.exists():
            tmp = self.root / f".{h}.{secrets.token_hex(4)}"
            tmp.write_bytes(text.encode("utf-8"))
            tmp.chmod(0o600)
            os.replace(tmp, target)
        self._touch(target)
        return h

    def _touch(self, path):
        now = self.clock()
        try:
            os.utime(path, (now, now))
        except OSError:
            pass

    def get(self, h):
        """The text stored under `h`, or None when it is missing or no longer matches its hash (that entry is removed)."""
        path = self.root / h
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or snapshot.blob_hash(text) != h:
            path.unlink(missing_ok=True)
            return None
        self._touch(path)
        return text

    def missing(self, hashes):
        return [h for h in hashes if not (self.root / h).is_file()]

    def total_bytes(self):
        return sum(p.stat().st_size for p in self._entries())

    def evict(self, pinned=()):
        """Remove the least recently used entries until the store fits its cap; an entry in `pinned` (a file a waiting job needs) stays. Returns the hashes removed."""
        for stale in self.root.glob(".*"):                                    # a half-written file left by a crash
            if stale.name != ".epoch" and self.clock() - stale.stat().st_mtime > 3600:
                stale.unlink(missing_ok=True)
        entries = sorted(((p.stat().st_mtime, p) for p in self._entries()), key=lambda t: t[0])
        total, gone, pinned = sum(p.stat().st_size for _, p in entries), [], set(pinned)
        for _, p in entries:
            if total <= self.max_bytes:
                break
            if p.name in pinned:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
            gone.append(p.name)
        return gone
