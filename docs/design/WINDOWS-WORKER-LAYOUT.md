# Windows worker layout (slice 4b of WINDOWS-SPECIAL-USE.md)

**Goal.** A Windows computer gets the folders and the unprivileged local account a worker needs, set up over SSH with PowerShell, so that the account running jobs can read the release and its tools, write only its own state, and can change nothing else.
**Done when.** On the real Windows 11 VM: `winlayout.ensure` creates `%ProgramData%\claude-tenta\{tools,release,config,state,state\jobs}` and the standard local account `ctworker`; a second run changes nothing; `winlayout.check` reports the layout healthy; and a LIVE probe, run as that account, shows it can read `tools` and `release`, write in `state\jobs`, and cannot write in `release`, `config` or `tools` nor be a member of Administrators. The Linux suite is unchanged.
**Not in scope.** Starting the worker (a scheduled task or service, which needs the account's password and is its own slice), denying interactive logon, the firewall, the transport and its keys, writing the worker's configuration, the registry, Windows 10 testing.

## Decisions
- Root `%ProgramData%\claude-tenta`, inheritance removed: only SYSTEM and Administrators have full control. Under it: `tools` (git, Python) and `release` (the fetched release) and `config`, each read-and-execute for the worker account; `state` (clones, results, job folders under `state\jobs`) modify for the worker account. The account may only pass through the root folder itself.
- The account is a **standard** local user, never in Administrators, password random and never stored: the slice that starts the worker sets a new one at that moment (an administrator can always reset it), so nothing has to be kept.
- A Python or git is used only if the worker account can read it: a Python under a person's profile (`C:\Users\...`) is not reused; the tools installer puts its own in `tools\python` (found there on a repeat). The winget route is gone: it installs under one person's profile.
- Everything is idempotent, reports `changed`, and keeps the real error text, like the tools installer.
