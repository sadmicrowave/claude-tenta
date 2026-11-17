"""The colours of every message the tool prints, in one place.

Nothing else in the package writes an escape code for a colour. Code asks for a *role* ("ok", "warn", "bad", "accent", "muted" ...) and this module says what that
role looks like. To change the look of the whole tool, change a role here, pick another built-in theme, or drop a small `theme.toml` next to your settings:

    theme = "light"            # a built-in theme: default | light | mono  (optional)
    [colors]
    primary = "38;5;99"        # any role below, written as terminal colour codes (the part between ESC[ and m)
    ok_chip = "48;5;22;38;5;255;1"

Where it is read from, first found wins: the file named by CW_THEME_FILE, `~/.cw/theme.toml`, `theme.toml` in the folder of the active cw.toml.
CW_THEME=<name> picks a built-in theme without any file. A bad file never breaks a command: the default look is used and `cw-admin theme` says what was wrong.
Standard library only.
"""
import os
import re
import tomllib
from pathlib import Path

# role -> what it is for. The codes are the default theme's.
ROLES = {
    "strong": "bold text: titles and commands",
    "label": "the name of a fact in a card or a list (PROGRAM, PINNED TO, RISK ...): a colour, so it never depends on how your font draws bold",
    "prompt": "the question line of every yes/no prompt ([y/N]); a different colour from everything else so it cannot be missed",
    "muted": "secondary text: reasons, hints, answered questions",
    "italic": "emphasis",
    "primary": "the main colour: rails, section headings, the progress bar, key names, the logo's name",
    "primary_chip": "a filled label in the main colour: a section's name, a worker's name, the word TRY IT",
    "accent": "a second, cooler colour for highlights and data labels",
    "accent_chip": "a filled label in the accent colour",
    "ok": "success, safe choices, commands to copy",
    "ok_chip": "a filled confirm button",
    "warn": "caution, things to read before saying yes",
    "warn_chip": "a filled caution label",
    "bad": "errors and refusals",
    "bad_chip": "a filled error label",
    "root": "anything that runs as root: it must stand out from every other warning",
    "root_chip": "a filled 'RUNS AS ROOT' label",
    "wait_chip": "the filled word that starts a live waiting line (SENT, QUEUED, WAITING): a different yellow from the text so the eye lands on it",
    "info": "neutral notes and what is happening now",
    "neutral_chip": "a filled, unselected button",
    "brand": "the logo",
    "brand_dim": "the logo's shadow",
    "quiet": "the logo's subtitle",
}

THEMES = {
    "default": {
        "strong": "1", "label": "38;5;75", "prompt": "38;5;178", "muted": "2", "italic": "3",
        "primary": "38;2;173;98;152", "primary_chip": "48;2;146;46;118;38;5;255;1",
        "accent": "38;5;75", "accent_chip": "48;5;27;38;5;255;1",
        "ok": "38;5;41", "ok_chip": "48;5;28;38;5;255;1",
        "warn": "38;5;178", "warn_chip": "48;5;178;38;5;16;1",
        "bad": "38;5;203", "bad_chip": "48;5;160;38;5;255;1",
        "root": "38;5;207", "root_chip": "48;5;127;38;5;255;1",
        "info": "38;2;173;98;152", "neutral_chip": "48;5;240;38;5;255;1", "wait_chip": "48;5;220;38;5;16;1",
        "brand": "38;5;80", "brand_dim": "38;5;37", "quiet": "38;5;245",
    },
    "light": {                                              # for a white terminal: darker, no pale tones
        "strong": "1", "label": "38;5;25", "prompt": "38;5;130", "muted": "38;5;241", "italic": "3",
        "primary": "38;2;120;30;98", "primary_chip": "48;2;146;46;118;38;5;255;1",
        "accent": "38;5;25", "accent_chip": "48;5;25;38;5;255;1",
        "ok": "38;5;28", "ok_chip": "48;5;28;38;5;255;1",
        "warn": "38;5;130", "warn_chip": "48;5;172;38;5;16;1",
        "bad": "38;5;124", "bad_chip": "48;5;124;38;5;255;1",
        "root": "38;5;90", "root_chip": "48;5;90;38;5;255;1",
        "info": "38;2;120;30;98", "neutral_chip": "48;5;250;38;5;16;1", "wait_chip": "48;5;179;38;5;16;1",
        "brand": "38;5;30", "brand_dim": "38;5;37", "quiet": "38;5;244",
    },
    "mono": {r: ("1" if r in ("strong", "label", "prompt", "primary", "ok", "bad", "warn", "root") else "2" if r in ("muted", "quiet", "brand_dim") else "7" if r.endswith("_chip") else "")
             for r in ROLES},
}
CODE_RE = re.compile(r"^[0-9]{1,3}(;[0-9]{1,3}){0,15}$")
LEGACY = {"BOLD": "strong", "DIM": "muted", "ITALIC": "italic", "RED": "bad", "GREEN": "ok", "YELLOW": "warn", "CYAN": "info"}     # the names older code used

