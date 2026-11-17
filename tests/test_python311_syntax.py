"""The project supports Python 3.11, whose f-strings are stricter than 3.12's: no quote of the enclosing f-string reused inside its braces. A newer interpreter accepts that
silently, so this reads every source file's tokens and fails on it, instead of waiting for the 3.11 job of CI to find it."""
import io
import tokenize
import unittest
from pathlib import Path

import support  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def quote_of(text):
    body = text.lstrip("rRbBfFuU")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def violations(source):
    found, stack = [], []                               # stack: the quote of each f-string we are inside (innermost last)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        name = tokenize.tok_name[tok.type]
        if name == "FSTRING_START":
            q = quote_of(tok.string)
            if any(q == outer for outer in stack):
                found.append((tok.start[0], f"an f-string using {q} inside an f-string that also uses {q}"))
            stack.append(q)
        elif name == "FSTRING_END":
            stack.pop()
        elif name == "STRING" and stack and quote_of(tok.string) in stack:
            found.append((tok.start[0], f"a string using {quote_of(tok.string)} inside an f-string that also uses {quote_of(tok.string)}"))
    return found


class Python311(unittest.TestCase):
    def test_the_scanner_sees_what_3_11_refuses(self):
        self.assertTrue(violations('x = f"a {d["k"]} b"\n'))
        self.assertTrue(violations("x = f'a {\"-p \" + y if y else \"\"}'\n") == [])
        self.assertEqual(violations('x = f"a {d[\'k\']} b"\n'), [])

    def test_no_source_file_uses_syntax_newer_than_3_11(self):
        def is_python(p):
            return p.is_file() and (p.suffix == ".py" or (p.suffix == "" and "python" in (p.read_text().splitlines() or [""])[0]))
        bad = [f"{p.relative_to(ROOT)}:{n}: {why}" for d in ("src", "tests", "scripts") for p in sorted((ROOT / d).rglob("*")) if is_python(p) for n, why in violations(p.read_text())]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
