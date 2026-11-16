# Review of the existing claude-ballast install (2026-11-15)

Scope: the queue repo at commit `de7cef2` (code, registry, units, tests, docs) and the live worker host, reviewed read-only over SSH.
The deployed `dispatcher.py` and `registry.yaml` match `main` byte for byte (no drift). Nothing was changed on either machine.

Severity: **H** fix before anyone else installs it, **M** fix in the first release, **L** improve when convenient.

## Security

| # | Sev | Finding | Fix in the packaged version |
|---|---|---|---|
| S1 | H | **Network isolation fails open.** `isolate()` runs a job with full network if `unshare` cannot build a namespace, unless `CW_REQUIRE_NETNS=1` (default off, and not set on the host). The README's "jobs have no network" is therefore only true while the probe passes. | Default `require_netns = true`. A job class that needs no network is refused, not run, when isolation is unavailable. The wizard probes and tells the user. |
| S2 | H | **The admin account has a root-equivalent group.** On the reviewed install the admin account was in the `lxd` group (lxd installed). Membership in `lxd` gives root without a sudo password, which makes the sudo password and everything built around it decorative. | Hardening check `no-root-equivalent-groups` (lxd, docker, disk, adm warning). Wizard offers to remove them or to remove the package. |
| S3 | H | **SSH password login is still on** (`Permission denied (publickey,password)`). The key-only step in INSTALL.md is optional and was not applied. | Hardening module `ssh-keys-only` (offered by default, verifies key login first, keeps the old session open). |
| S4 | M | **The sudo "remember for 15 minutes" design does not work and is not installed.** `claude-ballast-session` is absent on the host: Ubuntu 26.04 ships sudo-rs, which only caches per terminal and has no `!tty_tickets`. Where it does work (classic sudo) it hands root to any process running as the admin user for 15 minutes, and the Mac side adds a 15-minute SSH master socket. | Remove `sudoers-session`. See "Sudo ergonomics" in DESIGN.md for replacements that work on sudo-rs and do not widen exposure. |
| S5 | M | **Job environment puts `~worker/.local/bin` first on PATH** (`run_command`). Anything a job or a package step writes there shadows system binaries for later jobs. | PATH = fixed system dirs only; throwaway tool folders are appended explicitly per job. |
| S6 | M | **systemd units have no sandboxing.** `systemd-analyze security claude-ballast-fetch.service` scores 9.2 UNSAFE. | Add `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges` (where the class set does not need sudo), `ReadWritePaths=` for state. Jobs that need `sudo` run through a separate unit. |
| S7 | M | *(addressed in the packaged version: jobs run in a bubblewrap jail and the restriction stays on; see docs/components/security.md)* **Unprivileged user namespaces are fully open** (`apparmor_restrict_unprivileged_userns = 0`), which is what makes the `unshare` isolation work but also widens kernel attack surface for every local user. | Prefer a transient systemd unit (`PrivateNetwork=yes`, `NoNewPrivileges`, `MemoryMax`, `CPUQuota`) as the isolation primitive; keep `unshare` as a fallback; document the trade-off. |
| S8 | M | **Thermal classes are `network: true` with `sudo`.** Two classes get the network back only because sudo does not work inside the namespace. | Run the privileged helper from a root-owned systemd unit triggered by a path or socket, so the job itself stays network-less. |
| S9 | M | **Package installs are unpinned.** `apt:`, `npm:` and `pip:` entries take the newest version, installed as root (apt) or into a root-owned tools folder that every later job puts on `PYTHONPATH`. | Allow `==version` pins and hashes in registry entries; the wizard's default packs pin. Show hashes in the `apply` diff. |
| S10 | M | **Alerts rely on a secret topic.** An ntfy.sh topic has no authentication, and the alert body carries job details. | Support ntfy tokens and self-hosted ntfy; send only a short code and the job id, not details. |
| S11 | M | **A single false positive locks the worker indefinitely.** On the reviewed install one test job that printed the machine's hostname tripped the `hostname` rule and left the worker locked for hours until a human unlocked it. Fail-closed is right, but it needs a visible, rate-limited reminder. | Repeat the alert at a back-off schedule while locked; `cw-status` on the client prints the lock reason. Per-class `output_allow` stays the escape hatch, canaries never allowed. |
| S12 | L | **README says the results deploy key is "write-only".** GitHub deploy keys with write access can also read and can force-push. The results repo is not an integrity boundary. | Say so plainly. Never treat results as trusted; consider signing result files (the worker signs, the client verifies). |
| S13 | L | **State directories are world-readable** (`state/work`, `state/done`, `results/` are 755). Job inputs are readable by any local account. | 750, group `cw` for the admin user. |
| S14 | L | **`claude-analyze` can read anything the worker user can**, including the decoy credentials, and has the network and the API key. Tool use is limited to Read/Grep/Glob and results are guarded, but attached files are untrusted prompts. | Keep as an optional pack, off by default; document the prompt-injection model; run with a dedicated, spend-capped API key (already advised). |
| S15 | L | **Fixed IP ranges in `harden-network.sh`** (all RFC1918 denied outbound, IPv6 not covered, gateway auto-detected). Works for one LAN layout; breaks for others who need to reach a local DNS server or a mirror. | Generate from config (`lan_allow_out`, `dns_servers`), cover IPv6, always dry-run first. |

What is already good and should be kept: argv-only commands with typed params; digest-pinned approvals; installed-registry-only trust;
file-name validation (`FILE_NAME_RE`, no dot-names, size caps); `--ignore-scripts` and `--only-binary` for package fetches; hash-chained
security log; canary files with auditd; signed-tag self-update; outbound-only polling; clean separation of the queue and results repos;
`yaml.safe_load` everywhere; no `shell=True`.

## Completeness and consistency

- **No single source of truth for names and paths.** The server hostname, the two usernames, the install directories, the repo URLs and the
  client's host label are repeated in about 20 places across six files. Several are in test expectations.
- **Style kit copied three times** (`cw-run`, `cw-admin`, `cw-log`, plus shell and dispatcher copies) and kept in sync by a test. Becomes one module.
- **Three 1,300-2,700 line single-file programs.** Fine for one author; hard for contributors. Split into a package, keep thin entry points.
- **Hardware assumptions:** `mbpfan`, `applesmc`, specific sensor labels, capacity 4, 3.3 GB RAM. Must become a hardware profile.
- **Linux scope:** developed on Ubuntu 26.04 with sudo-rs and kernel 7.x. Declare Ubuntu LTS and Debian 12+ as supported; everything else best-effort.
- **Tests use fakes only.** There is no clean-VM end-to-end run. Add a CI job that provisions a container or VM and runs the wizard non-interactively.
- **Python and YAML dependency on the worker** (`PyYAML` from apt) is implicit; state it, check it in the wizard.
- **Docs mix reference and tutorial** (INSTALL.md is ~600 lines). Becomes generated from the wizard steps plus a short manual.
- **Leftovers:** compiled `.pyc` files exist in the working tree (ignored, fine); `PACKAGING-PLAN.md` becomes this repo's roadmap.

## Privacy (why the original private repos were not published)

The original install grew in two private repos whose history contains per-install details: commit authorship, hostnames and handles in docs,
defaults and tests, and job payloads and command history on the data branches. No credentials were found, but history cannot be assumed
clean, so this project starts from a **new** repo with fresh, single-author history and parameterized code. The wizard creates a
private queue/results pair under each user's own GitHub account. `scripts/privacy-check` blocks commits that contain personal or
environment data (see DESIGN.md, "Privacy guard").
