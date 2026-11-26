# Workspace jobs: run any command on a folder, somewhere else

Status: design, 2026-11-26 (slice 0 of `theme/offload-value`). Decisions marked **(owner)** were given by the project owner on 2026-11-26. Nothing here is built yet; section "What the code does today" is checked against the code and is the only description of current behaviour.
Companion: [why offload: a ranked ladder of reasons](OFFLOAD-REASONS.md). The plain-language parts are written to be reusable for a public website and name no AI model or company.

## In plain words

Most slow work is "run this command on this folder": a test suite, a linter, a build, a scanner, a document generator, a data job. A **workspace job** sends a folder (a git repository or not) to a worker, runs one command there, and brings back a short answer. The long output stays on the worker, with a one-line way to fetch it.

To make that cheap the second time, the worker remembers what it was sent (only what changed travels again) and the software the command needs (built once, reused, removed after 30 unused days, rebuilt on the next use, and the result says so).

On top of this, after setup the tool reads the person's own command history, says how much time offloading would have saved, recommends the commands worth sending, and asks yes or no for each.

## What the code does today (verified)

Each line was read in the code on `theme/offload-value` (identical to `main` at 68ad59f).

1. **Attach is text-only.** `--dir`, `--include` and `--file` collect files with `path.read_text()`; a file that is not valid UTF-8 is left out and the job does not start unless the person accepts the gap (`files.not_text`, `src/ct/client/submit.py:257-259`; the left-out rules in `collect_dir`, `submit.py:223`). The job carries file contents as JSON text (plain, or one compressed text value on Direct), so a binary file cannot travel. Secret-looking names and text are held back (`SECRET_TEXT`, `submit.py:59-70,249-273`).
2. **The whole selected set is rebuilt and resent on every run.** Nothing is hashed per file, cached on the worker or compared with an earlier run: `collect_dir` rebuilds the dict of every file each time and the job carries all of it. The only size relief is on Direct: a set over `BIG_SET_BYTES` (1 GB) is compressed as one value (`submit.py:276-305`, `jobs.unpack_files`, `src/ct/jobs.py:179`). The `sha256` calls in `worker.py` (lines 273, 430, 807) hash registries, not job files.
3. **Selection.** In a git folder: tracked files plus untracked files that are not ignored, never `.git` (`submit.py:146-188`). Outside git: a walk that skips hidden files and folders, build output and dependency folders unless an `--include` names them (`walked`, `submit.py:203`); GitHub's language ignore templates are vendored for this (`submit.py:85-129`). `--include` globs narrow either case.
4. **The saved `submit:` recipe.** `ct-run offload profile --dir/--include/--file` keeps how the files were packaged in `profile-packs.json` (`make_recipe`, `src/ct/client/offload.py:356`). `ct-admin adopt` writes it into the class as `submit:` (`with_recipe`, `offload.py:378`; only for a class with `accepts_files`). `ct-run submit CLASS` uses it only when no `--dir/--file/--include` is typed (`use_recipe`, `offload.py:384`); `--no-pack` sends none; an absolute folder or `..` is refused (`pack.recipe.unsafe`). The recipe holds **folder, include patterns and files only**: not the command (the class holds that), not the environment, not the working directory, not the outputs. The worker never acts on it.
5. **Size limits today.** Per registry `limits.files`: 200 files, 500 KB per file, 2000 KB in total, 10 batches (`src/ct/registry.py:31`); a set over the file-count limit is split into independent jobs only when the class is `batchable` (`submit.py:344-362`). A worker installed before these defaults is read as 50 files, 100 KB, 300 KB, 6 batches (`OLD_FILE_LIMITS`, `submit.py:28`). Direct has no size limit and compresses over 1 GB; the worker unpacks under a cap and a free-disk check (`jobs.unpack_files`). A job is limited by `timeout_s` (never more than an hour), `max_rss_mb`, `max_disk_mb` and a cap on one written file (`docs/components/jobs.md`, "Execution limits"). A profile run's timeout is estimate x speed factor x 3.
6. **Environments.** A class or profile run may list pinned `apt:`, `npm:`, `pip:` packages, at most 8 (`MAX_PACKAGES`, `src/ct/pkgspec.py:17`). A profile run fetches them into a **per-job throwaway folder** `<env_root>/<job id>` (`Packages.prepare`, `src/ct/packages.py:222`). A registry class installs them for real, as root, only after the owner approves that registry (`install_registry_packages`, `packages.py:298`). **The throwaway folders are pruned by folder mtime after the constant `PKG_KEEP_DAYS = 7`** (`packages.py:21,285-295`; called from `worker.py:785`). **Registry packages (tools folder, apt) are never removed**; the only other deletion is that installing a registry removes every throwaway folder (`packages.py:320-322`). No tool detects what a folder needs (no marker-file reading anywhere in `src/ct`).
7. **Outputs.** Only the fields a class declares (`output_keys`), capped; a failed command publishes only its error lines. The full output of a failed command stays on the worker (mode 0600, 30 days) and is read with `ct-admin output <job>` (`docs/components/jobs.md`, "What is published"). `returns: patch` sends the difference from the files it was sent, as data the person applies (`src/ct/patch.py`: text files only, `MAX_FILE_BYTES` 1 MB, `MAX_FILES` 500).
8. **The command log** records the commands Claude Code runs, with seconds. `classify` returns "compound command (offload a single command)" for any line with more than one segment, so `cd x && cmd | tail` is never a candidate (`src/ct/client/cmdlog.py:91-92`). `rank` and `measure` read the log with the settings `offload.repeat_runs` (3) and `offload.repeat_days` (14).
9. **Placement.** `sched.choose` leaves a worker out for a list of reasons and then ranks the rest (preferred, freshness, held, fits now, headroom, pickup time, load, name; `src/ct/sched.py:123-208`). **A worker is eligible for a class when the class is in that worker's own registry** (`cls not in w.offered` is the "class missing" reason, `sched.py:142`). So the orchestrator already picks per job among every worker that offers the class, and a "registered and passed" class needs nothing new there.
10. **Profile and adopt are per worker.** `cmd_profile` sends one profile job; `ct-admin adopt JOB --worker NAME` adds the class to that worker's registry (`src/ct/client/cli.py:96`, `src/ct/admin/cli.py:1141`). Nothing today profiles one command on every worker or adopts it on each. **This is the gap behind "profile on every worker" (slice 7).**
11. **The usage hint is not on this theme yet.** `offload.recommend_hint` (default on), `offload.recommend_hint_hours` (24), `show_hint` and `HintState` exist on `theme/orchestrator` (PR 77 merged there) and not on `main` or this theme (`git merge-base` check: `origin/theme/orchestrator` is not an ancestor). Slice 8 depends on that code arriving (see slice list).

