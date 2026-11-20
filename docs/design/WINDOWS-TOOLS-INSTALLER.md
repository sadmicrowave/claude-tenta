# Windows tools installer (feature scope)

Part of [Windows as a Special Use device](WINDOWS-SPECIAL-USE.md), the first piece of slice 5. One small branch.

## Goal
`ct-setup` puts `git` and `python` (3.11 or newer) on a Windows computer, in the worker's own tools folder, over SSH, with PowerShell and nothing else. Nothing is assumed to be installed.

## Done when
1. On a Windows 10 or 11 computer with only OpenSSH Server, one call leaves a working `git` and `python` in the tools folder, each at a pinned version.
2. Running it again changes nothing and says so.
3. A `git` or `python` (3.11+) already on the computer is used and nothing is installed for it.
4. A download whose SHA-256 differs from the pinned one is refused before anything is unpacked, naming both hashes.
5. Every failure keeps PowerShell's real error text and says the one thing to do.
6. Real run on the test VM, from a clean state (tools folder deleted), twice.

## Facts from the test VM (read-only look, 2026-11-20)
Windows NT 10.0.26300, AMD64, Windows PowerShell 5.1, execution policy Restricted (a `.ps1` file will not run: send the script as `-EncodedCommand`), default SSH shell is cmd, `tar.exe` and `curl.exe` present, `winget` present, 34 GB free, GitHub reachable on 443, Python 3.13 present (installed by the person, so not a given), Git absent.

## Design
- **Delivery:** the script is sent as `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand <base64 of UTF-16LE>` so cmd quoting and the policy never matter.
- **Git:** MinGit (the official minimal Git for Windows zip), pinned version and SHA-256 in the repository, unpacked with `tar` into `<tools>\git`. No installer, no administrator rights, nothing on the system PATH.
- **Python:** an existing Python 3.11 or newer is used; else `winget` installs it for this user (winget checks the package's own hash). The embeddable zip was dropped: python.org publishes only MD5 for its Windows files (the SHA-256 field is empty), which cannot be a pin, and verifying a download by GPG or Sigstore on Windows is a project of its own. With no Python and no winget the error says to install Python from python.org and run again.
- **Where:** `<tools>` is a parameter (default `%ProgramData%\claude-tenta\tools`).
- **Idempotent:** a marker file in `<tools>` records the versions and hashes installed; a match means "nothing to do".
- **Result:** the script's last line is one JSON object (`{"git": "...", "python": "...", "changed": true}`) that the Python side parses; any other output is shown only on failure.
- **Code:** `src/ct/setup/wintools.py` builds and runs it through the existing `Ssh`; the pinned table is data, not code.

## Tests
Unit: the encoded command round-trips; the pin table is complete and every hash is 64 hex; a hash mismatch is refused; the result line is parsed; a failure keeps the real text; the "already there" and "reuse an existing tool" decisions (script text checked, plus the Python-side parser). Real: the VM run in Done-when 6, kept as a documented by-hand route (no automated Windows runner for this yet).

## Not in this branch
The worker loop, the job runner and Job Objects, release selection, the Windows cards, WSL, Windows Home, Claude jobs, and refusing `network: false` classes (already decided; built with the runtime).
