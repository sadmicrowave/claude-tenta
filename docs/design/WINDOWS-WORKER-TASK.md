# Windows worker start task (slice 4c of WINDOWS-SPECIAL-USE.md)

**Goal.** The worker is started on a Windows computer the way systemd starts it on Linux: by the operating system, at boot, as the unprivileged `armworker` account, restarted if it dies, stoppable and removable, with no one logged in.
**Done when.** On the real Windows 11 VM, `wintask.ensure` registers a Scheduled Task `armada-worker` that runs a command as `armworker` at startup; `start` shows the process running AS `armworker` (its owner read back from the system, not assumed); `stop` ends it and everything it started; a second `ensure` changes nothing and does not touch the account's password; `remove` leaves nothing behind; and the trigger, restart and no-time-limit settings are read back. The Linux suite is unchanged.
**Not in scope.** Making `armada.cli run` itself work on Windows (config, paths, the transport: the next slice; this slice proves the START mechanics with a harmless long-running command and keeps the real command as the default), a reboot test on the VM, logs rotation, `arm-admin`/status cards, the firewall.

## Decisions
- One Scheduled Task, trigger AtStartup, principal `armworker` with logon type Password ("run whether logged on or not"): no desktop is needed (the layout slice proved a task as this account runs). It is registered with a password set at that moment and discarded straight after; Task Scheduler keeps it. A repeat `ensure` compares a hash of the task's definition (kept in its description) and does nothing when it matches, so the password is not reset needlessly.
- Settings: restart on failure every minute without a count limit, no execution-time limit, one instance only, start when available, run on battery. The command runs from the release folder with `PYTHONPATH` and `ARM_CONFIG` set, and its output goes to `state\worker.log`.
- Status reads the task state, the last result and the owner of the running process from the system (WMI), so "running as armworker" is observed.
- Stopping ends the task and its process tree (the worker's own jobs are in Job Objects with kill-on-close, from the job runner slice).
