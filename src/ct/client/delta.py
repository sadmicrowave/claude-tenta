"""Delta sync on the client: send a folder as a manifest of hashes and only the files the worker does not hold (design: docs/design/WORKSPACE-JOBS.md, "Built: delta sync").

What this computer believes the worker holds is kept in a small ledger per worker (hashes only, never content). It is a guess that costs nothing to keep: a new `epoch` published by the worker (it was reset) empties it, and a
wrong guess is caught by the worker, which says which files it lacks (`needs-files`) and gets them from `top_up` with no new approval, because the approved job file already pins every file by hash."""
import json
import os
import time
from pathlib import Path

from . import refuse
from .. import snapshot
from ..words import plural

KEEP_DAYS = 14


def ledger_path(repos):
    return Path(repos.queue).parent / "delta-sent.json"


def _jobs_dir(repos):
    return Path(repos.queue).parent / "delta-jobs"


def supported(info):
    """The worker's published `cache` section when it can take a manifest (version 1 and an epoch), else None: an older worker gets the whole set, as before."""
    c = (info or {}).get("cache")
    return c if isinstance(c, dict) and c.get("version") == 1 and c.get("epoch") else None


def snapshot_limits(info):
    lim = ((info or {}).get("limits") or {}).get("files") or {}
    return int(lim.get("snapshot_count") or 5000), int(lim.get("snapshot_total_kb") or 50000)


def fits(files, info):
    """True when the whole folder is within the snapshot caps the worker published (else the old way of sending is used, which has its own limits and compression)."""
    count, kb = snapshot_limits(info)
    return len(files) <= count and sum(len(t.encode("utf-8")) for t in files.values()) <= kb * 1000


def load(repos, epoch):
    try:
        data = json.loads(ledger_path(repos).read_text())
    except (OSError, ValueError):
        data = {}
    if data.get("epoch") != epoch:
        return set()                                              # the worker was reset (or this is the first run): it holds nothing we sent
    return set(data.get("hashes") or [])


def save(repos, epoch, hashes):
    try:
        ledger_path(repos).write_text(json.dumps({"epoch": epoch, "hashes": sorted(hashes)}))
    except OSError:
        pass                                                      # the ledger is only a guess: without it the next run sends everything, which is correct


class Plan:
    def __init__(self, manifest, blobs, files, epoch):
        self.manifest, self.blobs, self.epoch = manifest, blobs, epoch
        self.snapshot = snapshot.snapshot_id(manifest)
        self.total_files = len(files)
        self.total_bytes = sum(e["size"] for e in manifest.values())
        self.sent_files = sum(1 for n, e in manifest.items() if e["sha256"] in blobs)
        self.sent_bytes = sum(len(t.encode("utf-8")) for t in blobs.values())
        self.reused_files = self.total_files - self.sent_files
        self.reused_bytes = self.total_bytes - sum(e["size"] for e in manifest.values() if e["sha256"] in blobs)

    def job_fields(self):
        return {"manifest": self.manifest, "snapshot": self.snapshot, "blobs": self.blobs}


def modes_of(roots, names):
    """{name: "755"} for the files that are executable on disk (looked up under each root in turn)."""
    out = {}
    for n in names:
        for root in roots:
            p = Path(root) / n
            if p.is_file():
                if os.stat(p).st_mode & 0o111:
                    out[n] = "755"
                break
    return out


def plan(files, info, repos, resend_all=False, modes=None):
    """What to send for `files`: the manifest of all of them, and the blobs the worker is not believed to hold."""
    cache = supported(info)
    manifest = snapshot.manifest_of(files, modes)
    held = set() if resend_all else load(repos, cache["epoch"])
    blobs = {}
    for text in files.values():
        h = snapshot.blob_hash(text)
        if h not in held and h not in blobs:
            blobs[h] = text
    return Plan(manifest, blobs, files, cache["epoch"])


def remember_sent(repos, p, jid, roots):
    """After the job is pushed: the worker will hold every file of it (so the next run sends only what changed), and where each came from, so a missing file can be sent later."""
    save(repos, p.epoch, load(repos, p.epoch) | set(snapshot.hashes(p.manifest)))
    sources = {}
    for name, e in p.manifest.items():
        for root in roots:
            path = Path(root) / name
            if path.is_file():
                sources.setdefault(e["sha256"], str(path.resolve()))
                break
    d = _jobs_dir(repos)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{jid}.json").write_text(json.dumps({"sources": sources}))
        for old in d.glob("*.json"):
            if time.time() - old.stat().st_mtime > KEEP_DAYS * 86400:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def top_up(ctx, jid, res):
    """A job the worker holds for lack of some files: read exactly those from where they were (each must still match its hash), push them for the worker, and say so. Returns the number sent, 0 when they were already sent
    for this list. A file that changed since is a refusal: the job would not be the one that was approved."""
    repos, need = ctx.repos, sorted(res.get("needs") or [])
    try:
        rec = json.loads((_jobs_dir(repos) / f"{jid}.json").read_text())
    except (OSError, ValueError):
        rec = {}
    if rec.get("topped_up") == need:
        return 0
    blobs, lost = {}, []
    for h in need:
        path = (rec.get("sources") or {}).get(h)
        try:
            text = Path(path).read_bytes().decode("utf-8") if path else None
        except (OSError, UnicodeDecodeError):
            text = None
        if text is not None and snapshot.blob_hash(text) == h:
            blobs[h] = text
        else:
            lost.append(h)
    ledger = json.loads(ledger_path(repos).read_text()) if ledger_path(repos).exists() else {}
    save(repos, ledger.get("epoch"), set(ledger.get("hashes") or []) - set(need))      # whatever it was, those are not there: the next run sends them again
    if lost:
        raise refuse.error("files.cache.source_changed", ("Job", jid), count=plural(len(lost), "file"))
    repos.push_files(repos.jobs_branch, {f"blobs/{jid}.json": json.dumps(blobs)}, f"files for job: {jid}")
    rec["topped_up"] = need
    try:
        (_jobs_dir(repos) / f"{jid}.json").write_text(json.dumps(rec))
    except OSError:
        pass
    return len(blobs)
