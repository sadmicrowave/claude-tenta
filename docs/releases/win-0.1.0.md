# Armada for Windows 0.1.0 (preview)

The first tag of the Windows line. It carries the source and the Windows tools and release fetch only: there is **no Windows worker yet**, so nothing runs from it.

## What it is for
- Proves the Windows release path end to end: `ct-setup add-worker <windows address>` installs git and Python on a Windows 10 or 11 (Pro or Enterprise) computer over SSH and fetches exactly this commit into `%ProgramData%\armada\release`, verified.

## Not included
- The Windows worker loop and job runner, `ct-admin apply` for Windows, Windows 10 testing, signed-tag verification.
