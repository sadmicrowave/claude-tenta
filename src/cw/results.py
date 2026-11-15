"""What leaves the worker for a result: only what the class declares, capped, and for a failure only the error messages.

  shape_output     publish only the declared JSON fields, then cap the length.
  failure_summary  the top-level error lines of a failed command, never its stack traces (frames and source excerpts can hold paths,
                   addresses and other things that should not leave the machine).
  OutputKeeper     the full output of a failed command stays on the worker (mode 0600) for a while, so a human can find out why.
"""
import json
import re
import time
from pathlib import Path

OUTPUT_KEEP_DAYS = 30
OUTPUT_KEEP_CHARS = 2_000_000
_ERROR_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception)\b:[^\n]*")                      # a traceback's last line starts in column 0
_SUMMARY_LINE = re.compile(r"^(Ran \d+ tests?\b.*|FAILED\b.*|OK\b.*|\d+ (failed|passed|errors?)\b.*|npm ERR!.*|make: \*\*\*.*|(error|fatal)\b.*"
                           r"|.*\b(cannot|no such file|not found|permission denied|undefined reference|unable to)\b.*)$", re.I)


def shape_output(cls, text, default_cap):
    """Reduce a job's output to what the class says may be published: declared JSON keys only, then a length cap. Returns (text, note)."""
    keys, note = cls.get("output_keys"), ""
    if keys:
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError
            dropped = [k for k in obj if k not in keys]
            text = json.dumps({k: obj[k] for k in keys if k in obj}, indent=2)
            if dropped:
                note = f"{len(dropped)} undeclared output field(s) not published"
        except ValueError:
            return "", "output was not the declared JSON shape, so none of it was published"
    cap = int(cls.get("output_max_chars", default_cap))
    return text[:cap] + ("\n[truncated]" if len(text) > cap else ""), note


def failure_summary(text, max_lines=8, max_len=300, where="on the worker"):
    """The top-level error messages from a failed command's output: the exception lines (the last line of each traceback), test-runner and
    build summary lines, or the last few lines when nothing like that is found."""
    keep, seen = [], set()
    for line in (text or "").replace("\r", "").split("\n"):
        line = line.strip("\x00").rstrip()
        if _ERROR_LINE.match(line) or (line and not line[0].isspace() and _SUMMARY_LINE.match(line)):
            line = line[:max_len]
            if line not in seen:
                seen.add(line)
                keep.append(line)
    if not keep:
        keep = [l[:max_len] for l in (text or "").splitlines() if l.strip() and not l.startswith((" ", "\t"))][-5:]
    more = len(keep) - max_lines
    keep = keep[:max_lines]
    return "\n".join(keep) + (f"\n[{more} more error line(s) {where}]" if more > 0 else "")


class OutputKeeper:
    def __init__(self, state, keep_days=OUTPUT_KEEP_DAYS, keep_chars=OUTPUT_KEEP_CHARS):
        self.dir, self.keep_days, self.keep_chars = Path(state) / "output", keep_days, keep_chars

    def keep(self, jid, text):
        self.dir.mkdir(mode=0o700, exist_ok=True)
        f = self.dir / f"{jid}.txt"
        f.write_text((text or "")[:self.keep_chars])
        f.chmod(0o600)

    def read(self, jid):
        """The kept output, or None if there is none (the id is a plain job id, so it cannot name another file)."""
        f = self.dir / f"{jid}.txt"
        return f.read_text() if re.fullmatch(r"[a-z0-9][a-z0-9-]{3,63}", jid or "") and f.exists() else None

    def listing(self, limit=30):
        """[(job id, size_kb, age_seconds)] newest first."""
        files = sorted(self.dir.glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True) if self.dir.exists() else []
        return [(f.stem, f.stat().st_size // 1024, time.time() - f.stat().st_mtime) for f in files[:limit]]

    def prune(self, now=None):
        """Delete kept output older than keep_days. Returns how many files went."""
        n, cutoff = 0, (now or time.time()) - self.keep_days * 86400
        for f in self.dir.glob("*.txt") if self.dir.exists() else []:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                n += 1
        return n
