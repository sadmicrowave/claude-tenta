# Command set review (DRAFT, for discussion)

Status: the decisions below are accepted and `cw-run`, the worker engine and `cw-admin` are built; `cw-setup` is next.

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

## Decisions so far

- `cw-log` becomes part of a planning group, `cw-run offload`, beside fit, profile, adopt and exclusions (it records the user's own commands).
- `cw-admin` stays flat (`cw-admin unlock`, `cw-admin approve`); its help screen groups commands under Worker, Jobs, Security, Registry and Release.
- `cw-admin login` and `logout` are removed: read-only commands need no password under the sudoers rule, everything else asks.
- `cw-unlock`, `cw-approve` and the other per-command server binaries do not exist in the packaged version: the worker has one engine, `<name> <command>`.

## Proposed tree

```
cw-setup                                 NEW  the wizard: client | server | audit
cw-help                                  every command, grouped

cw-run                                   a requester
 ├─ submit <class>  result <job>  jobs  classes
 ├─ setup  prune                         repair / housekeeping
 └─ offload                              what is worth running on the worker
     ├─ log  hook|top|show|clear         (was cw-log)
     ├─ fit -- <cmd>   history           (was fit, fits)
     ├─ profile -- <cmd>
     ├─ exclusions
     └─ propose <profile-job>            (was cw-run adopt: a registry proposal, or an exclusion)

cw-admin                                 the administrator, over SSH (flat; the help groups them)
 ├─ Worker    status   check
 ├─ Jobs      review   approve   cancel   kill
 ├─ Security  quarantine   unlock   output
 ├─ Registry  adopt <profile-job>   retire <class>   proposals
 └─ Release   diff   apply

<name>                                   on the worker: the same engine the timers use
 ├─ status  show  review  approve  cancel  kill
 ├─ quarantine  unlock  output  prune-results  test-alert
 └─ (internal) fetch  step  exec  run  canary-alarm  check-registry  install-packages
System helpers (root, not user commands): update  canary-watch  sandbox-test
```

## Decided (all three open points accepted)

- `cw-run status` is now `cw-run jobs` ("my jobs"). `status` means the worker's health only, and the `cw-status` alias is dropped.
- `adopt` is split. `cw-run offload propose` records a registry proposal or an exclusion (no authority); `cw-admin adopt` is the guided
  one-step "register it and install it" (needs authority).
- `branches`, `merge` and `tidy` become `cw-admin proposals` (list, diff, merge, tidy) for registry proposals. The code-branch merge
  tools stay a maintainer's tool, not part of the end-user product.

## Build status

| Command | State |
|---|---|
| `cw-run submit / result / jobs / classes / setup / prune` | built and tested |
| `cw-run offload log hook / top / show / clear` (was `cw-log`) | built and tested |
| `cw-run offload fit / history / profile / exclusions / propose` | built and tested |
| `<name> ...` (the worker's engine) | built and tested |
| `cw-admin` Worker / Jobs / Security (status, check, review, approve, cancel, kill, quarantine, unlock, output) | built and tested |
| `cw-admin` Registry (adopt, retire, proposals) and Release (diff, apply with health check and rollback) | built and tested |
| root-run `<name>-update` entry point that `cw-admin apply` calls over `sudo` | to build (with the wizard's server half) |
| `cw-setup` (wizard) | to build |
