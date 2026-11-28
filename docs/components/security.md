# Security model

## In plain words

The worker is a computer that runs commands on your behalf, so the design assumes that **something will eventually go wrong** (a bug, a bad
command, a stolen key) and limits how much damage it can do.

- **It only runs what you allowed.** Jobs are picked from a fixed list; the commands and parameters are fixed in advance.
- **It cannot phone home.** Jobs run with no network. If a job somehow printed a secret, it still could not send it anywhere; it can only put it in the
  result, and the result is checked.
- **It checks what leaves.** Every result is scanned for private addresses, hardware addresses, keys, tokens, account files, environment dumps,
  your machine names and long encoded blobs. A hit withholds the result, **locks the worker**, and sends a short alert. A person has to look and
  unlock it.
- **It sets traps.** Fake credential files are placed where a thief would look. Nothing legitimate reads them, so any read raises the alarm.
- **It cannot be fooled by a swapped job.** What you approve is the exact file you read.
- **Your own computer's secrets stay home.** Nothing in a job can reach the files on your laptop; the worker never sees them.
- **Installs are deliberate.** New code is installed only when you ask (or only from releases signed by a key you trust).

What it does **not** do: it is not a sandbox for hostile code from strangers, and it is not a replacement for keeping the worker updated and off the
open internet. The scan is a tripwire, not the wall. The wall is the lack of a network and the fixed list of outputs.

## Technical specification

### Threat model

Assets: the worker's host, your home network behind it, the secrets reachable from it, and the contents of your repositories. Adversaries considered: a
malicious or buggy job file, malicious content inside a job's input files, a compromised GitHub token or deploy key, a malicious package, a malicious
pack, and a local attacker with the worker account. Out of scope: a compromised admin account or a compromised laptop (they hold the keys), kernel exploits,
and physical access.

### Controls

| Control | Where | Property |
|---|---|---|
| Installed-registry-only trust | `worker.py`, `update.py` | the repository can only propose; root installs after validation |
| Typed, argv-only commands | `jobs.py`, `registry.py` | no shell, no string building; every parameter has a type |
| Policy | `packs.py`, `registry.py` | denied programs and parameter patterns; approval forced by rule; rules can only be added to |
| Digest-pinned approval | `cli.py` (worker), `admin/jobs.py` | approval carries the file's SHA-256 |
| Snapshot of each job | `worker.py` | edits to the repository copy are ignored |
| Job jail | `jail.py`, `runner.py` | bubblewrap: no network (unless the class needs it), no capabilities, no new privileges, own process tree and host name, read-only system folders, an empty home and no view of the install's configuration; **fails closed** when the jail cannot be built (`isolation.require_netns`) |
| Resource limits | `runner.py`, `worker.py` | timeout, memory, disk, file size, capacity, load, temperature |
| Output shaping | `results.py` | only declared fields; failures reduced to error lines |
| Output guard | `guard.py` | rules below; a hit withholds, logs, alerts and locks |
| Decoy credentials | `guard.py`, `canarywatch.py` | planted files and tokens; auditd watches reads; their text in any output is a breach |
| Hash-chained security log | `guard.py` | each entry includes the hash of the previous one; the chain can be verified |
| Lockdown | `alerts.py` | no job is processed while locked; reminders until cleared |
| Pinned packages | `packages.py`, `pkgspec.py` | exact versions, install scripts off, throwaway tools folder |
| Signed updates | `update.py` | `signed-tags` mode installs only tags signed by a key in `allowed_signers` |
| Sandboxed services | `render.py`, `sandboxtest.py` | the units are sandboxed; a changed unit is kept only if a test proves jobs still work |

### The guard's rules

`private_ip` (RFC 1918, link-local, CGNAT) · `ipv6_local` · `mac` · `private_key` · `token` (API keys, GitHub and cloud tokens, JWTs) · `passwd_line` ·
`env_dump` (five or more `NAME=value` lines) · `mdns_name` (`.local`, `.lan`, `.home`, `.internal`) · `hostname` (the machine's own) · `encoded_blob` (80 or
more base64-like characters) · `canary` (a planted decoy). A class may list `output_allow` rules to ignore for that class; `canary` can never be allowed.
The guard never logs or returns the matched text, only the rule's name. Published text is also scrubbed of anything shaped like an API key.

### Lockdown and alerts

A guard hit (`quarantine`), a decoy in an output (`breach`), or an auditd-reported read of a decoy file (`canary_access`) locks the worker and writes a
`state/lockdown` marker. Two alerts are sent, on purpose: one says what was withheld and why in a sentence; one says the machine is locked, once. While locked,
reminders go out at `alerts.remind_hours` (default 1, 6, 24 hours) and then every `alerts.remind_repeat_h`. Alerts are short and carry no class names, rule
names, paths or addresses; those stay in the log. Channels: the log, syslog, and an https endpoint (`ntfy`-style or a webhook) with an optional token read from
`secrets.env`. The channel cannot be used to trigger anything.

`arm-admin quarantine` lists and reads withheld results, and `arm-admin unlock` clears the lock after you have looked.

### Accounts and files

| Account | Can | Cannot |
|---|---|---|
| `server.worker_user` | read the queue, write the results, run jobs | use sudo, log in with a shell, or belong to root-equivalent groups |
| `server.admin_user` | SSH in, run read-only commands without a password (`access.sudo = readonly-nopasswd`), approve and install with its password | (hardening checks it is not in `lxd`, `docker`, `disk`, `libvirt`, `kvm`) |
| root | install releases, the canary watcher, the firewall | |

Folders: code and config are root-owned; state is `0750` worker-owned; `secrets.env` is `0640` root:worker. See [hardening](hardening.md).

### Known limits

- The scan is pattern matching and will miss a leak shaped differently; the no-network rule is what contains it.
- **The jail shares the kernel.** bubblewrap is a namespace sandbox, not a virtual machine; a kernel bug in namespaces is still reachable. Capabilities are dropped, no new privileges are allowed and Ubuntu's AppArmor profile stops a jailed job from creating further namespaces with powers, but it is the same class of protection as before, only tighter.
- **bubblewrap has had vulnerabilities**, mostly in its setuid mode, which this tool refuses to use. Keep the package updated (the `unattended-upgrades` module does it).
- **No cgroup limits.** Memory, time and temperature are watched by the worker's loop, not enforced by the kernel.
- Work that needs root is never given `sudo` inside a job. It goes through a signed, pinned root-level action run by a separate helper in its own sandbox; see [Root-level actions](privileged.md). That helper refuses to run where systemd cannot enforce its sandbox.
- The tripwire needs kernel audit; container-style machines do not have it.


### Spend, caps and patches (0.2)

- **Cap across workers.** `limits.max_in_flight` is checked on your computer before any job is sent.
- **Patches, not pushes.** A class that writes returns its changes as a patch in the result. The worker never pushes anything; you apply the patch after reading it, and your computer refuses paths outside the folder, `.git`, links and binary changes whatever the worker said.
