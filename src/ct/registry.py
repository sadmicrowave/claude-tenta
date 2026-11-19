"""The registry: which job classes may run, and the limits and rules around them.

The worker trusts ONLY its installed, root-owned copy. Everything here is pure checking of that data, so a typo can be refused before a
registry is installed instead of breaking every pass afterwards. PyYAML is imported only when a file is actually read.
"""
import json
import re
from pathlib import Path

from .guard import GUARD_NAMES
from .pkgspec import check_packages, is_pinned

COST_W = {"light": 1, "medium": 2, "heavy": 4}      # capacity points; a heavy job runs alone on a capacity-4 machine
CLASS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
PLACEHOLDER = re.compile(r"^\{(\w+)\}$")
POS_INT_KEYS = ("patch_max_kb", "timeout_s", "max_timeout_s", "max_rss_mb", "max_temp_c", "max_disk_mb", "max_turns", "output_max_chars", "max_running")
HELPER_TOOLS = ("Task", "Agent")                    # tools that start helper agents with turns of their own: never on a device, whatever the class says (see docs/design/SINGLE-JOB-DEVICES.md)
RISKY_TOOLS = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch")        # write, run or fetch: only for a class marked `reviewed: true`


def tool_name(entry):
    """'Bash(git log:*)' -> 'Bash': the tool a permission entry is about."""
    return str(entry).split("(", 1)[0].strip()
ENV_KEYS = ("PYTHONPATH", "NODE_PATH")              # the only variables a class may set, and only to folders under the tools folder
MAX_OUT = 100_000
BASELINE_DENY = ("sudo", "su", "doas", "pkexec")      # always refused, whatever any pack says: privilege tools have no place in a job

# Size limits the client must know too. The installed registry's `limits:` section overrides any of these.
LIMITS_DEFAULTS = {
    "files": {"max_count": 200, "max_file_kb": 500, "max_total_kb": 2000, "max_batches": 10},
    "output_max_chars": MAX_OUT,
    "packages": {"max_download_mb": 1000, "max_env_mb": 2000, "require_pins": True},
}

# Defaults for the verdict rules of a profile run. The installed registry's `profiling:` section overrides any of these.
PROFILING_DEFAULTS = {
    "mem_ceiling_frac": 0.6,   # hard stop during a profile run: this fraction of installed RAM
    "temp_ceiling_c": 85,      # hard stop: hottest core
    "disk_ceiling_mb": 4096,   # hard stop: files the command writes
    "fit": {"rss_frac_max": 0.5, "wall_s_max": 1800, "temp_c_max": 82},
    "cost": {"heavy": {"rss_frac": 0.25, "wall_s": 300, "temp_c": 78, "cores_busy": 1.5, "min_wall_s": 60},
             "medium": {"rss_frac": 0.10, "wall_s": 30, "cores_busy": 1.0}},
    "margin": {"time": 3, "rss": 2, "temp_c": 8},
}


