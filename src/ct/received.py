"""The worker's own record of every job that reached it through the Direct mailbox (security spot-check F4, docs/security/DIRECT-PROFILE-APPROVAL.md).

On Direct the mailbox key alone queues a job, and a profile run may start without approval. If that key is ever copied, the owner needs to see what it was used for, from the worker's
side, not from the client's own notes. One JSON line per job, written the first time the worker looks at the job file, before anything runs: when, the job id, its class and kind,
the SHA-256 of the exact file, its size, and what the worker did with it (queued, waiting for approval, started with no approval needed, rejected and the refusal code).

It holds no job contents, parameters or file names. It is kept on the worker as the worker account, so a job that already runs as that account could alter it: it is evidence of
use of the key, not a tamper-proof log. It is capped (one file and its predecessor) so it can never fill the disk. Standard library only."""
import json
from pathlib import Path

from .util import iso

NAME = "received-jobs.jsonl"
CAP_BYTES = 512 * 1024                      # at this size the file becomes <name>.1 (replacing the older one): the record is the last ~1 MB, a few thousand jobs
OUTCOMES = ("queued", "awaiting_approval", "started_unapproved", "rejected", "error")


class Received:
    def __init__(self, state, clock=None):
        self.path = Path(state) / NAME
        self.clock = clock
        self._seen = None

    def _ids(self):
        if self._seen is None:
            self._seen = {r.get("id") for r in self.read(10 ** 9)}
        return self._seen

    def record(self, jid, raw, outcome, *, cls=None, kind=None, code=""):
        """Write the entry for a job the first time it is seen. Never raises: a record that cannot be written must not stop a job (the failure goes to the caller as False)."""
        import hashlib
        try:
            if jid in self._ids():
                return True
            entry = {"at": iso(self.clock()) if self.clock else iso(), "id": jid, "class": cls if isinstance(cls, str) else None, "kind": kind if isinstance(kind, str) else None,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "outcome": outcome, "refusal": code or None}
            if self.path.exists() and self.path.stat().st_size >= CAP_BYTES:
                self.path.replace(self.path.with_name(NAME + ".1"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
            self._ids().add(jid)
            return True
        except OSError:
            return False

    def read(self, limit=50):
        """The newest `limit` entries, oldest first (the previous file is read too when this one is short)."""
        out = []
        for p in (self.path.with_name(NAME + ".1"), self.path):
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        out.append(r)
            except OSError:
                continue
        return out[-limit:]
