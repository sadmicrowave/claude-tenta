"""A refusal or error of the setup commands, built from the catalog (src/armada/refusals.py): the entry's title and text, its steps unless the caller gives better ones, the facts as labelled rows, and the code as the last row. The one way a setup module refuses."""
from .. import refusals


def error(code, *rows, steps=None, **vars):
    """The SetupError to raise: `raise refuse.error("win.unreachable", ("Computer", host), steps=[...], said="timed out")`. `rows` are (label, value) facts; `vars` fill the text."""
    from .server import SetupError                                     # (server.py imports this module, so SetupError is imported where it is used)
    entry = refusals.CATALOG[code]
    return SetupError(entry.title, refusals.explain(code, **vars), list(steps if steps is not None else entry.steps), facts=[*rows, ("Code", code)])
