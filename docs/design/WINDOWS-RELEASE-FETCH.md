# Windows release fetch (slice 5, first half)

**Goal.** This computer finds the latest Windows release and puts exactly that source on a Windows computer, verified.
**Done when.** `release-win-X.Y.Z` tags are their own line (the Linux check never sees them); `ct-setup add-worker` on a Windows computer, after the tools, fetches the commit the latest Windows tag points to into `%ProgramData%\claude-tenta\release`, says "already there" on a repeat, refuses a commit that differs from the one asked for, keeps a working release when a fetch fails, and says plainly when no Windows release exists yet.
**Not in scope.** Running anything from the release, the Windows worker runtime, `ct-admin apply` for Windows, creating a Windows tag, signed-tag verification, Windows 10.

Decisions: the tag is found here with `git ls-remote` (no clone); the Windows computer fetches that commit by id from `updates.source` (https only) with the git the tools installer put there; the script compares the commit it ends with to the one it was told; a new release is built beside the old one and swapped in only after it verifies.
Run on the real Windows 11 VM: fetched (8 s), repeat unchanged (1 s), unknown commit refused with git's own words and the existing release kept.
