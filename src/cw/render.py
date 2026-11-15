"""Turning the configuration into the files an install needs: systemd units, the sudoers rule, and the two small command wrappers.

Templates use {{placeholders}}. Every value that can appear in a rendered file is built here from the already-validated configuration and
checked AGAIN against a strict pattern (defence in depth: a value that could carry a space, a quote, a newline or a shell character is
refused, never escaped). An unknown placeholder, or one left unfilled, is an error. The sandbox settings for the services that run jobs
live in one place, so the fetch and step units can never drift apart.
"""
import re
from pathlib import Path

from .config import NAME_RE, PATH_RE, USER_RE

# Jobs run inside the fetch and step services, so these limit what a job (or anything it runs) can reach. The update tool tests them
# (sandboxtest) before keeping a changed unit file. Deliberately not set, because each would break jobs today: NoNewPrivileges (the thermal
# classes use sudo), ProtectKernelTunables (fan control writes under /sys), RestrictNamespaces (network isolation uses unshare),
# SystemCallFilter, PrivateDevices.
SANDBOX = """\
# Sandbox. See sandboxtest: the update tool checks these before keeping a changed unit file.
ProtectSystem=strict
ReadWritePaths={{state_root}} {{worker_home}} -/run/sudo -/run/sudo-rs
PrivateTmp=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
SystemCallArchitectures=native"""

# Read-only subcommands the admin may run as the worker without a password (access.sudo = "readonly-nopasswd"). Each is matched exactly.
READ_ONLY = ("status", "status --hours *", "show *", "review *", "quarantine", "quarantine *", "output", "output *")

UNITS = ("fetch.service", "fetch.timer", "step.service", "step.timer", "canary.service", "canary.timer", "update.service", "update.timer")
BIN_DIR = "/usr/local/bin"
SBIN_DIR = "/usr/local/sbin"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_CHECKS = {
    "name": NAME_RE, "worker_user": USER_RE, "admin_user": USER_RE, "alias": re.compile(r"^[A-Z][A-Z0-9_]{2,40}$"),
    "state_root": PATH_RE, "worker_home": PATH_RE, "lib": PATH_RE, "config": PATH_RE, "code": PATH_RE,
    "fetch_s": re.compile(r"^\d{1,4}$"), "step_s": re.compile(r"^\d{1,4}$"), "canary_s": re.compile(r"^\d{1,4}$"), "update_min": re.compile(r"^\d{1,4}$"),
    "commands": re.compile(r"^[A-Za-z0-9._/ *,-]+$"),
}


class RenderError(Exception):
    pass


def render(template, values):
    """Fill {{placeholders}}. `{{sandbox}}` is expanded first (it holds placeholders of its own). Raises RenderError on anything unknown,
    unfilled, or not matching its pattern."""
    template = template.replace("{{sandbox}}", SANDBOX)

    def fill(m):
        key = m.group(1)
        if key not in values:
            raise RenderError(f"the template uses {{{{{key}}}}} but no value is defined for it")
        text = str(values[key])
        rx = _CHECKS.get(key)
        if rx is None or not rx.fullmatch(text):
            raise RenderError(f"the value for {{{{{key}}}}} is not allowed in a generated file: {text!r}")
        return text
    return _PLACEHOLDER.sub(fill, template)


def values_for(cfg):
    """Every value any template may use, from the configuration."""
    return {"name": cfg.identity.name, "worker_user": cfg.server.worker_user, "admin_user": cfg.server.admin_user or "", "alias": alias_for(cfg),
            "state_root": cfg.paths.state_root, "worker_home": cfg.paths.worker_home, "lib": f"{cfg.paths.code}/lib", "config": cfg.paths.config,
            "code": cfg.paths.code, "fetch_s": cfg.schedule.fetch_s, "step_s": cfg.schedule.step_s, "canary_s": cfg.schedule.canary_s,
            "update_min": cfg.schedule.update_min, "commands": ""}


def alias_for(cfg):
    return cfg.identity.name.upper().replace("-", "_") + "_READ"


