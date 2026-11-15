# Packaging design: config, wizard, packs

Goal: someone with a spare Linux box and a laptop can answer a short list of questions and end up with the same architecture,
hardened by default, and can extend it (job classes, hardening steps, off-limits rules) without editing core code.

## Principles

1. **Secure by provisioning, not by fixing.** Nothing in the hardening list is a post-install "fix". The wizard asks plain questions, writes the
   answers to `cw.toml`, and applies the matching hardening while it builds the machine: key-only SSH, no root-equivalent groups for the admin
   account, isolation that fails closed, restricted file modes, sandboxed units. A user never has to know which commands to run, or in what order.
2. **Questions in, config out.** The wizard's job is to turn answers into a config file and then converge the machine to it. `cw-setup apply`
   can be re-run at any time and is idempotent; `cw-admin audit` reports drift from the config.
3. **Every root-run change is verified.** After installing a new version the tool runs a self-test job end to end and offers an automatic
   rollback to the previous version if it fails, so hardening can never silently strand the worker.
4. **Quiet is not safe.** Alerts are short and carry no environment detail; a locked worker sends reminders (default after 1, 6, 24 hours, then daily,
   at normal priority) until a human clears it. Both are config values (`[alerts]`).

## Roles and repos

| Thing | Where | Public? |
|---|---|---|
| This repo (`claude-worker`): source, wizard, packs, docs | GitHub, public | yes |
| `<user>/cw-queue` (jobs mailbox + the user's own registry) | created by the wizard, private | no |
| `<user>/cw-results` (results mailbox) | created by the wizard, private | no |

The wizard creates the two private repos under the user's account (with `gh` if present, otherwise it prints exact steps), adds a read-only
deploy key to the queue repo and a write deploy key to the results repo, and writes the names into `cw.toml`. The software is installed
from this public repo at a pinned, signed release; the private repos hold **data and policy only, never code**. (That also removes the need
for the worker to trust a code path through a repo the user pushes jobs to.)

## Configuration

Three layers, later wins: built-in defaults, `cw.toml` (non-secret, one per install), environment variables (`CW_*`).
Secrets (API key, ntfy token) live only in `secrets.env` (mode 0600, root:worker), are never rendered into units or commands, and are
loaded through `EnvironmentFile=`.

`cw.toml` lives at `~/.config/claude-worker/cw.toml` on the client and `/etc/claude-worker/cw.toml` on the server. Both are gitignored.
A JSON Schema (`config/cw.schema.json`) validates it; the same validation runs in the wizard, in `cw check`, and before any template render.
Every value that reaches a shell, a unit file, a sudoers line or a firewall rule must match a strict pattern from the schema
(hostnames, usernames, paths, ports, CIDRs); nothing is interpolated unvalidated.

```toml
# config/cw.example.toml  (documentation values only)
[identity]
name        = "cw"                    # prefix for units, users, dirs
[server]
host        = "worker.example.lan"    # or IP; the client SSHes here
admin_user  = "cwadmin"               # human admin on the server (needs a password to sudo)
worker_user = "cwworker"              # unprivileged user that runs jobs
[github]
owner       = "your-github-handle"
queue_repo  = "cw-queue"
results_repo= "cw-results"
[paths]
code   = "/opt/cw"
state  = "/var/lib/cw"
config = "/etc/cw"
client_home = "~/.cw"
[hardware]
profile      = "small-server"         # old-laptop | small-server | workstation | custom
capacity     = 4
max_load     = 3.0
max_temp_c   = 75
thermal      = "none"                 # none | lm-sensors | apple-smc (selects the fan/temperature module)
[isolation]
require_netns = true                  # refuse to run a no-network class if isolation is unavailable
method        = "systemd-run"         # systemd-run | unshare
[alerts]
kind   = "ntfy"                       # none | ntfy | webhook | email
url    = "https://ntfy.example.com/cw-alerts"
remind_hours = [1, 6, 24]             # reminders while locked; then every `remind_repeat_h`
remind_repeat_h = 24
# the access token, if any, lives in secrets.env (CW_ALERT_TOKEN), never here
[policy]
packs  = ["core"]                     # off-limits rule packs, see below
[registry]
packs  = ["core"]                     # job class packs
[hardening]
modules = ["ssh-keys-only", "ufw-lan-egress", "auditd-canaries", "no-root-equivalent-groups", "unattended-upgrades"]
```

## Extension model: packs

A *pack* is a directory with a `pack.toml` manifest and files. Four kinds, one loader, one validation path:

```
packs/<name>/pack.toml        kind = "classes" | "policy" | "hardening" | "alerts"; requires = {os=[...], packages=[...]}
packs/<name>/classes/*.yaml   job classes (same schema as registry.yaml today, plus `description` and `risk`)
packs/<name>/policy/*.toml    off-limits rules
packs/<name>/hardening/<id>/  module.toml + check + apply + revert
```

Search path: built-in `packs/`, then `~/.config/claude-worker/packs/` (client side drafts) and, on the server, only packs that were
**applied** (installed root-owned under `/etc/<name>/packs/`, hash recorded). The server never loads a pack from a repo directly.

Rules that keep extensibility from becoming a hole:

1. **Applying a pack is an admin action** with a diff and a digest, exactly like `cw-admin apply` is today. Packs can contain code (hardening
   modules run as root), so third-party packs are shown in full, pinned by commit, and optionally require a signed tag.
2. **Policy can only tighten.** Merge order is core, then packs; for any key a `deny` wins over an `allow`, ceilings take the minimum,
   `locked = true` entries in core cannot be overridden. A pack cannot remove a core off-limits rule or a canary rule.
3. **Classes are declarative.** argv + typed params only; no shell strings, no inline scripts. A class declares `network`, `cost`, limits,
   `output_keys`, `output_allow`, `packages` (pinned). Classes whose risk is `high` (network, sudo, files in, claude) default to `tier: approve`.
4. **Every pack has tests**: a `tests/` folder run by `cw pack test <name>` with a fake runner, so contributors can check a class without a server.

### Off-limits parameters and consideration requirements (`policy/*.toml`)

```toml
[[deny.command]]        # never allowed as argv[0], in any class or profile run
names  = ["sudo", "su", "doas", "pkexec", "nsenter", "mount"]
reason = "privilege escalation"
locked = true

[[deny.param]]          # param values a class may never accept
regex  = "(^|/)\\.\\.(/|$)"
reason = "path traversal"

[[require.approval]]    # conditions that force a human approval regardless of the class tier
when   = { network = true }
reason = "job can reach the network"

[[require.consideration]]   # prompts the admin must answer in `cw-admin approve` (recorded in the security log)
when   = { class_kind = "claude" }
ask    = "Does the attached material come from a source you trust?"

[limits]
max_timeout_s = 3600
max_files_kb  = 2000
```

## Hardening modules

Each module has a `module.toml` (id, title, plain-words explanation, risk of lockout, default on/off, supported OS), and three scripts that
must be idempotent: `check` (exit 0 = already satisfied, prints why not), `apply`, `revert`. The wizard runs `check` for every module,
explains the ones that fail, asks one question each, shows the exact commands, applies, re-runs `check`, and records the result in
`/etc/<name>/hardening.json`. `cw-admin audit` re-runs all checks any time and reports drift.

Initial set, from the review: `ssh-keys-only`, `no-root-equivalent-groups`, `ufw-lan-egress` (generated from config, IPv4 and IPv6),
`netns-required`, `auditd-canaries`, `unattended-upgrades`, `state-permissions`, `unit-sandboxing`, `tcp-listeners-audit`.

## The wizard (`cw-setup`)

Two halves; the second runs on the server (over SSH from the first, or directly on the box). Every step prints the commands before it runs them,
supports `--dry-run`, can be resumed (state in `cw-setup.state`), and is idempotent.

**Client half** (`cw-setup client`)
1. Check prerequisites: Python 3.9+, git, ssh, optional `gh`.
2. Ask: server address, admin username, GitHub account, names for the two private repos. Default every answer to a safe, generic value.
3. Create an SSH key if none exists; test login; offer `ssh-copy-id`.
4. Create the two private repos (`gh repo create --private`, or print the instructions), add deploy keys when the server half has produced them.
5. Write `cw.toml`, install the `cw-*` commands, offer the Claude Code skill.

**Server half** (`cw-setup server`)
1. Detect OS and version, RAM, cores, disk, sensors, sudo flavour, systemd, existing groups and listeners. Refuse unsupported OSes with a clear message.
2. Suggest a hardware profile from what was found; show every threshold it implies and let the user change them.
3. Create the two users and directories with correct modes; install the code from a pinned release; render and validate units.
4. Generate the deploy keys, show the public halves for the client half to upload.
5. Offer **each hardening module and each optional feature, one question at a time**, with the plain explanation and the risk of locking yourself out.
6. Choose packs (job classes and policy), alerts, and the sudo approach (below).
7. **Finish with a real test job** through the full loop (push, run, guard, result), and a negative test (a canary read and an over-limit job are stopped).

## Sudo ergonomics (replaces `sudoers-session`)

Problem: many back-to-back admin commands each prompt for the sudo password. The current fix does not work on sudo-rs and, where it does,
gives root to every process of the admin user for 15 minutes. Options, in order of preference:

1. **One session, many commands (default).** `cw-admin session` opens a single `ssh -t` with one shell on the server; sudo then caches the
   password for that terminal using its normal timeout, and `cw-admin` runs its batch inside it. Works on sudo and sudo-rs, no sudoers change.
2. **Narrow NOPASSWD for read-only wrappers.** `cw-status`, `cw-show`, `cw-review` run as the worker user and change nothing; allow exactly those
   (full path, no wildcard arguments) without a password. State-changing commands (`apply`, `approve`, `unlock`, `kill`) keep the password.
3. **No caching** for people who prefer it.

Whichever is chosen is recorded in `cw.toml` and verified by `cw-admin audit`. No `!tty_tickets`, no long timestamps.

## Privacy guard (what keeps the public repo clean)

- `scripts/privacy-check` runs as `pre-commit` and `commit-msg` hooks, in CI, and with `--history` before any push. Generic rules catch private IPs,
  MACs, e-mails, `.local` hostnames, keys, tokens, ntfy topics and home paths. A personal denylist outside the repo
  (`~/.config/claude-worker-dev/denylist`) catches your own handle, names and hostnames without publishing them.
- It also refuses to commit unless the git author is a `users.noreply.github.com` address.
- Documentation and tests use reserved example values only (`192.0.2.0/24`, `example.com`).
- `.gitignore` excludes `cw.toml`, `secrets.env`, state, results, jobs and keys.
- The old private repos are never pushed here; code is brought across by hand, parameterized, with a fresh history.

## Roadmap (order of work; the live install keeps running throughout)

1. Config loader and schema; a `names` module so no module hard-codes a name or path. Port the dispatcher first, then shell scripts, then client tools.
2. Fix the H and M review items in the ported code (S1-S10) behind tests.
3. Pack loader, policy merge, hardening module runner; migrate `registry.yaml` into `packs/core` and personal classes into a user-private pack.
4. Wizard, server half then client half; clean-VM end-to-end test in CI.
5. Docs generated from schema and modules; signed releases; `cw-admin audit`.
6. Migrate the original live install to the packaged build as the first real user.

## Open questions

- Python package (pip/pipx) for the client, or keep the single-file scripts plus an installer? (Leaning: package with thin entry points.)
- Minimum supported distro set for v1: Ubuntu 24.04+/Debian 12+ proposed.
- License: MIT or Apache-2.0 (Apache gives an explicit patent grant; either fits open source).