## The workspace recipe

A recipe is everything needed to repeat "this command on this folder" without retyping. It is saved with the class (extending today's `submit:` section) and is command-agnostic. Seven parts:

### 1. Selection

What goes: the folder given (default: where the command was typed).
- **Git never required.** If a `.gitignore` is present (git folder or not) its rules decide; in a git folder tracked + untracked-not-ignored files, as today. With no `.gitignore`: the vendored default rules (hidden, build output, dependency folders) plus the person's `include` and `exclude` globs. `include` narrows to what the command needs; `exclude` removes more.
- Secrets: unchanged. Secret-looking names and text are held back; on the GitHub transport a secret-looking text is never sent, elsewhere it is sent with a warning (`collect_dir`). Nothing left out is dropped silently; every file left out is listed.
- **Change from today:** binary files may be selected (see snapshot), with their own size limits.

### 2. Content-hashed snapshot

The selection becomes a **manifest**: for every file its relative path, mode, size and SHA-256 of its bytes; the **snapshot id** is the SHA-256 of the sorted manifest. Files are read as bytes, so text and binary are treated alike. The existing guards stay: a name check, the secret scan on text, a per-file and total cap, and `..`/absolute paths refused.

### 3. Delta sync against a worker-side cache keyed by hash

The worker keeps a content-addressed store of file bytes by SHA-256 (the cache) and a snapshot index. A run sends the manifest first; the worker answers with the hashes it lacks; only those files travel (compressed). The worker then materialises the folder for the job from the store (hard links or copies; the job's own folder stays the only writable place, so a job can never change a cached file). The store has a size cap and evicts least recently used first; a missing file is just sent again. A second run of an unchanged folder transfers a manifest only. Transport detail (Direct versus GitHub, which cannot do a request-and-answer round trip cheaply) is a slice-2 design question: on GitHub the first job may need to carry the manifest plus the files the client believes are absent, recorded from the last result, with the worker asking again when a file is missing.

### 4. Environment

Marker files **propose**, the person **confirms**, packages stay **pinned** (existing rule). Detection is read-only and lives on the person's computer:

| Marker | Proposes |
| ------ | -------- |
| `pyproject.toml`, `requirements*.txt`, `setup.cfg` | `apt:python3`, `apt:python3-pip`, and the listed `pip:` packages, pinned |
| `package.json`, `package-lock.json` | `apt:nodejs`, `apt:npm`, the dependencies pinned from the lock file |
| `Cargo.toml` | the Rust toolchain package for the worker's OS |
| `go.mod` | the Go toolchain package |
| `Makefile` | `apt:make` (and whatever its first target visibly calls, shown, never guessed silently) |

A proposal never installs anything. It is shown as the existing approval screen (`Packages.plan_text`) with sizes and the exact commands, and installs only after the person says yes; more than 8 packages or an unpinnable one is reported, not truncated. The environment is then built and kept as described below.

### 5. The exact command and working directory

The recipe stores the command as a list of words (never a shell string) and the working directory relative to the snapshot root (default the root; `..` and absolute paths refused, as for the folder today). The class's existing limits (time, memory, disk, network off unless declared) apply unchanged.

### 6. Outputs

Default: the **exit code and a short summary** (the class's `output_keys`, capped). Optional per recipe: named **artifact** files (size-capped, scanned by the guard like any output) or a **patch** (`returns: patch`, which today is text-only). The **full log stays on the worker** with a one-line note in the result saying how to fetch it (today: `ct-admin output <job>`; extending this from failed jobs to every job of a workspace class, and the retention period, is slice 4's decision, in line with the token-saving requirements in OFFLOAD-REASONS rung 2).

### 7. Safety

Unchanged: profile runs always need approval; adopt asks once; packs only get stricter; secrets never sent; the job runs in the jail with no network unless declared; output passes the guard. A workspace recipe adds no new way to run anything the registry did not approve.

## Environment lifecycle (owner, 2026-11-26)

**One shared environment per package-set hash.** The hash is taken over the sorted, pinned package list plus the worker's OS release. Two classes that need the same packages share one environment; today each profile run builds its own throwaway folder (fact 6).

- **Last-used stamp.** Every use touches a stamp file in the environment (not the folder mtime, which changes only when files are added).
- **Setting `packages.keep_days`, default 30**, validated in `ct.toml` like the other settings and read at prune time. It replaces the hard-coded 7-day `PKG_KEEP_DAYS`, whose mtime rule also pruned folders that were still in use.
- **Pruned after `keep_days` unused.** The prune pass runs where `prune_old_envs` runs today (the worker's loop, `worker.py:785`).
- **Rebuilt on the next use**, and the **result says so** in one line: "the packages for this class were rebuilt (unused for N days); this run took X s longer".
- **An environment in use is skipped.** A lock (a file held for the life of each job that uses the environment) is checked before removal; a held lock means skip and try next pass.
- **apt packages** are removable only from a **manifest of what tenta itself installed** (written at install time, one entry per package with who asked for it). Removal is root work done by the update tool; a package another class lists, or that was already on the system before tenta (not in the manifest), is **never removed**. Until the manifest exists, auto-clean covers only npm and pip environments and per-job folders, and apt leftovers are **reported** (name and size), not removed.
- **Disk-pressure backstop.** When free space falls under the existing reserve, evict least recently used environments first (never one in use), then the file cache, and say so.

### End-user failures (every one: how a person reaches it, the message, the test to add later)

| Failure | How a person reaches it | Message (plain words, with the real error text) | Test to add |
| ------- | ----------------------- | ----------------------------------------------- | ----------- |
| **Offline during a rebuild** | The environment was pruned, the worker has no route to the package servers when the next job needs it | "The packages for `CLASS` were removed after N unused days and cannot be rebuilt now: `<tool's own error>`. Check that the worker can reach the package servers, then send the job again." The job is **not run** with missing software, and is not marked `does-not-fit` | prepare raises `PrepError` on a fake runner that fails; assert job ends `needs-environment`, no exclusion recorded |
| **Rebuild exceeds the job time limit** | A long first-install after a prune on a slow link | The rebuild has its own limit (`Packages.total_timeout`, 900 s) and is **not charged to the job's `timeout_s`**; if it runs out: "Rebuilding the packages took longer than 15 min; the job did not start. Send it again to continue" and the partly built environment is kept for the retry | fake clock: rebuild longer than the job limit does not mark the job timed out |
| **Cleanup races a running job** | The prune pass starts while a job (or two jobs sharing the environment) is using it | The environment is skipped; the prune report says "skipped 1 environment in use". Never a half-deleted environment under a running job | prune with a held lock removes nothing; a concurrent prune and run leave the run intact |
| **Disk full during a rebuild** | The backstop cannot free enough | "Not enough free space to rebuild the packages (need X MB, have Y MB); freed Z MB of the oldest unused environments and it is still too little" | fake free-space reading |
| **Old install without the new setting** | A worker upgraded from a release that has the 7-day constant | Treated as the default 30 days; `ct-admin workers` shows the value and its source | config default test |
| **Apt leftover reported, not removable** | No manifest yet, or a package another class needs | "N apt packages from classes that no longer run remain (size); tenta did not install them or still needs them, so it leaves them" | prune with no manifest lists, never removes |
| **A cached file is missing or changed** | Cache evicted or corrupted between the manifest and the run | The worker asks again for the hashes it lacks; if it still cannot verify a hash, the job is refused with the file name | store with a missing and a corrupted entry |

## The offload review flow (slices 6-8)

Goal: show value the first minute after setup, from the person's own history.

1. **After install: the saved-hours summary.** Read the command log and print "N runs, H hours of commands that could have run elsewhere", with the top commands. It reads only local data and sends nothing.
2. **Ranked recommendations** from the same log, after slice 1 so compound commands count: each shape with count, median seconds, the rung it meets (OFFLOAD-REASONS) and the saved hours.
3. **On demand:** `ct-run offload review` runs the same flow at any time; the same flow is offered at the end of setup.
4. **One y/n per command**, highest saving first: "Register `<command>` as a worker class? (y/n)". **That answer is the per-command approval; there is no "yes to all".** A no is remembered so it is not asked again until the shape changes.
5. **On yes:** build the recipe (folder, command, environment proposal), then **profile it on every worker**; each worker where it **succeeds** is a **candidate**. The review does not pick a worker. Adopt asks once as today (`offload.auto_adopt`), per worker, with the person's own approval key. A failing worker is reported with the tool's real error and leaves a gap the person can see (`ct-run classes`).
6. **The orchestrator, not the review, picks per job** among the workers that offer the class, by availability and the other criteria it already uses (fact 9). The review flow adds no ranking of its own.
7. **The usage-history hint (slice 8):** after a `ct-run` command, at most once per `offload.review_hint_hours`, one line such as "You ran `<command>` 14 times last week (3.1 h). `ct-run offload review` can set it up." Reuses the existing default-on hint mechanism (`show_hint`, `HintState`, fact 11): a terminal only, never in a pipe, script or `--json`, never fails the command, and an off switch `offload.review_hint = false` (and the existing `CT_HINTS=never`).

**Missing pieces found against the code:** (a) profile and adopt on every worker (fact 10) is new work: it is slice 7. (b) Placement needs nothing new for the "candidate" idea (fact 9). (c) The hint arrives with `theme/orchestrator` (fact 11): slice 8 starts only after that theme is merged into `main` and `theme/offload-value` has merged `main`.

**Safety, unchanged:** each y/n is the per-command approval; a profile run always needs approval; adopt asks once; packs only get stricter; secrets are never sent; there is no blanket yes.

## Slices

All branch from `theme/offload-value`, merge into it one at a time, then the theme PR goes to `main`. Names after slice 1 are proposals.

| # | Branch | What | Depends on |
| - | ------ | ---- | ---------- |
| 0 | `docs/offload-workspace-design` | this design and the OFFLOAD-REASONS update | none |
| 1 | `fix/offload-sees-compound-commands` | `cd x && cmd \| tail` counts as the command that took the time; `rank` and `measure` see it | 0 |
| 2 | `feat/workspace-snapshot-sync` | selection with `.gitignore`/default rules and `exclude`, byte-exact files, content-hashed manifest, delta sync, worker cache with LRU cap | 0 |
| 3 | `feat/shared-environments` | one environment per package-set hash, last-used stamp, `packages.keep_days`, in-use lock, disk backstop, apt manifest, rebuild note, the failure table's tests | 0 |
| 4 | `feat/workspace-recipe` | the saved recipe (command, working directory, environment, outputs), marker-file proposals, full log kept with a fetch line | 2, 3 |
| 5 | `feat/busy-host-rung` | the "your computer is busy" rung (host load against core count) in the candidacy ladder | 0 and the owner's weight (not chosen here) |
| 6 | `feat/offload-review-summary` | post-install saved-hours summary, ranked recommendations, `ct-run offload review` listing and the setup offer | 1 |
| 7 | `feat/offload-review-profile-all` | y/n per command, profile on every worker, per-worker adopt, a remembered no | 4, 6 |
| 8 | `feat/offload-review-hint` | rate-limited usage-history hint and `offload.review_hint` | 6; `theme/orchestrator` merged into `main` and `main` merged into the theme |

Slices 2 and 3 do not touch the same files (the file packaging and the worker's cache, against `packages.py` and its prune call) and can run in parallel; 6 can start once 1 is merged.

## Not decided here

- The weight of the busy-host rung (the owner sets it).
- Whether the GitHub transport can do the manifest exchange or must carry a best-guess delta (slice 2).
- The retention period of full logs for successful jobs (slice 4).
- Windows workers: Special Use only (`WINDOWS-SPECIAL-USE.md`); the environment and cache rules above are written for Linux workers.
