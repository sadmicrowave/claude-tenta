# A Windows computer as a Special Use worker (design, 2026-11-19; detection built, the rest planned)

**The decision.** Windows is a *device* target, not a host install: a Windows computer can be a worker only as `special-use` (see SPECIAL-USE-DEVICES.md), with its own release, pulled and installed on it when the network scan finds a Windows computer. It is not a second way to install the Linux release, and the current build does not change. Treating Windows as an ordinary full-isolation worker is out of scope.

**What this does and does not cost (corrected).** The smallest way to put a Windows machine to work today is still a Linux virtual machine (or WSL2) on it, added by its own address: that needs no new code. This design is the next smallest *native* path: it is much smaller than a full Windows host install because it promises less (below), but it is still a port of the platform layer, not a switch.

## What the person sees
1. The scan finds a computer whose SSH greeting says `OpenSSH_for_Windows`, lists it as `Windows, OpenSSH 9.5 (Special Use only)`, and counts it separately from Linux servers. **Built.**
2. Picking it runs the candidacy check: for Windows it stops at once, before anything is run on it, with a card that says it can only be a Special Use worker with the separate Windows release, that nothing was changed, and what to do today (a Linux VM or WSL2, added by its own address). **Built.**
3. When the Windows release exists, the same pick installs it: no purpose question (it is Special Use), the Windows release is fetched from the release source and installed, `ct-admin apply` later brings that worker to the newest Windows release and never the Linux one.

A Windows computer is only found when its owner has turned on the OpenSSH Server optional feature. A machine without it is not discoverable by this scan; finding it by SMB, RDP or WinRM is a possible later step.

## The promise, stated plainly
- **Kept:** every job needs your approval (a Windows worker does not honour `tier: auto`: the tier is always `approve`), approvals are signed over the exact job file, results pass the output guard, the Claude ledger and limits apply, and what the worker publishes (state, registry) works as on Linux.
- **Not promised:** the job jail. There is no equivalent of bubblewrap, so "no network unless the class declares it" and "no view of the worker's home" cannot be guaranteed; jobs run as a dedicated standard (non-admin) local account, with a Job Object for kill and limits. The status card says `ISOLATION RELAXED` and `Used for: special use`, as for any Special Use worker, and the setup says so before it installs.
- **Out for the first version:** root-level (privileged) actions, tripwire canaries, the whole-machine protections (none apply), and Claude jobs.

## What has to be written (the platform layer)
| Linux today | Windows replacement | Size |
| --- | --- | --- |
| systemd timers and services | Task Scheduler tasks (fetch, step) running the worker in a loop | M |
| bubblewrap jail | a dedicated standard account, ACLs on its folders, a Job Object (kill, memory, CPU) | M |
| `useradd`, `chmod`, `chown` | a local account and `icacls` | S |
| sudoers and a root helper over a systemd socket | none in the first version (no privileged actions) | none |
| `/proc`, `hwmon`, `who` | WMI/CIM counters and `query user` | S |
| `fcntl` locks, process groups | `msvcrt` locks, Job Objects | S |
| apt, ufw, auditd, unattended-upgrades | none (not part of a Special Use device) | none |
| the installer's remote shell (`sh`) | PowerShell over the OpenSSH server | M |
| Python from the distribution | an embeddable Python unpacked into the worker's folder | S |
| git and `ssh-keygen -Y` | Git for Windows and the OpenSSH client that Windows ships | S |

The code that does not change: the class model, the orchestrator, approvals and signatures, the client and admin commands (they run on the administrator's computer), the registry and the transport logic. A small `platform` module (posix and windows implementations behind one interface) is the seam, so the Linux build only gains an import, not behaviour.

## Release split
- A separate release line and artifact for Windows (own version tag and release branch, own CI), so a Windows change can never block or alter a Linux release. The Linux release carries the detection and the hand-off, and nothing else of Windows.
- The release source names the Windows release; `ct-admin apply` and the install pick it by the worker's platform, published in its state.

## Slices
1. **Built:** detection in the scan, the separate count, the stop with the explanation, the cards.
2. The `platform` seam in the Linux code (locks, process control, sensors), with unit tests that must pass unchanged on Linux.
3. A Windows continuous-integration job (a Windows runner running the platform module's unit tests).
4. The Windows worker runtime: the loop, the Job Object runner, the folder and account layout, the transport over OpenSSH and Git for Windows.
5. The PowerShell installer driven over SSH, and the release selection by platform.
6. The status, workers and class cards for a Windows worker; documentation; the test matrix; an end-to-end route on a real Windows machine (by hand at first).

## Decisions
- Supported: Windows 10 and 11 (Pro and Enterprise). Home is not a target.
- No Claude jobs in version 1: the sign-in tool and its isolation need their own work.
- A Windows worker refuses a class that needs no network (`network: false`), because it cannot enforce it. The refusal names the class and says why; the review card does not offer it. Nothing is accepted with a warning.
- Windows 10 is an **unverified target** (2026-11-20): no Windows 10 test image could be found, so everything is proven on Windows 11 only. It is expected to work and is not claimed to; a beta tester will confirm. Nothing Windows-11-only may be used.
