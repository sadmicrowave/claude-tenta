"""Hardening modules. Each one is part of provisioning, not a repair: the wizard offers every module, explains it in plain words, says how it
could lock you out, shows the exact commands, applies it, and checks again. `cw-setup audit` re-runs every check later to find drift.

A module has:  check(host, cfg) -> Check       is it already true?  (read-only)
               preflight(host, cfg) -> text|None   a reason NOT to apply it now (for example no SSH key installed yet)
               plan(host, cfg) -> [Action]      the exact steps, as data, shown before they run
               revert(host, cfg) -> [Action]    how to undo it
Modules are plain classes registered with @module, so a pack can add its own the same way.
"""
import shlex

from .. import firewall, jail

ROOT_EQUIVALENT = ("lxd", "docker", "disk", "libvirt", "kvm")      # a member can become root without a password
WORKER_FORBIDDEN = ROOT_EQUIVALENT + ("sudo", "admin", "wheel", "adm")


class Check:
    def __init__(self, ok, detail=""):
        self.ok, self.detail = ok, detail


class Action:
    """One step. kind 'run' (argv), 'write' (path, text, mode) or 'remove' (path). `show()` is exactly what a person is shown."""

    def __init__(self, kind, *, argv=None, path=None, text=None, mode=0o644, why=""):
        self.kind, self.argv, self.path, self.text, self.mode, self.why = kind, argv, path, text, mode, why

    def show(self):
        if self.kind == "run":
            return shlex.join(self.argv)
        if self.kind == "write":
            return f"write {self.path} (mode {self.mode:o}):\n" + "\n".join("    " + line for line in self.text.splitlines())
        return f"remove {self.path}"

    def do(self, host):
        if self.kind == "run":
            return host.run(self.argv)
        if self.kind == "write":
            host.write(self.path, self.text, self.mode)
        else:
            host.remove(self.path)
        from .host import Result
        return Result(0)


MODULES = {}


def module(cls):
    MODULES[cls.id] = cls()
    return cls


class Module:
    id = title = why = risk = ""
    default = True

    def preflight(self, host, cfg):
        return None

    def revert(self, host, cfg):
        return []


def groups_of(host, user):
    r = host.run(["id", "-nG", user])
    return r.out.split() if r.ok else None


@module
class SshKeysOnly(Module):
    id, title = "ssh-keys-only", "SSH logins with keys only"
    why = ("A password can be guessed or phished; a key file cannot be typed by an attacker. This turns off password logins (and root logins) "
           "for SSH. Your key keeps working.")
    risk = "If your key is not installed on the server you would lock yourself out. This refuses to run until it is."

    def conf_path(self, cfg):
        return f"/etc/ssh/sshd_config.d/10-{cfg.identity.name}.conf"

    def text(self, cfg):
        return ("# Written by cw-setup. First match wins in sshd, so this file is read before cloud images' own drop-ins.\n"
                "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n")

    def check(self, host, cfg):
        r = host.run(["sshd", "-T"])
        if r.ok:
            words = {line.split()[0]: line.split()[1] for line in r.out.lower().splitlines() if len(line.split()) >= 2}
            bad = [k for k, want in (("passwordauthentication", "no"), ("kbdinteractiveauthentication", "no")) if words.get(k) != want]
            return Check(not bad, "password logins are on" if bad else "password logins are off")
        have = host.read(self.conf_path(cfg))
        return Check(bool(have) and have == self.text(cfg), "cannot read the effective SSH settings (run as root)" if not have else "drop-in present")

    def preflight(self, host, cfg):
        keys = host.read(f"/home/{cfg.server.admin_user}/.ssh/authorized_keys") or ""
        if not any(line.strip() and not line.startswith("#") for line in keys.splitlines()):
            return f"{cfg.server.admin_user} has no SSH key installed yet; add one first (ssh-copy-id from your computer), or you would be locked out"
        return None

    def plan(self, host, cfg):
        return [Action("write", path=self.conf_path(cfg), text=self.text(cfg), mode=0o644),
                Action("run", argv=["sshd", "-t"], why="check the new settings before using them"),
                Action("run", argv=["systemctl", "reload", "ssh"], why="existing sessions stay open; new ones use the new rules")]

    def revert(self, host, cfg):
        return [Action("remove", path=self.conf_path(cfg)), Action("run", argv=["systemctl", "reload", "ssh"])]


@module
class NoRootEquivalentGroups(Module):
    id, title = "no-root-equivalent-groups", "No accounts that are secretly root"
    why = ("Membership of groups such as lxd, docker or disk gives root without a sudo password, which would make the sudo password and "
           "everything built around it decorative. The worker account must also have no sudo.")
    risk = "Removing your account from docker or lxd stops you using them from that account. Nothing in this tool needs them."

    def offenders(self, host, cfg):
        out = []
        for user, forbidden in ((cfg.server.admin_user, ROOT_EQUIVALENT), (cfg.server.worker_user, WORKER_FORBIDDEN)):
            groups = groups_of(host, user)
            if groups is not None:
                out += [(user, g) for g in groups if g in forbidden]
        return out

    def check(self, host, cfg):
        bad = self.offenders(host, cfg)
        return Check(not bad, ", ".join(f"{u} is in {g}" for u, g in bad) if bad else "no root-equivalent group memberships")

    def plan(self, host, cfg):
        return [Action("run", argv=["gpasswd", "-d", u, g], why=f"take {u} out of {g}") for u, g in self.offenders(host, cfg)]


