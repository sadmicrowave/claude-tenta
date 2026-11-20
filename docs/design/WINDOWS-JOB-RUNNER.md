# Windows job runner (slice 4a of WINDOWS-SPECIAL-USE.md)

**Goal.** `runner.run_command` runs one command on Windows, watched the way it is on Linux: its own output, exit status, a timeout that stops the WHOLE process tree, a memory limit, and the same metrics keys.
**Done when.** On the real Windows 11 VM (and the Windows CI runner) the tests in `tests/test_winproc.py` pass: an `ok` run with its output, an `error` run with its exit code, a timeout that leaves no grandchild alive, a memory limit that ends the job with status `limit`, a worker stop (`Killed`) that stops the tree, and a job that sees only the environment `build_env` gives it. The Linux suite is unchanged.
**Not in scope.** The worker loop and its state, the folder and account layout, a service, the transport, file locks, `ct-admin`/status cards for Windows, a network jail (the registry already refuses classes that need no network on Windows), Claude jobs, load average (reported as 0 until the worker publishes something real).

## Decisions
- One process tree = one Windows **Job Object** with kill-on-close (a worker that dies takes its jobs with it). The process is created suspended, put in the job, then resumed, so no grandchild can start outside it.
- Stopping: `TERM` = Ctrl-Break to the process group (a program may clean up), `KILL` = terminate the Job Object. Same two-step `stop_group` as Linux.
- Memory: the runner polls the working set of every process in the job (as it polls `/proc` on Linux) and stops the job over the limit; the Job Object has no memory limit of its own, so a limit is a clean `limit` result, not a failed allocation.
- CPU time and metrics come from the job's own accounting.
- All Win32 calls are in `src/ct/winproc.py` (ctypes, nothing imported at module load), reached only through `ct.plat`, so the seam test and the Linux build are unaffected.
- Environment: a short system PATH (`%SystemRoot%\system32;%SystemRoot%`), `SystemRoot`, `TEMP`/`TMP` inside the job's own folder, `HOME`/`USERPROFILE` as given; nothing else of the worker's environment.
