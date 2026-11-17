"""Leftovers of the install this project came from (when it was called claude-ballast and its commands were cw-*): an old virtual environment on the PATH, shell lines,
Claude Code hooks and a skill that still call the old commands, and old folders. They break things quietly (a command that fails with 'No module named cw', a hook that keeps
writing a retired log), so ct-setup on a computer looks for them and says what each one is and the one command that clears it. It never removes anything itself: they are
your shell and Claude Code settings."""
import json
import sys

SED_INPLACE = "''" if sys.platform == "darwin" else ""          # macOS sed wants an empty backup suffix
RC_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile")
OLD_FOLDERS = ((".cw-venv", "the old command environment (its cw-* commands no longer work)"), (".cw", "the old command log and notes"),
               (".config/claude-ballast", "the old settings folder"))
OLD_COMMANDS = ("cw-admin", "cw-run", "cw-setup", "cw-worker", "cw-help")
RC_MARKS = (".cw-venv", "CW_HOME")
HOOK_MARKS = ("cw-log",)
HOOK_FILES = (".claude/settings.json", ".claude/settings.local.json")      # privacy-ok: Claude Code's own file names


def find(host):
    """[(what, fix command)] for each leftover found on this computer, in the order a person would clear them."""
    home = host.env.get("HOME", "")
    out = []
    for rc in RC_FILES:
        text = host.read(f"{home}/{rc}") or ""
        marked = [n for n, line in enumerate(text.splitlines(), 1) if any(m in line for m in RC_MARKS) and not line.lstrip().startswith("#")]
        if marked:
            out.append((f"~/{rc} line {', '.join(map(str, marked))} still names the old install", f"sed -i {SED_INPLACE} '/\\.cw-venv/d;/CW_HOME=/d' ~/{rc}"))
    for rel in HOOK_FILES:
        text = host.read(f"{home}/{rel}") or ""
        try:
            hooks = json.dumps(json.loads(text).get("hooks", {})) if text else ""
        except ValueError:
            hooks = ""
        if any(m in hooks for m in HOOK_MARKS):
            out.append((f"~/{rel} has Claude Code hooks that run the old command log",
                        "replace their command with: ct-run offload log hook pre  (and: ct-run offload log hook post)"))
    skill = f"{home}/.claude/skills/worker-offload"
    if host.exists(skill):
        out.append(("~/.claude/skills/worker-offload teaches Claude the old cw-run commands", "rm -rf ~/.claude/skills/worker-offload"))
    for rel, what in OLD_FOLDERS:
        if host.exists(f"{home}/{rel}"):
            out.append((f"~/{rel} is {what}", f"rm -rf ~/{rel}"))
    found = [c for c in OLD_COMMANDS if host.which(c)]
    if found:
        out.append((f"{', '.join(found)} still resolve on your PATH", "open a new terminal after the fixes above; if they still resolve, run: which " + found[0]))
    return out
