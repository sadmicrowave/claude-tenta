"""worker-state.json: what one worker is, published so the administrator's computer can compare workers without logging in to any of them.

Two kinds of fact. The ENVIRONMENT changes rarely (release, operating system, processor, which Claude sign-in and tool, packages the installed classes need,
the trusted approver keys, whether the worker is locked): it is published when it changes, and the scheduler's candidate choice is only valid for the
environment it was made against. What is OBSERVED changes constantly (load, heat, free memory, queue length): it is only refreshed at most every half hour,
so it is a recent reading, not a live one. `status` on the worker is the live view.
"""
import hashlib
import json
import os
import re
import shutil

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


def tool_check(registry, roots, mode):
    """{class: what its program would be}, for every command class that names tool folders: the program (the first word of its command), where it is found (the first folder, in the order the class lists them,
    that holds an executable of that name; otherwise the system's own folders; otherwise not found), and each folder's state here (does it exist, does this worker allow it). It is what a person
    needs before approving a job of such a class: which tool will really run. Looked at on the worker, because only it can see its own folders."""
    from .registry import inside
    from .util import SAFE_PATH
    real_roots = [os.path.realpath(r) for r in (roots or [])]
    out = {}
    for name, c in ((registry or {}).get("classes") or {}).items():
        tools = c.get("tools")
        if not tools or c.get("kind", "command") != "command":
            continue
        run = c.get("run") or []
        prog = run[0] if run else ""
        folders = []
        for t in tools:
            r = os.path.realpath(t)
            folders.append({"path": t, "exists": os.path.isdir(r), "allowed": mode == "relaxed" and inside(r, real_roots)})
        found, where = None, None
        if prog:
            if "/" in prog:
                found = prog if os.path.isfile(prog) and os.access(prog, os.X_OK) else None
                where = "tool folder" if found and any(inside(os.path.realpath(prog), [os.path.realpath(f["path"])]) for f in folders if f["allowed"]) else "path"
            else:
                for f in folders:
                    cand = os.path.join(os.path.realpath(f["path"]), prog)
                    if f["exists"] and f["allowed"] and os.path.isfile(cand) and os.access(cand, os.X_OK):
                        found, where = cand, "tool folder"
                        break
                if not found:
                    found = shutil.which(prog, path=SAFE_PATH)
                    where = "system" if found else None
        out[name] = {"program": prog, "found": found, "where": where, "folders": folders}
    return out


def transport_facts(configured, origin_url):
    """{'transport': what this worker really fetches jobs over, 'transport_configured': what its configuration says (only when that differs)}. The truth is where its queue copy points (that is where
    `git fetch` goes), not the configuration: a half-moved install can have one and not the other (seen on a real worker, whose configuration said direct while it still fetched from GitHub)."""
    actual = ("github" if "github.com" in origin_url else "direct") if origin_url else configured
    return {"transport": actual, **({"transport_configured": configured} if configured != actual else {})}


def build(*, snapshot, registry, extra, jail, locked, now_iso):
    """snapshot: Worker.snapshot(); extra: {name, release, os, cpu, claude, approvers, native_claude}; returns (environment dict, full document)."""
    environment = {
        "name": extra.get("name"), "release": extra.get("release"), "os": extra.get("os") or {},
        "hardware": {"cores": snapshot.get("cores"), "mem_total_mb": snapshot.get("mem_total_mb"), "cpu": extra.get("cpu") or "",
                     "native_claude": extra.get("native_claude"), "capacity": extra.get("capacity")},
        "claude": extra.get("claude") or {"auth": "none"},
        "isolation": {"ready": bool(jail.ready()), "method": jail.name, "mode": extra.get("isolation_mode") or "full", "purpose": extra.get("isolation_purpose") or "tenta-only", "tool_roots": list(extra.get("tool_roots") or [])},
        "platform": extra.get("platform") or "linux",
        "classes": sorted(((registry or {}).get("classes") or {})),
        **({"tool_check": checks} if (checks := tool_check(registry, extra.get("tool_roots"), extra.get("isolation_mode"))) else {}),         # which tool a class with tool folders would really run
        "packages": packages_of(registry),
        "approvers": list(extra.get("approvers") or []),
        **({"profile_time": extra["profile_time"]} if extra.get("profile_time") else {}),      # what a profile run's time limit is built from: the speed factor and where it came from, the margin, the owner's ceiling
        "profile_approval": extra.get("profile_approval") or "required",              # what this worker will really do with a profile run: "none" only when the owner allowed it AND its jail isolates
        "locked": bool(locked),
        "claude_paused": bool((extra.get("claude_budget") or {}).get("paused")),
        "transport": extra.get("transport"), "pickup_max_s": extra.get("pickup_max_s"),
        **({"transport_configured": extra["transport_configured"]} if extra.get("transport_configured") else {}),
    }
    observed = {k: snapshot.get(k) for k in ("load_1m", "temp_c", "mem_available_mb", "disk_free_gb", "running", "waiting")}
    observed["at"] = snapshot.get("at")
    observed["job_ceiling_mb"] = extra.get("job_ceiling_mb")            # the largest job file this worker accepts right now (it moves with free disk and memory, so it is a reading, not a fact)
    if extra.get("claude_budget") is not None:
        observed["claude_today"] = extra["claude_budget"]              # today's use and the limits (counts only: no job names)
    return environment, {"v": VERSION, "updated": now_iso, **environment, "observed": observed}


def digest(environment):
    return hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
