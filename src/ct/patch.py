"""A write-capable job hands its work back as DATA: a patch inside its result, never a branch and never a push.

A class marked `returns: patch` runs in its own working folder like any other job; before it runs the worker records every text file there (the input files), and when it ends the
worker compares the folder with that record and publishes the difference as one unified diff (`git apply` reads it). The comparison is plain Python on purpose: running git inside a folder
a job could write to would run whatever configuration or hooks the job left there, outside the jail. The patch is size-limited, passes the output guard like any other output, and is
APPLIED BY YOU, on your computer, after you have read it (`ct-run patch <job>`). Nothing here touches anything outside the job's folder.

What a patch cannot carry, and says so instead: binary files, symbolic links, and anything past the size limit. Standard library only.
"""
import difflib
import os
import re
from pathlib import Path
from .words import plural

MAX_FILE_BYTES = 1_000_000        # one file bigger than this is left out of the record (and so of the patch), with a note
MAX_FILES = 500
UNSAFE = re.compile(r"(^|/)(\.git|\.\.)(/|$)")


def snapshot(root):
    """({relative path: text}, notes) for the regular text files under `root`. Symbolic links, binary and oversize files are skipped with a note, never followed."""
    root, files, notes = Path(root), {}, []
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".claude") and not (Path(dirpath) / d).is_symlink()]
        for name in names:
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                notes.append(f"{rel} is a symbolic link; a patch does not carry links")
            elif len(files) >= MAX_FILES:
                notes.append(f"more than {plural(MAX_FILES, 'file')}; the rest are not compared")
                return files, notes
            else:
                try:
                    if p.stat().st_size > MAX_FILE_BYTES:
                        notes.append(f"{rel} is over {MAX_FILE_BYTES // 1000} KB and is not compared")
                        continue
                    files[rel] = p.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    notes.append(f"{rel} is not text; a patch does not carry binary files")
                except OSError:
                    continue
    return files, notes


def _hunks(rel, old, new):
    a = old.splitlines(keepends=True) if old is not None else []
    b = new.splitlines(keepends=True) if new is not None else []
    head = [f"diff --git a/{rel} b/{rel}\n"]
    if old is None:
        head.append("new file mode 100644\n")
    if new is None:
        head.append("deleted file mode 100644\n")
    body = list(difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{rel}", "/dev/null" if new is None else f"b/{rel}"))
    fixed = []
    for line in body:
        fixed.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(head + fixed) if body else ""


def make(before, after, max_bytes):
    """(patch text, [changed paths], note). The patch is empty text when nothing changed; when it is over `max_bytes` it is NOT returned (a half patch would not apply) and the note says so."""
    changed, parts = [], []
    for rel in sorted(set(before) | set(after)):
        old, new = before.get(rel), after.get(rel)
        if old == new:
            continue
        piece = _hunks(rel, old, new)
        if piece:
            changed.append(rel)
            parts.append(piece)
    text = "".join(parts)
    if len(text.encode()) > max_bytes:
        return "", changed, f"the patch is {len(text.encode()) // 1000} KB, over the class's limit of {max_bytes // 1000} KB, so it was not returned ({plural(len(changed), 'file')} changed)"
    return text, changed, ""


def problems(text):
    """Why a patch must not be applied as it is, as a list of plain sentences (empty: nothing found). Checked again on your computer, whatever the worker said."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(?:diff --git a/(.+) b/(.+)|\+\+\+ b/(.+)|--- a/(.+))$", line)
        if m:
            for path in filter(None, m.groups()):
                if path.startswith("/") or UNSAFE.search(path):
                    out.append(f"it touches {path!r}, which is outside the working folder or inside .git")
        if re.match(r"^(new|old) mode 120000|^(new file|deleted file) mode 120000", line):
            out.append("it creates or changes a symbolic link")
        if line.startswith(("GIT binary patch", "Binary files ")):
            out.append("it contains a binary change")
        if re.match(r"^(rename|copy) (from|to) ", line):
            out.append("it renames or copies files (not produced by this worker)")
    return sorted(set(out))
