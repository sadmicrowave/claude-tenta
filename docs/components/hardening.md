# Hardening

## In plain words

Hardening means making the worker harder to attack: password logins off, a firewall that keeps it away from your other devices, no hidden ways to become
root, automatic security updates, tripwires. Here it is **part of setting up**, not something you fix later. The wizard goes through each protection
one at a time, says in plain words what it does and **how it could lock you out**, shows the exact commands, applies it and checks that it worked. A
protection that could lock you out refuses to run until it is safe (for example, it will not turn off password logins until your SSH key is installed).

Later, `ct-setup audit` checks every protection again and tells you if anything has drifted.

## Technical specification

### Module interface

Each module is a class in `src/ct/setup/hardening.py`, registered by `@module`:

| Member | Purpose |
|---|---|
| `id`, `title`, `why`, `risk` | the words shown to the person |
| `check(host, cfg) -> Check(ok, detail)` | read-only: is it already true, and if not, why |
| `preflight(host, cfg) -> text or None` | a reason not to apply it now (lockout risk, missing prerequisite, unsupported machine) |
| `plan(host, cfg) -> [Action]` | the exact steps as data: `run` (argument list), `write` (path, text, mode) or `remove` |
| `revert(host, cfg) -> [Action]` | how to undo it |

`run_module` does: check; if already satisfied, say so; otherwise show the explanation, run `preflight`, show every step, apply (unless a dry run or declined),
and check again. Steps never go through a shell, and the same steps are what is shown, so a dry run cannot differ from the real thing. Results are recorded in
`<config>/hardening.json`.

### The modules

| Id | Satisfied when | Lockout guard |
|---|---|---|
| `ssh-keys-only` | `sshd -T` reports password and keyboard-interactive logins off | refuses unless the admin's `authorized_keys` has a key; writes `10-<name>.conf` (first match wins), validates with `sshd -t`, then reloads (open sessions stay) |
| `no-root-equivalent-groups` | the admin is in none of `lxd`, `docker`, `disk`, `libvirt`, `kvm`; the worker is in none of those or `sudo`, `admin`, `wheel`, `adm` | |
| `ufw-lan-egress` | ufw is active and the LAN-blocking rules exist | refuses unless the address you are connected from is allowed by `network.ssh_from`; rules come from `[network]` (IPv4, and IPv6 when enabled); SSH-in rules first |
| `auditd-canaries` | auditd and the canary timer are active | blocked, with a reason, on a machine with no kernel audit |
| `unattended-upgrades` | package installed and switched on | |
| `state-permissions` | state folder and `secrets.env` are not readable by other accounts | |
| `job-isolation` | a real jailed command runs as the worker and sees only loopback; `bwrap` is not setuid; on Ubuntu 24.04+ `kernel.apparmor_restrict_unprivileged_userns` is `1` | installs `bubblewrap` if missing; writes `61-<name>-userns.conf` and sets the sysctl back to `1` (Ubuntu's default) |

`hardening.modules` in `ct.toml` chooses which are offered; the default set is `job-isolation`, `ssh-keys-only`, `ufw-lan-egress`, `auditd-canaries`,
`no-root-equivalent-groups`, `unattended-upgrades`.

### The firewall

Generated from `[network]` and `server.ssh_port`. Order is fixed: default deny incoming and allow outgoing; SSH in from `ssh_from`; DNS to your DNS servers (or the
detected gateway); DHCP and mDNS; exact `lan_allow_out` allowances; then denies for private ranges (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, and `fc00::/7` when
IPv6 is on). Link-local IPv6 is left alone because neighbour discovery needs it. With `network.ipv6 = "disabled"` the update writes a sysctl file that turns IPv6 off and ufw gets
no IPv6 rules. By default a dry run only prints the rules.

### Adding a module

Write a class with the five members above, register it with `@module`, add tests that run it against the fake machine in `tests/test_setup.py`, and name it in
`hardening.modules`. Pack-provided modules are not loadable yet (see [packs](packs.md)).
