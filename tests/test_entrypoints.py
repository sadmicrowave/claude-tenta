"""Every command in pyproject.toml points at a real function, and cw-help lists every command that exists."""
import importlib
import io
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import support  # noqa: F401

from cw import help as cw_help

ROOT = Path(__file__).resolve().parents[1]


class EntryPoints(unittest.TestCase):
    def scripts(self):
        return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]

    def test_each_script_resolves_to_a_callable(self):
        for name, target in self.scripts().items():
            module, _, func = target.partition(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)), name)

    def test_the_four_commands_exist(self):
        self.assertEqual(sorted(self.scripts()), ["cw-admin", "cw-help", "cw-run", "cw-setup"])

    def test_help_mentions_every_command_and_every_cw_admin_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cw_help.main(), 0)
        text = out.getvalue()
        for name in self.scripts():
            if name != "cw-help":                   # this is that screen
                self.assertIn(name, text)
        from cw.admin import cli
        for cmd in cli.HELP:
            self.assertTrue(any(cmd in row[0] for _, _, rows in cw_help.SECTIONS for row in rows if row[0].startswith("cw-admin")), cmd)

    def test_the_commands_run_as_modules(self):
        import subprocess
        import sys
        for mod in ("cw.help", "cw.client.cli", "cw.admin.cli", "cw.setup"):
            r = subprocess.run([sys.executable, "-m", mod, "help"], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "src"), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"})
            self.assertEqual(r.returncode, 0, f"{mod}: {r.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()
