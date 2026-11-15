"""Edit registry.yaml text: remove a class (and the comments written for it). Adding one is `client.offload.add_class_to_registry`."""
import re

CLASS_LINE = re.compile(r"^  ([a-z0-9][a-z0-9-]*):\s*$")


def class_block(text, name):
    """(start, end) line numbers of a class: its comment lines above it, the line itself, its indented body. None if it is not there."""
    lines, in_classes, at = text.splitlines(), False, None
    for i, line in enumerate(lines):
        if re.match(r"^classes:\s*$", line):
            in_classes = True
        elif in_classes and re.match(r"^\S", line) and not line.startswith("#"):
            in_classes = False
        elif in_classes:
            m = CLASS_LINE.match(line)
            if m and m.group(1) == name:
                at = i
    if at is None:
        return None
    start = at
    while start > 0 and lines[start - 1].startswith("  #"):
        start -= 1
    end = at + 1
    while end < len(lines) and (lines[end].startswith("    ") or (not lines[end].strip() and end + 1 < len(lines) and lines[end + 1].startswith("    "))):
        end += 1
    return start, end


def class_names(text):
    out, in_classes = [], False
    for line in text.splitlines():
        if re.match(r"^classes:\s*$", line):
            in_classes = True
        elif in_classes and re.match(r"^\S", line) and not line.startswith("#"):
            in_classes = False
        elif in_classes and CLASS_LINE.match(line):
            out.append(CLASS_LINE.match(line).group(1))
    return out


def without_class(text, name):
    """The text with the class removed, or None if it is not there."""
    where = class_block(text, name)
    if not where:
        return None
    start, end = where
    rest = text.splitlines()[:start] + text.splitlines()[end:]
    while start < len(rest) and not rest[start].strip():
        del rest[start]
    while rest and not rest[-1].strip():
        rest.pop()
    return "\n".join(rest) + "\n"


def is_profile_kind(text, name):
    where = class_block(text, name)
    return bool(where) and any(re.match(r"^    kind:\s*profile\s*$", line) for line in text.splitlines()[where[0]:where[1]])
