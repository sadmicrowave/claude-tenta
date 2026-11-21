# Claude Tenta for Windows 0.2.0 (preview)

The first Windows release that can be a worker. `ct-setup add-worker <windows address>` sets a Windows computer up over SSH, a job sent from your computer runs there, and its result comes back.

## What you get
- **One command from your computer.** It says what it will and will not touch and asks, then installs git and Python for the worker (existing ones are used; downloads are checked against pinned hashes and, for Python, its signature), creates the worker's folders and accounts, fetches this release, sets up the Direct connection (the mailbox), writes the worker's files, registers a task that starts the worker at boot, and sends a first test job. Every step can be run again; running it again only changes what is not already right (`ct-setup install --worker NAME`).
- **Two unprivileged accounts, never administrators.** One runs the worker and its jobs; one only receives jobs over SSH and can run nothing but git on the two mailbox repositories (it cannot log on at a desktop). Their passwords are random and are not kept by the setup (Windows' Task Scheduler holds the worker account's, so do not change it).
- **`ct-run` works as it does for any worker:** `ct-run classes`, `ct-run submit CLASS --wait`, results, the round-trip breakdown.
- **Job classes** come from the `windows-core` pack (`win-status`, `win-echo`); more are added the way every class is (`ct-admin adopt`).
- **Each job runs in a Job Object:** its whole process tree is stopped together on a timeout, a limit or when the worker stops.

## What it changes on the Windows computer
Everything is under `C:\ProgramData\claude-tenta` except: two local accounts, one marked block in the OpenSSH server's `sshd_config` (for the mailbox account only; backed up and checked before use), the git trust list of the worker's own git, and one Scheduled Task. All of it can be removed again.

## Requirements
- Windows 11 Pro or Enterprise, with the OpenSSH server installed and key login working for an administrator account. (Windows 10 is expected to work and is unverified until a tester tries it; Home is not a target.)
- Internet access to github.com and python.org for the setup.

## Limits you should know about
- **Special Use only.** A Windows worker has no file or network isolation for jobs (there is no equivalent of the Linux jail): a class must say `network: true` to be offered there, and what a job does is limited by what the worker account may do. Use it for tools boxes and trusted work.
- No Claude jobs on Windows in this version; the Direct connection only (no GitHub connection).
- `ct-admin apply` does not update a Windows worker yet (it refuses with a clear message): set it up again to move it to a newer release.
- Not included: removing a Windows worker with `ct-setup uninstall`, approvals and signatures on Windows, load average (reported as 0), a double-click setup tool for a computer without OpenSSH.
