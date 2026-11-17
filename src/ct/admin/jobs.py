"""Review and approve jobs. A job is approved only as the exact file you looked at: the approval carries its SHA-256 and the worker
refuses it if its own copy differs. `review` remembers what you were shown, so `approve --all` can approve a set in one go without
re-reading, but only the ones that have not changed since."""
import hashlib
import json
import re
from pathlib import Path

from .. import review as review_mod
from ..client.repo import ClientError

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")


class Viewed:
    """{job id: sha256 of the job file you were shown ("" = the worker's own copy was shown there)} kept in the client home."""

    def __init__(self, home):
        self.path = Path(home) / "viewed.json"

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def mark(self, jid, digest):
        seen = self.load()
        seen[jid] = digest or ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(seen, indent=1, sort_keys=True))


def job_bytes(repos, jid, branches=("jobs", "main")):
    """The job file as it is on GitHub (the jobs branch, or main for old ones), or None."""
    for b in branches:
        text = repos.show_file(f"origin/{b}", f"jobs/{jid}.json")
        if text is not None:
            return text.encode()
    return None


def digest_of(raw):
    return hashlib.sha256(raw).hexdigest()


def waiting(repos):
    """[(id, published result)] for every job the worker shows as waiting for approval."""
    repos.pull_results()
    out = []
    d = repos.results / "results"
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        res = repos.read_result(f.stem)
        if res and res.get("status") == "awaiting_approval" and ID_RE.match(f.stem):
            out.append((f.stem, res))
    return out


def root_banner(job, info, product):
    """What a person must read before approving something that runs as ROOT: the exact program and arguments, where it may write, whether it has a network."""
    from .. import actions
    try:
        argv = " ".join(actions.build_argv(info, job.get("params") or {}))
    except ValueError as e:
        argv = f"{' '.join(info.get('run') or [])}   (these parameters are not allowed: {e})"
    writable = ", ".join(info.get("writable") or []) or "nothing"
    return (f"RUNS AS ROOT on {product}\n  program    {argv}\n  may write  {writable}, and nothing else on the system\n"
            f"  network    {'yes' if info.get('network') else 'none'}\n  limit      {info.get('timeout_s', 30)} s\n"
            "  Your signature is the only thing that lets it run, and it covers exactly this file. Approve only a program and arguments you recognise.\n\n")


def describe(job, pub=None, product="the worker", info=None):
    """The text a person reads before approving. Profile runs get the full review; any other job shows its fields (file bodies by size)."""
    if info and info.get("privileged"):
        shown = {k: v for k, v in job.items() if k != "files"}
        return root_banner(job, info, product) + json.dumps(shown, indent=2)
    if job.get("class") == "profile-run":
        pub = pub or {}
        return review_mod.review_text(job, pub.get("facts"), pub.get("package_text"), pub.get("glance"), product=product)
    shown = {k: v for k, v in job.items() if k != "files"}
    if job.get("files"):
        shown["files"] = {k: f"{len(v)} bytes" for k, v in job["files"].items()}
    return json.dumps(shown, indent=2)


def review_card(job, pub=None, product="the worker", info=None, digest=None):
    """What a person reads before approving, as a card for `ui.say`. Something that runs as ROOT is drawn in the root outline with its program, where it may write, its
    network and its time limit as labelled rows, then the job file itself; a profile run gets the caution outline; anything else the plain rail."""
    from .. import actions, cards, ui
    jid = job.get("id", "")
    pin = [("Digest", f"{digest[:12]}...  approval is pinned to this exact file")] if digest else []
    if info and info.get("privileged"):
        try:
            argv = " ".join(actions.build_argv(info, job.get("params") or {}))
        except ValueError as e:
            argv = f"{' '.join(info.get('run') or [])}   (these parameters are not allowed: {e})"
        shown = {k: v for k, v in job.items() if k != "files"}
        rows = [("Program", argv), ("May write", ", ".join(info.get("writable") or []) or "nothing, and nothing else on the system"), ("Network", "yes" if info.get("network") else "none"),
                ("Limit", f"{info.get('timeout_s', 30)} s"),
                "Your signature is the only thing that lets it run, and it covers exactly this file. Approve only a program and arguments you recognise.",
                cards.Heading("Job file"), cards.Block(json.dumps(shown, indent=2).split("\n"))] + pin
        return dict(title=f"{job.get('class')} on {product}", details=rows, tag="RUNS AS ROOT", color=ui.ROOT)
    return review_mod.text_card(jid, describe(job, pub, product, info), product, digest)


def classify(waiting_jobs, seen, current):
    """Split waiting jobs into (ready, skipped): ready = [(id, digest)] viewed and unchanged, skipped = [(id, why)]."""
    ready, skipped = [], []
    for jid, _ in waiting_jobs:
        raw = current(jid)
        now = digest_of(raw) if raw is not None else ""
        if jid not in seen:
            skipped.append((jid, "not viewed yet"))
        elif raw is None and seen[jid] == "":
            skipped.append((jid, "its file is no longer on GitHub; review it again"))
        elif seen[jid] != now:
            skipped.append((jid, "changed since you viewed it"))
        else:
            ready.append((jid, now))
    return ready, skipped


def require_id(jid):
    if not jid or not ID_RE.match(jid):
        raise ClientError("That is not a job id", "Job ids are lowercase letters, digits and dashes.", example="ct-run jobs")
    return jid
