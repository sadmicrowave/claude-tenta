"""`ct-admin action add`: register a root-level action the way `adopt` registers a class: one command, one preview, one question.

The definition is a YAML file (or a pack folder with actions/ and classes/) saying what the action runs, what it may write and which class runs it. It has no
`sha256`: this fetches the program's fingerprint from the worker itself, so you review the real program and its pin together. The result is an ordinary registry
change (a branch, merged and signed by the same code `adopt` uses). Nothing here runs anything as root.
"""
import json
from pathlib import Path

from .. import actions as actions_mod
from .. import render
from ..client import refuse
from ..client.repo import ClientError

HEADER = "  # root-level action: added with ct-admin action add; the worker only runs a program whose SHA-256 is the one below"


def load_definition(source):
    """{'actions': {...}, 'classes': {...}} from a YAML file, or from a pack folder (its actions/*.yaml and classes/*.yaml). Raises ClientError in plain words."""
    import yaml                                          # PyYAML: only these functions need it, so ct-admin starts without it
    path = Path(source).expanduser()
    files = sorted(list((path / "actions").glob("*.yaml")) + list((path / "classes").glob("*.yaml"))) if path.is_dir() else [path]
    if not files or not all(f.is_file() for f in files):
        raise refuse.error("action.source.missing", ("Path", str(source)), example="ct-admin action add packs/fan-control")
    out = {"actions": {}, "classes": {}}
    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            raise refuse.error("action.file.unreadable", ("File", str(f)), reason=str(e).splitlines()[0])
        if not isinstance(data, dict) or set(data) - {"actions", "classes"}:
            raise refuse.error("action.file.shape", ("File", f.name))
        for kind in out:
            for name, body in (data.get(kind) or {}).items():
                if name in out[kind]:
                    raise refuse.error("action.duplicate", ("Action", name), kind=kind)
                out[kind][name] = body
    if not out["actions"]:
        raise refuse.error("action.none")
    for name, body in out["classes"].items():
        if not isinstance(body, dict) or body.get("kind") != "privileged" or body.get("action") not in out["actions"]:
            raise refuse.error("action.class.unlinked", ("Class", name))
    return out


def program_of(action):
    run = action.get("run") if isinstance(action, dict) else None
    return run[0] if isinstance(run, list) and run and isinstance(run[0], str) else None


def ask_worker(ctx, path):
    """The worker's own facts about the program (hash, owner, mode), through its read-only command; raises ClientError when it cannot be a root-level action."""
    r = ctx.ssh.capture(f"{render.WORKER_CMD} action-hash --json {ctx.ssh.quote([path])}")
    try:
        info = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise refuse.error("action.worker.silent", ("Worker", ctx.name), ("Path", str(path)), reason=((r.stderr or "").strip().splitlines()[-1:] or ["No error text was returned."])[-1])
    if not info.get("ok"):
        raise refuse.error("action.path.invalid", ("Path", str(path)), problem=info.get("problem") or "The worker did not say why.")
    return info


def entry_lines(name, action, digest):
    """The action as registry.yaml text, with the pin the worker reported."""
    import yaml
    body = {k: v for k, v in action.items() if k != "sha256"}
    body["sha256"] = digest
    text = yaml.safe_dump({name: body}, sort_keys=False, default_flow_style=None, width=140)
    return ["", HEADER] + ["  " + l for l in text.rstrip().splitlines()]


def class_lines(name, body):
    import yaml
    return [""] + ["  " + l for l in yaml.safe_dump({name: body}, sort_keys=False, default_flow_style=False).rstrip().splitlines()]


def summary(name, action, info):
    """Rows for the preview: everything the worker will be allowed to do as root, in plain words."""
    run = " ".join(action["run"])
    params = action.get("params") or {}
    return [("Program", info["path"]), ("Pinned to", info["sha256"]), ("Runs", run),
            *([("Arguments", ", ".join(f"{k} ({_spec(v)})" for k, v in params.items()))] if params else []),
            ("May write", ", ".join(action.get("writable") or []) or "nothing"), ("Network", "yes" if action.get("network") else "none"),
            ("Time limit", f"{action.get('timeout_s', 60)} s"), ("Each run", "needs your signature on that exact job; the worker's own approval is not enough")]


def card_info(target, name, action, info, again):
    """The wizard's own card for this action (see ct.setup.tui): framed in the root colour, the facts aligned, the risk in words, and Register / Cancel buttons."""
    rows = [(k, v) for k, v in summary(name, action, info) if k != "Each run"]
    return {"label": "Root-level action", "tag": "runs as root", "code": "root", "title": f"{'Pin again' if again else 'Register'} {name}",
            "why": action.get("description") or f"Lets jobs of its class run {info['path']} as root, and nothing else.",
            "rows": rows, "risk": "This program runs as root, outside the jail that limits every other job. Read it first. Each run still needs your signature on that exact job.",
            "now": "registered; only the pin changes" if again else "not registered", "steps": [f"ssh {target} cat {info['path']}"],
            "apply": "Register", "skip": "Cancel", "lockout": False}


def card_remove_info(target, name, action, classes):
    """The wizard's card for taking an action away: framed in the failure colour, what goes and what stays, Remove / Cancel buttons."""
    return {"label": "Root-level action", "tag": "removal", "code": "bad", "title": f"Remove {name}",
            "why": "Jobs of its class will no longer be able to run this program as root.",
            "rows": [("Program", " ".join(action.get("run") or [])), ("Class removed", ", ".join(classes) or "none"), ("Queued jobs", "any still waiting for it are rejected"),
                     ("The program", "stays on the worker, untouched")],
            "risk": "The change is signed and installed on the worker at once. You can register it again with ct-admin action add.", "now": "registered",
            "steps": [], "apply": "Remove", "skip": "Cancel", "lockout": False}


def _spec(v):
    if isinstance(v, dict) and "enum" in v:
        return "one of " + "/".join(map(str, v["enum"]))
    if isinstance(v, dict) and "int" in v:
        return f"a number {v['int'][0]} to {v['int'][1]}"
    return "text of a fixed shape"


def check_definition(defn):
    """Problems the worker would refuse the definition for, before anything is written (the hash is checked separately). The same rules the registry uses."""
    fake = {n: {**a, "sha256": "0" * 64} for n, a in defn["actions"].items()}
    return actions_mod.validate(fake)
