"""Is this computer good enough to be a worker? A read-only look, after you said yes.

One SSH login with your key (no password is asked or sent) runs a short fixed script that only READS: the release, the processor, memory, free disk, Python, systemd, whether sudo
exists and whether the account may use it, whether it is a virtual machine or a container, and whether a claude-tenta install is already there. Nothing is installed, written or changed on it.
The answer is a verdict with a reason for every problem and what to do about it. `verdict()` is pure (give it the facts) so the rules are testable without a machine.
"""
import re
import subprocess
from dataclasses import dataclass, field

SCRIPT = r"""
. /etc/os-release 2>/dev/null
echo "OS_ID=$ID"; echo "OS_LIKE=$ID_LIKE"; echo "OS_VERSION=$VERSION_ID"; echo "OS_NAME=$PRETTY_NAME"
echo "ARCH=$(uname -m)"; echo "KERNEL=$(uname -r)"; echo "CORES=$(nproc 2>/dev/null)"
echo "MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null)"
echo "DISK_MB=$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')"
echo "PYTHON=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
echo "SYSTEMD=$([ -d /run/systemd/system ] && echo yes || echo no)"
echo "SUDO=$(command -v sudo >/dev/null 2>&1 && echo yes || echo no)"
echo "SUDO_GROUP=$(id -nG 2>/dev/null | tr ' ' '\n' | grep -qxE 'sudo|wheel|admin' && echo yes || echo no)"
echo "GIT=$(command -v git >/dev/null 2>&1 && echo yes || echo no)"
echo "VIRT=$(systemd-detect-virt 2>/dev/null || echo none)"
echo "INSTALLS=$(ls -d /etc/*/ct.toml 2>/dev/null | sed 's|/etc/||; s|/ct.toml||' | tr '\n' ' ')"
echo "WHO=$(id -un)"; echo "HOSTNAME=$(hostname 2>/dev/null)"
"""
MIN_PYTHON = (3, 11)
MIN_UBUNTU, MIN_DEBIAN = (24, 4), (12, 0)
MIN_MEM_MB, WARN_MEM_MB = 1024, 2048
MIN_DISK_MB, WARN_DISK_MB = 2000, 5000
CONTAINERS = ("docker", "lxc", "lxc-libvirt", "openvz", "podman", "wsl", "systemd-nspawn")

# what the scan will do, in the words shown before you are asked to start it
CHECKS = ("the release and version (Ubuntu 24.04 or newer, or Debian 12 or newer)", "the processor type and the number of cores", "memory and free disk space",
          "that Python 3.11 or newer is there", "that systemd is the init system", "that sudo exists and this account is allowed to use it",
          "whether it is a virtual machine or a container", "whether a claude-tenta install is already on it")


@dataclass
class Verdict:
    meets: bool
    facts: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)          # [(status, what, detail)] status: OK | WARN | FAIL
    fixes: list = field(default_factory=list)          # plain steps for each FAIL

    @property
    def failures(self):
        return [r for r in self.rows if r[0] == "FAIL"]

    @property
    def warnings(self):
        return [r for r in self.rows if r[0] == "WARN"]


