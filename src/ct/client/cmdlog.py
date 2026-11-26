"""Learning which of your commands are worth offloading: record the shell commands Claude Code runs, then rank them.

  hook pre|post    called by Claude Code before and after each Bash command; reads the hook JSON on stdin, never blocks, never prints
  top              the commands you run most that could suit the worker
  show / clear     where the log is and how big; forget it

The log (<client home>/command-log.jsonl, mode 0600) stays on your computer and is never pushed anywhere. A command that looks like it carries
a secret is not recorded at all. `top` prints normalised "shapes" (tool, subcommand, flag names) so paths and values stay out of what you read.
A candidate is: not network or version control, not a package manager, not privileged or interactive, not trivial shell plumbing, not
several real commands in one line, and slow enough (median at least --min-seconds, default 30) to be worth the 15-60 seconds of job overhead.

Compound lines (`cd x && scripts/t a | tail`) are judged by their real command: the line is split into segments, plumbing segments (cd, tail, grep,
echo, tee, sleep, loop keywords and the like) are dropped, and the whole row's seconds belong to the one segment left. A segment that is network, version
control, a package manager or privileged excludes the whole line (`cd x && git push`). With two or more real commands left the time cannot be split
honestly between them, so the line is skipped ("compound command") rather than credited to one. A heredoc body is data, not commands.
"""
import hashlib
import json
import os
import re
import shlex
import time
from collections import defaultdict
from pathlib import Path
from statistics import median

from . import candidacy

MAX_LINE = 2000

# never recorded: looks like a secret
SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|bearer\s|authorization:|-----BEGIN|AKIA[0-9A-Z]{12,}|"
                    r"ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|[A-Z_]*(KEY|TOKEN|SECRET|PASS)[A-Z_]*=)")
# tools that are never candidates, with the reason shown by `top --all`
NEVER = {
    **dict.fromkeys(("git", "gh", "curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ping", "dig", "nslookup", "ftp"), "network / version control"),
    **dict.fromkeys(("npm", "npx", "pnpm", "yarn", "pip", "pip3", "pipx", "brew", "apt", "apt-get", "gem", "cargo", "go", "docker", "podman", "kubectl",
                     "terraform", "aws", "gcloud", "az"), "package manager / infrastructure"),
    **dict.fromkeys(("sudo", "su", "doas", "systemctl", "launchctl", "chmod", "chown", "kill", "pkill", "killall"), "privileged / process control"),
    **dict.fromkeys(("vim", "vi", "nano", "emacs", "less", "more", "man", "top", "htop", "open", "code", "claude", "ssh-add", "ssh-keygen", "security"),
                    "interactive / local application"),
    **dict.fromkeys(("cd", "ls", "pwd", "echo", "printf", "cat", "head", "tail", "wc", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "touch", "export", "source",
                     "set", "unset", "alias", "which", "type", "env", "true", "false", "test", "sleep", "date", "grep", "sed", "awk", "sort", "uniq", "find"),
                    "trivial shell plumbing"),
}
INTERP = {"python3", "python", "node", "bash", "sh"}
MULTI = {"git", "npm", "docker", "kubectl", "cargo", "go", "pip", "brew", "make", "systemctl", "gh", "python3", "python", "node", "bash", "sh"}
# segments that only move around, show or wait; dropped from a compound line (the NEVER plumbing names, plus these)
PLUMBING = {t for t, why in NEVER.items() if why == "trivial shell plumbing"} | {
    "tee", "cut", "tr", "pgrep", "ps", "jq", "basename", "dirname", "readlink", "realpath", "stat", "du", "df", "file", "nl", "rev", "column", "paste",
    "wait", "exit", "read", "return", "break", "continue", ":", "[", "[[", "xxd", "od", "hexdump", "diff", "cmp", "tput", "clear"}
SHELL_OPENERS = {"for", "while", "until", "if", "case", "done", "fi", "esac", "{", "}"}      # a segment that starts with one is shell syntax, not a command
SHELL_PREFIXES = {"do", "then", "else", "elif", "!"}                                        # these precede a real command in the same segment
# a folder name generic enough to print beside a script name (scripts/t); any other folder may name a private project and is never kept
GENERIC_DIRS = {"scripts", "script", "bin", "tools", "tool", "tests", "test", "ci", "dev", "build", "hack", "tasks"}
VERSIONED = re.compile(r"python\d*\.\d+")                 # python3.13: an interpreter, not a project script
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
INTERP_FLAGS = {"-B", "-I", "-u", "-O", "-OO", "-E", "-s", "-S"}                  # interpreter flags that change nothing about what is run
INLINE = "inline script (nothing to offload by name)"
OWN = "this tool's own commands"


