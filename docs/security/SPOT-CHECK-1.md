# Security spot-check 1 (2026-11-22)

A read-only review of what changed most recently, taken together: the Direct transport and the decision to drop profile-run approval on it, files attached to a job (names, symlinks, secrets, ignore rules), the vendored templates, the Windows worker, and how changes reach `main`. Two findings were reproduced with a script; the rest are from reading the code. **CVSS 4.0 vectors are my estimates**: confirm each in the calculator (https://www.first.org/cvss/calculator/4.0) before treating a rating as fixed. CVSS ranks a finding; it does not score the app.

## Findings

| # | Finding | Evidence | Estimated vector | Rating (est.) | Fix |
|---|---|---|---|---|---|
| F1 | `arm-run submit --dir` follows symlinks: a link inside the project that points outside it sends the target's contents. The secret-name hold-back looks at the link's name, not the target's | Reproduced: a link named `notes.txt` to a file outside the project was sent | `CVSS:4.0/AV:L/AC:L/AT:N/PR:N/UI:A/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N` | Medium | Resolve each file; leave out (and list) any whose real path is outside the folder or that is a link |
| F2 | Collecting files runs `git` inside the project folder. A repository whose own config sets `core.fsmonitor` runs a command of the attacker's choosing when `git ls-files` runs, so attaching an untrusted checkout runs code as the user | Reproduced with git 2.50.1: a hook script created a marker file | `CVSS:4.0/AV:L/AC:L/AT:N/PR:N/UI:A/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N` | High | Run git with `-c core.fsmonitor=false -c core.hooksPath=<null device>` and a scrubbed environment; for a folder whose config sets an executable option, skip git mode and say so |
| F3 | A file name may contain `:` and Windows reserved names: on a Windows worker `C:x` is drive-relative and could be written outside the job folder. No Windows class accepts files yet | Code reading (`jobs.file_name_problem`, `worker._execute`) | `CVSS:4.0/AV:A/AC:L/AT:N/PR:H/UI:N/VC:N/VI:L/VA:N/SC:N/SI:N/SA:N` | Low now, rises when a Windows class takes files | Refuse `:`, trailing dots or spaces and reserved device names in names; before any Windows class sets `accepts_files` |
| F4 | Direct without profile approval (decided): the mailbox key alone queues a profile run, so a stolen key means arbitrary commands, inside the worker's jail, on every worker that key reaches. The key has no passphrase by design | Code reading (`mailshell.decide`, key made with `-N ""`); the approval step is not removed yet | `CVSS:4.0/AV:A/AC:L/AT:N/PR:H/UI:N/VC:L/VI:L/VA:L/SC:N/SI:N/SA:N` | Medium (accepted design risk with conditions) | One mailbox key per worker (check), `arm-setup` command to rotate it, a worker-side record of every job a Direct client sent, profile-run refused on a worker without a working jail (every Windows worker), the setting off by default until those exist |
| F5 | Direct has no size limit (decided): one job can fill the worker's disk | Design | `CVSS:4.0/AV:A/AC:L/AT:N/PR:H/UI:N/VC:N/VI:N/VA:L/SC:N/SI:N/SA:N` | Low | The worker keeps its own ceiling and checks free disk before it accepts a job (as decided: never lower than the gate) |
| F6 | Secrets in an ordinarily named file are sent with only a warning. On the GitHub transport the job is pushed to a private GitHub repository, so a secret leaves the person's own network | Code reading (`submit.collect_dir`) | `CVSS:4.0/AV:L/AC:L/AT:N/PR:N/UI:A/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N` | Medium on GitHub, Low on Direct | Make the content check transport-dependent: hold on GitHub, warn on Direct (belongs to the pre-job summary) |
| F7 | Changes reach `main` with no human review: auto-merge is a standing rule, `main` needs 0 approvals, administrators are not bound by the protection, and there is no CODEOWNERS file. CI is tests and the privacy check only | `gh api` branch protection | Not a vulnerability of the app: a supply-chain control gap | Medium (process) | Keep manual merge for PRs that touch security paths (jail, approvals, signing, mailshell, update, privacy-check); add code scanning, secret scanning and dependency alerts; run OpenSSF Scorecard |
| F8 | The privacy check skips `src/armada/client/ignore_templates/`, so a change there is never scanned | Code reading (`scripts/privacy-check`) | `CVSS:4.0/AV:N/AC:H/AT:N/PR:H/UI:N/VC:N/VI:L/VA:N/SC:N/SI:N/SA:N` | Low | A test that pins each vendored file's hash to the recorded upstream commit, so any edit fails |

## What held up

- The mailbox account is `restrict,command="..."`: no shell, no forwarding, no tty; the forced command only allows git on the two repositories (`mailshell.decide`).
- The approval key and the mailbox key are different keys. A registry change needs the approval key's signature (`update.py`, `approvals.check_registry`), so a stolen mailbox key cannot add a class.
- A job file is validated on the worker (class in the installed registry, typed parameters, age limit, names), and the refusal catalog now makes each of those rules visible and reviewable.
- Approvals are pinned to the exact job file by its digest; a job that changed after you looked is not approved.
- Output is scanned before it is published; the approval and mailbox key folders are mode 0700.

## Not examined (next spot-check)

The jail itself (bubblewrap options, namespaces), the update and install path (tags can move; how `arm-update` verifies what it installs), the Windows PowerShell scripts beyond their argument checks, the output scanner's bypasses, how `gh` tokens are handled, and denial of service by many small jobs. The AI layer has no code yet: an agent that can submit jobs is the largest open design question (a human is no longer the gate).

## Order I would fix them

F2 and F1 first (both are in one function, small, and F2 is a code-execution path), then F8 (a test), F3 (before any Windows class takes files), F4 and F5 as part of the Direct work, F6 with the pre-job summary, F7 as a decision about process.

## Status

- F1 (links) and F2 (git run in an untrusted repository): fixed in `armada.client.submit` (`safe_git`, links never followed), each with a test that failed before the fix.
- F8 (vendored templates not scanned): fixed. Every file is pinned by hash in `src/armada/client/ignore_templates/SHA256SUMS` (written by `scripts/refresh-gitignore`); `tests/test_ignore_templates.py` fails on any edit, addition or removal, and `submit.templates_problems()` makes a folder fall back to the interim rule if the templates are not exactly what was vendored.
- F7: decided: security-sensitive paths are merged by hand (`MERGE-POLICY.md`).
- F6: fixed in `pre-job-summary` (secret-looking text is held on the GitHub transport, warned on Direct; a pre-job summary card names the count, size and destination).
- F5: fixed in `direct-compress`: a Direct set over 1 GB is sent compressed, and the worker unpacks it only under a cap (a quarter of free disk, half of free memory, at most 10 times its job-file ceiling) after a free-disk check; the unpacking stops at the cap whatever size the job claims (`jobs.unpack_files`, tests in `tests/test_direct_compress.py`). Release compatibility: a worker advertises `compress` in registry.json from the release that added it; a client never sends compressed to a worker that does not, and the refusal names `--no-compress`.
- F3, F4: open (see the fix order above).
