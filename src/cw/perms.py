"""File modes for the worker's state: job inputs, results and the git clones are not for other accounts on the machine.

New files and folders are created with no access for "other" and no group write (umask 027), and existing ones are tightened once.
The worker group keeps read access, so an admin account that is a member of it can look, read-only. Nothing is ever made MORE open.
"""
import os
from pathlib import Path

STATE_UMASK = 0o027
MODES_MARKER = ".modes-1"


def tighten_modes(roots, marker):
    """Remove group-write and all 'other' access under `roots`, once (the marker records that it was done). Returns how many paths changed."""
    marker = Path(marker)
    if marker.exists():
        return 0
    roots = [Path(r) for r in roots]
    parents = {r.parent for r in roots}
    if len(parents) == 1 and len(next(iter(parents)).parts) >= 4:      # e.g. /var/lib/<name> itself, never /var/lib or /
        roots = [*roots, next(iter(parents))]
    changed = 0
    for root in roots:
        stack = [str(root)]
        while stack:
            path = stack.pop()
            try:
                if os.path.islink(path):
                    continue
                mode = os.lstat(path).st_mode & 0o7777
                new = mode & ~0o027
                if new != mode:
                    os.chmod(path, new)
                    changed += 1
                if os.path.isdir(path):
                    with os.scandir(path) as it:
                        stack += [e.path for e in it]
            except OSError:
                pass                                                   # not ours, or it vanished: leave it
    try:
        marker.write_text("1\n")
    except OSError:
        pass
    return changed