def limits_of(reg):
    out = json.loads(json.dumps(LIMITS_DEFAULTS))
    for k, v in ((reg or {}).get("limits") or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def profiling_rules(reg):
    def merge(a, b):
        out = dict(a)
        for k, v in (b or {}).items():
            out[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
        return out
    return merge(PROFILING_DEFAULTS, (reg or {}).get("profiling"))


def policy_of(reg):
    """The off-limits rules the worker enforces at job time: {'deny_commands': set of program names, 'deny_params': [(compiled regex, reason)]}.
    The baseline is always included, so a registry with no policy section is still safe."""
    section = (reg or {}).get("policy") or {}
    deny = set(BASELINE_DENY) | set(section.get("deny_commands") or [])
    params = []
    for item in section.get("deny_params") or []:
        try:
            params.append((re.compile(item["regex"]), str(item.get("reason", "forbidden by policy"))))
        except (re.error, KeyError, TypeError):
            continue
    return {"deny_commands": deny, "deny_params": params}


def policy_problems(given):
    if given is None:
        return []
    if not isinstance(given, dict):
        return ["policy: must be a mapping"]
    errs = [f"policy: unknown setting {k!r}" for k in given if k not in ("deny_commands", "deny_params")]
    dc = given.get("deny_commands", [])
    if not (isinstance(dc, list) and all(isinstance(x, str) for x in dc)):
        errs.append("policy.deny_commands: must be a list of program names")
    dp = given.get("deny_params", [])
    if not (isinstance(dp, list) and all(isinstance(x, dict) and isinstance(x.get("regex"), str) for x in dp)):
        errs.append("policy.deny_params: must be a list of {regex, reason}")
    else:
        for x in dp:
            try:
                re.compile(x["regex"])
            except re.error:
                errs.append(f"policy.deny_params: {x['regex']!r} is not a valid pattern")
    return errs


def limits_problems(given):
    if given is None:
        return []
    if not isinstance(given, dict):
        return ["limits: must be a mapping"]
    errs = []
    for k, v in given.items():
        base = LIMITS_DEFAULTS.get(k)
        if base is None:
            errs.append(f"limits: unknown setting {k!r} (known: {', '.join(LIMITS_DEFAULTS)})")
        elif isinstance(base, dict):
            if not isinstance(v, dict):
                errs.append(f"limits.{k}: must be a mapping")
                continue
            for kk, vv in v.items():
                if kk not in base:
                    errs.append(f"limits.{k}: unknown setting {kk!r} (known: {', '.join(base)})")
                elif isinstance(base[kk], bool):
                    if not isinstance(vv, bool):
                        errs.append(f"limits.{k}.{kk}: must be true or false")
                elif isinstance(vv, bool) or not isinstance(vv, int) or vv <= 0:
                    errs.append(f"limits.{k}.{kk}: must be a positive integer")
        elif isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            errs.append(f"limits.{k}: must be a positive integer")
    return errs


def _param_ok(spec):
    ok = isinstance(spec, dict) and len([k for k in ("enum", "int", "regex") if k in spec]) == 1
    if ok and "int" in spec:
        ok = isinstance(spec["int"], list) and len(spec["int"]) == 2 and all(isinstance(x, int) for x in spec["int"])
    if ok and "enum" in spec:
        ok = isinstance(spec["enum"], list) and bool(spec["enum"])
    if ok and "regex" in spec:
        try:
            re.compile(spec["regex"])
        except (re.error, TypeError):
            ok = False
    return ok


def inside(path, roots):
    """True when `path` is one of `roots` or under one of them (compared as plain paths: no '..' is ever accepted before this is asked)."""
    return any(path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/") for r in roots)


def tools_problems(w, tools, kind, tool_roots):
    """Problems with a class's `tools` (a list of folders of tools that already exist on the worker). Structural always; against the worker's own allowed folders when `tool_roots` is given
    (a list, possibly empty: this worker allows none)."""
    if kind != "command":
        return [f"{w}: only a command class can name tool folders"]
    if not isinstance(tools, list) or not tools or any(not isinstance(t, str) for t in tools):
        return [f"{w}: tools must be a list of folders"]
    errs = []
    for t in tools:
        if not t.startswith("/") or ".." in t.split("/") or "//" in t or len(t) > 200 or not re.fullmatch(r"/[A-Za-z0-9._/-]+", t):
            errs.append(f"{w}: tool folder {t!r} must be an absolute path of letters, digits and . _ - / only (no spaces, no ..)")
        elif tool_roots is not None and not inside(t, tool_roots):
            errs.append(f"{w}: tool folder {t} is not inside a folder this worker allows (isolation.tool_roots"
                        + (": " + ", ".join(tool_roots) if tool_roots else ": none; set isolation.mode = relaxed and list the folder") + ")")
    if len(set(tools)) != len(tools):
        errs.append(f"{w}: tools lists a folder twice")
    return errs


def validate(reg, tools_root, allow_empty=False, tool_roots=None):
    """Return a list of problems (empty = fine). `tools_root` is the folder a class's `env` paths must stay under. `allow_empty` accepts a
    registry with no classes of its own (your private layer: the packs provide the rest)."""
    if not isinstance(reg, dict):
        return ["the registry must be a YAML mapping"]
    errs = policy_problems(reg.get("policy"))
    classes = reg.get("classes")
    if classes is None and allow_empty:
        classes = {}
    if not isinstance(classes, dict) or (not classes and not allow_empty):
        return ["classes: must be a non-empty mapping"]
    if "profiling" in reg and not isinstance(reg["profiling"], dict):
        errs.append("profiling: must be a mapping")
    errs += limits_problems(reg.get("limits"))
    from . import actions as actions_mod                      # imported here: actions needs this module's helpers
    errs += actions_mod.validate(reg.get("actions"), (reg.get("policy") or {}).get("deny_commands") or [])
    tools_root = str(tools_root)
    for name, c in classes.items():
        w = f"class {name!r}"
        if not isinstance(name, str) or not CLASS_NAME_RE.match(name):
            errs.append(f"{w}: name must be lowercase letters, digits and dashes")
        if not isinstance(c, dict):
            errs.append(f"{w}: must be a mapping")
            continue
        kind = c.get("kind", "command")
        if kind not in ("command", "claude", "profile", "privileged"):
            errs.append(f"{w}: unknown kind {kind!r}")
        if c.get("cost", "medium") not in COST_W:
            errs.append(f"{w}: cost must be light, medium or heavy")
        if c.get("tier", "approve") not in ("auto", "approve"):
            errs.append(f"{w}: tier must be auto or approve")
        for k in POS_INT_KEYS:
            if k in c and (isinstance(c[k], bool) or not isinstance(c[k], int) or c[k] <= 0):
                errs.append(f"{w}: {k} must be a positive integer")
        if "network" in c and not isinstance(c["network"], bool):
            errs.append(f"{w}: network must be true or false")
        if "packages" in c:
            try:
                if check_packages(c["packages"]) != sorted(c["packages"]):
                    errs.append(f"{w}: packages must be sorted and unique")
                elif limits_of(reg)["packages"].get("require_pins", True):
                    loose = [p for p in c["packages"] if not is_pinned(p)]
                    if loose:
                        errs.append(f"{w}: pin an exact version for {', '.join(loose)} (for example npm:eslint@9.12.0 or pip:pyflakes==3.2.0); "
                                    "a profile run fetches the current version and drafts the pinned entry for you")
            except (ValueError, TypeError) as e:
                errs.append(f"{w}: {e}")
        if "env" in c:
            ev = c["env"]
            if not isinstance(ev, dict) or any(k not in ENV_KEYS or not isinstance(v, str) or
                                               not all(x.startswith(tools_root + "/") or x == tools_root for x in v.split(":"))
                                               for k, v in ev.items()):
                errs.append(f"{w}: env may only set {', '.join(ENV_KEYS)} to folders under {tools_root}")
        if "tools" in c:
            errs += tools_problems(w, c["tools"], kind, tool_roots)
        if "batchable" in c and not isinstance(c["batchable"], bool):
            errs.append(f"{w}: batchable must be true or false")
        if "returns" in c and kind != "claude":
            errs.append(f"{w}: only a claude class can return a patch")
        if c.get("returns") == "patch" and not c.get("accepts_files"):
            errs.append(f"{w}: a class that returns a patch needs accepts_files: true (the patch is the difference from the files it was sent)")
        if c.get("batchable") and not c.get("accepts_files"):
            errs.append(f"{w}: batchable needs accepts_files: true")
        if "output_keys" in c and not (isinstance(c["output_keys"], list) and c["output_keys"]
                                       and all(isinstance(k, str) for k in c["output_keys"])):
            errs.append(f"{w}: output_keys must be a non-empty list of field names")
        allow = c.get("output_allow", [])
        if not isinstance(allow, list) or any(x not in GUARD_NAMES - {"canary"} for x in allow):
            errs.append(f"{w}: output_allow may only list: " + ", ".join(sorted(GUARD_NAMES - {"canary"})))
        params = c.get("params") or {}
        if not isinstance(params, dict):
            errs.append(f"{w}: params must be a mapping")
            params = {}
        for pn, spec in params.items():
            if not _param_ok(spec):
                errs.append(f"{w}: param {pn!r} needs exactly one of enum / int [lo, hi] / regex")
        if kind == "command":
            run = c.get("run")
            if not isinstance(run, list) or not run or not all(isinstance(t, str) and t for t in run):
                errs.append(f"{w}: run must be a non-empty list of strings")
            else:
                for t in run:
                    m = PLACEHOLDER.match(t)
                    if m and m.group(1) not in params:
                        errs.append(f"{w}: run uses {{{m.group(1)}}} but no such param is declared")
        elif kind == "claude":
            if "returns" in c and c["returns"] != "patch":
                errs.append(f"{w}: returns may only be patch (the work comes back as a patch inside the result)")
            tools = c.get("allowed_tools")
            if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
                errs.append(f"{w}: allowed_tools must be a non-empty list of strings")
            else:
                names = [tool_name(t) for t in tools]
                for bad in [n for n in names if n in HELPER_TOOLS]:
                    errs.append(f"{w}: allowed_tools names {bad}, which starts helper agents. A device runs one job and does not start helpers; remove it")
                risky = [n for n in names if n in RISKY_TOOLS or n.startswith("mcp__")]
                if risky and c.get("reviewed") is not True:
                    errs.append(f"{w}: allowed_tools names {', '.join(sorted(set(risky)))}, which can write, run commands or fetch. Say you have read what the class can do by adding `reviewed: true`, or remove them")
        elif kind == "profile" and c.get("run"):
            errs.append(f"{w}: a profile class takes its command from each job, so it must not set run")
        elif kind == "privileged":
            act = c.get("action")
            if not isinstance(act, str) or act not in (reg.get("actions") or {}):
                errs.append(f"{w}: action must name one of the registry's actions ({', '.join(sorted(reg.get('actions') or {})) or 'none are defined'})")
            if c.get("tier", "approve") != "approve":
                errs.append(f"{w}: a privileged class always needs approval (tier: approve); it runs as root")
            for forbidden in ("run", "params", "packages", "network", "env", "accepts_files", "batchable", "kind_extra"):
                if forbidden in c:
                    errs.append(f"{w}: a privileged class takes its program, parameters and limits from its action, so it must not set {forbidden}")
    return errs


def parse(text):
    """YAML text -> data. PyYAML is imported here so the rest of the package works without it."""
    import yaml
    return yaml.safe_load(text) or {}


def load(path, tools_root):
    """Read and validate a registry file. Returns (registry, limits); raises ValueError listing every problem."""
    try:
        reg = parse(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror}")
    except Exception as e:                                   # yaml.YAMLError, without importing yaml at module level
        raise ValueError(f"{path} is not valid YAML: {e}")
    problems = validate(reg, tools_root)
    if problems:
        raise ValueError("; ".join(problems))
    return reg, limits_of(reg)
