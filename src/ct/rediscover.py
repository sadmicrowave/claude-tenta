"""Finding a worker again after its address changed (docs/design/DISCOVERY.md).

A worker is identified by its SSH host key, which this computer recorded the first time it connected: a machine that answers on the SSH port and shows that same key is that worker,
wherever it is now. A name (mDNS or otherwise) is only a hint and is never accepted without the key. Nothing is logged in to: the keys are the ones a machine volunteers to anyone
(`ssh-keyscan`), and the recorded ones come from this computer's own known_hosts (`ssh-keygen -F`). Standard library only; every outside effect is injectable.
"""
from . import refusals
import re
from pathlib import Path
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
    return parse_keys(run(["ssh-keyscan", "-T", "2", "-p", str(int(port)), "-t", KEY_TYPES, ip], 8))


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


def save_address(path, new, old=None):
    """Write `new` as `server.host` in the worker's configuration file, keeping the previous value on the same line as a comment so it can be put back. Returns the previous value.
    Only the one line changes; everything else in the file stays as it was."""
    import tomllib
    text = Path(path).read_text(encoding="utf-8")
    was = (tomllib.loads(text).get("server") or {}).get("host", old or "")
    out, in_server, done = [], False, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_server = stripped.split("#")[0].strip() == "[server]"
        if in_server and not done and re.match(r"host\s*=", stripped):
            line, done = f'host = "{new}"  # was: {was}', True
        out.append(line)
    if not done:
        raise refusals.refuse("config.host.missing", path=path)
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return was


# ---- the ladder: cheapest proof first, the full scan last (docs/design/DISCOVERY.md) -------------------------------------------------------------------

import json  # noqa: E402
import socket  # noqa: E402


def remember(home, name, host, port=22, arp=None, resolve=None):
    """Note where worker `name` was last seen answering (its address, and the address of its network card from the neighbour table when this computer has it), so a later failure can try
    that first. Best effort: nothing here may ever make a working command fail."""
    try:
        ip = (resolve or socket.gethostbyname)(host)
        mac = ((arp or _arp)() or {}).get(ip, "")
        f = Path(home) / "last-seen.json"
        data = json.loads(f.read_text()) if f.exists() else {}
        data[name] = {"ip": ip, "mac": mac}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data, indent=1))
    except Exception:
        pass


def last_seen(home, name):
    try:
        return (json.loads((Path(home) / "last-seen.json").read_text()) or {}).get(name) or {}
    except Exception:
        return {}


def _arp():
    from .setup import discover
    return discover.arp_table()


def resolve_mdns(name, run=None):
    """The IPv4 address the network's multicast name service gives for a multicast (.lo\u200bcal) name, or None (`avahi-resolve` on Linux, `dns-sd` on a Mac). A hint only: the key decides."""
    run = run or _run
    if not name.endswith(".lo" + "cal"):
        return None
    out = run(["avahi-resolve", "-4", "-n", name], 4)
    m = re.search(r"\s(\d+\.\d+\.\d+\.\d+)\s*$", (out or "").strip())
    if m:
        return m.group(1)
    out = run(["dns-sd", "-G", "v4", name], 3) if not out else ""
    m = re.search(r"\s(\d+\.\d+\.\d+\.\d+)\s", out or "")
    return m.group(1) if m else None


def ladder(host, port, name, home, *, recorded=None, shown=None, mdns=None, arp=None, scan_real=None, scan_virtual=None, full=True, progress=None):
    """Find the worker by the cheapest proof first. Rungs: the configured address, the last address that worked, the name's multicast answer, the neighbour table entry for the same network
    card, then (only when `full`) a scan of the real networks and, last, the virtual ones. Returns (Match, rung, devices-seen). Stops at the first rung whose candidates show the recorded key;
    a rung that finds only other machines is not a failure, the next one runs."""
    rec = recorded if recorded is not None else recorded_keys(host, port)
    read = shown or (lambda ip: shown_keys(ip, port))
    seen = []

    tried = set()

    def check(candidates, rung):
        cands = [c for c in dict.fromkeys(c for c in candidates if c) if c not in tried]          # an address that was already waited on is not waited on again
        tried.update(cands)
        if progress:
            progress(rung)
        if not cands:
            return None
        with ThreadPoolExecutor(max_workers=16) as pool:
            found = dict(zip(cands, pool.map(read, cands)))
        m = match(rec, found)
        return m if m.kind in ("one", "several") else None
    if not rec:
        return match(rec, {}), "none", seen
    mem = last_seen(home, name)
    table = (arp or _arp)() if mem.get("mac") else {}
    for rung, cands in (("the configured address", [host]), ("the last address that worked", [mem.get("ip")]),
                        ("the network's name service", [(mdns or resolve_mdns)(host)]), ("the neighbour table", [ip for ip, mac in table.items() if mac == mem.get("mac")])):
        m = check(cands, rung)
        if m:
            return m, rung, seen
    if full:
        for rung, scan in (("a scan of your real networks", scan_real), ("a scan of the virtual networks inside this computer", scan_virtual)):
            if scan is None:
                continue
            devices, _ = scan()
            seen += devices
            m = check([d.ip for d in devices], rung)
            if m:
                return m, rung, seen
    return Match("none", note="no computer that answered shows that worker's key"), "all of the above", seen
