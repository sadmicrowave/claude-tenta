"""Packs: the extension unit. A pack is a folder with a `pack.toml` and files; this module finds them, checks them strictly, and composes
what the worker will trust.

    packs/<name>/pack.toml          name, version, description, kinds = ["classes", "policy"], requires.os = ["ubuntu", "debian"]
    packs/<name>/classes/*.yaml     job classes (a `classes:` mapping, nothing else: limits and profiling rules belong to the install)
    packs/<name>/policy/*.toml      off-limits rules: [[deny.command]], [[deny.param]], [[require.approval]]

Rules that keep extension from becoming a hole:
  * Policy can only TIGHTEN. Rules from every pack are added together; there is no `allow`, no override and no removal, so a pack can never
    loosen what another pack (or the built-in baseline) forbids. Unknown keys are errors, so an attempted `allow` is refused, not ignored.
  * A class name belongs to one place. Two packs defining the same class, or your own registry redefining a pack's class, is an error naming
    both, never a silent override (a class you do not expect to be replaced must not be).
  * The server composes from packs that were installed by root (under <config>/packs), never from a repository it fetched.
  * Packs carry data, not code. (Hardening modules run as root; they are not loadable from packs yet.)
Only the standard library plus PyYAML (already needed to read a registry) is used.
"""
import hashlib
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import ui as _ui
from .registry import BASELINE_DENY, PLACEHOLDER  # noqa: F401  (PLACEHOLDER re-exported for pack tests)

PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
KINDS = ("classes", "policy", "actions")
WHEN_KEYS = {"network": bool, "kind": str, "accepts_files": bool}
MANIFEST_KEYS = {"name", "version", "description", "kinds", "requires"}


class PackError(Exception):
    def __init__(self, problems):
        problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Policy:
    deny_commands: dict = field(default_factory=dict)       # program name -> reason
    deny_params: list = field(default_factory=list)         # [(regex, reason)]
    require_approval: list = field(default_factory=list)    # [(when dict, reason)]

    def add(self, other):
        """Tighten only: everything in `other` is added; nothing is ever removed or relaxed."""
        for name, why in other.deny_commands.items():
            self.deny_commands.setdefault(name, why)
        for item in other.deny_params:
            if item not in self.deny_params:
                self.deny_params.append(item)
        for item in other.require_approval:
            if item not in self.require_approval:
                self.require_approval.append(item)
        return self

    def to_registry_section(self):
        return {"deny_commands": sorted(self.deny_commands), "deny_params": [{"regex": rx, "reason": why} for rx, why in self.deny_params]}


@dataclass
class Pack:
    name: str
    version: str
    description: str
    kinds: tuple
    path: Path
    os: tuple = ()
    classes: dict = field(default_factory=dict)
    actions: dict = field(default_factory=dict)
    policy: Policy = field(default_factory=Policy)
    digest: str = ""


def _only(table, allowed, where):
    extra = set(table) - set(allowed)
    return [f"{where}: unknown key {k!r} (allowed: {', '.join(sorted(allowed))})" for k in sorted(extra)]


