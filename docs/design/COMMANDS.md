# Command set review (DRAFT, for discussion)

Status: the decisions below are accepted and `ct-run`, the worker engine and `ct-admin` are built; `ct-setup` is next.

## Today (the original install)

```
ct-run          requester: queue jobs, read results, plan what to offload
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

ct-admin        administrator: act on the worker over SSH, and manage the private repo
 ├─ check                          is SSH login and `ct` working
 ├─ login / logout                 hold the sudo password for 15 minutes (does not work on sudo-rs)
 ├─ branches / merge / tidy        review and merge branches of the queue repo
 ├─ diff / apply                   preview and install a version on the worker
 ├─ adopt <profile-job>            register a profiled command as a class and install it
 ├─ retire <class>                 remove a class
 ├─ review / approve               see and approve waiting jobs
 ├─ status                         worker health, activity, safety checks
 ├─ quarantine / unlock / output   withheld results, clearing a lockdown, a failed command's full output
 └─ kill / cancel                  stop a running / not-yet-started job

ct-log          recorder of the commands Claude Code runs, ranked for offloading
 ├─ hook pre|post                  (called by Claude Code)
 ├─ top                            what is worth offloading
 └─ show / clear                   the log's location / forget it

ct-help         one page listing every tool
ct-status       alias for `ct-admin status`

On the worker (each is one engine, `ct`, under a different name)
 ct-status  ct-approve  ct-review  ct-show  ct-kill  ct-cancel
 ct-quarantine  ct-unlock  ct-output  ct-prune-results  ct-test-alert
System helpers: ct-update  canary-watch  fan-profile  sys-status  net-check  ct-sandbox-test  harden-network.sh
```

## What stands out

1. **The same operation lives in up to three places.** approve, review, kill, cancel, quarantine, unlock, output and status exist as a
   `ct-admin` command (over SSH) and again as a server command. The packaged version already has one engine (`<name> <command>`);
   the client's job is only to reach it.
2. **`ct-admin` does four different jobs:** operating the worker (status, approve, review, kill, cancel, quarantine, unlock, output),
   managing a repository (branches, merge, tidy, diff, apply), managing the registry (adopt, retire), and the connection (check, login, logout).
3. **`ct-run` mixes two audiences:** a requester (submit, result, status, classes) and someone planning capacity (fit, fits, profile, adopt, exclusions).
4. **`adopt` exists twice** (`ct-run adopt` records an exclusion, `ct-admin adopt` registers a class) and **`status` means two things**
   (`ct-run status` = my jobs, `ct-admin status` = the worker's health, plus the `ct-status` alias).
5. **`ct-log` is about planning what to offload**, so it belongs next to fit / profile / adopt / exclusions, not next to administration.
6. **`login` / `logout` are obsolete** with the new sudo design (read-only commands need no password; the rest ask).
7. **`branches` / `merge` / `tidy` / `diff` / `apply` manage the code and registry in the private repo.** In the packaged design code comes from
   the public project (a signed release) and the private repo holds only jobs and registry/policy, so most of these shrink to registry proposals plus `update`.

## Decisions so far

- `ct-log` becomes part of a planning group, `ct-run offload`, beside fit, profile, adopt and exclusions (it records the user's own commands).
- `ct-admin` stays flat (`ct-admin unlock`, `ct-admin approve`); its help screen groups commands under Worker, Jobs, Security, Registry and Release.
- `ct-admin login` and `logout` are removed: read-only commands need no password under the sudoers rule, everything else asks.
- `ct-unlock`, `ct-approve` and the other per-command server binaries do not exist in the packaged version: the worker has one engine, `<name> <command>`.

## Proposed tree

```
ct-setup                                 NEW  the wizard: client | server | audit
ct-help                                  every command, grouped

ct-run                                   a requester
 ├─ submit <class>  result <job>  jobs  classes
 ├─ setup  prune                         repair / housekeeping
 └─ offload                              what is worth running on the worker
     ├─ log  hook|top|show|clear         (was ct-log)
     ├─ fit -- <cmd>   history           (was fit, fits)
     ├─ profile -- <cmd>
     ├─ exclusions
     └─ propose <profile-job>            (was ct-run adopt: a registry proposal, or an exclusion)

ct-admin                                 the administrator, over SSH (flat; the help groups them)
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

- `ct-run status` is now `ct-run jobs` ("my jobs"). `status` means the worker's health only, and the `ct-status` alias is dropped.
- `adopt` is split. `ct-run offload propose` records a registry proposal or an exclusion (no authority); `ct-admin adopt` is the guided
  one-step "register it and install it" (needs authority).
- `branches`, `merge` and `tidy` become `ct-admin proposals` (list, diff, merge, tidy) for registry proposals. The code-branch merge
  tools stay a maintainer's tool, not part of the end-user product.

## Build status

| Command | State |
|---|---|
| `ct-run submit / result / jobs / classes / setup / prune` | built and tested |
| `ct-run offload log hook / top / show / clear` (was `ct-log`) | built and tested |
| `ct-run offload fit / history / profile / exclusions / propose` | built and tested |
| `<name> ...` (the worker's engine) | built and tested |
| `ct-admin` Worker / Jobs / Security (status, check, review, approve, cancel, kill, quarantine, unlock, output) | built and tested |
| `ct-admin` Registry (adopt, retire, proposals) and Release (diff, apply with health check and rollback) | built and tested |
| root-run `ct-update` (installs a release, then the validated registry; `ct-admin apply` calls it over `sudo`) | built and tested |
| `ct-setup install` (one command from your computer: asks once, sets up both sides, adds the deploy keys with `gh`, sends a first job) | built and tested, including end to end |
| `ct-setup demo` (the question and protection screens on sample content) | built and tested |
| `ct-setup uninstall` (previews first, removes exactly what the installer put there, leaves the protections) | built and tested, including end to end |
| `ct-setup client / server / audit` (questions, config writer, hardening modules, deploy keys, core registry pack) | built and tested; the clean-VM end-to-end run remains |

## End-to-end test

`scripts/e2e-orbstack` runs the routes of `tests/e2e/routes/` in disposable Ubuntu machine (OrbStack). It installs from a checkout, runs `ct-setup client` and then
`ct-setup server` twice (the first run must stop and ask for the deploy keys, the second must carry on), sends `sys-status` through the whole loop,
checks that `sudo` cannot be profiled and that a denied program cannot be installed, and re-installs the release. GitHub is simulated by an SSH user on the
same machine (`github.com` resolves to it), so every clone, fetch and push uses real keys. The decoy-credential tripwire needs kernel audit, which
container-style machines do not have; there the scenario says SKIP and the hardening module reports why. Run it on a full VM to cover that case.
