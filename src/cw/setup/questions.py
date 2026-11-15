"""The questions, in plain words, and the engine that turns answers into a validated configuration.

Each question maps to one setting in the schema, so its check is the schema's own check: the wizard can never write a value the loader
would refuse. Three ways to answer: a person at a terminal, an answers file (--answers), or defaults (--yes). Anything with no default
that nobody answered is reported by name, never guessed.
"""
import re

from .. import config

_Q = lambda key, title, why, **kw: dict(key=key, title=title, why=why, **kw)      # noqa: E731

# (key, question, why it matters, options)   half = which half of the wizard asks it; 'both' asks in either
QUESTIONS = [
    _Q("identity.name", "What should this install be called?", "It prefixes the services, folders and commands (for example `<name>-admin`). Keep the default unless you run two.", half="both"),
    _Q("server.host", "What is the server's name or address on your network?", "Your computer connects to it over SSH.", half="client", required=True),
    _Q("server.admin_user", "Which account on the server is yours (the one you SSH in as)?", "It must already exist. It needs its password for sudo, so a stolen SSH key alone is not enough.", half="both", required=True),
    _Q("server.worker_user", "Name for the unprivileged account that runs jobs", "Created for you. It has no sudo and no login shell.", half="server"),
    _Q("server.ssh_port", "SSH port on the server", "Almost always 22.", half="both"),
    _Q("github.owner", "Your GitHub username (or organisation)", "The two private repositories are created under it.", half="both", required=True),
    _Q("github.queue_repo", "Name for the private repository that holds jobs", "It is created private; the server gets a read-only key for it.", half="both"),
    _Q("github.results_repo", "Name for the private repository that holds results", "It is created private; the server gets a write key for it.", half="both"),
    _Q("updates.source", "Where do releases come from? (address of the project repository or your fork)", "The server installs only commits from here, on the branch below. Leave empty to set it later.", half="both"),
    _Q("hardware.profile", "What kind of machine is the server?", "Sets how many jobs run at once and when it backs off for heat or load. You can change the numbers later.", half="server"),
    _Q("hardware.thermal", "Temperature and fan support", "none works everywhere. lm-sensors reads standard sensors. apple-smc is for Intel Macs running Linux.", half="server"),
    _Q("network.ipv6", "IPv6 on the server", "Disabled turns it off (less to attack, nothing extra to firewall). Enabled keeps it and blocks unique-local addresses.", half="server"),
    _Q("network.lan_deny_out", "Stop the server starting connections to other devices on your network?", "If it were ever compromised it could not reach your other devices. The internet keeps working.", half="server"),
    _Q("network.lan_allow_out", "Networks on your LAN it may still reach (comma-separated, empty for none)", "For example a local package mirror.", half="server", when=lambda a: a.get("network.lan_deny_out", True)),
    _Q("network.dns_servers", "DNS servers it may use on your LAN (comma-separated, empty to use the router)", "Needed if your DNS server is not the router.", half="server", when=lambda a: a.get("network.lan_deny_out", True)),
    _Q("network.ssh_from", "Networks allowed to SSH in (comma-separated)", "The default is every private range. Narrow it to your own network if you can.", half="server"),
    _Q("access.sudo", "Which admin commands may run without a password?", "readonly-nopasswd lets you run status, show and review without one; anything that changes something always asks. password asks for everything.", half="server"),
    _Q("alerts.kind", "Where should security alerts go?", "none, ntfy (a phone notification) or webhook.", half="server"),
    _Q("alerts.url", "Address to send alerts to", "Use a private topic. Any access token goes in secrets.env, not here.", half="server", when=lambda a: a.get("alerts.kind", "none") != "none"),
    _Q("updates.mode", "How should updates be installed?", "manual: you run `cw-admin apply`. signed-tags: the server installs releases signed by a key you trust.", half="server"),
]
BY_KEY = {q["key"]: q for q in QUESTIONS}


class WizardError(Exception):
    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


def kind_of(key):
    section, _, name = key.partition(".")
    default, check, _doc = config.SCHEMA[section][name]
    return getattr(check, "kind", "list" if isinstance(default, list) else "int" if isinstance(default, int) and not isinstance(default, bool) else "str")