def parse_policy(text, where="policy"):
    """TOML text -> Policy. Strict: only deny.command, deny.param and require.approval exist."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PackError(f"{where}: not valid TOML: {e}")
    problems, p = _only(data, {"deny", "require"}, where), Policy()
    deny, req = data.get("deny", {}), data.get("require", {})
    problems += _only(deny, {"command", "param"}, f"{where}: [deny]") + _only(req, {"approval"}, f"{where}: [require]")
    for i, e in enumerate(deny.get("command", [])):
        w = f"{where}: deny.command[{i}]"
        problems += _only(e, {"names", "reason", "locked"}, w)
        names = e.get("names")
        if not (isinstance(names, list) and names and all(isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,60}", n) for n in names)):
            problems.append(f"{w}: names must be a non-empty list of program names (no paths)")
            continue
        if not isinstance(e.get("reason"), str) or not e["reason"].strip():
            problems.append(f"{w}: a reason is required")
            continue
        for n in names:
            p.deny_commands.setdefault(n, e["reason"])
    for i, e in enumerate(deny.get("param", [])):
        w = f"{where}: deny.param[{i}]"
        problems += _only(e, {"regex", "reason"}, w)
        try:
            re.compile(e.get("regex"))
        except (re.error, TypeError):
            problems.append(f"{w}: regex is missing or not a valid pattern")
            continue
        if not isinstance(e.get("reason"), str) or not e["reason"].strip():
            problems.append(f"{w}: a reason is required")
            continue
        p.deny_params.append((e["regex"], e["reason"]))
    for i, e in enumerate(req.get("approval", [])):
        w = f"{where}: require.approval[{i}]"
        problems += _only(e, {"when", "reason"}, w)
        when = e.get("when")
        if not isinstance(when, dict) or not when or any(WHEN_KEYS.get(k) is None or not isinstance(v, WHEN_KEYS[k]) for k, v in when.items()):
            problems.append(f"{w}: when must be a table using only {', '.join(f'{k} ({t.__name__})' for k, t in WHEN_KEYS.items())}")
            continue
        if not isinstance(e.get("reason"), str) or not e["reason"].strip():
            problems.append(f"{w}: a reason is required")
            continue
        p.require_approval.append((dict(when), e["reason"]))
    if problems:
        raise PackError(problems)
    return p


def digest_of(path):
    """sha256 over every file's relative name and bytes, in sorted order: what an admin approves is exactly this."""
    h = hashlib.sha256()
    root = Path(path)
    for f in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def load_pack(path):
    """Read and strictly check one pack folder. Raises PackError listing every problem."""
    path, problems = Path(path), []
    manifest = path / "pack.toml"
    try:
        m = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except OSError:
        raise PackError(f"{path}: there is no pack.toml")
    except tomllib.TOMLDecodeError as e:
        raise PackError(f"{manifest}: not valid TOML: {e}")
    problems += _only(m, MANIFEST_KEYS, "pack.toml")
    name = m.get("name")
    if not isinstance(name, str) or not PACK_NAME_RE.match(name):
        problems.append("pack.toml: name must be lowercase letters, digits and dashes")
    if name != path.name:
        problems.append(f"pack.toml: name {name!r} must match the folder name {path.name!r}")
    kinds = m.get("kinds")
    if not (isinstance(kinds, list) and kinds and all(k in KINDS for k in kinds)):
        problems.append(f"pack.toml: kinds must be a non-empty list from: {', '.join(KINDS)}")
        kinds = []
    for k in ("version", "description"):
        if not isinstance(m.get(k), str) or not m[k].strip():
            problems.append(f"pack.toml: {k} is required")
    req = m.get("requires", {})
    problems += _only(req, {"os"}, "pack.toml: [requires]") if isinstance(req, dict) else ["pack.toml: requires must be a table"]
    oses = req.get("os", []) if isinstance(req, dict) else []
    if not (isinstance(oses, list) and all(isinstance(o, str) for o in oses)):
        problems.append("pack.toml: requires.os must be a list of names")
        oses = []
    pack = Pack(name or path.name, str(m.get("version", "")), str(m.get("description", "")), tuple(kinds), path, tuple(oses))
    for sub, kind in (("classes", "classes"), ("policy", "policy"), ("actions", "actions")):
        has = (path / sub).is_dir() and any((path / sub).iterdir())
        if has and kind not in kinds:
            problems.append(f"pack.toml: it has a {sub}/ folder but kinds does not list {kind!r}")
        if kind in kinds and not has:
            problems.append(f"pack.toml: kinds lists {kind!r} but there is no {sub}/ folder with files in it")
    if "classes" in kinds and (path / "classes").is_dir():
        for f in sorted(x for x in (path / "classes").glob("*.yaml") if not x.name.startswith(".")):
            try:
                import yaml
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception as e:
                problems.append(f"classes/{f.name}: not valid YAML: {e}")
                continue
            extra = set(data) - {"classes"} if isinstance(data, dict) else {"?"}
            if extra or not isinstance(data, dict) or not isinstance(data.get("classes"), dict):
                problems.append(f"classes/{f.name}: must contain only a `classes:` mapping (limits and profiling rules belong to the install)")
                continue
            for cname, spec in data["classes"].items():
                if cname in pack.classes:
                    problems.append(f"classes/{f.name}: class {cname!r} is defined twice in this pack")
                pack.classes[cname] = spec
    if "actions" in kinds and (path / "actions").is_dir():
        from . import actions as actions_mod
        for f in sorted(x for x in (path / "actions").glob("*.yaml") if not x.name.startswith(".")):
            try:
                import yaml
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception as e:
                problems.append(f"actions/{f.name}: not valid YAML: {e}")
                continue
            if not isinstance(data, dict) or set(data) - {"actions"} or not isinstance(data.get("actions"), dict):
                problems.append(f"actions/{f.name}: must contain only an `actions:` mapping")
                continue
            for aname, spec in data["actions"].items():
                if aname in pack.actions:
                    problems.append(f"actions/{f.name}: action {aname!r} is defined twice in this pack")
                # a pack cannot know the hash of a program on YOUR machine: the pin comes from the administrator's own registry (action_pins), so it is checked later
                pack.actions[aname] = spec
                problems += [p for p in actions_mod.validate({aname: {**spec, "sha256": spec.get("sha256", "0" * 64)}}) if "sha256" not in p]
    if "policy" in kinds and (path / "policy").is_dir():
        for f in sorted(x for x in (path / "policy").glob("*.toml") if not x.name.startswith(".")):     # hidden files (editor or macOS leftovers) are never loaded
            try:
                pack.policy.add(parse_policy(f.read_text(encoding="utf-8"), f"policy/{f.name}"))
            except PackError as e:
                problems += e.problems
    if problems:
        raise PackError(problems)
    pack.digest = digest_of(path)
    return pack


