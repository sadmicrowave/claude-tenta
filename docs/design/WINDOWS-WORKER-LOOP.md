# Windows worker loop (slice 4d of WINDOWS-SPECIAL-USE.md)

**Goal.** `python -m ct.cli run` works on Windows: it starts under the `ctworker` account from the Scheduled Task, reads its configuration, keeps its state, receives a job over the Direct transport, runs it with the job runner and writes the result, so a person's computer can send a job to a Windows worker and get its result back.
This is too big for one branch; it is three small ones, each with its own done-when.

## 4d-1 Configuration and start-up (this branch)
**Done when.** (a) A worker configuration with Windows folders (`C:/ProgramData/claude-tenta/...`, forward slashes) loads and gives the right folder for every item, with system folders refused: unit tests that run anywhere. (b) On the real Windows 11 VM, `python -m ct.cli help`, `status` and one `fetch` pass start as `ctworker` from the release folder with that configuration and fail only for what is not there yet (no queue), with the real error text kept; the list of what else breaks on Windows is written here. (c) The Linux suite is unchanged.
**Not in scope.** Receiving a job, running one, writing the configuration file from the installer, the privileged helper (Linux only), file locks, the state publishing details.
Decisions: Windows folders are written with forward slashes and a drive letter (`C:/ProgramData/claude-tenta/state`): Python and git on Windows accept them, and no backslash can reach a shell. The run folder and the helper socket path live under the state folder (there is no `/run`).

## 4d-2 The Direct transport to a Windows worker (this branch; scope at the end)

## 4d-3 First job end to end (after)
`ct-run` sends a real job, the worker's loop runs it with `run_command`, the result comes back, on the real VM and by hand. Registry classes with `network: false` are refused (already built).

## Not covered anywhere yet
Windows 10 (unverified, awaiting a beta tester), a reboot of the VM, GitHub transport to a Windows worker.

## 4d-1 result (run on the real Windows 11 VM, as the administrator account)
- `ct.cli help` and `ct.cli status` work: memory, disk and the state read from the configuration (heat is "unreadable": no sensor, as on a VM). The worker's Windows unit tests (21) pass there.
- Found by running it, and fixed: the context built `LinuxSensors` directly (now `runner.default_sensors()`); the file lock was unwritten (now `msvcrt.locking`, tested across two processes); `git` was not on the worker's PATH (the start task now puts `tools\git\cmd` first on the worker's own PATH, never on the system PATH).
- Next failure, by design (what is not there yet): `fetch` / `run` stop with a raw `FileNotFoundError` for `config\registry.yaml`, which the installer slice will write. A raw traceback is not an acceptable end-user message: the installer slice must make the worker say plainly that it is not set up and what to run (both operating systems share the path).
- Not run: as `ctworker` through the start task (needs the registry and queue first), a reboot.

## 4d-2 The Direct transport to a Windows worker (scope)
**Goal.** Your computer pushes jobs to, and reads results from, a Windows worker over SSH exactly as it does for a Linux one: as a restricted mailbox account whose key can run only git on the worker's two bare repositories (`queue.git`, `results.git`), through the same mail shell.
**Done when.** On the real Windows 11 VM: the mailbox account, its folders and ACLs, the two bare repositories, the shell launcher and the client's public key are set up by one idempotent step (`winmailbox.ensure`); from the Mac, with only the mailbox key, `git push` to the queue works, `git ls-remote` on the results works, a push to the results and any other command are refused with the shell's own message; a second `ensure` changes nothing; and the account is not an administrator and cannot read the worker's state or config.
**Not in scope.** Writing the registry or the worker's configuration (installer slice), running the worker loop against the queue (4d-3), GitHub transport, the setup tool's check-in.
Facts that shape it (to be confirmed by probes on the VM before they are relied on):
- The client's push runs `git-receive-pack '<path>'` over SSH; the forced command (`restrict,command="..."` in the account's authorized keys) must be a launcher file that puts the tools' git on PATH and starts `python -m ct.mailshell`. The mail shell must treat `/C:/ProgramData/...` and `C:/ProgramData/...` as the same path, case-insensitively.
- The URL for a Windows path needs a leading slash: `ssh://user@host/C:/ProgramData/claude-tenta/state-mail/queue.git`.
- Git on Windows refuses repositories owned by another account ("dubious ownership"): the launcher trusts exactly the one repository, for that one command, as the Linux shell does.
- Where the account's key file lives on Windows OpenSSH (a profile that does not exist before the first login, or a Match block in sshd_config) is decided by a probe.

## 4d-2 result (run on the real Windows 11 VM, from the Mac)
`winmailbox.ensure` set up the mail account (`claude-tenta-mail`, a standard user with no interactive or remote-desktop logon), the two bare repositories under `state-mail`, the launcher and the key file, and a marked `Match User` block in sshd_config (checked with `sshd -t`, backed up, restarted); a second run changed nothing. With only the mailbox key, from the Mac: `git ls-remote` on the results works, `git push` of a job to the queue works and reads back, a push to the results is refused, any other command is refused, and another repository path is refused, all with the mail shell's own message. As the worker account (a one-time task): it reads and clones the queue, writes the results, and its push to the queue is rejected. `winmailbox.remove` undoes the account, the sshd_config block and the git trust and keeps the mailbox contents.
Probe finding (decided the sshd_config route): a Windows account that has never logged in has no profile, and a key file placed in `C:\Users\<name>\.ssh` was not used; `Match User` with an `AuthorizedKeysFile` under ProgramData worked.
Not run: the worker loop reading a job from the queue (4d-3), the client side `ct-setup` writing the key (installer slice), a reboot, Windows 10. The sshd_config edit changes the computer's SSH server settings for one account; it is marked, reversible and the only change to that file.
