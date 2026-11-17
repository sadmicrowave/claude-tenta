"""Every command in pyproject.toml points at a real function, and ct-help lists every command that exists."""
import importlib
import io
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import support  # noqa: F401

from ct import help as ct_help

ROOT = Path(__file__).resolve().parents[1]


class EntryPoints(unittest.TestCase):
    def scripts(self):
        return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]

    def test_each_script_resolves_to_a_callable(self):
        for name, target in self.scripts().items():
            module, _, func = target.partition(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)), name)

    def test_the_four_commands_exist(self):
        self.assertEqual(sorted(self.scripts()), ["ct-admin", "ct-help", "ct-run", "ct-setup"])

    def test_help_mentions_every_command_and_every_ct_admin_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(ct_help.main(), 0)
        text = out.getvalue()
        for name in self.scripts():
            if name != "ct-help":                   # this is that screen
                self.assertIn(name, text)
        from ct.admin import cli
        for cmd in cli.HELP:
            self.assertTrue(any(cmd in row[0] for _, _, rows in ct_help.SECTIONS for row in rows if row[0].startswith("ct-admin")), cmd)

    def test_the_commands_run_as_modules(self):
        import subprocess
        import sys
        for mod in ("ct.help", "ct.client.cli", "ct.admin.cli", "ct.setup"):
            r = subprocess.run([sys.executable, "-m", mod, "help"], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "src"), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"})
            self.assertEqual(r.returncode, 0, f"{mod}: {r.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()
