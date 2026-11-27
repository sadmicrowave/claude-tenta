"""A folder as a content-hashed manifest (design: docs/design/WORKSPACE-JOBS.md, "Built: delta sync"). Used by the client (to build it) and the worker (to check it).

A manifest is {relative path: {"sha256": hash of the file's exact bytes, "size": bytes, "mode": "644" or "755"}}. The snapshot id is the SHA-256 of the manifest written in sorted order, so two folders with the same names and
contents have the same id and a rename changes it. Files are text (valid UTF-8) for now: a blob is the file's text, and its hash is taken over its UTF-8 bytes, so a CRLF file is the same bytes on both sides."""
import hashlib
import json
import re

from . import jobs, refusals

MODES = ("644", "755")
HASH_RE = re.compile(r"[0-9a-f]{64}")


def blob_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_of(files, modes=None):
    """{path: entry} for {path: text}. `modes` names the files that are executable ({path: "755"}); every other file is 644."""
    modes = modes or {}
    return {name: {"sha256": blob_hash(text), "size": len(text.encode("utf-8")), "mode": modes.get(name, "644")} for name, text in files.items()}


def snapshot_id(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def check_manifest(manifest, kind="command"):
    """The manifest as it is, or a refusal naming what is wrong. Names follow the same rule as any attached file (jobs.file_name_problem); the rest is shape: a 64-character hash, a size, a mode."""
    if not isinstance(manifest, dict) or not manifest:
        raise refusals.refuse("files.manifest.invalid", detail="the manifest is not a set of named files")
    for name, e in manifest.items():
        problem = jobs.file_name_problem(name, kind)
        if problem:
            raise refusals.refuse(problem[0], name=name, reason=problem[1])
        if not isinstance(e, dict) or set(e) != {"sha256", "size", "mode"}:
            raise refusals.refuse("files.manifest.invalid", detail=f"the entry for {name!r} is not a hash, a size and a mode")
        if not isinstance(e["sha256"], str) or not HASH_RE.fullmatch(e["sha256"]):
            raise refusals.refuse("files.manifest.invalid", detail=f"the hash for {name!r} is not a SHA-256")
        if not isinstance(e["size"], int) or isinstance(e["size"], bool) or e["size"] < 0:
            raise refusals.refuse("files.manifest.invalid", detail=f"the size for {name!r} is not a number of bytes")
        if e["mode"] not in MODES:
            raise refusals.refuse("files.manifest.invalid", detail=f"the mode for {name!r} is not one of {', '.join(MODES)}")
    return manifest


def hashes(manifest):
    return sorted({e["sha256"] for e in manifest.values()})