@module
class UfwLanEgress(Module):
    id, title = "ufw-lan-egress", "A firewall that keeps the server away from your other devices"
    why = ("If the server were ever compromised it could not start connections to your other devices, only to the internet. SSH in is allowed only "
           "from the networks you named. The rules come from your configuration.")
    risk = "A wrong SSH network would lock you out, so it refuses unless the address you are connected from is allowed."

    def preflight(self, host, cfg):
        client = (host.env.get("SSH_CONNECTION") or "").split()[:1]
        return firewall.check_ssh_survives(cfg, client[0] if client else None)

    def check(self, host, cfg):
        r = host.run(["ufw", "status"])
        if not r.ok:
            return Check(False, "ufw is not installed or not readable (run as root)")
        if "inactive" in r.out.lower():
            return Check(False, "the firewall is not enabled")
        want = firewall.PRIVATE_V4[0] if cfg.network.lan_deny_out else None
        if want and want not in r.out:
            return Check(False, "the rule that blocks the LAN is missing")
        return Check(True, "enabled")

    def plan(self, host, cfg):
        steps = [Action("run", argv=["apt-get", "install", "-y", "ufw"], why="install ufw if it is missing")]
        steps += [Action("run", argv=["ufw", *r]) for r in firewall.rules(cfg, firewall.detect_gateway())]
        steps.append(Action("run", argv=["ufw", "--force", "enable"]))
        return steps

    def revert(self, host, cfg):
        return [Action("run", argv=["ufw", "--force", "disable"])]


@module
class AuditdCanaries(Module):
    id, title = "auditd-canaries", "Tripwires for stolen credentials"
    why = ("Decoy credential files are placed where an attacker would look. The kernel audit system raises the alarm the moment anything reads "
           "one, and the worker locks itself down.")
    risk = "None. It only watches."

    def preflight(self, host, cfg):
        if host.run(["systemctl", "is-enabled", "auditd"]).out.strip().startswith("masked"):
            return ("this machine has no kernel audit (auditd is masked, which is how container-style machines are set up), so the tripwire cannot work here; "
                    "use a full virtual machine or real hardware")
        return None

    def check(self, host, cfg):
        a = host.run(["systemctl", "is-active", "auditd"])
        t = host.run(["systemctl", "is-active", cfg.unit("canary.timer")])
        bad = [n for n, r in (("auditd", a), ("the canary timer", t)) if r.out.strip() != "active"]
        return Check(not bad, ", ".join(f"{b} is not running" for b in bad) if bad else "auditd and the canary timer are running")

    def plan(self, host, cfg):
        return [Action("run", argv=["apt-get", "install", "-y", "auditd"]), Action("run", argv=["systemctl", "start", "auditd"], why="auditd is enabled by its package; it has no unit to enable"),
                Action("run", argv=["systemctl", "enable", "--now", cfg.unit("canary.timer")])]


@module
class UnattendedUpgrades(Module):
    id, title = "unattended-upgrades", "Security updates install themselves"
    why = "Operating-system security fixes are installed automatically, so a known hole does not stay open while nobody is looking."
    risk = "A reboot is never forced. Very rarely an update changes behaviour."
    CONF = "/etc/apt/apt.conf.d/20auto-upgrades"
    TEXT = 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n'

    def check(self, host, cfg):
        have = host.run(["dpkg", "-s", "unattended-upgrades"]).ok
        on = 'Unattended-Upgrade "1"' in (host.read(self.CONF) or "")
        return Check(have and on, "enabled" if have and on else "not installed" if not have else "installed but not switched on")

    def plan(self, host, cfg):
        return [Action("run", argv=["apt-get", "install", "-y", "unattended-upgrades"]), Action("write", path=self.CONF, text=self.TEXT)]


@module
class StatePermissions(Module):
    id, title = "state-permissions", "Job data readable only by the right accounts"
    why = "Job inputs and results can contain private material. Only the worker account and your admin account should be able to read them."
    risk = "None."

    def paths(self, cfg):
        return [(cfg.path("state"), "750"), (cfg.paths.config + "/secrets.env", "640")]

    def check(self, host, cfg):
        bad = []
        for p, want in self.paths(cfg):
            if not host.exists(p):
                continue
            r = host.run(["stat", "-c", "%a", p])
            if r.ok and int(r.out.strip(), 8) & 0o007:
                bad.append(f"{p} is readable by every account (mode {r.out.strip()})")
        return Check(not bad, "; ".join(bad) if bad else "not readable by other accounts")

    def plan(self, host, cfg):
        return [Action("run", argv=["chmod", want, p]) for p, want in self.paths(cfg) if host.exists(p)]


