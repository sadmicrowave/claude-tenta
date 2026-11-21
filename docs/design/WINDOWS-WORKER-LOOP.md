# Windows worker loop (slice 4d of WINDOWS-SPECIAL-USE.md)

**Goal.** `python -m ct.cli run` works on Windows: it starts under the `ctworker` account from the Scheduled Task, reads its configuration, keeps its state, receives a job over the Direct transport, runs it with the job runner and writes the result, so a person's computer can send a job to a Windows worker and get its result back.
This is too big for one branch; it is three small ones, each with its own done-when.

## 4d-1 Configuration and start-up (this branch)
**Done when.** (a) A worker configuration with Windows folders (`C:/ProgramData/claude-tenta/...`, forward slashes) loads and gives the right folder for every item, with system folders refused: unit tests that run anywhere. (b) On the real Windows 11 VM, `python -m ct.cli help`, `status` and one `fetch` pass start as `ctworker` from the release folder with that configuration and fail only for what is not there yet (no queue), with the real error text kept; the list of what else breaks on Windows is written here. (c) The Linux suite is unchanged.
**Not in scope.** Receiving a job, running one, writing the configuration file from the installer, the privileged helper (Linux only), file locks, the state publishing details.
Decisions: Windows folders are written with forward slashes and a drive letter (`C:/ProgramData/claude-tenta/state`): Python and git on Windows accept them, and no backslash can reach a shell. The run folder and the helper socket path live under the state folder (there is no `/run`).

## 4d-2 The Direct transport to a Windows worker (next)
How a job reaches the worker over SSH. Known problem to design for: an SSH login on Windows does not have git on its PATH (the tools installer keeps git out of the system PATH on purpose), so `git push` from the person's computer cannot find `git-receive-pack`; the client has to name it (`--receive-pack`), learned from what the worker publishes. Done when a job pushed from the Mac lands in the Windows worker's queue.

## 4d-3 First job end to end (after)
`ct-run` sends a real job, the worker's loop runs it with `run_command`, the result comes back, on the real VM and by hand. Registry classes with `network: false` are refused (already built).

## Not covered anywhere yet
Windows 10 (unverified, awaiting a beta tester), a reboot of the VM, GitHub transport to a Windows worker.

## 4d-1 result (run on the real Windows 11 VM, as the administrator account)
- `ct.cli help` and `ct.cli status` work: memory, disk and the state read from the configuration (heat is "unreadable": no sensor, as on a VM). The worker's Windows unit tests (21) pass there.
- Found by running it, and fixed: the context built `LinuxSensors` directly (now `runner.default_sensors()`); the file lock was unwritten (now `msvcrt.locking`, tested across two processes); `git` was not on the worker's PATH (the start task now puts `tools\git\cmd` first on the worker's own PATH, never on the system PATH).
- Next failure, by design (what is not there yet): `fetch` / `run` stop with a raw `FileNotFoundError` for `config\registry.yaml`, which the installer slice will write. A raw traceback is not an acceptable end-user message: the installer slice must make the worker say plainly that it is not set up and what to run (both operating systems share the path).
- Not run: as `ctworker` through the start task (needs the registry and queue first), a reboot.
