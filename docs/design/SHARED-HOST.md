# Computers other people use (design, 2026-11-19; idle-awareness and resource limits built)

**The goal.** A computer people log into (a PC, a Mac mini) can be a worker in its idle time without changing anyone else's use of it. Most of this is now covered by the purpose question (`isolation.purpose`, see SPECIAL-USE-DEVICES.md): a `shared` computer gets only the protections that affect tenta's own jobs by default, and the whole-machine ones (SSH keys only, the LAN firewall, no root-equivalent groups, automatic updates, IPv6 off) are offered one by one, each skipped unless the person says yes, so the install no longer turns off password logins for everyone.

**Idle-aware (built).** A shared worker must also stay out of the way. `[availability]` in `ct.toml`:
- `pause_when_logged_in = true`: the worker starts no new job while anyone is logged in (the accounts of `who`; a running job finishes). `ignore_users = ["agent"]` lists the accounts that do not count, for example the admin account you SSH in with.
- `window = "22:00-07:00"`: jobs start only within these hours on the computer's own clock (a window that crosses midnight is fine; empty means any time).
- The worker publishes why it is holding (`observed.held`), `ct-admin workers` shows `AVAILABILITY held: ...`, `ct-worker status` shows `Available`, and the scheduler ranks a held worker after the free ones. It is **not** left out: with nothing else free a job still goes there and waits until the computer is available again.

**Resource limits (built).** The jobs run in the step service's cgroup, so the limits go on that unit and apply to every job it started. In `[availability]`: `cpu_weight` and `io_weight` (1 to 10000, the normal share is 100, so 20 lets a person's own work win), `memory_max_percent` (all the jobs together; one that would go over is stopped, so the person keeps their memory) and `nice` (19 = most polite). Only what the owner sets is written into `step.service` (`CPUWeight=`, `IOWeight=`, `MemoryMax=`, `Nice=` right after `Type=oneshot`); with nothing set the unit is byte for byte what it was, so an existing install is not touched. `systemd-analyze verify` of the rendered unit passed on systemd 255, 257 and 259 (the three workers). On a Mac nothing applies yet.

**Not built yet**
- The acceptance test: a fingerprint of the machine-wide files (sshd configuration, firewall rules, sudoers, groups) before and after an install on a shared computer is identical, and a second user can still log in by password. It needs a real machine or VM.
- A Mac worker (the Mac mini as a shared worker needs a macOS design of its own: launchd, no systemd, `sandbox-exec` instead of bubblewrap).
- Limits that cannot be promised: a local administrator can read what the worker account can read, so Claude jobs stay off on a shared computer by default.
