"""The output guard: everything a job produces is scanned here before it can leave the machine.

A match withholds that result (the full text stays on the worker), is written to the hash-chained security log, raises an alert and
locks the worker until a human clears it. Pattern matching is a tripwire, not the wall: the wall is that jobs run with no network and
publish only the fields their class declares. Nothing here ever returns or logs the text that matched, only the rule's name.
"""
import hashlib
import json
import math
import re
import secrets
import socket
from pathlib import Path

from .util import iso

_PRIV4 = (r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
          r"|169\.254(?:\.\d{1,3}){2}|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2})(?!\d)")
GUARD_RULES = {
    "private_ip": re.compile(_PRIV4),
    "ipv6_local": re.compile(r"(?i)(?<![0-9a-f:])(?:fe80|f[cd][0-9a-f]{2}):[0-9a-f:]{2,}"),
    "mac": re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bssh-(?:rsa|ed25519|dss) AAAA|ecdsa-sha2-\S+ AAAA"),
    "token": re.compile(r"sk-ant-[\w-]{8,}|gh[pousr]_\w{16,}|github_pat_\w{16,}|AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,}"
                        r"|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"),
    "passwd_line": re.compile(r"(?m)^[a-z_][\w.-]*:[x*!$][^:\n]*:\d+:\d+:"),
    "env_dump": re.compile(r"(?m)(?:^[A-Z][A-Z0-9_]{2,}=.*\n){5,}"),
    "mdns_name": re.compile(r"(?i)\b[a-z0-9][a-z0-9-]*\.(?:local|lan|home|internal)\b"),
}
SECRET_RE = re.compile(r"(sk-ant-[\w-]+|gh[pousr]_\w+|github_pat_\w+)")
BLOB_RE = re.compile(r"[A-Za-z0-9+/_-]{80,}={0,2}")
GUARD_NAMES = set(GUARD_RULES) | {"hostname", "encoded_blob", "canary"}

# One plain sentence per rule: why a result was withheld. Never contains the matched text.
RULE_WHY = {
    "private_ip": "it contained a private network (LAN) IP address",
    "ipv6_local": "it contained a local IPv6 address",
    "mac": "it contained a hardware (MAC) address",
    "private_key": "it contained what looks like a private or SSH key",
    "token": "it contained what looks like an API token or secret",
    "passwd_line": "it contained account-file (passwd) lines",
    "env_dump": "it looked like a dump of environment variables",
    "mdns_name": "it contained a local network host name (.local, .lan, .home)",
    "hostname": "it contained this machine's own host name",
    "encoded_blob": "it contained a long encoded blob that could hide data",
    "canary": "it contained a planted decoy credential (a breach)",
    "canary_access": "a planted decoy credential file was read",
}


def why_text(rules):
    """'Withheld because it contained ...' for a set of rule names; unknown names are shown as they are."""
    return "Withheld because " + "; and ".join(RULE_WHY.get(r, f"it matched rule {r}") for r in sorted(rules)) + "."


def scrub(text, cap):
    """Last-line hygiene on published text: redact anything shaped like an API key or token, then cap the length."""
    text = SECRET_RE.sub("[redacted]", text or "")
    return text[:cap] + ("\n[truncated]" if len(text) > cap else "")


def _entropy(text):
    counts = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(text) * math.log2(c / len(text)) for c in counts.values())


def scan(text, allow=(), hostname=None, canary_tokens=()):
    """Names of the rules that match `text`, never the matched content. A class may `allow` rules it legitimately trips;
    `canary` can never be allowed."""
    text = text or ""
    hits = [n for n, rx in GUARD_RULES.items() if n not in allow and rx.search(text)]
    host = socket.gethostname() if hostname is None else hostname
    if len(host) >= 4 and "hostname" not in allow and re.search(rf"(?i)(?<![\w-]){re.escape(host)}(?![\w-])", text):
        hits.append("hostname")
    if "encoded_blob" not in allow and any(_entropy(m.group()) > 4.2 for m in BLOB_RE.finditer(text)):
        hits.append("encoded_blob")
    if any(t and t in text for t in canary_tokens):
        hits.append("canary")
    return hits


# ---- decoy credentials --------------------------------------------------------------------------------------------------------
def canary_tokens(state):
    try:
        return json.loads((Path(state) / "canary.json").read_text()).get("tokens", [])
    except (OSError, ValueError):
        return []


def canary_files(state):
    try:
        return [f for f in json.loads((Path(state) / "canary.json").read_text()).get("files", []) if isinstance(f, str) and f.startswith("/")]
    except (OSError, ValueError):
        return []


def plant_canaries(state, home, identity="armada"):
    """Plant fake credentials nothing legitimate ever reads. Their text in any output is a breach; a read of the files is an alarm
    (canary-watch). Returns True if it planted them now, False if they were already there."""
    f = Path(state) / "canary.json"
    if f.exists():
        return False
    rnd = lambda n, alpha: "".join(secrets.choice(alpha) for _ in range(n))   # noqa: E731
    up, mixed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    aws, gh, sec = "AKIA" + rnd(16, up), "ghp_" + rnd(36, mixed), rnd(40, mixed)
    home = Path(home)
    creds, gitcreds = home / ".aws" / "credentials", home / ".git-credentials"
    (home / ".aws").mkdir(mode=0o700, exist_ok=True)
    creds.write_text(f"[default]\naws_access_key_id = {aws}\naws_secret_access_key = {sec}\n")
    gitcreds.write_text(f"https://{identity}:{gh}@github.com\n")
    creds.chmod(0o600)
    gitcreds.chmod(0o600)
    f.write_text(json.dumps({"tokens": [aws, sec, gh], "planted": iso(), "files": [str(creds), str(gitcreds)]}))
    f.chmod(0o600)
    return True


# ---- the security log -----------------------------------------------------------------------------------------------------------
class SecurityLog:
    """Append-only log in which every line carries the SHA-256 of the line before it, so an edit or deletion in the middle is detectable."""

    def __init__(self, path):
        self.path = Path(path)

    def append(self, event):
        prev = "0" * 64
        if self.path.exists() and self.path.stat().st_size:
            prev = hashlib.sha256(self.path.read_bytes().splitlines()[-1]).hexdigest()
        line = json.dumps({"ts": iso(), **event, "prev": prev}, sort_keys=True)
        with open(self.path, "a") as fh:
            fh.write(line + "\n")
        return line

    def verify(self):
        """(True, line_count) if the chain is intact, else (False, the 1-based number of the first line that does not follow its predecessor)."""
        prev, lines = "0" * 64, []
        try:
            lines = self.path.read_bytes().splitlines()
        except OSError:
            return True, 0
        for n, raw in enumerate(lines, 1):
            try:
                if json.loads(raw).get("prev") != prev:
                    return False, n
            except ValueError:
                return False, n
            prev = hashlib.sha256(raw).hexdigest()
        return True, len(lines)
