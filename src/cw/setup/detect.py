"""What the wizard can learn about a server before it asks anything, so the questions start from sensible suggestions."""
import re

# The minimum is set by Python: the configuration loader needs 3.11 (tomllib). Ubuntu 24.04 ships 3.12, Debian 12 ships 3.11; Ubuntu 22.04 ships 3.10.
SUPPORTED = {"ubuntu": (24, 4), "debian": (12, 0)}


class Facts:
    def __init__(self, **kw):
        self.os_id = self.os_version = self.sudo = ""
        self.ram_mb = self.cores = 0
        self.systemd = self.laptop = False
        self.thermal = "none"
        self.__dict__.update(kw)

    def os_problem(self):
        """Plain-words reason this machine is not supported, or None."""
        if self.os_id not in SUPPORTED:
            return f"{self.os_id or 'this operating system'} is not supported yet (Ubuntu 24.04 or newer and Debian 12 or newer are)"
        want = SUPPORTED[self.os_id]
        try:
            major, _, minor = self.os_version.partition(".")
            if (int(major), int(minor or 0)) < want:
                return f"{self.os_id} {self.os_version} is too old (need {want[0]}.{want[1]} or newer)"
        except ValueError:
            return f"cannot read the {self.os_id} version ({self.os_version!r})"
        if not self.systemd:
            return "systemd is required"
        return None

    def suggestions(self):
        """{'section.key': value} to use as defaults in the questions."""
        s = {"hardware.thermal": self.thermal}
        if self.laptop or (0 < self.ram_mb < 4096):
            s["hardware.profile"] = "old-laptop"
        elif self.ram_mb >= 16384 and self.cores >= 8:
            s["hardware.profile"] = "workstation"
        else:
            s["hardware.profile"] = "small-server"
        return s

    def summary(self):
        return [("Operating system", f"{self.os_id} {self.os_version}"), ("Memory", f"{self.ram_mb} MB"), ("Processor cores", str(self.cores)),
                ("sudo", self.sudo or "unknown"), ("Temperature sensors", self.thermal), ("Portable (battery)", "yes" if self.laptop else "no")]


def detect(host):
    f = Facts()
    osr = dict(m.groups() for m in re.finditer(r'^(\w+)="?([^"\n]*)"?$', host.read("/etc/os-release") or "", re.M))
    f.os_id, f.os_version = osr.get("ID", "").lower(), osr.get("VERSION_ID", "")
    mem = re.search(r"MemTotal:\s+(\d+)\s*kB", host.read("/proc/meminfo") or "")
    f.ram_mb = int(mem.group(1)) // 1024 if mem else 0
    f.cores = len(re.findall(r"^processor\s*:", host.read("/proc/cpuinfo") or "", re.M)) or 1
    f.systemd = host.exists("/run/systemd/system")
    f.laptop = host.exists("/sys/class/power_supply/BAT0") or host.exists("/sys/class/power_supply/BAT1")
    out = host.run(["sudo", "--version"]).out.lower()
    f.sudo = "sudo-rs" if "sudo-rs" in out else "sudo" if "sudo" in out else ""
    f.thermal = "apple-smc" if host.exists("/sys/devices/platform/applesmc.768") else "lm-sensors" if host.exists("/sys/class/hwmon/hwmon0") else "none"
    return f
