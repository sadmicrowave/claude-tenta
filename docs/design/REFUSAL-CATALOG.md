# The refusal catalog

Scope of the branch `refusal-catalog` (2026-11-22). A rule that refuses a job was written wherever it was needed, about 230 places, with the reason in nobody's head: `helper@.service.in` was refused by a character list that protects nothing, and a file the rule refused was dropped silently so a test run could start on a partial tree.

## The method

Every refusal on the job path is defined once in `src/ct/refusals.py` with a stable code, its kind (security, format, limit, placement, policy), who decides it, **what it protects and from whom**, what it depends on (a class kind, the transport, trust), where it can be configured, a fixed title, the text and the steps. Modules refuse with `raise refusals.refuse("code", fact=value)`. A refusal is both a `Reject` and a `ValueError`, so every existing catch still works. The published result of a refused job carries the code (`refusal`), shown as the last row of its card. `docs/reference/refusals.md` is generated from the catalog.

A test fails on a refusal written as a literal string in a module listed in `tests/test_refusals.py` (`POLICY_MODULES`), on an entry with an empty field, on a code used but not defined, and on an entry nothing uses.

## Slices

1. This branch: the catalog and the 41 job-validation refusals in `jobs.py` and `worker.py`. No rule changes: the same jobs are refused with the same text. The three in `registry.py` read a registry file, which is an operational error and goes with the follow-on.
2. Attachment rules: a name rule that checks only the path, the leading-dot rule for classes whose tool reads settings from its working folder (`kind: claude` for now, by kind, never a setting), never dropping a file silently, a default skip list from the project's own ignore rules.
3. Placement refusals: every reason a worker was passed over, not the first.
4. The remaining client refusals.
5. Follow-on: the operational errors, by the same method.

## Decisions this rests on

Direct has no size limit, only a notice above 1 GB; limits belong to the transport, not to a worker's registry; no approval for a profile run on Direct (a Direct setting, default none); the refusal code is shown as a last row. See the working notes in the conversation record.
