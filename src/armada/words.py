"""Small helpers for the words in messages, so no message has to guess whether a count is one."""


def plural(n, noun, many=None):
    """'1 file', '2 files', '0 files': the noun agrees with the count. The last word of a phrase is the one that changes ('undeclared output field' -> 'undeclared output fields').
    `many` is the plural when it is not made by adding s or es ('class' -> 'classes' is done for you; give 'children' for 'child')."""
    if n == 1:
        return f"{n} {noun}"
    if many:
        return f"{n} {many}"
    head, _, last = noun.rpartition(" ")
    last = last + ("es" if last.endswith(("s", "x", "ch", "sh")) else "s")
    return f"{n} {head + ' ' if head else ''}{last}"
