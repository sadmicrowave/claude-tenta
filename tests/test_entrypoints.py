"""Every command in pyproject.toml points at a real function, and arm-help lists every command that exists."""
import importlib
import io
import os
import sys
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import support  # noqa: F401

from armada import help as arm_help

ROOT = Path(__file__).resolve().parents[1]


class EntryPoints(unittest.TestCase):
    def scripts(self):
        return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]

    def test_each_script_resolves_to_a_callable(self):
        for name, target in self.scripts().items():
            module, _, func = target.partition(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), func)), name)

    def test_the_four_commands_exist(self):
        self.assertEqual(sorted(self.scripts()), ["arm-admin", "arm-help", "arm-run", "arm-setup"])

    def test_help_mentions_every_command_and_every_arm_admin_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(arm_help.main(), 0)
        text = out.getvalue()
        for name in self.scripts():
            if name != "arm-help":                   # this is that screen
                self.assertIn(name, text)
        from armada.admin import cli
        for cmd in cli.HELP:
            self.assertTrue(any(cmd in row[0] for _, _, rows in arm_help.SECTIONS for row in rows if row[0].startswith("arm-admin")), cmd)

    def test_the_commands_run_as_modules(self):
        import subprocess
        import sys
        for mod in ("armada.help", "armada.client.cli", "armada.admin.cli", "armada.setup"):
            r = subprocess.run([sys.executable, "-m", mod, "help"], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "src"), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"})
            self.assertEqual(r.returncode, 0, f"{mod}: {r.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()


class WithoutPyYAML(unittest.TestCase):
    """The commands on your computer must at least start in a virtual environment where PyYAML is missing, and say what to do; and the package must declare it."""

    def test_every_command_module_imports_without_yaml(self):
        import subprocess
        src = str(Path(__file__).resolve().parents[1] / "src")
        code = (f"import sys; sys.path.insert(0, {src!r}); sys.modules['yaml'] = None\n"
                "import armada.admin.cli, armada.client.cli, armada.setup.cli, armada.help, armada.cli\n"
                "print('ok')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr[-400:])

    def test_the_package_declares_pyyaml_for_the_commands_on_your_computer(self):
        import tomllib
        deps = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["dependencies"]
        self.assertTrue(any(d.lower().startswith("pyyaml") for d in deps), deps)
