"""Workspace recipe through the real `ct-run` commands (fake worker and queue): several command kinds on git and non-git folders, the proposal that never installs
on its own, the saved recipe reaching the adopted class, and the refusal of an empty selection."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from offload_support import OffloadFixture  # noqa: E402
from support import sh  # noqa: E402

KINDS = {                                   # a folder and the command someone would send for it
    "tests (python)": ({"requirements.txt": "pyflakes==3.2.0\n", "t.py": "x"}, ("python3", "-m", "pyflakes", "."), ["apt:python3", "apt:python3-pip", "pip:pyflakes==3.2.0"]),
    "linter (node)": ({"package.json": '{"devDependencies": {"eslint": "^9"}}', "package-lock.json": '{"packages": {"node_modules/eslint": {"version": "9.12.0"}}}', "a.js": "x"},
                       ("eslint", "."), ["apt:nodejs", "apt:npm", "npm:eslint@9.12.0"]),
    "build (make)": ({"Makefile": "all:\n\tcc a.c\n", "a.c": "int x;"}, ("make", "all"), ["apt:make"]),
    "scanner (rust)": ({"Cargo.toml": "[package]\nname='x'\n", "a.rs": "x"}, ("cargo", "clippy"), []),
    "data job (nothing)": ({"rows.csv": "1,2", "job.sh": "x"}, ("wc", "-l", "rows.csv"), []),
}


def make(files, git):
    d = Path(tempfile.mkdtemp()).resolve()
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    if git:
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return d


class Workspace(OffloadFixture):
    def sent_job(self, jid):
        return json.loads(sh("git", "show", f"jobs:jobs/{jid}.json", cwd=self.tmp / "queue.git"))

    def test_every_command_kind_on_git_and_plain_folders_proposes_but_never_installs_by_itself(self):
        self.push_main_registry()
        for git in (False, True):
            for kind, (files, argv, want) in KINDS.items():
                with self.subTest(kind=kind, git=git):
                    d = make(files, git)
                    code, out, err = self.profile("--dir", str(d), argv=argv)
                    self.assertEqual(code, 0, out + err)
                    jid = out.strip().splitlines()[-1]
                    self.assertEqual(self.sent_job(jid).get("packages", []), [], "a proposal alone puts no package on the run")
                    self.assertEqual("What this folder seems to need" in out + err, kind != "data job (nothing)")
                    code, out, err = self.profile("--dir", str(d), "--use-detected-packages", argv=argv)
                    if kind == "data job (nothing)":
                        self.assertIn("profile.markers.none", out + err)             # nothing proposed, so nothing to accept
                        continue
                    self.assertEqual(code, 0, out + err)
                    self.assertEqual(self.sent_job(out.strip().splitlines()[-1]).get("packages", []), want)

    def test_use_detected_packages_with_nothing_detected_is_refused(self):
        self.push_main_registry()
        code, out, err = self.profile("--dir", str(make({"rows.csv": "1"}, False)), "--use-detected-packages", argv=("wc", "-l", "rows.csv"))
        self.assertNotEqual(code, 0)
        self.assertIn("profile.markers.none", out + err)

    def test_an_empty_selection_is_refused_before_anything_is_sent(self):
        self.push_main_registry()
        d = make({"a.py": "x", ".gitignore": "*\n"}, False)
        code, out, err = self.profile("--dir", str(d), argv=("python3", "a.py"))
        self.assertNotEqual(code, 0)
        self.assertIn("files.dir.empty", out + err)
        self.assertIn(".gitignore", out + err)

    def test_the_exclude_is_saved_with_the_run_and_reaches_the_adopted_class(self):
        self.push_main_registry()
        d = make({"a.py": "x", "docs/d.md": "d"}, False)
        here = os.getcwd()
        os.chdir(d.parent)                                                                   # a recipe is saved relative to where the command was typed
        self.addCleanup(os.chdir, here)
        jid = self.run_profile_to_a_result("--name", "ws-lint", "--dir", d.name, "--exclude", "docs/**", argv=("ls",))
        self.assertEqual(self.ctx.packs.get(jid)["exclude"], ["docs/**"])
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 0, out + err)
        import yaml
        entry = yaml.safe_load(sh("git", "show", "registry/ws-lint:registry.yaml", cwd=self.tmp / "queue.git"))["classes"]["ws-lint"]
        self.assertEqual(entry["submit"]["exclude"], ["docs/**"])


if __name__ == "__main__":
    unittest.main()
