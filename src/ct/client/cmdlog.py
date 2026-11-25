"""Learning which of your commands are worth offloading: record the shell commands Claude Code runs, then rank them.

  hook pre|post    called by Claude Code before and after each Bash command; reads the hook JSON on stdin, never blocks, never prints
  top              the commands you run most that could suit the worker
  show / clear     where the log is and how big; forget it

The log (<client home>/command-log.jsonl, mode 0600) stays on your computer and is never pushed anywhere. A command that looks like it carries
a secret is not recorded at all. `top` prints normalised "shapes" (tool, subcommand, flag names) so paths and values stay out of what you read.
A candidate is: not network or version control, not a package manager, not privileged or interactive, not trivial shell plumbing, not a
compound command, and slow enough (median at least --min-seconds, default 30) to be worth the 15-60 seconds of job overhead.
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
SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
OWN = "this tool's own commands"


def segments(cmd):
    """Split a command line into simple commands (a very small shell subset; good enough for ranking)."""
    return [s for s in SEG_SPLIT.split(cmd.replace("\n", " ; ")) if s.strip()]


def shape(seg):
    """(tool, shape string) for one simple command, or None. Drops env assignments, `timeout N`, paths and values."""
    try:
        words = shlex.split(seg, comments=True)
    except ValueError:
        words = seg.split()
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        words.pop(0)
    while words and words[0] in ("time", "nohup", "command", "exec"):
        words.pop(0)
    if words[:1] == ["timeout"]:
        words = words[2:] if len(words) > 2 else []
    if not words:
        return None
    tool, rest = os.path.basename(words[0]), words[1:]
    parts = [tool]
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


def classify(cmd, own_tools=()):
    """(shape, tool, reason) for a whole command line; reason is None when it could be a candidate. `own_tools` are this install's own
    command names, which are never offloaded (they ARE the offloading)."""
    segs = [s for s in (shape(x) for x in segments(cmd)) if s]
    if not segs:
        return None
    for tool, sh in segs:
        if tool in NEVER or tool in own_tools:
            return sh, tool, NEVER.get(tool, OWN)
    if len(segs) > 1:
        return " | ".join(sh for _, sh in segs), segs[0][0], "compound command (offload a single command)"
    return segs[0][1], segs[0][0], None


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
            g["tool"], g["reason"], g["example"] = tool, reason, r["cmd"]
            g["ts"].append({"ts": r.get("ts")})
            if r.get("secs") is not None and len(segments(r["cmd"])) == 1:
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

    def clear(self):
        self.path.unlink(missing_ok=True)
