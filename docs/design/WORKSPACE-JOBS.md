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
11. **The usage hint** (`offload.recommend_hint`, `show_hint`, `HintState`) came to this theme when `main` was merged in; the history hint (slice 8) reuses it. See "Built: the usage-history hint".

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

The worker keeps a content-addressed store of file bytes by SHA-256 (the cache) and a snapshot index. A run sends the manifest first; the worker answers with the hashes it lacks; only those files travel (compressed). The worker then materialises the folder for the job from the store (hard links or copies; the job's own folder stays the only writable place, so a job can never change a cached file). The store has a size cap and evicts least recently used first; a missing file is just sent again. A second run of an unchanged folder transfers a manifest only. Transport detail is settled in "Built: delta sync" (no round trip on either transport): on GitHub the first job may need to carry the manifest plus the files the client believes are absent, recorded from the last result, with the worker asking again when a file is missing.

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

## Built: the recipe (slice `feat/workspace-recipe`, 2026-11-26)

Built on today's transport (text files, whole set per run), before the byte-exact manifest and delta sync, which stay with slice 2; the recipe says what to send and run and does not change when they arrive. Decisions (owner approved the design 2026-11-26):

- `submit:` gains `exclude`; `dir` is the working directory, because the worker runs the command at the root of what was sent and never acts on `submit:`. A separate `cwd` key would need a worker change (the jail's `--chdir`) and is in the backlog.
- A non-git folder's `.gitignore` is added to the default rules, never replaces them (the rules only get stricter).
- Marker proposals live in `src/ct/client/markers.py`; they propose, pins only, and `--use-detected-packages` is the person's yes.
- Outputs: the default summary is unchanged, the failed-output fetch line names `ct-admin output JOB`; artifacts and the output of successful jobs are backlog.

## Built: delta sync (slice `feat/workspace-delta-sync`, 2026-11-26)

Owner approved the design and the four decisions below on 2026-11-26. Same code and format on Direct and on GitHub: both are git repositories with no request-and-answer, so nothing in it needs a round trip.

**Decisions (owner).**
1. **Text only, documented.** A file must be valid UTF-8; a binary file is still reported as `files.not_text` and stops the job unless `--send-incomplete`. Reasons: the job file is JSON text (a binary blob would be base64, a third larger) and `returns: patch` is text-only. The hash-addressed blobs make binary a small later step (backlog).
2. **A miss is a top-up, not a new job.** The worker says which files it lacks (`needs-files`); this computer sends exactly those as `blobs/<job>.json` on the jobs branch; no new approval, because the approved job file pins every file by hash.
3. **Two sets of limits.** The per-file limit (500 KB on GitHub) and the count and size of what one job carries stay. A whole folder has its own caps, `limits.files.snapshot_count` (5000) and `snapshot_total_kb` (50000), published in `registry.json`. A folder over them goes the old way (whole set, Direct compression over 1 GB).
4. **Cache cap `cache.max_mb`**: unset means the smaller of 2048 and a quarter of free disk; shared by all classes on the worker; least recently used first.

**The job.** `manifest` (path to `{sha256, size, mode}`, mode `644` or `755`), `snapshot` (SHA-256 of the sorted manifest), `blobs` (`{sha256: text}` for the files the worker is not believed to hold). It replaces `files`; a job with both is refused. The hash is over the file's exact UTF-8 bytes, read and written without newline translation (a CRLF file arrives as CRLF; before this slice `read_text` turned it into LF).

**The worker's cache** (`<state>/filecache/`, 0700, the worker's own account; code `src/ct/filecache.py`). A file is stored under the SHA-256 **the worker computes**; a blob whose key is not its hash refuses the job (`files.cache.hash_mismatch`), so one sender cannot plant content under another file's hash. Every read is checked again; a changed entry is dropped. Intake stores the blobs, checks the manifest (names by the same rule as any attached file, shape, size, snapshot id, caps) and asks for what is missing. Exec copies the files from the cache into the job's own folder (copies, never hard links: a job could change a linked file) and applies the mode. The cache has a random `epoch`, made with the folder and published in `registry.json` as `cache: {version, epoch, max_mb}`.

**What the client sends.** A ledger per worker (hashes only, in the client's home next to its clone) is its guess of what the worker holds. A different published `epoch` empties it (the worker was reset); `--resend-all` ignores it. After a push, every file of the job is recorded as held. A wrong guess is caught by the worker.

**A miss.** `needs-files` is a pending status (the wait loop, `ct-run result` and `ct-run jobs` show it). The client reads the missing files from where they were, checks each still matches its hash, and pushes them; the worker verifies each by hash, stores it, and the job goes on. A file that changed on this computer since is `files.cache.source_changed` (send again with `--resend-all`). At exec nothing can be asked: a file that is missing or damaged then is `files.cache.damaged`, never a run on wrong files. Files a waiting job needs are never evicted.

**Eviction.** At most every 10 minutes, in the worker's cleanup pass: over the cap, least recently used first, never a pinned file; below the disk reserve, everything not pinned goes, and the log says so.

**Security.** A job cannot name a hash: only the client-made, approval-pinned job file does, and a 256-bit hash is held only by whoever holds the content, within one owner's own worker. A secret-looking file is held back before hashing, so it is never in a manifest. A file sent with a warning on Direct sits in the 0700 cache until evicted (as it already did in the inbox copy of the job). Under the `unshare` jail (which cannot hide folders) a job can see the state folder, as it could see the inbox before; the bubblewrap jail shows only the job's own folder.

**Updating workers.** A worker must run this release to take a manifest. Until then it publishes no `cache` and this computer keeps sending the whole set; nothing changes for it. Update with the normal path (`ct-admin apply`).

**Out of this slice.** Profile runs still send the whole set (they are a one-off; backlog). Windows workers.

### End-user failures (every one: how a person reaches it, the message, the test)

| Failure | How a person reaches it | What they see | Test |
| ------- | ----------------------- | ------------- | ---- |
| The worker was reset or cleared old files | Reinstall, wiped state, the cache over its cap | `needs-files`; the client sends exactly those; the job runs | `test_a_wrong_guess_is_corrected_by_a_top_up_and_the_job_still_runs` |
| The reset was seen in advance | The worker republished its registry with a new epoch | Everything is sent again at once, nothing waits | `test_a_reset_worker_is_noticed_from_its_epoch_and_everything_is_sent_again` |
| A file changed on this computer between send and top-up | Editing while a job waits | `files.cache.source_changed`: send again with `--resend-all` | `test_a_changed_source_is_refused_not_sent` |
| A cached file damaged on the worker | Disk damage, another process | `files.cache.damaged`; the job did not run | `test_a_cached_file_damaged_before_it_runs_is_never_run_and_says_so` |
| A file sent under the wrong hash | A damaged copy, a hostile sender | `files.cache.hash_mismatch`; nothing stored | `test_a_blob_whose_key_is_not_its_hash_rejects_the_whole_job` |
| An older worker | Not updated yet | The whole set is sent as before | `test_a_worker_that_does_not_say_it_has_a_cache_gets_the_old_whole_set` |
| A binary file | Any folder with an image | `files.not_text`, as before | `test_a_binary_file_is_still_not_text_and_still_stops_the_job` |
| A folder over the snapshot caps | A very large tree | The old way, with its own limits and compression | `test_a_folder_over_the_snapshot_caps_goes_the_old_way` |

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

## Built: environment lifecycle (slice `feat/workspace-env-cleanup`, 2026-11-26)

Owner approved on 2026-11-26: **A** (a class's npm and pip packages run from a shared environment), **apt report-only** (manifest written; the root removal command is a follow-up), **the refusal catalog and the failure table** as the known-problems list (`publishdiag.KNOWN` holds git push errors only). Code: `src/ct/envs.py`, `Packages.acquire` / `prune_old_envs` in `src/ct/packages.py`, `Worker.prune_envs`.

- Layout `<state>/profile-env/shared/<key>/` with `.complete`, `.last_used`; `<key>.lock` (use lock: shared by running jobs and by a build, exclusive for the prune pass, never waited for) and `<key>.build` (serialises two builds). `<key>.pruned.json` is left by a removal so the next build can say it is a rebuild.
- `packages.keep_days` (default 30, 1 to 3650) replaces the 7-day constant. Removal is for strictly more than that many days unused. Per-job folders of profile runs from before this release are removed by age under the same setting.
- A class's npm/pip packages are no longer root-installed; only apt is. The registry install writes `<state>/apt-manifest.json` (tenta-installed packages only, with the classes that asked and the size). Dependencies apt pulled in are not listed (backlog).
- A profile run builds the same shared environment; a profile that fits promotes only apt paths, and npm and pip programs are found by name on the environment's path at run time.
- A failed or timed-out build is discarded (a half-built environment is never reused); the design table said it would be kept for the retry. Downloads are cheap to repeat and a half-built tree could be reused with a package missing.
- A job whose environment cannot be built ends `needs-environment` (not run; never recorded against the class).
- The worker publishes `observed.environments` (count, MB, keep_days, apt leftovers) and `ct-admin workers` shows an `Environments` row.

**Workers must update** (`ct-admin apply`). An older worker keeps its old behaviour: 7-day per-job folders and root-installed npm and pip.

## Built: the review summary (slice `feat/offload-setup-summary`, 2026-11-27)

Steps 1 to 3 of the flow below, as `ct-run offload review` and a card at the end of `ct-setup install` (terminal only; a failure there is a note, never a failed install). Owner decisions 2026-11-27: the hours are **"hours of commands a worker could take"** (recorded seconds of candidate commands, no claim of speed or "saved"); ranked by the highest-weighted rung met, then by time; **too short** means fewer than 3 days or 20 commands (settings `offload.review_min_days`, `review_min_commands`; also `review_days` 14 and `review_top` 5). `CommandLog.review` is the pure function slice 7 builds on: `recs` is its ranked list (shape, example, count, median, total, untimed, rung, headline, why). Not built here: the y/n loop and profiling (slice 7), the history hint (slice 8); the one-time import of shell history came later ("Built: the first-day data").

## Built: the y/n review (slice `feat/offload-review`, 2026-11-27)

Steps 4 to 6 of the flow below, in `ct.client.review_flow`. Owner decisions 2026-11-27: one y/n per command (`y`, `n` = not now, `never`, Enter = skip this time), no yes to all; `n` is quiet for `offload.review_ask_again_days` (30), `never` until `--forget`; the folder a command last ran in is confirmed (never the home folder or `/`); a worker that cannot be asked (offline, silent over `sched.STALE_S`, locked, or without `profile_time`/`cache` in its registry) is named once with its reason and never skipped silently; a class already on some workers is offered for the rest; a command excluded on a worker is not tried there again. **Eligibility check against the code:** `sched.choose` reads each worker's `registry.json` classes, so a class registered on a worker is already a candidate there; the orchestrator needed no change. Without a terminal the list and exact commands are printed and nothing is asked. The end of `ct-setup install` asks once whether to go through the list now. Not built: the history hint (slice 8), installing the Claude Code hooks (backlog), a time estimate beyond the median of the recorded runs.

## Built: the first-day data (slice `feat/setup-offload-history`, 2026-11-28)

Why: a new install has an empty command log, so the summary and the y/n review say "Nothing recorded yet" and the goal, value visible right after setup, fails. **Owner decision 2026-11-28: the first release is the CLI and assumes no AI model use, so the data comes from shell history only.** The coding assistant's hooks (merging pre/post Bash hooks into its settings file) and its session transcripts are a later feature, parked on the local branch `feat/assistant-hooks-and-transcripts` (built, tested, not merged). Code: `client/histimport.py`, `client/histflow.py`; the command is `ct-run offload import [--source S] [--clear]`, offered at the end of `ct-setup install` while the log is empty or short.

**Sources: what each can honestly give** (owner decisions 2026-11-28: zsh, bash and fish; the last 90 days; more shells expand the value).

| Source | Date | Duration | Imported? |
| ------ | ---- | -------- | --------- |
| zsh `EXTENDED_HISTORY` (`~/.zsh_history`, `$ZDOTDIR`) | yes | whole seconds; 0 means "under a second", which is not a measurement, so untimed | yes |
| bash with `HISTTIMEFORMAT` (`#epoch` lines) | yes | never | only the dated commands |
| fish (`fish_history`, `$XDG_DATA_HOME`) | yes, every entry | never | yes |
| plain zsh or bash history | no | no | no: without a date there is no honest "in the last N days"; the lines are counted and said |
| PowerShell, nushell, a custom `HISTFILE` | PowerShell: no | no | not yet (backlog) |

Import reads private data, so: nothing is saved or sent while counting (the screen says so); each shell is a separate yes (one whose commands are all in the log already is not asked about); only command lines are kept, with the live hook's secret screening and length cap; the log stays mode 0600 and is never pushed or sent to a worker; the source files are never modified. Rows are marked `src` (`zsh`, `bash`, `fish`) and `id` (a hash of source, time, command and folder, so a second run adds nothing); a live row has neither. `import --clear` removes only imported rows. An untimed row counts as a run and adds no seconds, so no hours are invented; the summary says how many runs, and how much of the time, come from imported history, so imported hours are never passed off as measured. **Live recording after setup** needs a hook: the existing `offload log hook pre|post` is for an assistant or a shell hook; a zsh/bash/fish hook that calls it is a later feature (backlog), so until then the log grows by re-running the import.

**Windows.** The zsh, bash and fish files do not exist there, so the import finds nothing and says so; PowerShell's history has no dates and is not read.

### End-user failures (how a person reaches each, the message, the test)

| Failure | How it is reached | What is shown | Test |
| ------- | ----------------- | ------------- | ---- |
| A history file is huge or unreadable | years of history, wrong owner | the file is named with the size rule or the system's error; the others still import | `test_a_huge_file...`, `test_an_unreadable_file...` |
| A history file holds junk or odd bytes | an interrupted write, another tool | the bad lines are skipped, the scan goes on | `test_binary_junk...` |
| The log cannot be read or written | wrong owner or mode | `review.log.unreadable` / `import.log.unwritable` with the system's text; at setup a note and the install goes on | `test_an_unreadable_log...`, `test_the_real_error_of_an_unwritable_log_is_shown` |
| Imported hours overstate | a time is only known where the shell wrote one | untimed rows add no time; the summary says which runs are imported | `test_the_summary_card_says_which_runs_came_from_history` |
| History has no dates | plain zsh/bash history | counted and said, not imported | `test_zsh_plain_history...` |

## Built: the usage-history hint (slice `feat/offload-history-hint`, 2026-11-28)

After `ct-run jobs`, `result`, `submit` and `classes` (the same places as the faster-worker hint, and never both on one command: the faster-worker line goes first), one `HINT` card says "Your command history has commands a worker could take", with how many kinds of command have an offload reason and the recorded time of the timed runs, and points at `ct-run offload review`. Code: `ct.client.histhint` (pure `pick`, the state file `history-hint.json`, mode 0600) and `_history_hint` in `ct.client.cli`.

- **Settings:** `offload.history_hint` (default true) and `offload.history_hint_hours` (24). The wording and rate limit follow `offload.recommend_hint`.
- **When it speaks:** the review has state `ok`, at least the interval passed, and the set of commands differs from the one last named (a set that went away is forgotten). Opening `ct-run offload review` counts as seen, so the hint does not repeat what you just looked at.
- **Silenced by the review's answers:** a command answered never, or not now inside `offload.review_ask_again_days`, is not counted; commands a worker is never offered (secrets, network, tenta's own) are not counted either.
- **Honest numbers:** only timed runs add to the time; runs without timing (imported history without durations) are named as "not timed" and add no hours. The words are "offload reason" (the ladder's term).
- **Cheap and safe:** the interval is checked before the log is read; local files only; any failure is swallowed and the command's exit status is untouched.
- **Known limit:** a command already registered on every worker still counts, because telling needs a look at the workers (a network step); the same-set rule keeps it from repeating. Backlog.

## The offload review flow (slices 6-8)

Goal: show value the first minute after setup, from the person's own history.

1. **After install: the saved-hours summary.** Read the command log and print "N runs, H hours of commands that could have run elsewhere", with the top commands. It reads only local data and sends nothing.
2. **Ranked recommendations** from the same log, after slice 1 so compound commands count: each shape with count, median seconds, the rung it meets (OFFLOAD-REASONS) and the saved hours.
3. **On demand:** `ct-run offload review` runs the same flow at any time; the same flow is offered at the end of setup.
4. **One y/n per command**, highest saving first: "Register `<command>` as a worker class? (y/n)". **That answer is the per-command approval; there is no "yes to all".** A no is remembered so it is not asked again until the shape changes.
5. **On yes:** build the recipe (folder, command, environment proposal), then **profile it on every worker**; each worker where it **succeeds** is a **candidate**. The review does not pick a worker. Adopt asks once as today (`offload.auto_adopt`), per worker, with the person's own approval key. A failing worker is reported with the tool's real error and leaves a gap the person can see (`ct-run classes`).
6. **The orchestrator, not the review, picks per job** among the workers that offer the class, by availability and the other criteria it already uses (fact 9). The review flow adds no ranking of its own.
7. **The usage-history hint (slice 8):** after a `ct-run` command, at most once per `offload.history_hint_hours`, one line such as "You ran `<command>` 14 times last week (3.1 h). `ct-run offload review` can set it up." Reuses the existing default-on hint mechanism (`show_hint`, `HintState`, fact 11): a terminal only, never in a pipe, script or `--json`, never fails the command, and an off switch `offload.review_hint = false` (and the existing `CT_HINTS=never`).

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
| 4 | `feat/workspace-recipe` | the saved recipe (command, working directory, environment, outputs), marker-file proposals, full log kept with a fetch line | 2, 3 (built before them, on today's text-only transport: see "Built: the recipe") |
| 5 | `feat/busy-host-rung` | the "your computer is busy" rung (host load against core count) in the candidacy ladder | 0 and the owner's weight (not chosen here) |
| 6 | `feat/offload-review-summary` | post-install saved-hours summary, ranked recommendations, `ct-run offload review` listing and the setup offer | 1 |
| 7 | `feat/offload-review-profile-all` | y/n per command, profile on every worker, per-worker adopt, a remembered no | 4, 6 |
| 8 | `feat/offload-history-hint` | rate-limited usage-history hint and `offload.history_hint` | 6; `theme/orchestrator` merged into `main` and `main` merged into the theme |

Slices 2 and 3 do not touch the same files (the file packaging and the worker's cache, against `packages.py` and its prune call) and can run in parallel; 6 can start once 1 is merged.

## Not decided here

- The weight of the busy-host rung (the owner sets it).
- ~~Whether the GitHub transport can do the manifest exchange~~ decided: a best-guess delta from a ledger plus a top-up on a miss, on both transports ("Built: delta sync").
- The retention period of full logs for successful jobs (slice 4).
- Windows workers: Special Use only (`WINDOWS-SPECIAL-USE.md`); the environment and cache rules above are written for Linux workers.