def find_pack(name, search_dirs):
    for d in search_dirs:
        if (Path(d) / name / "pack.toml").is_file():
            return Path(d) / name
    raise PackError(f"pack {name!r} was not found (looked in: {', '.join(str(d) for d in search_dirs) or 'nowhere'})")


def load_named(names, search_dirs, os_id=None):
    """Load packs in the order named. Returns {name: Pack}. A pack for another operating system is refused."""
    out, problems = {}, []
    for n in names:
        try:
            p = load_pack(find_pack(n, search_dirs))
        except PackError as e:
            problems += [f"pack {n}: {x}" for x in e.problems]
            continue
        if os_id and p.os and os_id not in p.os:
            problems.append(f"pack {n}: needs one of {', '.join(p.os)}, this machine is {os_id}")
        out[n] = p
    if problems:
        raise PackError(problems)
    return out


def violates(policy, cname, spec):
    """Problems a class has against a policy: a denied program. (Approval rules are applied by `apply_policy`; param rules at job time.)"""
    kind = spec.get("kind", "command")
    first = os.path.basename(spec["run"][0]) if kind == "command" and isinstance(spec.get("run"), list) and spec["run"] else None
    if first and first in policy.deny_commands:
        return [f"class {cname!r}: runs {first}, which policy forbids ({policy.deny_commands[first]})"]
    return []


def matches(when, spec):
    kind = spec.get("kind", "command")
    for k, want in when.items():
        have = {"network": bool(spec.get("network", False)), "kind": kind, "accepts_files": bool(spec.get("accepts_files", False))}[k]
        if have != want:
            return False
    return True


