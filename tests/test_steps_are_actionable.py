"""Every instruction shown under WHAT TO DO (a catalog entry's steps, and the steps a caller gives a refusal) is something a person can follow: it names the command, the file or the page, never a bare "fix it"."""
import ast
import re
import unittest
from pathlib import Path

import support  # noqa: F401

ROOT = Path(__file__).resolve().parents[1] / "src" / "armada"
VAGUE = re.compile(r"^(fix|resolve|correct|check|repair)\s+(the|it|that|this)\b|\bsee above\b|\bmessages above\b|--set section\.key", re.I)


def step_strings(call):
    nodes = list(call.args[2:3]) + [k.value for k in call.keywords if k.arg == "steps"]
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub.value


class Steps(unittest.TestCase):
    def test_no_step_is_a_bare_instruction_to_fix_something(self):
        from armada import refusals
        found, bad = 0, []
        for code, entry in refusals.CATALOG.items():                                   # the steps every refusal carries by default
            for text in entry.steps:
                found += 1
                if VAGUE.search(text.strip()):
                    bad.append(f"{code}: {text[:80]}")
        for path in sorted(ROOT.rglob("*.py")):                                        # and the steps a caller gives (SetupError, or the catalog builders, with steps=...)
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("SetupError", "error"):
                    for text in step_strings(node):
                        found += 1
                        if VAGUE.search(text.strip()):
                            bad.append(f"{path.name}:{node.lineno}: {text[:80]}")
        self.assertGreater(found, 10)
        self.assertEqual(bad, [], "steps must say what to run or open, not just 'fix' it")


if __name__ == "__main__":
    unittest.main()
