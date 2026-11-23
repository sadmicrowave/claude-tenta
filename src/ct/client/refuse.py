"""A refusal the person reads on this computer, built from the catalog (src/ct/refusals.py): the entry's title and text, its steps unless the caller gives better ones, the facts as labelled rows, and the code as the last row. The one way a client module refuses."""
from .. import refusals
from .repo import ClientError


def error(code, *rows, steps=None, example=None, **vars):
    """The ClientError to raise: `raise refuse.error("placement.capacity.in_flight", ("Cap", "4"), adding=2, where="a: 1")`. `rows` are (label, value) facts; `vars` fill the text."""
    entry = refusals.CATALOG[code]
    return ClientError(entry.title, refusals.explain(code, **vars), list(steps if steps is not None else entry.steps), example=example, facts=[*rows, ("Code", code)])