def strip_heredocs(cmd):
    """Remove each heredoc (`<<'EOF'` and its body up to the closing word): it is data fed to a command, not commands. What runs after it stays."""
    out, closing = [], None
    for line in cmd.split("\n"):
        if closing is not None:
            closing = None if line.strip() == closing else closing
            continue
        m = HEREDOC.search(line)
        if m:
            closing = m.group(2)
            line = HEREDOC.sub("", line)
        out.append(line)
    return "\n".join(out)


def segments(cmd):
    """Split a command line into simple commands at && || ; | and newlines that are outside quotes (a very small shell subset; good enough for ranking)."""
    cmd, out, cur, quote, i = strip_heredocs(cmd), [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and quote != "'" and i + 1 < len(cmd):
            cur.append(cmd[i:i + 2])
            i += 2
            continue
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in ";|\n" or (c == "&" and cmd[i + 1:i + 2] == "&" and cmd[i - 1:i] != ">"):
            out.append("".join(cur))
            cur = []
            i += 2 if cmd[i:i + 2] in ("&&", "||") else 1
            continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return [x.strip().lstrip("({").rstrip(")}").strip() for x in out if x.strip()]


def shape(seg):
    """(tool, shape string) for one simple command, or None. Drops env assignments, `timeout N`, paths and values."""
    try:
        words = shlex.split(seg, comments=True)
    except ValueError:
        words = seg.split()
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words.pop(0)
    while words and words[0] in ("time", "nohup", "command", "exec") + tuple(SHELL_PREFIXES):
        words.pop(0)
    if words and words[0] in SHELL_OPENERS:
        return None
    if words[:1] == ["timeout"]:
        words = words[2:] if len(words) > 2 else []
    if not words:
        return None
    tool, rest = os.path.basename(words[0]), words[1:]
    if VERSIONED.fullmatch(tool):
        tool = re.match(r"[a-z]+\d?", tool).group()             # python3.13 -> python3
    if not re.fullmatch(r"[\w.][\w.+-]*|:|\[\[?", tool):
        return None                                           # $VAR, $(...), a leftover assignment: no name to judge
    parts = [tool]
    folder = os.path.basename(os.path.dirname(words[0]))
    if folder in GENERIC_DIRS and tool not in NEVER and tool not in MULTI and not VERSIONED.fullmatch(tool):
        parts = [f"{folder}/{tool}"]                         # scripts/t, not just t; no other part of the path is kept
    while tool in INTERP and rest[:1] and rest[0] in INTERP_FLAGS:
        rest = rest[1:]                                       # python -B -m unittest is python -m unittest
    if tool in INTERP and rest[:1] in (["-"], ["-c"], ["-e"]):
        return tool, f"{tool} {rest[0]}"                      # code typed on the command line or fed on standard input: it has no name
    if tool in MULTI and rest:
        if tool in INTERP and rest[0] == "-m" and len(rest) > 1:
            parts += ["-m", rest[1]]
            rest = rest[2:]
        elif tool in INTERP and not rest[0].startswith("-"):
            parts.append(os.path.basename(rest[0]))
            rest = rest[1:]
        elif tool not in INTERP and not rest[0].startswith("-"):
            parts.append(rest[0])
            rest = rest[1:]
    parts += sorted({w.split("=")[0] for w in rest if w.startswith("-") and len(w) > 1 and w != "--"})
    return tool, " ".join(parts)


def example_of(cmd):
    """The text to print as the example of a line: its first real (non-plumbing) segment, as one line, so a compound line never shows its `cd folder` or
    pipe tail, and a heredoc body is never shown."""
    segs = segments(cmd)
    for seg in segs:
        sh = shape(seg)
        if sh and sh[0] not in PLUMBING:
            return seg
    return segs[0] if segs else cmd


def classify(cmd, own_tools=()):
    """(shape, tool, reason) for a whole command line; reason is None when it could be a candidate. `own_tools` are this install's own
    command names, which are never offloaded (they ARE the offloading)."""
    segs = [s for s in (shape(x) for x in segments(cmd)) if s]
    if not segs:
        return None
    for tool, sh in segs:
        if tool in own_tools:
            return sh, tool, OWN
        if tool in NEVER and tool not in PLUMBING:
            return sh, tool, NEVER[tool]
    real = [s for s in segs if s[0] not in PLUMBING]
    for tool, sh in real:
        if tool in INTERP and sh.split()[1:2] in (["-"], ["-c"], ["-e"]):
            return sh, tool, INLINE
    if not real:
        return segs[0][1], segs[0][0], "trivial shell plumbing"
    if len(real) > 1:
        return " | ".join(sh for _, sh in real), real[0][0], "compound command (several real commands in one line; offload a single command)"
    return real[0][1], real[0][0], None


class CommandLog:
    def __init__(self, home, own_tools=()):
        self.home = Path(home)
        self.path, self.pending = self.home / "command-log.jsonl", self.home / ".log-pending"
        self.own_tools = tuple(own_tools)

    def read(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    @staticmethod
    def key_of(d):
        return d.get("tool_use_id") or hashlib.sha1(f"{d.get('session_id')}{d.get('tool_input', {}).get('command')}".encode()).hexdigest()

    def hook(self, phase, raw, now=None):
        """Record one Claude Code hook event. Never raises on bad input, and never records a secret or a non-Bash tool."""
        now = time.time() if now is None else now
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get("tool_name") != "Bash":
            return
        cmd = (d.get("tool_input") or {}).get("command") or ""
        if not cmd.strip() or SECRET.search(cmd):
            return
        self.home.mkdir(parents=True, exist_ok=True)
        self.pending.mkdir(mode=0o700, exist_ok=True)
        pend = self.pending / self.key_of(d)
        if phase == "pre":
            pend.write_text(str(now))
            return
        secs = None
        if pend.exists():
            try:
                secs = round(now - float(pend.read_text()), 2)
            except ValueError:
                pass
            pend.unlink(missing_ok=True)
        rec = {"ts": int(now), "cmd": cmd[:MAX_LINE], "secs": secs, "cwd": d.get("cwd")}
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(rec) + "\n")
        for old in self.pending.glob("*"):                 # abandoned starts (a command that never finished)
            if now - old.stat().st_mtime > 86400:
                old.unlink(missing_ok=True)

    def rank(self, min_seconds=30.0, show_all=False, records=None, repeat_runs=3, repeat_days=14, now=None):
        """Group records by shape and judge each against every rung of the ladder (candidacy.judge). Returns dicts sorted by count (then total time):
        candidates only (a rung is met), unless show_all. Each row carries `rungs` and `headline` (the name of the highest-weighted rung met)."""
        now = time.time() if now is None else now
        groups = defaultdict(lambda: {"count": 0, "secs": [], "ts": [], "example": "", "reason": None, "tool": ""})
        for r in (self.read() if records is None else records):
            c = classify(r["cmd"], self.own_tools)
            if not c:
                continue
            sh, tool, reason = c
            g = groups[sh]
            g["count"] += 1
            g["tool"], g["reason"], g["example"] = tool, reason, example_of(r["cmd"])
            g["ts"].append({"ts": r.get("ts")})
            if r.get("secs") is not None:
                g["secs"].append(r["secs"])
        rows = []
        for sh, g in groups.items():
            med = median(g["secs"]) if g["secs"] else None
            res = candidacy.judge({"runs": candidacy.runs_in_window(g["ts"], repeat_days, now), "median_s": med}, repeat_runs, min_seconds)
            reason = g["reason"]
            if reason is None and res["headline"] is None:
                reason = next(r["reason"] for r in res["rungs"] if r["rung"] == 4)
            if reason and not show_all:
                continue
            rows.append({"shape": sh, "count": g["count"], "median": med, "total": sum(g["secs"]), "example": g["example"], "reason": reason,
                         "rungs": res["rungs"], "headline": candidacy.NAMES[res["headline"]] if res["headline"] else None})
        rows.sort(key=lambda r: (-r["count"], -r["total"]))
        return rows

    GRID_RUNS, GRID_DAYS = (2, 3, 4), (7, 14, 30)

    def measure(self, min_seconds=30.0, records=None, repeat_runs=3, repeat_days=14, now=None):
        """What the person's own log says about the two repeat settings: how many commands would be candidates at each pair of values (`grid[runs][days]`),
        and a suggested `repeat_days` (candidacy.suggest_days) for `repeat_runs`. Only commands that could run on a worker are counted. Changes nothing."""
        now = time.time() if now is None else now
        recs = self.read() if records is None else records
        stamps = defaultdict(list)
        for r in recs:
            c = classify(r["cmd"], self.own_tools)
            if c and c[2] is None and r.get("ts"):
                stamps[c[0]].append(r["ts"])
        grid = {k: {d: len(self.rank(min_seconds, False, recs, k, d, now)) for d in self.GRID_DAYS} for k in self.GRID_RUNS}
        days, enough = candidacy.suggest_days(stamps.values(), repeat_runs)
        return {"shapes": len(stamps), "most_runs": max((len(v) for v in stamps.values()), default=0), "shapes_enough": enough, "suggested_days": days, "grid": grid,
                "current": len(self.rank(min_seconds, False, recs, repeat_runs, repeat_days, now)), "records": len(recs),
                "log_days": (now - min((r["ts"] for r in recs if r.get("ts")), default=now)) / 86400}

    def clear(self):
        self.path.unlink(missing_ok=True)
