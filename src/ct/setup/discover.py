"""Finding computers on your network that could become workers.

Only what a quick, polite look from your own computer can tell: which addresses on your own local networks answer on the SSH port, what each one's SSH greeting says it runs, its
name (if the network knows one) and its network card's maker (from the address table, when your computer already has it). Nothing is logged in to, nothing is sent but a connection to
port 22 and the greeting the machine volunteers. The deeper question, whether a machine is good enough, is `assess`, and only after you pick one.

Safe by construction: only private address ranges are scanned (never a public address), only the networks this computer itself is on, at most a /22 (1022 addresses; a bigger network
is scanned one /24 around this computer), and the connection attempts are short and bounded. Standard library only; every outside effect (sockets, name lookups, commands) is injectable.
"""
import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

MAX_PREFIX_SCANNED = 22                  # a network bigger than a /22 is scanned one /24 at a time, around this computer
CONNECT_TIMEOUT_S = 0.7
BANNER_TIMEOUT_S = 1.0
THREADS = 128

# network card makers that matter here (the first three bytes of the card's address); a hint, never a fact
OUI = {"00:0c:29": "VMware", "00:50:56": "VMware", "00:05:69": "VMware", "52:54:00": "QEMU/KVM", "08:00:27": "VirtualBox", "00:15:5d": "Hyper-V",
       "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi", "e4:5f:01": "Raspberry Pi", "d8:3a:dd": "Raspberry Pi", "2c:cf:67": "Raspberry Pi"}


@dataclass
class Device:
    ip: str
    name: str = ""
    banner: str = ""
    os_hint: str = ""
    kind: str = "unknown"                # linux-server | windows | other | unknown
    mac: str = ""
    vendor: str = ""
    latency_ms: float = 0.0
    iface: str = ""                      # the network interface of this computer it was found through
    virtual: bool = False                # found on a virtual network inside this computer (VM or container software): not on your home network
    known: bool = False                  # already one of your workers
    notes: list = field(default_factory=list)

    @property
    def label(self):
        return self.name or self.ip


def local_networks(run=None):
    """[(interface, ipaddress.IPv4Interface)] for this computer's own private IPv4 networks (loopback, link-local and public addresses are never scanned)."""
    run = run or _run
    out = run(["ifconfig"]) or ""
    found = []
    if out:                                                          # macOS and BSD: `inet 198.51.100.20 netmask 0xffffff00`
        iface = ""
        for line in out.splitlines():
            m = re.match(r"^(\S+?):\s", line)
            if m:
                iface = m.group(1)
            m = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)", line)
            if m:
                mask = m.group(2)
                try:
                    net = ipaddress.IPv4Interface(f"{m.group(1)}/{ipaddress.IPv4Address(int(mask, 16)) if mask.startswith('0x') else mask}")
                except ValueError:
                    continue
                found.append((iface, net))
    else:                                                            # Linux: `2: eth0    inet 198.51.100.20/24 ...`
        for line in (run(["ip", "-4", "-o", "addr", "show"]) or "").splitlines():
            m = re.match(r"^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+/\d+)", line)
            if m:
                found.append((m.group(1), ipaddress.IPv4Interface(m.group(2))))
    return [(i, n) for i, n in found if n.ip.is_private and not n.ip.is_loopback and not n.ip.is_link_local]


VIRTUAL_IFACES = ("bridge", "vmnet", "vmenet", "docker", "br-", "veth", "virbr", "lxc", "lxd", "tap", "utun", "podman", "cni")      # software networks inside a computer, not its real network cards


def is_virtual(iface):
    return (iface or "").startswith(VIRTUAL_IFACES)


def interface_of(ip, nets):
    """The interface of this computer whose network holds `ip` (the narrowest one when several do)."""
    best = None
    for iface, n in nets:
        if ipaddress.ip_address(ip) in n.network and (best is None or n.network.prefixlen > best[1].network.prefixlen):
            best = (iface, n)
    return best[0] if best else ""


def addresses_to_scan(nets):
    """([ip strings], [plain-words notes]) to probe: every host of each network up to a /22, a /24 around this computer for bigger ones; this computer's own addresses are left out."""
    own = {str(n.ip) for _, n in nets}
    seen, out, notes = set(), [], []
    for iface, n in nets:
        net = n.network
        if net.prefixlen < MAX_PREFIX_SCANNED:
            net = ipaddress.ip_network(f"{n.ip}/24", strict=False)
            notes.append(f"{n.network} on {iface} is large, so only {net} around this computer is scanned")
        if net in seen:
            continue
        seen.add(net)
        out += [str(h) for h in net.hosts() if str(h) not in own]
    return list(dict.fromkeys(out)), notes


def os_hint(banner):
    """(what the SSH greeting says it runs, kind) from a greeting like `SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5`."""
    b = banner or ""
    m = re.search(r"OpenSSH_([\d.]+)", b)
    ver = f", OpenSSH {m.group(1)}" if m else ""
    plain = f" {m.group(1)}" if m else ""
    if re.search(r"OpenSSH_for_Windows", b):                                    # the Windows OpenSSH server says so in its greeting
        return "Windows" + (f", OpenSSH {m.group(1)}" if m else "") + " (Special Use only)", "windows"
    if re.search(r"Ubuntu", b):
        return "Ubuntu" + ver, "linux-server"
    m = re.search(r"deb(\d+)", b)
    if re.search(r"Raspbian|Raspberry", b):
        return "Raspberry Pi OS" + ver, "linux-server"
    if re.search(r"Debian|\bdeb\d+", b):
        return "Debian" + (f" {m.group(1)}" if m else "") + ver, "linux-server"
    if re.search(r"dropbear|ROSSSH|Cisco|MikroTik|RouterOS|SSH-1", b, re.I):
        return "an appliance or router (" + (b.split("-", 2)[-1][:30] or "SSH") + ")", "other"
    if re.search(r"FreeBSD|OpenBSD|NetBSD", b):
        return b.split()[-1], "other"
    if "OpenSSH" in b:
        return "something running OpenSSH" + plain + " (the release is checked after you pick it)", "unknown"
    return (b[:40] or "no greeting"), "unknown"


