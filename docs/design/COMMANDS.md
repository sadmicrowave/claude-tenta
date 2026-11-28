# Command set review (DRAFT, for discussion)

Status: the decisions below are accepted and `arm-run`, the worker engine and `arm-admin` are built; `arm-setup` is next.

## Today (the original install)

```
arm-run          requester: queue jobs, read results, plan what to offload
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

arm-admin        administrator: act on the worker over SSH, and manage the private repo
 ├─ check                          is SSH login and `armada` working
 ├─ login / logout                 hold the sudo password for 15 minutes (does not work on sudo-rs)
 ├─ branches / merge / tidy        review and merge branches of the queue repo
 ├─ diff / apply                   preview and install a version on the worker
 ├─ adopt <profile-job>            register a profiled command as a class and install it
 ├─ retire <class>                 remove a class
 ├─ review / approve               see and approve waiting jobs
 ├─ status                         worker health, activity, safety checks
 ├─ quarantine / unlock / output   withheld results, clearing a lockdown, a failed command's full output
 └─ kill / cancel                  stop a running / not-yet-started job

arm-log          recorder of the commands a coding assistant runs, ranked for offloading
 ├─ hook pre|post                  (called by the assistant's hooks)
 ├─ top                            what is worth offloading
 └─ show / clear                   the log's location / forget it

arm-help         one page listing every tool
arm-status       alias for `arm-admin status`

On the worker (each is one engine, `armada`, under a different name)
 arm-status  arm-approve  arm-review  arm-show  arm-kill  arm-cancel
 arm-quarantine  arm-unlock  arm-output  arm-prune-results  arm-test-alert
System helpers: arm-update  canary-watch  fan-profile  sys-status  net-check  arm-sandbox-test  harden-network.sh
```

## What stands out

1. **The same operation lives in up to three places.** approve, review, kill, cancel, quarantine, unlock, output and status exist as a
   `arm-admin` command (over SSH) and again as a server command. The packaged version already has one engine (`<name> <command>`);
   the client's job is only to reach it.
2. **`arm-admin` does four different jobs:** operating the worker (status, approve, review, kill, cancel, quarantine, unlock, output),
   managing a repository (branches, merge, tidy, diff, apply), managing the registry (adopt, retire), and the connection (check, login, logout).
3. **`arm-run` mixes two audiences:** a requester (submit, result, status, classes) and someone planning capacity (fit, fits, profile, adopt, exclusions).
4. **`adopt` exists twice** (`arm-run adopt` records an exclusion, `arm-admin adopt` registers a class) and **`status` means two things**
   (`arm-run status` = my jobs, `arm-admin status` = the worker's health, plus the `arm-status` alias).
5. **`arm-log` is about planning what to offload**, so it belongs next to fit / profile / adopt / exclusions, not next to administration.
6. **`login` / `logout` are obsolete** with the new sudo design (read-only commands need no password; the rest ask).
7. **`branches` / `merge` / `tidy` / `diff` / `apply` manage the code and registry in the private repo.** In the packaged design code comes from
   the public project (a signed release) and the private repo holds only jobs and registry/policy, so most of these shrink to registry proposals plus `update`.

## Decisions so far

- `arm-log` becomes part of a planning group, `arm-run offload`, beside fit, profile, adopt and exclusions (it records the user's own commands).
- `arm-admin` stays flat (`arm-admin unlock`, `arm-admin approve`); its help screen groups commands under Worker, Jobs, Security, Registry and Release.
- `arm-admin login` and `logout` are removed: read-only commands need no password under the sudoers rule, everything else asks.
- `arm-unlock`, `arm-approve` and the other per-command server binaries do not exist in the packaged version: the worker has one engine, `<name> <command>`.

## Proposed tree

```
arm-setup                                 NEW  the wizard: client | server | audit
arm-help                                  every command, grouped

arm-run                                   a requester
 ├─ submit <class>  result <job>  jobs  classes
 ├─ setup  prune                         repair / housekeeping
 └─ offload                              what is worth running on the worker
     ├─ log  hook|top|show|clear         (was arm-log)
     ├─ fit -- <cmd>   history           (was fit, fits)
     ├─ profile -- <cmd>
     ├─ exclusions
     └─ propose <profile-job>            (was arm-run adopt: a registry proposal, or an exclusion)

arm-admin                                 the administrator, over SSH (flat; the help groups them)
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

- `arm-run status` is now `arm-run jobs` ("my jobs"). `status` means the worker's health only, and the `arm-status` alias is dropped.
- `adopt` is split. `arm-run offload propose` records a registry proposal or an exclusion (no authority); `arm-admin adopt` is the guided
  one-step "register it and install it" (needs authority).
- `branches`, `merge` and `tidy` become `arm-admin proposals` (list, diff, merge, tidy) for registry proposals. The code-branch merge
  tools stay a maintainer's tool, not part of the end-user product.

## Build status

| Command | State |
|---|---|
| `arm-run submit / result / jobs / classes / setup / prune` | built and tested |
| `arm-run offload log hook / top / show / clear` (was `arm-log`) | built and tested |
| `arm-run offload fit / history / profile / exclusions / propose` | built and tested |
| `<name> ...` (the worker's engine) | built and tested |
| `arm-admin` Worker / Jobs / Security (status, check, review, approve, cancel, kill, quarantine, unlock, output) | built and tested |
| `arm-admin` Registry (adopt, retire, proposals) and Release (diff, apply with health check and rollback) | built and tested |
| root-run `arm-update` (installs a release, then the validated registry; `arm-admin apply` calls it over `sudo`) | built and tested |
| `arm-setup install` (one command from your computer: asks once, sets up both sides, adds the deploy keys with `gh`, sends a first job) | built and tested, including end to end |
| `arm-setup demo` (the question and protection screens on sample content) | built and tested |
| `arm-setup uninstall` (previews first, removes exactly what the installer put there, leaves the protections) | built and tested, including end to end |
| `arm-setup client / server / audit` (questions, config writer, hardening modules, deploy keys, core registry pack) | built and tested; the clean-VM end-to-end run remains |

## End-to-end test

`scripts/e2e-orbstack` runs the routes of `tests/e2e/routes/` in disposable Ubuntu machine (OrbStack). It installs from a checkout, runs `arm-setup client` and then
`arm-setup server` twice (the first run must stop and ask for the deploy keys, the second must carry on), sends `sys-status` through the whole loop,
checks that `sudo` cannot be profiled and that a denied program cannot be installed, and re-installs the release. GitHub is simulated by an SSH user on the
same machine (`github.com` resolves to it), so every clone, fetch and push uses real keys. The decoy-credential tripwire needs kernel audit, which
container-style machines do not have; there the scenario says SKIP and the hardening module reports why. Run it on a full VM to cover that case.
