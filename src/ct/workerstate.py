"""worker-state.json: what one worker is, published so the administrator's computer can compare workers without logging in to any of them.

Two kinds of fact. The ENVIRONMENT changes rarely (release, operating system, processor, which Claude sign-in and tool, packages the installed classes need,
the trusted approver keys, whether the worker is locked): it is published when it changes, and the scheduler's candidate choice is only valid for the
environment it was made against. What is OBSERVED changes constantly (load, heat, free memory, queue length): it is only refreshed at most every half hour,
so it is a recent reading, not a live one. `status` on the worker is the live view.
"""
import hashlib
import json
import re

VERSION = 1
HEARTBEAT_S = 1800


def parse_os_release(text):
    d = dict(m.groups() for m in re.finditer(r'^(\w+)="?([^"\n]*)"?$', text or "", re.M))
    return {"id": d.get("ID", "").lower(), "version": d.get("VERSION_ID", "")}


def cpu_model(cpuinfo_text):
    m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo_text or "", re.M)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def packages_of(registry):
    """Every package the installed classes name (apt:, npm:, pip: entries), sorted: what this worker has had to install to run its classes."""
    out = set()
    for c in ((registry or {}).get("classes") or {}).values():
        for p in c.get("packages") or []:
            out.add(str(p))
    return sorted(out)


def build(*, snapshot, registry, extra, jail, locked, now_iso):
    """snapshot: Worker.snapshot(); extra: {name, release, os, cpu, claude, approvers, native_claude}; returns (environment dict, full document)."""
    environment = {
        "name": extra.get("name"), "release": extra.get("release"), "os": extra.get("os") or {},
        "hardware": {"cores": snapshot.get("cores"), "mem_total_mb": snapshot.get("mem_total_mb"), "cpu": extra.get("cpu") or "",
                     "native_claude": extra.get("native_claude"), "capacity": extra.get("capacity")},
        "claude": extra.get("claude") or {"auth": "none"},
        "isolation": {"ready": bool(jail.ready()), "method": jail.name},
        "classes": sorted(((registry or {}).get("classes") or {})),
        "packages": packages_of(registry),
        "approvers": list(extra.get("approvers") or []),
        "locked": bool(locked),
        "claude_paused": bool((extra.get("claude_budget") or {}).get("paused")),
        "transport": extra.get("transport"), "pickup_max_s": extra.get("pickup_max_s"),
    }
    observed = {k: snapshot.get(k) for k in ("load_1m", "temp_c", "mem_available_mb", "disk_free_gb", "running", "waiting")}
    observed["at"] = snapshot.get("at")
    if extra.get("claude_budget") is not None:
        observed["claude_today"] = extra["claude_budget"]              # today's use and the limits (counts only: no job names)
    return environment, {"v": VERSION, "updated": now_iso, **environment, "observed": observed}


def digest(environment):
    return hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
