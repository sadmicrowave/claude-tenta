"""The questions, in plain words, and the engine that turns answers into a validated configuration.

Each question maps to one setting in the schema, so its check is the schema's own check: the wizard can never write a value the loader
would refuse. Three ways to answer: a person at a terminal, an answers file (--answers), or defaults (--yes). Anything with no default
that nobody answered is reported by name, never guessed.
"""
import re

from .. import config

_Q = lambda key, title, why, **kw: dict(key=key, title=title, why=why, **kw)      # noqa: E731

# (key, question, why it matters)   half = which half of the wizard asks it; 'both' asks in either
QUESTIONS = [
    _Q("identity.name", "What should this install be called?", "Every service, folder and command is named after it (claude-ballast, claude-ballast-update, ...). Keep the default unless you run two installs on one machine.", half="both"),
    _Q("server.host", "What is the server's name or address?", "Your computer connects to it over SSH, for example worker.example or 192.0.2.10.", half="client", required=True),
    _Q("server.admin_user", "Which account on the server is yours?", "The account you log in to the server with. It must already exist and have your SSH key. Its sudo password is what protects every change, so a stolen key alone is not enough.", half="both", required=True),
    _Q("server.worker_user", "What should the account that runs jobs be called?", "Created for you with no sudo and no login shell, so nothing a job does can reach your own account.", half="server"),
    _Q("server.ssh_port", "Which port does SSH listen on?", "Almost always 22.", half="both"),
    _Q("github.owner", "What is your GitHub username (or organisation)?", "The two private repositories are created under it, using your signed-in `gh` command.", half="both", required=True),
    _Q("github.queue_repo", "What should the private repository that holds your jobs be called?", "Created private. The server gets a read-only key to it, so it can fetch jobs but never change them.", half="both"),
    _Q("github.results_repo", "What should the private repository that holds the results be called?", "Created private. The server gets a write key to it, so it can publish results.", half="both"),
    _Q("updates.source", "Where do software updates come from?", "The address of the project repository (or your fork) the server installs releases from. Only commits on its main branch can be installed, and only when you run `cw-admin apply`. Leave it empty to set it later.", half="both"),
    _Q("hardware.profile", "How strong is the server?", "Sets how many jobs run at once (a heavy job counts as 4 points) and when new jobs wait because the machine is busy or hot. The numbers can be changed later in cw.toml.", half="server"),
    _Q("network.ipv6", "Should the server use IPv6?", "Pick the one that matches how your network works. If nothing needs IPv6 to reach the server, turning it off leaves less to attack.", half="server"),
    _Q("network.lan_deny_out", "Stop the server from starting connections to your other devices?", "If the server were ever compromised, it could not reach your other computers, only the internet. Answer yes unless a job really must reach something on your network.", half="server"),
    _Q("network.lan_allow_out", "Which parts of your network may it still reach?", "Comma-separated networks, for example 192.0.2.0/24 (a local package mirror, say). Leave empty for none.", half="server", when=lambda a: a.get("network.lan_deny_out", True)),
    _Q("network.dns_servers", "Which DNS servers may it use on your network?", "Leave empty to use your router, which is found automatically. Set it if your DNS server is another machine. Comma-separated addresses.", half="server", when=lambda a: a.get("network.lan_deny_out", True)),
    _Q("network.ssh_from", "Which networks may connect to the server over SSH?", "The default is every private range. Narrow it to your own network if you can. The connection you are using must be inside it, or the firewall step refuses to run.", half="server"),
    _Q("access.sudo", "Which admin commands may run without a password?", "Everything that changes something (approve, cancel, kill, unlock, install) always asks for your sudo password. This is only about the commands that read.", half="server"),
    _Q("alerts.kind", "Where should security alerts go?", "A short alert is sent when a result is withheld and when the worker locks itself down, and reminders follow while it stays locked, so a quiet phone never means all is well.", half="server"),
    _Q("alerts.url", "What is the alert address?", "Paste it. It is hidden as you type (shown as dots) because a private topic name is what keeps others from reading your alerts. Access tokens go in secrets.env, not here.", half="server", when=lambda a: a.get("alerts.kind", "none") != "none"),
    _Q("updates.mode", "How should updates be installed?", "Either way, nothing is installed that is not on the project's main branch.", half="server"),
]
# What each choice means, in the order shown. A question with choices shows them as a numbered list; type the name, the number, or press Tab to complete.
CHOICES = {
    "hardware.profile": [("old-laptop", "2 jobs at a time, backs off early (70 C, low load). For an old laptop or anything under 4 GB of memory."),
                         ("small-server", "4 points at a time, backs off at 75 C. A mini PC or a small server."),
                         ("workstation", "8 points at a time, backs off at 80 C. A desktop or workstation with plenty of memory."),
                         ("custom", "Starts like small-server; you set capacity, max_load and max_temp_c yourself in cw.toml.")],
    "network.ipv6": [("enabled", "Keep IPv6. The firewall blocks the private (unique-local) range so jobs cannot reach your devices that way."),
                     ("disabled", "Turn IPv6 off on the server. Less to attack and nothing extra to firewall. Choose this if nothing needs IPv6 to reach it.")],
    "access.sudo": [("readonly-nopasswd", "status, show, review, quarantine and output work without a password."),
                    ("password", "Every admin command asks for your sudo password, even the ones that only read.")],
    "alerts.kind": [("none", "No push alerts. Security events are only in the log and in `cw-admin status`."),
                    ("ntfy", "A push notification to your phone through ntfy (ntfy.sh or your own server)."),
                    ("webhook", "A short message posted to any https address you choose.")],
    "updates.mode": [("manual", "You install updates yourself with `cw-admin apply`, after reading what changes."),
                     ("signed-tags", "The server installs a new release by itself, but only a release tag signed by a key you trust.")],
}
LABELS = {"identity.name": "Install name", "server.host": "Server address", "server.admin_user": "Admin account", "server.worker_user": "Worker account", "server.ssh_port": "SSH port",
          "github.owner": "GitHub account", "github.queue_repo": "Queue repository", "github.results_repo": "Results repository", "updates.source": "Release source",
          "updates.mode": "Updates", "hardware.profile": "Machine", "network.ipv6": "IPv6", "network.lan_deny_out": "Keep it off your LAN",
          "network.lan_allow_out": "LAN exceptions", "network.dns_servers": "DNS servers", "network.ssh_from": "SSH allowed from", "access.sudo": "Passwordless commands",
          "alerts.kind": "Alerts", "alerts.url": "Alert address"}