@module
class JobIsolation(Module):
    id, title = "job-isolation", "Jobs run in a jail"
    why = ("Every job runs with no network, no capabilities, no new privileges, its own process tree, and no view of the worker's home folder "
           "(where its repository keys live) or of this install's configuration. This installs the tool that does it (bubblewrap) and, on Ubuntu "
           "24.04 and newer, puts the kernel's user-namespace restriction back on for everyone else.")
    risk = "None for logins. Until it works, jobs are refused rather than run without a jail."
    SYSCTL = "kernel.apparmor_restrict_unprivileged_userns"

    def sysctl_path(self, cfg):
        return f"/etc/sysctl.d/61-{cfg.identity.name}-userns.conf"

    def sysctl_value(self, host):
        r = host.run(["sysctl", "-n", self.SYSCTL])
        return r.out.strip() if r.ok else None

    def probe(self, host, cfg):
        """(ok, why). Runs a real jailed command as the worker account, exactly as a job would."""
        if cfg.isolation.method == "unshare":
            r = host.run(["runuser", "-u", cfg.server.worker_user, "--", "unshare", "--user", "--net", "--map-current-user", "cat", "/proc/net/dev"])
            names = [line.split(":")[0].strip() for line in r.out.splitlines()[2:] if ":" in line]
            return (r.ok and names == ["lo"]), ("isolation works" if r.ok and names == ["lo"] else (r.out.strip().splitlines() or ["unshare failed"])[-1][:160])
        path = host.which("bwrap") or "/usr/bin/bwrap"
        if not host.exists(path):
            return False, "bubblewrap is not installed"
        mode = host.run(["stat", "-c", "%a", path]).out.strip()
        if mode and int(mode, 8) & 0o4000:
            return False, "bubblewrap is installed setuid, which is refused"
        probe = jail.Bwrap(path=path, hide=[cfg.paths.config]).probe_argv("/tmp")
        r = host.run(["runuser", "-u", cfg.server.worker_user, "--", *probe])
        names = [line.split(":")[0].strip() for line in r.out.splitlines()[2:] if ":" in line]
        if not r.ok:
            return False, "a test jail could not be created: " + ((r.out.strip().splitlines() or ["no message"])[-1][:160])
        return (names == ["lo"]), ("a jail can be created for the worker" if names == ["lo"] else "a test jail still had a network interface")

    def check(self, host, cfg):
        ok, why = self.probe(host, cfg)
        if not ok:
            return Check(False, why)
        if cfg.isolation.method == "bwrap" and self.sysctl_value(host) == "0":
            return Check(False, "jails work, but user namespaces are open to every account (" + self.SYSCTL + " = 0)")
        return Check(True, why)

    def plan(self, host, cfg):
        steps = []
        if cfg.isolation.method == "bwrap" and not host.exists(host.which("bwrap") or "/usr/bin/bwrap"):
            steps.append(Action("run", argv=["apt-get", "install", "-y", "bubblewrap"], why="the tool that builds the jail"))
        if cfg.isolation.method == "bwrap" and self.sysctl_value(host) == "0":
            text = (f"# Written by cw-setup. Ubuntu's default: only programs with an AppArmor profile (such as bubblewrap) may use unprivileged user namespaces.\n"
                    f"{self.SYSCTL} = 1\n")
            steps += [Action("write", path=self.sysctl_path(cfg), text=text), Action("run", argv=["sysctl", "-w", f"{self.SYSCTL}=1"])]
        return steps

    def revert(self, host, cfg):
        return [Action("remove", path=self.sysctl_path(cfg)), Action("run", argv=["sysctl", "-w", f"{self.SYSCTL}=0"])]


def run_module(mod, host, cfg, *, dry_run=True, out=print, confirm=None):
    """Offer one module. Returns ('satisfied'|'applied'|'declined'|'blocked'|'failed'|'planned', detail). Never changes anything when dry_run."""
    before = mod.check(host, cfg)
    if before.ok:
        return "satisfied", before.detail
    out(f"\n{mod.title}\n  {mod.why}\n  Risk: {mod.risk}\n  Now: {before.detail}")
    problem = mod.preflight(host, cfg)
    if problem:
        return "blocked", problem
    steps = mod.plan(host, cfg)
    if not steps:
        return "failed", before.detail + " (this needs your attention; there is nothing to apply automatically)"
    out("  Steps:")
    for s in steps:
        out("    " + s.show().replace("\n", "\n    "))
    if dry_run:
        return "planned", before.detail
    if confirm is not None and not confirm(f"Apply {mod.id}?"):
        return "declined", before.detail
    for s in steps:
        r = s.do(host)
        if not r.ok:
            return "failed", f"`{s.show().splitlines()[0]}` failed: {r.out.strip()[:200]}"
    after = mod.check(host, cfg)
    return ("applied", after.detail) if after.ok else ("failed", "applied, but the check still fails: " + after.detail)


def audit(host, cfg, ids=None):
    """[(id, title, Check)] for the modules named in the config (or `ids`). Read-only."""
    chosen = ids if ids is not None else cfg.hardening.modules
    return [(i, MODULES[i].title, MODULES[i].check(host, cfg)) for i in chosen if i in MODULES]