def wrapper_path(cfg):
    """The command that runs the worker's CLI as the calling user (units and the sudo entry point call it as the worker)."""
    return f"{cfg.paths.code}/bin/{cfg.identity.name}"


def unit_files(cfg, templates_dir):
    """{installed file name: text} for every systemd unit, from the templates directory of the release being installed."""
    v, out = values_for(cfg), {}
    for unit in UNITS:
        src = Path(templates_dir) / "systemd" / f"{unit}.in"
        out[f"{cfg.identity.name}-{unit}"] = render(src.read_text(), v)
    return out


def sudoers_text(cfg, templates_dir):
    """The sudoers file for read-only commands without a password, or None when access.sudo is 'password' or no admin is configured."""
    if cfg.access.sudo != "readonly-nopasswd" or not cfg.server.admin_user:
        return None
    w = wrapper_path(cfg)
    v = {**values_for(cfg), "commands": ", ".join(f"{w} {c}" for c in READ_ONLY)}
    return render((Path(templates_dir) / "sudoers" / "access.in").read_text(), v)


def cli_wrapper(cfg):
    """<code>/bin/<name>: runs the worker's CLI as whoever calls it. The paths are validated, so they are safe inside single quotes."""
    v = values_for(cfg)
    for k in ("lib", "config"):
        if not _CHECKS[k].fullmatch(v[k]):
            raise RenderError(f"the value for {k} is not allowed in a generated file: {v[k]!r}")
    return ("#!/bin/sh\n# Runs the worker's commands as whoever calls it. The units and the sudo entry point call it as the worker account.\n"
            f"exec env PYTHONPATH='{v['lib']}' CW_CONFIG='{v['config']}/cw.toml' /usr/bin/python3 -m cw.cli \"$@\"\n")


def sudo_wrapper(cfg):
    """/usr/local/bin/<name>: the admin's entry point; runs the CLI as the worker account through sudo."""
    for k, rx in (("worker_user", USER_RE), ("code", PATH_RE), ("name", NAME_RE)):
        val = {"worker_user": cfg.server.worker_user, "code": cfg.paths.code, "name": cfg.identity.name}[k]
        if not rx.fullmatch(val):
            raise RenderError(f"the value for {k} is not allowed in a generated file: {val!r}")
    return ("#!/bin/sh\n# The administrator's entry point: runs the worker's commands as the worker account (read-only ones need no password).\n"
            f"exec sudo -p '[sudo] password for %p to run {cfg.identity.name}: ' -u {cfg.server.worker_user} {wrapper_path(cfg)} \"$@\"\n")


def update_wrapper_path(cfg):
    return f"{SBIN_DIR}/{cfg.identity.name}-update"


def update_wrapper(cfg):
    """/usr/local/sbin/<name>-update: the ONE command that installs a release, run through sudo (so it asks for the admin's password).
    It runs the installed update tool as root with a fixed environment; nothing from the caller's environment or arguments reaches a shell."""
    v = values_for(cfg)
    for k in ("lib", "config"):
        if not _CHECKS[k].fullmatch(v[k]):
            raise RenderError(f"the value for {k} is not allowed in a generated file: {v[k]!r}")
    return ("#!/bin/sh\n# Installs a release of the software on this machine. Run it with sudo: sudo " + cfg.identity.name + "-update --rev <commit>\n"
            f"cd / || exit 1\nexec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin PYTHONPATH='{v['lib']}' CW_CONFIG='{v['config']}/cw.toml' /usr/bin/python3 -m cw.update \"$@\"\n")


def sysctl_text(cfg):
    """/etc/sysctl.d file that turns IPv6 off, or None when network.ipv6 is 'enabled'. Settled at install time and at boot."""
    if cfg.network.ipv6 != "disabled":
        return None
    return (f"# Installed by the {cfg.identity.name} installer because network.ipv6 = \"disabled\" in the configuration.\n"
            "net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\nnet.ipv6.conf.lo.disable_ipv6 = 1\n")
