"""Finding a worker again after its address changed (docs/design/DISCOVERY.md).

A worker is identified by its SSH host key, which this computer recorded the first time it connected: a machine that answers on the SSH port and shows that same key is that worker,
wherever it is now. A name (mDNS or otherwise) is only a hint and is never accepted without the key. Nothing is logged in to: the keys are the ones a machine volunteers to anyone
(`ssh-keyscan`), and the recorded ones come from this computer's own known_hosts (`ssh-keygen -F`). Standard library only; every outside effect is injectable.
"""
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

KEY_TYPES = "ed25519,ecdsa,rsa"
KEY_RE = re.compile(r"^(ssh-\S+|ecdsa-\S+)$")


def parse_keys(text):
    """{(type, key)} from what `ssh-keyscan` or `ssh-keygen -F` printed: lines `<host> <type> <key>`; comment lines (`# ...`) are skipped. The host part is ignored (it may be hashed)."""
    keys = set()
    for line in (text or "").splitlines():
        f = line.split()
        if line.startswith("#") or len(f) < 3:
            continue
        for i in range(1, len(f) - 1):
            if KEY_RE.match(f[i]):
                keys.add((f[i], f[i + 1]))
                break
    return keys


def _run(argv, timeout=8):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def recorded_keys(host, port=22, known_hosts=None, run=None):
    """The host keys this computer recorded for `host` (a name or an address), from its own known_hosts (hashed entries included: ssh-keygen does the lookup)."""
    run = run or _run
    name = host if int(port) == 22 else f"[{host}]:{int(port)}"
    argv = ["ssh-keygen", "-F", name] + (["-f", str(known_hosts)] if known_hosts else [])
    return parse_keys(run(argv))


def shown_keys(ip, port=22, run=None):
    """The host keys the machine at `ip` shows to anyone who connects (nothing is logged in to)."""
    run = run or _run
    return parse_keys(run(["ssh-keyscan", "-T", "3", "-p", str(int(port)), "-t", KEY_TYPES, ip], 12))


@dataclass
class Match:
    kind: str                       # one | several | none | unknown
    addresses: list = field(default_factory=list)
    note: str = ""


def match(recorded, found):
    """Which of `found` ({address: keys}) is the worker whose recorded keys are `recorded`. `unknown`: this computer has never connected to it, so nothing can prove who is who."""
    if not recorded:
        return Match("unknown", note="this computer has no recorded host key for that worker, so no machine can be proved to be it")
    hits = sorted(ip for ip, keys in found.items() if recorded & keys)
    if len(hits) == 1:
        return Match("one", hits)
    if len(hits) > 1:
        return Match("several", hits, "more than one computer shows that worker's key (a copied or cloned machine shares it), so it cannot be told apart")
    return Match("none", note="no computer that answered shows that worker's key")


def locate(host, port, nets, *, scan, shown=None, recorded=None, threads=32, hints=None):
    """Scan the networks, read the keys of what answers on the SSH port and match them with the recorded ones. Returns (Match, devices). `hints` ({address: name}, from mDNS) only orders
    the output: a name never decides anything."""
    rec = recorded if recorded is not None else recorded_keys(host, port)
    devices, _notes = scan(nets)
    read = shown or (lambda ip: shown_keys(ip, port))
    with ThreadPoolExecutor(max_workers=threads) as pool:
        found = dict(zip([d.ip for d in devices], pool.map(read, [d.ip for d in devices])))
    return match(rec, found), devices
