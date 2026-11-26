"""Workspace recipe (slice 3 of theme/offload-value): any folder, git or not, any command. Selection (.gitignore decides, exclude), environment markers that
PROPOSE packages (pinned only), the saved recipe with `exclude`, and the full-output fetch line. Nothing here reads the host: folders are built in a
throwaway directory and the markers read only that folder."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (points HOME at a throwaway folder)

from ct import registry, results
from ct.client import markers, offload as off, submit
from ct.client.repo import ClientError

LIMITS = {"max_count": 200, "max_file_kb": 500, "max_total_kb": 2000, "max_batches": 10}


def folder(files, git=False):
    d = Path(tempfile.mkdtemp()).resolve()
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    if git:
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return d


class Selection(unittest.TestCase):
    def test_a_gitignore_decides_in_a_folder_that_is_not_a_git_repository(self):
        d = folder({"a.py": "x", "gen/out.py": "g", "run.log": "l", "notes.md": "n", "sub/keep.txt": "k", "sub/tmp.txt": "t", "sub/.gitignore": "tmp.txt\n", ".gitignore": "gen/\n*.log\n"})
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertEqual(sorted(files), ["a.py", "notes.md", "sub/keep.txt"])                                       # the folder's own rules, nested ones too, on top of the defaults
        self.assertTrue({"gen", "run.log", "sub/tmp.txt"} <= set(left.excluded))                                    # each left-out name is told
        self.assertIn(".gitignore", left.source)

    def test_a_gitignore_only_adds_to_the_defaults_never_lets_in_what_they_keep_out(self):
        d = folder({"package.json": "{}", "node_modules/m.js": "n", "a.js": "a", "gen/x.js": "g", ".gitignore": "gen/\n"})
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS)), [".gitignore", "a.js", "package.json"])      # a .gitignore that forgets node_modules does not send it
        d = folder({"a.py": "a", "node_modules/m.js": "n", ".gitignore": "# nothing\n"})
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS)), ["a.py"])

    def test_no_gitignore_uses_the_default_rules(self):
        d = folder({"a.py": "x", "node_modules/m.js": "n", ".hidden": "h", "data/rows.csv": "1,2"})
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS)), ["a.py", "data/rows.csv"])

    def test_exclude_removes_files_in_a_plain_folder_a_git_folder_and_beats_include(self):
        files = {"a.py": "x", "docs/d.md": "d", "README.md": "r", "src/b.py": "b"}
        for git in (False, True):
            d = folder(files, git=git)
            left = submit.LeftOut()
            got = submit.collect_dir(d, LIMITS, exclude=["docs/**", "*.md"], left=left)
            self.assertEqual(sorted(got), ["a.py", "src/b.py"], msg=f"git={git}")
            self.assertEqual(sorted(left.excluded), ["README.md", "docs/d.md"], msg="every excluded file is told")
            both = submit.collect_dir(d, LIMITS, include=["*.py", "**/*.py"], exclude=["a.py"])
            self.assertEqual(sorted(both), ["src/b.py"], msg="exclude only removes; it wins over include")

    def test_secrets_are_still_held_whatever_the_rules_say(self):
        d = folder({"a.py": "x", "secrets.json": "K=1", "server.pem": "p", ".gitignore": "nothing-matches\n"})
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertEqual(sorted(n for n, _, _ in left.held), ["secrets.json", "server.pem"])
        self.assertNotIn("secrets.json", files)

    def test_a_selection_that_ends_up_empty_is_refused_and_says_why(self):
        d = folder({"a.py": "x", ".gitignore": "*\n"})
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        with self.assertRaises(ClientError) as cm:
            submit.check_not_empty(files, left, str(d))
        self.assertIn("nothing to send", str(cm.exception.title).lower())
        self.assertIn(".gitignore", cm.exception.why)
        self.assertIn(("Folder", str(d)), cm.exception.facts)
        submit.check_not_empty({"a": "x"}, left, str(d))                    # a non-empty set passes


class Failures(unittest.TestCase):
    def test_a_recipe_used_from_another_folder_says_the_folder_is_missing_not_empty(self):
        with self.assertRaises(ClientError) as cm:
            submit.check_not_empty({}, submit.LeftOut(), "/no/such/folder")
        self.assertEqual(cm.exception.facts[-1], ("Code", "files.dir.missing"))

    def test_a_recipe_file_that_is_gone_or_binary_is_named_not_a_stack_trace(self):
        d = folder({"ok.txt": "fine"})
        (d / "bin.dat").write_bytes(b"\xff\xfe\x00")
        self.assertEqual(off.read_recipe_files([str(d / "ok.txt")]), {"ok.txt": "fine"})
        for name in ("gone.txt", "bin.dat"):
            with self.assertRaises(ClientError) as cm:
                off.read_recipe_files([str(d / name)])
            self.assertIn(("File", str(d / name)), cm.exception.facts)
            self.assertEqual(cm.exception.facts[-1], ("Code", "pack.recipe.file_missing"))


class Markers(unittest.TestCase):
    def test_python_with_pinned_and_unpinned_requirements(self):
        d = folder({"requirements.txt": "pyflakes==3.2.0\nrequests>=2\n# comment\n-r other.txt\n"})
        r = markers.detect(d)
        self.assertEqual(r.packages, ["apt:python3", "apt:python3-pip", "pip:pyflakes==3.2.0"])
        self.assertEqual(r.unpinned, ["pip:requests>=2"])                   # reported, never guessed into a pin
        self.assertEqual(r.found, ["requirements.txt"])

    def test_pyproject_dependencies(self):
        d = folder({"pyproject.toml": "[project]\nname='x'\ndependencies=['pyflakes==3.2.0','rich']\n"})
        r = markers.detect(d)
        self.assertEqual(r.packages, ["apt:python3", "apt:python3-pip", "pip:pyflakes==3.2.0"])
        self.assertEqual(r.unpinned, ["pip:rich"])

    def test_node_pins_come_from_the_lock_file_only(self):
        pkg = {"dependencies": {"left-pad": "^1.0.0"}, "devDependencies": {"eslint": "^9"}}
        lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/left-pad": {"version": "1.3.0"}, "node_modules/eslint": {"version": "9.12.0"}}}
        d = folder({"package.json": json.dumps(pkg), "package-lock.json": json.dumps(lock)})
        r = markers.detect(d)
        self.assertEqual(r.packages, ["apt:nodejs", "apt:npm", "npm:eslint@9.12.0", "npm:left-pad@1.3.0"])
        self.assertEqual(r.unpinned, [])
        nolock = markers.detect(folder({"package.json": json.dumps(pkg)}))
        self.assertEqual(nolock.packages, ["apt:nodejs", "apt:npm"])
        self.assertEqual(nolock.unpinned, ["npm:eslint", "npm:left-pad"])

    def test_makefile_proposes_make_and_shows_what_its_first_target_calls(self):
        d = folder({"Makefile": ".PHONY: test\ntest:\n\tpython3 -m pytest -q\n\techo done\nbuild:\n\tcc x.c\n"})
        r = markers.detect(d)
        self.assertEqual(r.packages, ["apt:make"])
        self.assertIn("test", " ".join(r.notes))
        self.assertIn("python3 -m pytest -q", " ".join(r.notes))            # shown, not installed

    def test_rust_and_go_are_reported_as_not_installable_yet_never_invented(self):
        for name, text in (("Cargo.toml", "[package]\nname='x'\n"), ("go.mod", "module x\n")):
            r = markers.detect(folder({name: text}))
            self.assertEqual(r.packages, [], msg=name)
            self.assertEqual(r.found, [name])
            self.assertTrue(r.problems and "cannot install" in r.problems[0], msg=name)

    def test_a_data_job_with_no_marker_proposes_nothing(self):
        r = markers.detect(folder({"rows.csv": "1,2", "job.sh": "echo"}))
        self.assertEqual((r.packages, r.found, r.problems, r.unpinned), ([], [], [], []))

    def test_an_unreadable_marker_is_reported_with_the_real_error_and_never_crashes(self):
        r = markers.detect(folder({"pyproject.toml": "this is [not toml", "package.json": "{nope"}))
        self.assertEqual(len(r.problems), 2)
        self.assertTrue(any("pyproject.toml" in p and "could not be read" in p for p in r.problems))
        self.assertTrue(all(len(p) > 30 for p in r.problems))               # the parser's own text is kept

    def test_more_than_eight_packages_is_reported_not_truncated(self):
        reqs = "".join(f"p{i}==1.0\n" for i in range(9))
        r = markers.detect(folder({"requirements.txt": reqs}))
        self.assertGreater(len(r.packages), 8)
        with self.assertRaises(ClientError) as cm:
            r.usable()
        self.assertIn("11 packages", cm.exception.why)
        self.assertEqual(markers.detect(folder({"requirements.txt": "a==1\n"})).usable(), ["apt:python3", "apt:python3-pip", "pip:a==1"])

    def test_a_marker_that_is_a_link_or_huge_is_not_followed(self):
        d = folder({"real.txt": "evil==1\n"})
        (d / "requirements.txt").symlink_to(d / "real.txt")
        self.assertEqual(markers.detect(d).found, [])
        big = folder({"requirements.txt": "x==1\n" * 400000})
        self.assertTrue(markers.detect(big).problems)

    def test_the_proposal_text_names_every_package_and_the_flag_that_accepts_it(self):
        r = markers.detect(folder({"requirements.txt": "pyflakes==3.2.0\nrich\n"}))
        text = "\n".join(markers.proposal_lines(r))
        for needle in ("apt:python3", "pip:pyflakes==3.2.0", "pip:rich", "--use-detected-packages", "needs a pin"):
            self.assertIn(needle, text)


class Recipe(unittest.TestCase):
    def test_exclude_is_part_of_the_recipe(self):
        cwd = Path(tempfile.mkdtemp()).resolve()
        recipe, _ = off.make_recipe(".", ["src/**"], None, cwd, exclude=["docs/**"])
        self.assertEqual(recipe, {"dir": ".", "include": ["src/**"], "exclude": ["docs/**"]})

    def test_use_recipe_returns_the_exclude_and_typed_options_win(self):
        cwd = Path(tempfile.mkdtemp()).resolve()
        r = {"dir": "src", "exclude": ["*.md"]}
        self.assertEqual(off.use_recipe(r, None, None, None, cwd), (str(cwd / "src"), None, None, ["*.md"], True))
        self.assertEqual(off.use_recipe(r, "other", None, None, cwd), ("other", None, None, None, False))
        self.assertEqual(off.use_recipe(r, None, None, None, cwd, exclude=["x"])[4], False)

    def test_the_registry_accepts_a_safe_exclude_and_refuses_the_rest(self):
        self.assertIsNone(registry.pack_problem({"dir": ".", "exclude": ["docs/**", "*.md"]}))
        for bad in ({"exclude": []}, {"exclude": "docs"}, {"exclude": ["/etc/*"]}, {"exclude": ["../x"]}, {"exclude": ["a"] * 21}, {"exclude": [""]}, {"cwd": "sub"}):
            self.assertIsNotNone(registry.pack_problem(bad), msg=str(bad))
        self.assertIn("exclude", registry.pack_problem({"nope": 1}))

    def test_an_unsafe_exclude_in_a_saved_recipe_is_refused_not_followed(self):
        with self.assertRaises(ClientError) as cm:
            off.use_recipe({"dir": ".", "exclude": ["../../etc/*"]}, None, None, None, Path("."))
        self.assertIn("not safe", str(cm.exception))

    def test_the_class_lines_carry_exclude(self):
        entry = {"tier": "auto", "run": ["x"], "accepts_files": True, "submit": {"dir": ".", "exclude": ["docs/**"]}}
        self.assertIn('    submit: {"dir": ".", "exclude": ["docs/**"]}', off.class_yaml_lines("lint", entry, {"reasons": ["ok"]}, "profile-aaa111"))


class FullOutputLine(unittest.TestCase):
    def test_a_shortened_result_says_the_one_command_that_fetches_the_rest(self):
        note = results.kept_output_note("tenta", "job-abc123")
        self.assertIn("ct-admin output job-abc123", note)
        self.assertIn("only the error lines were sent", note)


if __name__ == "__main__":
    unittest.main()