problems = []                                               # what was wrong with the user's theme file (shown by `cw-admin theme`)
source = "built in"
_colors = dict(THEMES["default"])


def theme_files(env=None):
    env = os.environ if env is None else env
    if env.get("CW_THEME_FILE"):
        yield Path(env["CW_THEME_FILE"])
    yield Path.home() / ".cw" / "theme.toml"
    try:
        from . import config
        found = config.default_config_path(env)
        if found:
            yield Path(found).parent / "theme.toml"
    except Exception:                                       # no config yet (first install): the other places are enough
        pass


def parse(text):
    """theme.toml text -> (colours, problems). Unknown roles and codes that are not plain colour numbers are refused, never passed to a terminal."""
    out, bad = dict(THEMES["default"]), []
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return out, [f"not valid TOML: {e}"]
    name = raw.pop("theme", "default")
    colors = raw.pop("colors", {})
    for k in raw:
        bad.append(f"{k!r} is not a setting (use theme = \"...\" and a [colors] table)")
    if name not in THEMES:
        bad.append(f"theme {name!r} is not one of {', '.join(THEMES)}")
        name = "default"
    out = dict(THEMES[name])
    for role, code in (colors if isinstance(colors, dict) else {}).items():
        if role not in ROLES:
            bad.append(f"[colors] {role!r} is not a role (roles: {', '.join(ROLES)})")
        elif not isinstance(code, str) or (code and not CODE_RE.match(code)):
            bad.append(f"[colors] {role} must be colour numbers like \"38;5;75\"")
        else:
            out[role] = code
    return out, bad


def load(env=None):
    """(Re)read the theme. Called once on import; call again after changing the environment or a file."""
    global _colors, source, problems
    env = os.environ if env is None else env
    _colors, problems, source = dict(THEMES["default"]), [], "built in"
    if env.get("CW_THEME"):
        if env["CW_THEME"] in THEMES:
            _colors, source = dict(THEMES[env["CW_THEME"]]), f"CW_THEME={env['CW_THEME']}"
        else:
            problems.append(f"CW_THEME={env['CW_THEME']!r} is not one of {', '.join(THEMES)}")
        return
    for path in theme_files(env):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _colors, problems = parse(text)
        source = str(path)
        return


def code(role):
    """The terminal colour codes for a role ('' when the theme gives it none). Older names (GREEN, BOLD ...) are accepted too."""
    return _colors.get(LEGACY.get(role, role), "")


def resolve(*parts):
    """Role names and/or raw colour codes -> one code string for an escape sequence ('' when there is nothing to apply)."""
    return ";".join(c for c in (code(x) if (x in ROLES or x in LEGACY) else x for x in parts) if c)


def sgr(text, *parts):
    """Wrap text in the escape codes for these roles (the caller decides whether colour is on)."""
    c = resolve(*parts)
    return f"\x1b[{c}m{text}\x1b[0m" if c else text


def rgb(text, colour):
    """Text in an exact RGB colour (artwork, whose colours are its own and do not follow the theme). The caller decides whether colour is on."""
    return f"\x1b[38;2;{colour[0]};{colour[1]};{colour[2]}m{text}\x1b[0m"


def preview():
    """(role, code, what it is for) for every role, for `cw-admin theme`."""
    return [(r, code(r), ROLES[r]) for r in ROLES]


load()
