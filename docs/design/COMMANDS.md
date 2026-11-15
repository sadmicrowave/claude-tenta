# Command set review (DRAFT, for discussion)

Status: a proposal. Nothing here is built yet; the tools below are described as they exist today and as they could be consolidated.

## Today (the original install)

```
cw-run          requester: queue jobs, read results, plan what to offload
 ├─ setup                          clone the two private repos
 ├─ submit <class>                 queue a job
 ├─ result <job> [--wait]          read a result
 ├─ status                         my recent jobs
 ├─ classes                        what the worker can run
 ├─ exclusions                     commands that were profiled and do not fit
 ├─ fit -- <cmd>                   would this command fit? (registered / excluded / unknown)
 ├─ fits                           the history of fit checks
 ├─ profile -- <cmd>               queue a profiling run
 ├─ adopt <profile-job>            record the verdict (fits -> proposal, does not fit -> exclusion)
 └─ prune                          squash old job files

cw-admin        administrator: act on the worker over SSH, and manage the private repo
 ├─ check                          is SSH login and `cw` working
 ├─ login / logout                 hold the sudo password for 15 minutes (does not work on sudo-rs)
 ├─ branches / merge / tidy        review and merge branches of the queue repo
 ├─ diff / apply                   preview and install a version on the worker
 ├─ adopt <profile-job>            register a profiled command as a class and install it
 ├─ retire <class>                 remove a class
 ├─ review / approve               see and approve waiting jobs
 ├─ status                         worker health, activity, safety checks
 ├─ quarantine / unlock / output   withheld results, clearing a lockdown, a failed command's full output
 └─ kill / cancel                  stop a running / not-yet-started job

cw-log          recorder of the commands Claude Code runs, ranked for offloading
 ├─ hook pre|post                  (called by Claude Code)
 ├─ top                            what is worth offloading
 └─ show / clear                   the log's location / forget it

cw-help         one page listing every tool
cw-status       alias for `cw-admin status`

On the worker (each is one engine, `cw`, under a different name)
 cw-status  cw-approve  cw-review  cw-show  cw-kill  cw-cancel
 cw-quarantine  cw-unlock  cw-output  cw-prune-results  cw-test-alert
System helpers: cw-update  canary-watch  fan-profile  sys-status  net-check  cw-sandbox-test  harden-network.sh
```

## What stands out

1. **The same operation lives in up to three places.** approve, review, kill, cancel, quarantine, unlock, output and status exist as a
   `cw-admin` command (over SSH) and again as a server command. The packaged version already has one engine (`<name> <command>`);
   the client's job is only to reach it.
2. **`cw-admin` does four different jobs:** operating the worker (status, approve, review, kill, cancel, quarantine, unlock, output),
   managing a repository (branches, merge, tidy, diff, apply), managing the registry (adopt, retire), and the connection (check, login, logout).
3. **`cw-run` mixes two audiences:** a requester (submit, result, status, classes) and someone planning capacity (fit, fits, profile, adopt, exclusions).
4. **`adopt` exists twice** (`cw-run adopt` records an exclusion, `cw-admin adopt` registers a class) and **`status` means two things**
   (`cw-run status` = my jobs, `cw-admin status` = the worker's health, plus the `cw-status` alias).
5. **`cw-log` is about planning what to offload**, so it belongs next to fit / profile / adopt / exclusions, not next to administration.
6. **`login` / `logout` are obsolete** with the new sudo design (read-only commands need no password; the rest ask).
7. **`branches` / `merge` / `tidy` / `diff` / `apply` manage the code and registry in the private repo.** In the packaged design code comes from
   the public project (a signed release) and the private repo holds only jobs and registry/policy, so most of these shrink to registry proposals plus `update`.
