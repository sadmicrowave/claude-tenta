# Release plan (a working plan, not a promise)

- **Main line (Linux; macOS as an administrator's computer).** The current build and everything in it: scheduler, aliases, Special Use and shared computers, the setup flows. Releases are tagged `release-X.Y.Z` on the release branch.
- **Windows line (separate, planned).** A Windows computer as a Special Use worker only, with its own release branch, tags and continuous integration, so it can never block or change a Linux release. The scan already recognises a Windows computer and says so; installing it waits for this line. Design: `docs/design/WINDOWS-SPECIAL-USE.md`.
- **Later, not scheduled:** a Mac worker (its own design: launchd and a different sandbox), and Windows as a full host install rather than a device.