def options_of(key):
    """The allowed words for an enum setting, found by trying the schema's own check against the likely ones; None if it is not an enum."""
    section, _, name = key.partition(".")
    check = config.SCHEMA[section][name][1]
    cells = getattr(check, "__closure__", None) or []
    for c in cells:
        v = c.cell_contents
        if isinstance(v, tuple) and v and all(isinstance(x, str) for x in v):
            return list(v)
    return None


def parse(key, text):
    """Plain text from a person -> the value the schema expects (still unchecked)."""
    if not isinstance(text, str):
        return text
    kind = kind_of(key)
    text = text.strip()
    if kind == "bool":
        return config._coerce("bool", text)
    return config._coerce(kind, text)


def check_one(key, value):
    """(clean value, problem or None) using the schema's own check."""
    section, _, name = key.partition(".")
    value, err = config.SCHEMA[section][name][1](value)
    return value, err


def default_for(key, suggestions=None):
    section, _, name = key.partition(".")
    if suggestions and key in suggestions:
        return suggestions[key]
    default = config.SCHEMA[section][name][0]
    return default.replace("{name}", "claude-worker").replace("{worker_user}", "cwworker") if isinstance(default, str) else default


def show(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else ("yes" if value is True else "no" if value is False else "" if value is None else str(value))


def collect(half, *, ask=None, say=print, given=None, suggestions=None, use_defaults=False, only=None):
    """Run the questions that belong to `half` ('client' or 'server'). Returns the answers as a nested dict and the list of unanswered
    required settings. `given` ({'section.key': value}) is used instead of asking; with use_defaults nothing is asked at all."""
    given, suggestions, flat = dict(given or {}), suggestions or {}, {}
    missing = []
    for q in QUESTIONS:
        key = q["key"]
        if q["half"] not in (half, "both") or (only and key not in only):
            continue
        if q.get("when") and not q["when"]({**{k: default_for(k) for k in BY_KEY}, **flat}):
            continue
        default = default_for(key, suggestions)
        if key in given:
            raw = given[key]
        elif use_defaults or ask is None:
            raw = default
        else:
            raw = _ask_one(q, default, ask, say)
        if raw in (None, "") and q.get("required"):
            missing.append(key)
            continue
        if raw in (None, "") and default in (None, ""):
            continue
        value, err = check_one(key, parse(key, raw))
        if err:
            if key in given or use_defaults or ask is None:
                raise WizardError([f"{key} {err} (got {raw!r})"])
            raw = _ask_one(q, default, ask, say, problem=err)
            value, err = check_one(key, parse(key, raw))
            if err:
                raise WizardError([f"{key} {err} (got {raw!r})"])
        flat[key] = value
    for key, raw in given.items():                     # settings with no question (hardening.modules, registry.packs, ...) can still be given
        if key not in BY_KEY and _known(key) and not (only and key not in only):
            value, err = check_one(key, parse(key, raw))
            if err:
                raise WizardError([f"{key} {err} (got {raw!r})"])
            flat[key] = value
    nested = {}
    for key, value in flat.items():
        section, _, name = key.partition(".")
        nested.setdefault(section, {})[name] = value
    return nested, missing


def _ask_one(q, default, ask, say, problem=None):
    """Ask until the answer passes the schema's check. An empty answer takes the default."""
    key = q["key"]
    opts = options_of(key)
    for _ in range(5):
        say(f"\n{q['title']}")
        say(f"  {q['why']}")
        if opts:
            say("  Choices: " + ", ".join(opts))
        if problem:
            say(f"  That did not work: {problem}")
        shown = show(default)
        answer = ask(f"  [{shown}] " if shown else "  ").strip()
        raw = answer if answer != "" else default
        if raw in (None, ""):
            if q.get("required"):
                problem = "this one needs an answer"
                continue
            return raw
        _value, problem = check_one(key, parse(key, raw))
        if not problem:
            return raw
    return raw


def validate_answers(nested):
    """Whole-config validation (cross-field rules too). Returns (clean, problems)."""
    return config.validate(nested)


def parse_set(pairs):
    """['server.host=worker.example', ...] -> {'server.host': 'worker.example'}; unknown keys are an error."""
    out, problems = {}, []
    for p in pairs or []:
        key, eq, val = p.partition("=")
        if not eq or key not in BY_KEY and not _known(key):
            problems.append(f"--set {p!r}: use section.key=value with a known setting")
            continue
        out[key] = val
    if problems:
        raise WizardError(problems)
    return out


def _known(key):
    section, _, name = key.partition(".")
    return name in config.SCHEMA.get(section, {})
