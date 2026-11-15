"""The server's firewall, generated from the configuration ([network] and server.ssh_port).

Goal: if the machine is ever compromised it cannot reach the other devices on your home network (limited lateral movement), while the
internet, SSH in from the networks you named, DNS, DHCP and mDNS keep working. ufw is used; by default this only PRINTS the rules.

Order matters in ufw, so it is fixed here: the SSH-in rules come first (so enabling the firewall cannot lock you out), then the exact
allowances out to the LAN, then the blanket denies for private ranges. Applying is refused if the SSH connection being used right now
would not be allowed in afterwards. IPv6: only unique-local addresses (fc00::/7) are blocked; link-local (fe80::/10) is left alone because
neighbour discovery needs it and blocking it would cut IPv6 off.
"""
import ipaddress
import shlex
import subprocess

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


def rules(cfg, gateway=None, ipv6=False):
    """The ufw rules, in the order they must be applied: a list of argument lists (each starts after `ufw`)."""
    n, port = cfg.network, str(cfg.server.ssh_port)
    out = [["default", "deny", "incoming"], ["default", "allow", "outgoing"]]
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


def apply(cfg, run=None, gateway=None, ipv6=False, client_address=None, dry_run=True, out=print):
    """Print (dry_run) or apply the rules. Returns the number of rules. Stops at the first failing rule, leaving ufw as far as it got:
    nothing is enabled until every rule succeeded."""
    gateway = gateway or detect_gateway()
    rule_list = rules(cfg, gateway, ipv6)
    out("Firewall rules" + ("" if dry_run else " being applied") + f" (gateway: {gateway or 'not detected'})")
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
    for r in rule_list:
        res = run(["ufw", *r])
        if res.returncode != 0:
            raise FirewallError(f"`ufw {shlex.join(r)}` failed: {(res.stderr or res.stdout).strip()[:200]}")
    res = run(["ufw", "--force", "enable"])
    if res.returncode != 0:
        raise FirewallError(f"`ufw --force enable` failed: {(res.stderr or res.stdout).strip()[:200]}")
    return len(rule_list)
