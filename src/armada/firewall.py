"""The server's firewall, generated from the configuration ([network] and server.ssh_port).

Goal: if the machine is ever compromised it cannot reach the other devices on your home network (limited lateral movement), while the
internet, SSH in from the networks you named, DNS, DHCP and mDNS keep working. ufw is used; by default this only PRINTS the rules.

Order matters in ufw, so it is fixed here: the SSH-in rules come first (so enabling the firewall cannot lock you out), then the exact
allowances out to the LAN, then the blanket denies for private ranges. Applying is refused if the SSH connection being used right now
would not be allowed in afterwards. IPv6: only unique-local addresses (fc00::/7) are blocked; link-local (fe80::/10) is left alone because
neighbour discovery needs it and blocking it would cut IPv6 off.
"""
import ipaddress
import re
import shlex
import subprocess
from pathlib import Path
from .setup.report import sink as _sink

PRIVATE_V4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
PRIVATE_V6 = ("fc00::/7",)
MDNS = "224.0.0.251"


class FirewallError(Exception):
    pass


def detect_gateway(run=None):
    """The default gateway's address from `ip route`, or None."""
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=10))
    try:
        for line in run(["ip", "-4", "route", "show", "default"]).stdout.splitlines():
            parts = line.split()
            if "via" in parts:
                addr = parts[parts.index("via") + 1]
                ipaddress.ip_address(addr)
                return addr
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return None


def ipv6_enabled(cfg):
    return cfg.network.ipv6 == "enabled"


def rules(cfg, gateway=None, ipv6=None, session=None):
    """The ufw rules, in the order they must be applied: a list of argument lists (each starts after `ufw`). `ipv6` defaults to the
    configuration (network.ipv6); when IPv6 is disabled on the server there is nothing to block, so no IPv6 rule is written."""
    ipv6 = ipv6_enabled(cfg) if ipv6 is None else ipv6
    n, port = cfg.network, str(cfg.server.ssh_port)
    out = [["default", "deny", "incoming"], ["default", "allow", "outgoing"]]
    if session:
        out.append(session_rule(session, port))
    for net in n.ssh_from:
        out.append(["allow", "in", "from", net, "to", "any", "port", port, "proto", "tcp"])
    dns = list(n.dns_servers) or ([gateway] if gateway else [])
    for server in dns:
        out += [["allow", "out", "to", server, "port", "53", "proto", "udp"], ["allow", "out", "to", server, "port", "53", "proto", "tcp"]]
    out += [["allow", "out", "to", "any", "port", "67", "proto", "udp"],                     # DHCP
            ["allow", "out", "to", MDNS, "port", "5353", "proto", "udp"]]                    # mDNS
    for net in n.lan_allow_out:
        out.append(["allow", "out", "to", net])
    if n.lan_deny_out:
        for net in PRIVATE_V4 + (PRIVATE_V6 if ipv6 else ()):
            out.append(["deny", "out", "to", net])
    return out


def session_rule(client_ip, port):
    """Let the SSH server's replies to the connection in use go out while the firewall starts. That connection began before the firewall existed, so it is
    not tracked yet and its replies would meet the rule that blocks the LAN: the session froze for minutes. Removed again once the firewall runs."""
    return ["allow", "out", "from", "any", "port", str(port), "to", client_ip, "proto", "tcp"]


def session_address(ssh_connection):
    """The client address in SSH_CONNECTION ('client_ip client_port server_ip server_port'), only when it is a real IP address; otherwise None."""
    try:
        return str(ipaddress.ip_address((ssh_connection or "").split()[0]))
    except (ValueError, IndexError):
        return None


def commands(rule_list):
    """The exact commands, for showing to a person before anything runs."""
    return [shlex.join(["ufw", *r]) for r in rule_list] + ["ufw --force enable"]


def check_ssh_survives(cfg, client_address):
    """Refuse to apply if the connection in use (SSH_CONNECTION's client address) would not be allowed in by ssh_from.
    Returns None if fine, else a plain-words problem."""
    if not client_address:
        return None
    try:
        ip = ipaddress.ip_address(client_address)
    except ValueError:
        return f"could not read your current SSH address ({client_address!r}), so the firewall cannot check it would not lock you out"
    if any(ip in ipaddress.ip_network(net) for net in cfg.network.ssh_from):
        return None
    return (f"you are connected from {ip}, which network.ssh_from does not allow ({', '.join(cfg.network.ssh_from) or 'nothing'}); "
            "applying the firewall would lock you out. Add your network to network.ssh_from first")


def ufw_defaults_text(text, ipv6):
    """The contents of /etc/default/ufw with IPV6 set to yes or no (added if missing). ufw only writes IPv6 rules when this is yes, so with
    IPv6 disabled on the machine it must be no."""
    want = f"IPV6={'yes' if ipv6 else 'no'}"
    lines, found = text.splitlines(), False
    for i, line in enumerate(lines):
        if re.match(r"^\s*IPV6\s*=", line):
            lines[i], found = want, True
    if not found:
        lines.append(want)
    return "\n".join(lines) + "\n"


def apply(cfg, run=None, gateway=None, ipv6=None, client_address=None, dry_run=True, out=None, defaults_path="/etc/default/ufw"):
    """Print (dry_run) or apply the rules. Returns the number of rules. Stops at the first failing rule, leaving ufw as far as it got:
    nothing is enabled until every rule succeeded."""
    out = out or _sink()
    gateway = gateway or detect_gateway()
    ipv6 = ipv6_enabled(cfg) if ipv6 is None else ipv6
    rule_list = rules(cfg, gateway, ipv6)
    out("Firewall rules" + ("" if dry_run else " being applied") + f" (gateway: {gateway or 'not detected'}; IPv6 {'enabled' if ipv6 else 'disabled'})")
    for c in commands(rule_list):
        out("  " + c)
    if dry_run:
        out("\nNothing was changed. Run again with --apply to install these rules.")
        return len(rule_list)
    problem = check_ssh_survives(cfg, client_address)
    if problem:
        raise FirewallError(problem)
    if not cfg.network.dns_servers and not gateway:
        raise FirewallError("no DNS server is configured and the gateway could not be detected, so DNS would stop working; set network.dns_servers")
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    try:
        current = Path(defaults_path).read_text()
        new = ufw_defaults_text(current, ipv6)
        if new != current:
            Path(defaults_path).write_text(new)
    except OSError:
        pass                                           # no /etc/default/ufw to adjust (ufw not installed yet): the rules below will say so
    for r in rule_list:
        res = run(["ufw", *r])
        if res.returncode != 0:
            raise FirewallError(f"`ufw {shlex.join(r)}` failed: {(res.stderr or res.stdout).strip()[:200]}")
    res = run(["ufw", "--force", "enable"])
    if res.returncode != 0:
        raise FirewallError(f"`ufw --force enable` failed: {(res.stderr or res.stdout).strip()[:200]}")
    return len(rule_list)
