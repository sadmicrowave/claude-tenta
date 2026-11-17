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


def section_end(lines, section):
    """Index just after the last line of a top-level `section:` (trailing blank and comment lines belong to what follows), or None if the section is not there."""
    try:
        at = next(i for i, l in enumerate(lines) if re.match(rf"^{section}:\s*(\{{\}})?\s*$", l))
    except StopIteration:
        return None
    end = next((i for i in range(at + 1, len(lines)) if re.match(r"^\S", lines[i]) and not lines[i].startswith("#")), len(lines))
    while end > at + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return at, end


def add_entries(text, section, entry_lines):
    """Add the (already indented) lines to the end of a top-level section, creating the section when the file has none. `section: {}` becomes a normal section."""
    lines = text.splitlines()
    found = section_end(lines, section)
    if found is None:
        lines += ([""] if lines and lines[-1].strip() else []) + [f"{section}:"] + entry_lines
    else:
        at, end = found
        if lines[at].strip().endswith("{}"):
            lines[at] = f"{section}:"
        lines[end:end] = entry_lines
    return "\n".join(lines).rstrip("\n") + "\n"


def action_names(text):
    lines, out = text.splitlines(), []
    found = section_end(lines, "actions")
    if found:
        out = [m.group(1) for l in lines[found[0] + 1:found[1]] if (m := CLASS_LINE.match(l))]
    return out


def set_action_pin(text, name, digest):
    """Replace the `sha256:` line of one action (the new pin after you changed its program). None if the action or its pin line is not there."""
    lines = text.splitlines()
    found = section_end(lines, "actions")
    if not found:
        return None
    at = next((i for i in range(found[0] + 1, found[1]) if (m := CLASS_LINE.match(lines[i])) and m.group(1) == name), None)
    if at is None:
        return None
    end = next((i for i in range(at + 1, found[1]) if CLASS_LINE.match(lines[i])), found[1])
    for i in range(at + 1, end):
        if re.match(r"^    sha256:", lines[i]):
            lines[i] = f'    sha256: "{digest}"'
            return "\n".join(lines) + "\n"
    return None


def action_block(text, name):
    """(start, end) of an action in `actions:`: its comment lines above it, the line itself, its body. None when it is not there."""
    lines = text.splitlines()
    found = section_end(lines, "actions")
    if not found:
        return None
    at = next((i for i in range(found[0] + 1, found[1]) if (m := CLASS_LINE.match(lines[i])) and m.group(1) == name), None)
    if at is None:
        return None
    start = at
    while start > found[0] + 1 and lines[start - 1].startswith("  #"):
        start -= 1
    end = at + 1
    while end < found[1] and not CLASS_LINE.match(lines[end]) and not lines[end].startswith("  #"):
        end += 1
    return start, end


def classes_using(text, action):
    """Names of the classes that run this action (`kind: privileged`, `action: <name>`)."""
    lines, out = text.splitlines(), []
    for cls in class_names(text):
        s, e = class_block(text, cls)
        if any(re.match(rf"^    action:\s*{re.escape(action)}\s*$", l) for l in lines[s:e]):
            out.append(cls)
    return out


def without_action(text, name):
    """The text without the action and without every class that runs it (and no empty `actions:` left behind). None when the action is not there."""
    where = action_block(text, name)
    if not where:
        return None
    for cls in classes_using(text, name):
        text = without_class(text, cls) or text
    where = action_block(text, name)
    lines = text.splitlines()
    del lines[where[0]:where[1]]
    found = section_end(lines, "actions")
    if found and found[1] == found[0] + 1:
        del lines[found[0]]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"