for _q in QUESTIONS:
    _q["label"] = LABELS.get(_q["key"], _q["key"].partition(".")[2].replace("_", " ").capitalize())
    _q["secret"] = _q["key"] == "alerts.url"          # a private topic address: typed hidden (as dots) and shown as "set"
    _q["choices"] = CHOICES.get(_q["key"])
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
    return default.replace("{name}", "claude-ballast").replace("{worker_user}", "cwworker") if isinstance(default, str) else default


def show(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else ("yes" if value is True else "no" if value is False else "" if value is None else str(value))


def count_to_ask(halves, given=None, only=None):
    """How many questions a person would be asked across these halves (each question once): not already answered, and applicable with default answers."""
    given, seen = dict(given or {}), set()
    for q in QUESTIONS:
        key = q["key"]
        if q["half"] not in tuple(halves) + ("both",) or key in given or (only and key not in only) or key in seen:
            continue
        if q.get("when") and not q["when"]({**{k: default_for(k) for k in BY_KEY}, **given}):
            continue
        seen.add(key)
    return len(seen)


BACK = "back"        # returned in place of the answers when the person stepped back past the first question of a flow (only when allow_back is set)


def _nest(flat):
    nested = {}
    for key, value in flat.items():
        section, _, name = key.partition(".")
        nested.setdefault(section, {})[name] = value
    return nested


def _extras(given, only, flat):
    """Settings that have no question (hardening.modules, registry.packs, ...) can still be given."""
    for key, raw in given.items():
        if key not in BY_KEY and _known(key) and not (only and key not in only):
            value, err = check_one(key, parse(key, raw))
            if err:
                raise WizardError([f"{key} {err} (got {raw!r})"])
            flat[key] = value


def collect(half, *, ask=None, say=print, given=None, suggestions=None, use_defaults=False, only=None, ui=None, blanks=None, allow_back=False, pre_items=None, confirm=None):
    """Run the questions that belong to `half` (a name, or several: 'client', 'server'). Returns the answers as a nested dict and the list of unanswered
    required settings. `given` ({'section.key': value}) is used instead of asking; with use_defaults nothing is asked at all.
    With `ui` (the terminal screens) the questions are a flow the person can step back through; if `allow_back` is set and they step back before the first
    question, (BACK, None) is returned so the caller can take them to an earlier step. `pre_items` are answered lines from an earlier step, shown above.
    `confirm` is the text of a last screen (every answer, then Enter to go on or the up arrow to change one) shown before the flow ends."""
    halves = (half,) if isinstance(half, str) else tuple(half)
    if ui is not None and not use_defaults:
        return _flow(halves, dict(given or {}), suggestions or {}, only, ui, blanks, allow_back, list(pre_items or []), confirm)
    given, suggestions, flat = dict(given or {}), suggestions or {}, {}
    missing = []
    for q in QUESTIONS:
        key = q["key"]
        if q["half"] not in halves + ("both",) or (only and key not in only):
            continue
        if q.get("when") and not q["when"]({**{k: default_for(k) for k in BY_KEY}, **flat}):
            continue
        default = default_for(key, suggestions)
        asked = key not in given and ask is not None and not use_defaults
        if key in given:
            raw = given[key]
        elif not asked:
            raw = default
        else:
            raw = _ask_one(q, default, ask, say)
        if raw in (None, "") and q.get("required"):
            missing.append(key)
            continue
        if raw in (None, "") and default in (None, ""):
            if asked and blanks is not None:
                blanks.add(key)                          # answered by leaving it empty: do not ask it again in another half
            continue
        value, err = check_one(key, parse(key, raw))
        if err:
            if not asked:
                raise WizardError([f"{key} {err} (got {raw!r})"])
            raw = _ask_one(q, default, ask, say, problem=err)
            value, err = check_one(key, parse(key, raw))
            if err:
                raise WizardError([f"{key} {err} (got {raw!r})"])
        flat[key] = value
    _extras(given, only, flat)
    return _nest(flat), missing


def _ask_one(q, default, ask, say, problem=None):
    """Ask until the answer passes the schema's check. An empty answer takes the default. (The plain-text way.)"""
    key = q["key"]
    opts = options_of(key)
    for _ in range(5):
        shown = show(default)
        say(f"\n{q['title']}")
        say(f"  {q['why']}")
        if q.get("choices"):
            for n, (value, desc) in enumerate(q["choices"], 1):
                say(f"    {n}  {value:<18} {desc}")
        elif opts:
            say("  Choices: " + ", ".join(opts))
        if problem:
            say(f"  That did not work: {problem}")
        answer = ask(f"  [{shown}] " if shown else "  ").strip()
        raw = answer if answer != "" else default
        if raw in (None, ""):
            if q.get("required"):
                problem = "this one needs an answer"
                continue
            return raw
        _value, problem = check_one(key, parse(key, _numbered(q, raw)))
        if not problem:
            return _numbered(q, raw)
    return raw


def _numbered(q, text):
    """A choice can be given by its number in the list."""
    if q.get("choices") and isinstance(text, str) and text.strip().isdigit() and 1 <= int(text) <= len(q["choices"]):
        return q["choices"][int(text) - 1][0]
    return text


def _flow(halves, given, suggestions, only, ui, blanks, allow_back, pre_items, confirm=None):
    """The questions as a flow on the terminal screens: forward with Enter, back with the up arrow (your earlier answer ready to change)."""
    qs = [q for q in QUESTIONS if q["half"] in halves + ("both",) and not (only and q["key"] not in only)]
    defaults = {k: default_for(k) for k in BY_KEY}
    parsed, missing = {}, []
    for q in qs:                                   # answers that were handed in rather than asked
        key = q["key"]
        if key not in given:
            continue
        raw, default = given[key], default_for(key, suggestions)
        if raw in (None, "") and q.get("required"):
            missing.append(key)
        elif not (raw in (None, "") and default in (None, "")):
            value, err = check_one(key, parse(key, raw))
            if err:
                raise WizardError([f"{key} {err} (got {raw!r})"])
            parsed[key] = value
    answers, blank_here = {}, set()
    context = {}                                   # every answer handed in, even for questions outside this flow, so a follow-up question knows its condition
    for key, raw in given.items():
        if key in BY_KEY and raw not in (None, ""):
            value, err = check_one(key, parse(key, raw))
            if not err:
                context[key] = value

    def applicable(q):
        if q["key"] in given:
            return False
        return not q.get("when") or bool(q["when"]({**defaults, **context, **parsed, **answers}))

    def visible():
        return [q for q in qs if applicable(q)]

    def display(q):
        key = q["key"]
        return "set" if q.get("secret") else ("(none)" if key in blank_here else show(answers[key]))

    def neighbour(q, step):
        vis = visible()
        i = vis.index(q) + step if q in vis else (0 if step > 0 else len(vis) - 1)
        return vis[i] if 0 <= i < len(vis) else None

    vis = visible()
    cur, problem = (vis[0] if vis else None), None
    while True:
        while cur is not None:
            vis, key = visible(), cur["key"]
            idx = vis.index(cur)
            items = pre_items + [(x["label"], display(x)) for x in vis[:idx] if x["key"] in answers or x["key"] in blank_here]
            default = answers[key] if key in answers else default_for(key, suggestions)
            view = {"items": items, "pos": len(pre_items) + idx, "total": len(pre_items) + len(vis), "can_back": idx > 0 or allow_back, "prefill": ui.rawtext.get(key, "")}
            result = ui.ask(cur, default, options_of(key), problem, show(default), view)
            problem = None
            if result[0] == "back":
                if len(result) > 1:
                    ui.rawtext[key] = result[1]              # keep what was typed (even a hidden address) for when the person comes back
                earlier = neighbour(cur, -1)
                if earlier is None:
                    return BACK, None
                cur = earlier
                continue
            text = _numbered(cur, result[1].strip())
            ui.rawtext[key] = text
            raw = text if text != "" else default
            if raw in (None, ""):
                if cur.get("required"):
                    problem = "this one needs an answer"
                    continue
                answers.pop(key, None)
                blank_here.add(key)
                if blanks is not None:
                    blanks.add(key)
            else:
                value, err = check_one(key, parse(key, raw))
                if err:
                    problem = err
                    continue
                answers[key] = value
                blank_here.discard(key)
            cur = neighbour(cur, +1)
        vis = visible()
        if confirm and vis:
            shown = pre_items + [(x["label"], display(x)) for x in vis if x["key"] in answers or x["key"] in blank_here]
            if ui.confirm(shown, len(pre_items) + len(vis), confirm) == "back":
                cur = vis[-1]
                continue
        break
    keep = {q["key"] for q in visible()}
    flat = {**parsed, **{k: v for k, v in answers.items() if k in keep}}
    _extras(given, only, flat)
    vis = visible()
    ui.review = (pre_items + [(x["label"], display(x)) for x in vis if x["key"] in answers or x["key"] in blank_here], len(pre_items) + len(vis))
    return _nest(flat), missing


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