def vendor_of(mac):
    return OUI.get((mac or "").lower()[:8], "")


def arp_table(run=None):
    """{ip: mac} from the address table this computer already keeps (nothing is sent to fill it: the scan's own connections do)."""
    run = run or _run
    table = {}
    for line in (run(["arp", "-an"]) or "").splitlines():
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F]{1,2}(?::[0-9a-fA-F]{1,2}){5})", line)
        if m:
            table[m.group(1)] = ":".join(p.zfill(2) for p in m.group(2).lower().split(":"))
    return table


def _run(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=6).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _run_timed(argv, seconds):
    """What a command that never ends printed in `seconds` (dns-sd browses until stopped)."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=seconds).stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else out
    except (OSError, subprocess.SubprocessError):
        return ""


def mdns_names(run=None):
    """{ip: name plus the mDNS suffix} for the machines that announce an SSH service on the network (Bonjour/mDNS: `dns-sd` on a Mac, `avahi-browse` on Linux). Empty when neither exists or nothing answers.
    Asking costs a couple of seconds and sends only the standard multicast question every computer on the network already answers."""
    run = run or _run_timed
    out = {}
    avahi = run(["avahi-browse", "-rpt", "_ssh._tcp"], 4)
    for line in (avahi or "").splitlines():
        f = line.split(";")
        if len(f) > 8 and f[0] == "=" and f[2] == "IPv4":
            out[f[7]] = f[6]
    if out:
        return out
    browse = run(["dns-sd", "-B", "_ssh._tcp", "local."], 3) or ""
    names = sorted(set(m.group(1).strip() for m in re.finditer(r"\sAdd\s+\d+\s+\d+\s+\S+\s+_ssh\._tcp\.\s+(.+)$", browse, re.M)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, text in zip(names, pool.map(lambda n: run(["dns-sd", "-G", "v4", f"{n}.local"], 2) or "", names)):
            m = re.search(r"\s(\d+\.\d+\.\d+\.\d+)\s", text)
            if m:
                out[m.group(1)] = f"{name}.local"
    return out


def probe(ip, port=22, connect=None, resolve=None):
    """A Device when `ip` answers on the SSH port, else None. `connect(ip, port) -> (banner, seconds)` and `resolve(ip) -> name` can be replaced for tests."""
    connect = connect or _connect
    got = connect(ip, port)
    if got is None:
        return None
    banner, seconds = got
    hint, kind = os_hint(banner)
    name = ""
    try:
        name = (resolve or _resolve)(ip) or ""
    except Exception:
        pass
    return Device(ip=ip, name=name, banner=banner, os_hint=hint, kind=kind, latency_ms=round(seconds * 1000, 1))


def _connect(ip, port):
    import time
    t0 = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT_S) as s:
            took = time.monotonic() - t0
            s.settimeout(BANNER_TIMEOUT_S)
            try:
                banner = s.recv(255).decode("latin-1", "replace").strip().splitlines()[0]
            except (OSError, IndexError):
                banner = ""
            return banner, took
    except OSError:
        return None


def _resolve(ip):
    try:
        name = socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return ""
    return name if name != ip else ""


def scan(nets=None, *, known=(), run=None, connect=None, resolve=None, threads=THREADS, progress=None, names=None, until=None):
    """Probe every address of this computer's own networks on the SSH port. Returns (devices, notes): devices sorted with likely servers first, then by address.
    `known` is the set of addresses and names of workers you already have (they are marked, never offered twice).
    `until(device) -> bool`, when given, is asked about each machine as it answers; the first True ends the scan (addresses not yet probed are skipped), so a search for one known machine does not
    wait for the rest of the network. Machines already found are still returned."""
    nets = local_networks(run) if nets is None else nets
    addrs, notes = addresses_to_scan(nets)
    if not addrs:
        return [], notes + ["this computer is not on a private network, so there is nothing to scan"]
    import threading
    stop = threading.Event()

    def one(ip):
        if stop.is_set():
            return None
        d = probe(ip, 22, connect, resolve)
        if d and until is not None and not stop.is_set():
            try:
                if until(d):
                    stop.set()
            except Exception:                         # a failing check must not end or break the scan
                pass
        return d
    with ThreadPoolExecutor(max_workers=threads) as pool:
        found = [d for d in pool.map(one, addrs) if d]
    macs = arp_table(run)
    announced = (names or mdns_names)() if found else {}
    known = {k.lower() for k in known if k}
    for d in found:
        d.name = d.name or announced.get(d.ip, "")
        d.iface = interface_of(d.ip, nets)
        d.virtual = is_virtual(d.iface)
        d.mac = macs.get(d.ip, "")
        d.vendor = vendor_of(d.mac)
        d.known = d.ip in known or d.name.lower() in known or d.name.lower().removesuffix(".local") in {k.removesuffix(".local") for k in known}
        if d.vendor in ("VMware", "QEMU/KVM", "VirtualBox", "Hyper-V"):
            d.notes.append("a virtual machine")
    order = {"linux-server": 0, "unknown": 1, "windows": 2, "other": 3}
    found.sort(key=lambda d: (d.known, d.virtual, order.get(d.kind, 1), tuple(int(p) for p in d.ip.split("."))))             # your real network first, then software networks inside this computer
    if progress:
        progress(len(addrs), len(found))
    return found, notes