def apply_policy(policy, classes):
    """Returns (classes with approval forced where a rule requires it, notes, problems). A denied program is an error; a rule that needs
    approval quietly makes the class ask first, and says so in the notes."""
    out, notes, problems = {}, [], []
    for cname, spec in classes.items():
        if not isinstance(spec, dict):
            out[cname] = spec
            continue
        problems += violates(policy, cname, spec)
        spec = dict(spec)
        for when, why in policy.require_approval:
            if matches(when, spec) and spec.get("tier", "approve") != "approve":
                spec["tier"] = "approve"
                notes.append(f"class {cname!r} now asks for your approval each time: {why}")
        out[cname] = spec
    return out, notes, problems


def compose(packs, registry_names, policy_names, user_registry=None):
    """The registry the worker will trust.

    packs: {name: Pack} (already loaded); registry_names / policy_names: which packs contribute classes / policy, in order;
    user_registry: the parsed registry from the user's own queue repository (their private classes and their limits / profiling rules).
    Returns (registry dict, notes). Raises PackError listing every problem; nothing is returned if any exists."""
    problems, notes, classes, owner, policy = [], [], {}, {}, Policy(deny_commands={n: "built-in baseline" for n in BASELINE_DENY})
    for n in registry_names:
        for cname, spec in packs[n].classes.items():
            if cname in classes:
                problems.append(f"class {cname!r} is defined by both pack {owner[cname]!r} and pack {n!r}")
            classes[cname], owner[cname] = spec, n
    for n in policy_names:
        policy.add(packs[n].policy)
    user = dict(user_registry or {})
    actions, pins = {}, (user.get("action_pins") or {})
    for n in registry_names:
        for aname, spec in packs[n].actions.items():
            if aname in actions:
                problems.append(f"action {aname!r} is defined by more than one pack")
            actions[aname] = dict(spec)
            if "sha256" not in actions[aname]:
                if aname in pins:
                    actions[aname]["sha256"] = pins[aname]
                else:
                    problems.append(f"action {aname!r} (pack {n!r}) runs a program on your machine, so it needs its SHA-256 pinned: add `action_pins: {{{aname}: <sha256>}}` "
                                    f"to your registry.yaml (`ct-admin action-hash <path>` prints it)")
    for aname, spec in (user.get("actions") or {}).items():
        if aname in actions:
            problems.append(f"your registry redefines action {aname!r}, which a pack provides; give yours another name")
        actions[aname] = spec
    for aname, spec in actions.items():
        prog = os.path.basename((spec.get("run") or [""])[0])
        if prog in policy.deny_commands:
            problems.append(f"action {aname!r} runs {prog}, which policy refuses: {policy.deny_commands[prog]}")
    for cname, spec in (user.get("classes") or {}).items():
        if cname in classes:
            problems.append(f"your registry redefines class {cname!r}, which pack {owner[cname]!r} provides; give yours another name")
        classes[cname] = spec
    classes, notes, more = apply_policy(policy, classes)
    problems += more
    if problems:
        raise PackError(problems)
    reg = {k: v for k, v in user.items() if k not in ("classes", "policy", "actions", "action_pins")}
    reg["classes"] = classes
    if actions:
        reg["actions"] = actions
    reg["policy"] = policy.to_registry_section()
    return reg, notes


def main(argv=None):
    """python3 -m ct.packs check <pack folder>...     validate packs (run it on a pack you wrote or downloaded; changes nothing)"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] != "check":
        _ui.say("Say which packs to check", "Use: python3 -m ct.packs check <pack folder>...", tag="ERROR", color=_ui.RED)
        return 2
    bad = 0
    for d in argv[1:]:
        try:
            p = load_pack(d)
            _ui.say("A pack", ("Pack", f"{p.name} {p.version}"), ("Classes", str(len(p.classes))),
                    ("Rules", f"{len(p.policy.deny_commands)} programs and {len(p.policy.deny_params)} patterns refused, {len(p.policy.require_approval)} approval rules"),
                    ("sha256", p.digest[:12]), tag="OK", color=_ui.GREEN, stream=sys.stdout)
        except PackError as e:
            bad += 1
            _ui.say("Not a valid pack", ("Pack", str(d)), *[("Problem", x) for x in e.problems], tag="FAIL", color=_ui.RED)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
