"""Guard (spot-check finding F3): every place in the product that writes an attached file under a name taken from a job goes through the one shared name check (jobs.file_name_problem), directly or by
check_files. A new writer that skips it fails here, so a Windows worker can never be handed `C:x` or `nul.txt` by a path nobody reviewed."""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[1] / "src" / "ct"
WRITES = {"write_text", "write_bytes"}
CHECKS = {"file_name_problem", "check_files"}
NOT_JOB_NAMES = {("gitio.py", "_publish_files")}                    # writes the worker's own results/<id>.json (the id is checked by jobs.check_id), never a name a job chose
JOB_FILE_NAMES = {"files"}                                           # the name every job's attached-file dict carries in this code base


def calls(node):
    return {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "") for n in ast.walk(node) if isinstance(n, ast.Call)}


def loops_over_job_files(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.For):
            it = n.iter
            base = it.func.value if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) else it
            if isinstance(base, ast.Name) and base.id in JOB_FILE_NAMES and calls(n) & WRITES:
                yield n


class FileNameGuard(unittest.TestCase):
    def test_every_loop_that_writes_job_files_runs_the_shared_name_check(self):
        found, bad = 0, []
        for f in sorted(ROOT.rglob("*.py")):
            if "client" in f.parts:                                   # the client reads files; it never writes a name taken from a job
                continue
            for fn in (n for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                for loop in loops_over_job_files(fn):
                    if (f.name, fn.name) in NOT_JOB_NAMES:
                        continue
                    found += 1
                    if not calls(loop) & CHECKS:
                        bad.append(f"{f.relative_to(ROOT)}:{loop.lineno} ({fn.name})")
        self.assertGreaterEqual(found, 1, "the guard no longer finds the worker's write loop: update it with the code")
        self.assertEqual(bad, [], "a job file is written under its name without jobs.file_name_problem in the same loop")

    def test_the_client_checks_every_name_it_collects_or_is_given(self):
        text = (ROOT / "client" / "submit.py").read_text(encoding="utf-8") + (ROOT / "client" / "cli.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("files[path.name] = path.read_text()"), 1)           # --file goes through add_named_file only
        self.assertNotIn("files[p.name] =", text)


if __name__ == "__main__":
    unittest.main()
