# Removing an install: Windows workers and your own computer

Scope of the branch `uninstall-all` (2026-11-21). Before it, only a Linux worker could be removed (`arm-setup uninstall`); a Windows worker could be installed and reinstalled but not removed, and nothing took the commands off your own computer.

## What each command removes

`arm-setup uninstall --worker NAME` on a Windows worker (preview first, `--apply` asks once, `--keep-data` as on Linux):
1. the start task, and any job still running as the worker account (`wintask.remove`);
2. the mailbox account, its sshd_config block and the git trust (`winmailbox.remove`);
3. the Python the setup put under the worker's tools folder, through its own uninstaller (a folder delete would leave its registration behind and a later install would stop with error 1601), the rights the setup set (only the four it changed, and only entries whose account no longer exists), the worker account and its profile, and the folders (`winremove`);
4. this computer's side of that worker: its line in `workers.toml`, its configuration file and its own client folder (`workers/<name>`, never a folder shared with other installs).

`arm-setup uninstall --client`: the links to `arm-run`, `arm-admin`, `arm-setup`, `arm-help` in `~/.local/bin` (only links that point at these commands), the two lines the setup added to the shell startup file, and unless `--keep-data` the configuration files and the client folder (approval key, mailbox keys, clones). It prints the one command that removes the Python environment; it never runs it (the process is using it).

## What stays, on purpose

Windows itself, OpenSSH, the administrator account, your GitHub repositories (delete them in GitHub), any worker computer (remove it first with `--worker`), and on Linux the protections (key-only SSH, the firewall) as before.

## How another user reaches the same state (rule 8)

An interrupted removal: every step only removes what is still there, so running it again finishes it, and a failing step names itself and keeps the real error. A computer that is off: named before anything is removed. A worker removed by hand: each step says "already gone".

## Not covered

Removing a Mac worker (there is no Mac worker), the Windows 10 behaviour of the Python uninstaller (unverified target), and removing the OpenSSH server the Windows setup may have installed.
