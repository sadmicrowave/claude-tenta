"""Profile runs: measure one command the administrator approved, and decide whether the worker should offload it.

A profile run executes an exact command once, with hard ceilings, and its measurements become a verdict:
  fits | does-not-fit | inconclusive, plus a cost class (light/medium/heavy) and a DRAFTED registry entry with limits that are the measured
  values times a margin. Nothing is installed or allowed by a verdict: the administrator still reviews and applies the entry.
`limiting` records which resource made a command not fit, so an exclusion can be retried after the hardware changes.
"""
import math
import os
import shutil

from .runner import MAX_TIMEOUT, run_group
from .util import SAFE_PATH

# Commands whose arguments ARE the program: what they will do cannot be judged from the command name.
INTERPRETERS = {"python", "python3", "bash", "sh", "dash", "zsh", "node", "perl", "ruby", "php", "env", "xargs",
                "awk", "gawk", "sed", "find", "nice", "timeout", "time", "watch", "script"}


def number(x, unit=""):
    return f"{x:,.0f}{unit}" if abs(x) >= 10 or float(x).is_integer() else f"{x:.1f}{unit}"


def command_facts(argv, find_program=None, lookup=None):
    """What the worker itself can say about the command, independent of what the requester claims about it."""
    find_program = find_program or (lambda name: shutil.which(name, path=SAFE_PATH))

    def default_lookup(*cmd):          # extra information only: a missing tool must never break the job
        try:
            r = run_group(list(cmd), 5)
            return r.stdout.strip() if r.returncode == 0 else ""
        except OSError:
            return ""
    lookup = lookup or default_lookup
    first = argv[0]
    path = first if "/" in first else find_program(first)
    facts = {"binary": path}
    if path:
        real = os.path.realpath(path)
        try:
            facts["size_kb"] = max(1, os.path.getsize(real) // 1024)
        except OSError:
            pass
        pkg = lookup("dpkg", "-S", real)
        if pkg:
            facts["package"] = pkg.split(":")[0].strip()
        line = (lookup("whatis", os.path.basename(real)).splitlines() or [""])[0]
        if line and "nothing appropriate" not in line:
            facts["summary"] = line[:200]
    if os.path.basename(first) in INTERPRETERS:
        facts["interpreter"] = True        # the arguments ARE the program
    return facts


def profile_limits(rules, mem_total_mb):
    """The hard stops for one profile run: a fraction of installed RAM, a temperature, a disk write ceiling."""
    return {"max_rss_mb": int(mem_total_mb * rules["mem_ceiling_frac"]) if mem_total_mb else None,
            "max_temp_c": rules["temp_ceiling_c"], "max_disk_mb": rules["disk_ceiling_mb"]}


def glance(job, rules, limits, snapshot, timeout_s, facts=None, product="the worker"):
    """The numbers an approver wants at a glance: the requester's estimate, the hard stop this run is held to, and how much headroom the
    worker has right now. Plain text, rendered once so every screen shows the same thing. `snapshot` is the machine's current state."""
    snap, n = snapshot, number
    est = lambda k, unit="": "~" + n(job[k], unit) if job.get(k) is not None else "not estimated"  # noqa: E731
    rows = [
        ("", "Estimate", "Hard stop for this run", f"{product} now ({snap['at'][11:19]} UTC)"),
        ("Time", est("est_time_s", " s"), f"{n(timeout_s)} s", f"{snap['running']} running, {snap['waiting']} waiting (this run goes alone)"),
        ("Memory", est("est_mem_mb", " MB"),
         f"{n(limits['max_rss_mb'])} MB ({rules['mem_ceiling_frac']:.0%} of RAM)" if limits.get("max_rss_mb") else "-",
         f"{n(snap['mem_available_mb'])} MB free of {n(snap['mem_total_mb'])} MB" if snap["mem_available_mb"] is not None else "-"),
        ("CPU", est("est_cpu_cores", " core"), f"none ({snap['cores']} cores)", f"load {snap['load_1m']}"),
        ("Disk written", est("est_disk_mb", " MB"), f"{n(limits['max_disk_mb'])} MB",
         f"{snap['disk_free_gb']} GB free" if snap["disk_free_gb"] is not None else "-"),
        ("Heat", "not estimated", f"stops at {n(limits['max_temp_c'])}C",
         f"core now {n(snap['temp_c'])}C" if snap["temp_c"] is not None else "sensor unreadable"),
    ]
    w = [max(len(r[i]) for r in rows) for i in range(4)]
    out = ["AT A GLANCE"] + ["  " + "   ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows]
    if facts and facts.get("binary"):
        size = f" ({n(facts['size_kb'])} KB)" if facts.get("size_kb") else ""
        out.append(f"  Program: {facts['binary']}{size}" + (f", package {facts['package']}" if facts.get("package") else ""))
    return "\n".join(out)


def _round_up(x, step):
    return int(math.ceil(x / step) * step)


def profile_verdict(status, rc, note, m, argv, files, rules, product="the worker"):
    """Turn one profiling run's measurements into a decision. Returns {verdict, reasons, limiting, [cost, entry]}."""
    total = m.get("mem_total_mb") or 0
    peak, wall, cores = m.get("peak_rss_mb") or 0, m.get("wall_s") or 0, m.get("cores_busy")
    temp, disk = m.get("peak_temp_c"), m.get("disk_mb") or 0
    frac = peak / total if total else 0
    fit, cost = rules["fit"], rules["cost"]
    why, limiting = [], []
    if status == "limit":
        limiting = ["memory" if "memory" in note else "heat" if "temperature" in note else "disk"]
        return {"verdict": "does-not-fit", "reasons": [note + " while profiling"], "limiting": limiting}
    if status == "timeout":
        return {"verdict": "does-not-fit", "limiting": ["time"], "reasons": [f"did not finish within the {m.get('wall_s')}s profiling limit"]}
    if status != "ok":
        return {"verdict": "inconclusive", "limiting": [],
                "reasons": [f"the command did not complete cleanly (status {status}, exit {rc}); fix the command and profile it again"]}
    if frac > fit["rss_frac_max"]:
        why.append(f"peak memory {peak} MB is {frac:.0%} of {product}'s {total} MB")
        limiting.append("memory")
    if wall > fit["wall_s_max"]:
        why.append(f"took {wall:.0f}s, over the {fit['wall_s_max']}s limit")
        limiting.append("time")
    if temp is not None and temp > fit["temp_c_max"]:
        why.append(f"CPU reached {temp}C, over the {fit['temp_c_max']}C limit")
        limiting.append("heat")
    if why:
        return {"verdict": "does-not-fit", "reasons": why, "limiting": limiting}
    h, md = cost["heavy"], cost["medium"]
    heavy = frac > h["rss_frac"] or wall > h["wall_s"] or (temp is not None and temp > h["temp_c"]) or \
        (cores is not None and cores >= h["cores_busy"] and wall >= h["min_wall_s"])
    medium = frac > md["rss_frac"] or wall > md["wall_s"] or (cores is not None and cores > md["cores_busy"])
    c = "heavy" if heavy else "medium" if medium else "light"
    mg = rules["margin"]
    entry = {"tier": "auto", "cost": c, "run": argv,
             "timeout_s": min(MAX_TIMEOUT, max(30, _round_up(wall * mg["time"], 10))),
             "max_rss_mb": max(100, _round_up(peak * mg["rss"], 50)),
             "max_temp_c": min(rules["temp_ceiling_c"], max(70, round((temp or 70) + mg["temp_c"])))}
    if disk >= 1:
        entry["max_disk_mb"] = max(100, _round_up(disk * mg["rss"], 50))
    if files:
        entry["accepts_files"] = True
    return {"verdict": "fits", "cost": c, "limiting": [], "entry": entry,
            "reasons": [f"{wall:.1f}s, {peak} MB peak ({frac:.0%} of RAM), " + (f"{cores} cores busy, " if cores is not None else "")
                        + f"{str(temp) + 'C' if temp is not None else 'temp unknown'}: cost {c}"]}
