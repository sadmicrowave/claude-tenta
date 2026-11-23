"""ct-setup uninstall: take an install off a server, completely and on purpose.

It removes exactly what the installer put there, found from the install's own configuration (so it respects a different name or different folders): the
timers and services and their unit files, the sudoers rule, the sysctl files, the decoy-watching audit rule, the commands, and the code, configuration and
state folders. It shows every step first (a preview unless --apply) and asks once before it removes anything.

What it does NOT do, on purpose:
  * the protections (key-only SSH, the firewall, automatic security updates, the user-namespace restriction) stay; removing the worker does not make the machine less safe;
  * the worker account stays unless you say --remove-account (reinstalling reuses it);
  * with --keep-data the state folder (the clones of the two repositories, the security log, kept outputs) stays.
Your GitHub repositories and your computer's own configuration are never touched.
"""
from . import refuse
from .. import config, render
from . import hardening
from .host import Result
from .server import SetupError
from .report import sink as _sink

UNIT_PARTS = tuple(sorted(render.UNITS, key=lambda u: not u.endswith(".timer")))          # every unit the installer can write, timers first (the shared list, so a new unit can never be left behind)


class Uninstall:
    def __init__(self, host, cfg, *, say=None, dry_run=True, confirm=None, keep_data=False, remove_account=False):
        say = say or _sink()
        self.host, self.cfg, self.say, self.dry_run, self.confirm = host, cfg, say, dry_run, confirm
        self.keep_data, self.remove_account = keep_data, remove_account
        self.report = []

    def exists(self, path):
        return self.host.exists(path)

    def steps(self):
        """[(title, [Action])]: only what is actually there."""
        c, n = self.cfg, self.cfg.identity.name
        units = [(part, f"/etc/systemd/system/{c.unit(part)}") for part in UNIT_PARTS]
        present = [(part, path) for part, path in units if self.exists(path)]
        out = []
        stop = [hardening.Action("run", argv=["systemctl", "disable", "--now", c.unit(part)]) for part, path in present if part.endswith((".timer", ".socket"))]
        stop += [hardening.Action("run", argv=["systemctl", "stop", c.unit(part)]) for part, path in present if part.endswith(".service") and "@" not in part]
        if (present or self.exists(c.paths.code)) and self.host.run(["id", c.server.worker_user]).ok:
            stop.append(hardening.Action("run", argv=["pkill", "-u", c.server.worker_user], why="end any job still running (nothing here is an error if there is none)"))
        if stop:
            out.append(("Stop the timers and services", stop))
        if present:
            out.append(("Remove the unit files", [hardening.Action("remove", path=path) for _, path in present] + [hardening.Action("run", argv=["systemctl", "daemon-reload"])]))
        files = [p for p in (f"/etc/sudoers.d/{n}", f"/etc/sysctl.d/60-{n}-ipv6.conf", f"/etc/sysctl.d/61-{n}-userns.conf") if self.exists(p)]
        audit = f"/etc/audit/rules.d/60-{n}.rules"
        extra = [hardening.Action("remove", path=p) for p in files]
        if self.exists(audit):
            extra += [hardening.Action("remove", path=audit), hardening.Action("run", argv=["augenrules", "--load"])]
        for p in ("/usr/local/bin/ct-worker", "/usr/local/sbin/ct-update", f"/usr/local/bin/{n}", f"/usr/local/sbin/{n}-update"):      # the second pair is what earlier installs used
            if self.exists(p):
                extra.append(hardening.Action("remove", path=p))
        if extra:
            out.append(("Remove the sudoers rule, system settings, audit rule and commands", extra))
        folders = [c.paths.code, c.paths.config, c.path("run_dir"), c.path("watch_state"), c.path("source_clone")] + ([] if self.keep_data else [c.paths.state_root])
        gone = [hardening.Action("run", argv=["rm", "-rf", p]) for p in folders if self.exists(p)]
        if gone:
            out.append(("Remove the code, configuration" + ("" if self.keep_data else " and state") + " folders", gone))
        if self.remove_account and self.host.run(["id", c.server.worker_user]).ok:
            out.append(("Remove the worker account and its home folder", [hardening.Action("run", argv=["userdel", "-r", c.server.worker_user])]))
        return out

    def left_in_place(self):
        rows = ["the protections: key-only SSH, the firewall rules, automatic security updates and the user-namespace restriction (and the packages bubblewrap, auditd and ufw)",
                "IPv6 stays off until the next restart if it was turned off (the setting that kept it off is removed)"]
        if self.keep_data:
            rows.append(f"the state folder {self.cfg.paths.state_root} (--keep-data)")
        if not self.remove_account:
            rows.append(f"the worker account {self.cfg.server.worker_user} (reinstalling reuses it; --remove-account removes it)")
        rows.append("your GitHub repositories and your own computer's configuration")
        return rows

    def run_all(self):
        steps = self.steps()
        if not steps:
            self.say("\n  Nothing is installed here, so there is nothing to remove.")
            self.report.append(("uninstall", "nothing", ""))
            return self.report
        for title, actions in steps:
            self.say(f"\n{title}")
            for a in actions:
                self.say("    " + a.show().replace("\n", "\n    "))
        self.say("\nWhat stays:")
        for row in self.left_in_place():
            self.say(f"  - {row}")
        if self.dry_run:
            self.report.append(("uninstall", "planned", f"{sum(len(a) for _, a in steps)} steps"))
            return self.report
        if self.confirm is not None and not self.confirm(f"Remove the {self.cfg.identity.name} install from this machine?"):
            self.report.append(("uninstall", "declined", ""))
            return self.report
        for title, actions in steps:
            for a in actions:
                r = a.do(self.host)
                if not r.ok and not (a.argv and a.argv[0] in ("pkill", "systemctl")):           # stopping something that is not running is not a failure
                    raise refuse.error("uninstall.step.failed", ("Step", title), command=a.show().splitlines()[0], said=r.out.strip()[:200])
        self.report.append(("uninstall", "removed", self.cfg.identity.name))
        return self.report


def load_config(path=None):
    try:
        return config.load(path)
    except config.ConfigError as e:
        raise refuse.error("uninstall.config.unreadable", problems="; ".join(e.problems))