def parse(text):
    """{KEY: value} from the script's output."""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _ver(text):
    m = re.match(r"^(\d+)\.?(\d*)", text or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def verdict(facts, user="the account"):
    """The verdict for what the script reported (an empty dict means it could not log in)."""
    rows, fixes = [], []

    def add(status, what, detail, fix=None):
        rows.append((status, what, detail))
        if status == "FAIL" and fix:
            fixes.append(fix)
    if not facts:
        return Verdict(False, {}, [("FAIL", "Login", "key login did not work, so nothing could be read")], [])
    osid, like, ver = facts.get("OS_ID", ""), facts.get("OS_LIKE", ""), _ver(facts.get("OS_VERSION"))
    name = facts.get("OS_NAME") or f"{osid} {facts.get('OS_VERSION', '')}".strip()
    family = "ubuntu" if osid == "ubuntu" else "debian" if osid in ("debian", "raspbian") or "debian" in like.split() and osid != "ubuntu" else ""
    if osid == "ubuntu" and ver and ver >= MIN_UBUNTU:
        add("OK", "Release", name)
    elif osid == "ubuntu":
        add("FAIL", "Release", f"{name}: Ubuntu 24.04 or newer is needed", "Install Ubuntu Server 24.04 or 26.04 on it (22.04 has Python 3.10, which is too old for now).")
    elif family == "debian" and ver and ver >= MIN_DEBIAN:
        add("OK", "Release", name)
    elif family == "debian":
        add("FAIL", "Release", f"{name}: Debian 12 or newer is needed", "Install Debian 12 or 13 on it.")
    else:
        add("FAIL", "Release", f"{name or 'an unknown system'}: only Ubuntu 24.04 or newer and Debian 12 or newer are supported", "Install Ubuntu Server 24.04 or 26.04, or Debian 12 or 13, on it.")
    arch = facts.get("ARCH", "")
    add("OK" if arch in ("x86_64", "aarch64") else "FAIL", "Processor", f"{arch or 'unknown'}, {facts.get('CORES', '?')} core(s)",
        f"A {arch} processor is not supported; use an x86-64 or 64-bit ARM machine." if arch not in ("x86_64", "aarch64") else None)
    try:
        mem = int(facts.get("MEM_MB") or 0)
    except ValueError:
        mem = 0
    add("FAIL" if mem < MIN_MEM_MB else "WARN" if mem < WARN_MEM_MB else "OK", "Memory", f"{mem} MB",
        f"{mem} MB is too little (1 GB is the least); add memory or use another machine." if mem < MIN_MEM_MB else None)
    if mem and mem < WARN_MEM_MB and mem >= MIN_MEM_MB:
        rows[-1] = ("WARN", "Memory", f"{mem} MB: it will work, but only light jobs will fit (2 GB or more is comfortable)")
    try:
        disk = int(facts.get("DISK_MB") or 0)
    except ValueError:
        disk = 0
    add("FAIL" if disk < MIN_DISK_MB else "WARN" if disk < WARN_DISK_MB else "OK", "Disk", f"{disk // 1024} GB free" + (" (5 GB or more is comfortable; Ubuntu's installer often leaves half the disk unused: sudo lvextend -r -l +100%FREE /dev/mapper/ubuntu--vg-ubuntu--lv)" if MIN_DISK_MB <= disk < WARN_DISK_MB else ""),
        f"Only {disk // 1024} GB is free; free some space (2 GB is the least)." if disk < MIN_DISK_MB else None)
    py = _ver(facts.get("PYTHON"))
    if py and py >= MIN_PYTHON:
        add("OK", "Python", facts["PYTHON"])
    else:
        add("FAIL", "Python", f"{facts.get('PYTHON') or 'not installed'}: 3.11 or newer is needed",
            "Install Python 3.11 or newer from the system's own packages (Ubuntu 24.04 and Debian 12 have it): sudo apt-get install python3")
    add("OK" if facts.get("SYSTEMD") == "yes" else "FAIL", "Init system", "systemd" if facts.get("SYSTEMD") == "yes" else "not systemd: the worker's timers and services need it",
        None if facts.get("SYSTEMD") == "yes" else "Use a machine that runs systemd (a full Ubuntu or Debian install does).")
    if facts.get("SUDO") != "yes":
        add("FAIL", "Administrator rights", "sudo is not installed", "Install sudo and add the account to the sudo group: su -c 'apt-get install sudo && usermod -aG sudo " + user + "'")
    elif facts.get("SUDO_GROUP") != "yes":
        add("FAIL", "Administrator rights", f"{facts.get('WHO') or user} is not in the sudo group", f"On the machine: su -c 'usermod -aG sudo {facts.get('WHO') or user}', then log in again.")
    else:
        add("OK", "Administrator rights", f"{facts.get('WHO') or user} may use sudo (the install will ask for its password there; it is never sent anywhere)")
    virt = facts.get("VIRT", "none")
    if virt in CONTAINERS:
        add("WARN", "Environment", f"a {virt} container: it works, but the tripwire and some protections cannot be proved here")
    elif virt and virt != "none":
        add("OK", "Environment", f"a virtual machine ({virt}) with its own kernel")
    else:
        add("OK", "Environment", "real hardware")
    inst = [x for x in (facts.get("INSTALLS") or "").split() if x]
    if inst:
        add("WARN", "Existing install", f"claude-tenta is already on it as: {', '.join(inst)}. Adding it again needs a different name, or this is an update, not a new worker")
    if facts.get("GIT") != "yes":
        add("OK", "Git", "not installed yet; the setup installs it")
    return Verdict(not [r for r in rows if r[0] == "FAIL"], facts, rows, fixes)


def run_script(host, user, port=22, run=None):
    """Log in with the key (never a password, new host keys are accepted the first time) and read the facts. Returns (facts dict, error text or '')."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR", "-p", str(int(port)), f"{user}@{host}", "sh -s"]
    try:
        r = (run or subprocess.run)(argv, input=SCRIPT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return {}, str(e)
    if r.returncode:
        return {}, (r.stderr or "").strip().splitlines()[-1] if (r.stderr or "").strip() else f"ssh exited {r.returncode}"
    return parse(r.stdout), ""


def assess(host, user, port=22, run=None):
    facts, err = run_script(host, user, port, run)
    v = verdict(facts, user)
    if not facts:
        denied = "denied" in err.lower() or "permission" in err.lower()
        v.rows = [("FAIL", "Login", (err or "could not log in") + (" (key login is not set up for this account)" if denied else ""))]
        v.fixes = [f"Put this computer's key on it: ssh-copy-id {user}@{host} (it asks that account's password once)"] if denied else \
                  [f"Check that {host} is on, on this network, and running an SSH server, then try: ssh {user}@{host} true"]
    return v
