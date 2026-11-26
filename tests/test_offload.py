import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct import registry  # noqa: E402
from ct.client import cli  # noqa: E402
from ct.client import offload as off  # noqa: E402
from ct.client.cli import adopt_tier as cli_tier  # noqa: E402
from ct.client.repo import ClientError  # noqa: E402

INFO = {"mem_total_mb": 8000, "classes": {"echo": {"run": ["/bin/echo", "{word}"], "cost": "light", "tier": "auto"},
                                          "lint": {"run": ["/usr/bin/eslint", ".", "--max-warnings", "0"], "cost": "medium", "tier": "approve"}}}


class Decision(unittest.TestCase):
    def test_a_registered_command_matches_by_template_and_by_resolved_path(self):
        self.assertEqual(off.fit_lookup(["/bin/echo", "hello"], INFO, [])["class"], "echo")
        self.assertEqual(off.fit_lookup(["echo", "hello"], INFO, [])["class"], "echo")              # a bare name matches the path the worker resolved
        self.assertEqual(off.fit_lookup(["eslint", ".", "--max-warnings", "0"], INFO, [])["verdict"], "registered")
        for argv in (["/bin/echo"], ["/bin/echo", "a", "b"], ["/bin/other", "hello"], ["eslint", ".", "--max-warnings", "1"]):
            self.assertEqual(off.fit_lookup(argv, INFO, [])["verdict"], "unknown", argv)
        self.assertEqual(off.fit_lookup(["x"], None, [])["verdict"], "unknown")

    def test_an_excluded_command_is_refused_unless_memory_was_the_limit_and_the_worker_has_more_now(self):
        ex = [{"argv": ["big"], "reasons": ["too big"], "limiting": ["memory"], "profiled": {"mem_total_mb": 4000}},
              {"argv": ["slow"], "reasons": ["too slow"], "limiting": ["time"], "profiled": {"mem_total_mb": 4000}}]
        self.assertEqual((lambda r: (r["verdict"], r["rc"], r["then_mb"], r["now_mb"]))(off.fit_lookup(["big"], INFO, ex)), ("retry", 5, 4000, 8000))
        self.assertEqual(off.fit_lookup(["slow"], INFO, ex)["rc"], 3)                              # more RAM does not make it faster
        same = dict(INFO, mem_total_mb=4000)
        self.assertEqual(off.fit_lookup(["big"], same, ex)["rc"], 3)
        self.assertEqual(off.fit_lookup(["big"], {}, ex)["rc"], 3)

    def test_the_exit_codes_and_wording(self):
        rc = lambda v: off.fit_lookup(*v)["rc"]   # noqa: E731
        self.assertEqual(rc((["/bin/echo", "x"], INFO, [])), 0)
        self.assertEqual(rc((["nope"], INFO, [])), 4)
        msg = off.fit_message(off.fit_lookup(["/bin/echo", "x"], INFO, []), ["/bin/echo", "x"], "homect")
        self.assertEqual(msg, "REGISTERED as class echo (light, tier auto): ct-run submit echo --wait")
        self.assertIn("propose a profiling run", off.fit_message({"verdict": "unknown"}, ["nope"], "homect"))
        self.assertIn("ct-run offload profile -- nope", off.fit_message({"verdict": "unknown"}, ["nope"], "homect"))
        self.assertIn("when homect had 4000 MB", off.fit_message({"verdict": "retry", "reasons": "r", "then_mb": 4000, "now_mb": 8000}, [], "homect"))
        self.assertIn("Do not offload this", off.fit_message({"verdict": "excluded", "reasons": "r"}, [], "x"))


class History(unittest.TestCase):
    def setUp(self):
        self.t = [1_700_000_000.0]
        self.log = off.FitLog(Path(tempfile.mkdtemp()), clock=lambda: self.t[0])

    def add(self, cmd, verdict="unknown", **kw):
        self.log.record(cmd.split(), {"verdict": verdict, **kw}, "fit", 12, 8000, "terminal")

    def test_a_check_is_kept_privately_and_in_the_shape_it_was_recorded(self):
        self.add("eslint .", "registered", **{"class": "lint", "cost": "medium", "tier": "auto"})
        (r,) = self.log.read()
        self.assertEqual((r["command"], r["verdict"], r["class"], r["worker_mb"], r["by"], r["ms"]), ("eslint .", "registered", "lint", 8000, "terminal", 12))
        self.assertEqual(oct(self.log.path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn("reasons", r)                                                             # empty fields are not written

    def test_a_command_that_looks_like_it_carries_a_secret_is_hidden(self):
        self.add("curl -H Authorization:Bearer-abc https://x")
        self.add("API_KEY=abc make")
        self.assertEqual([r["command"] for r in self.log.read()], ["(hidden: it looked like it carried a secret)"] * 2)

    def test_it_trims_itself_by_age_and_by_count(self):
        self.add("old one")
        self.t[0] += (off.FIT_KEEP_DAYS + 1) * 86400
        self.add("new one")
        self.assertEqual([r["command"] for r in self.log.read()], ["new one"])
        for i in range(off.FIT_KEEP_MAX + 20):
            self.add(f"cmd{i}")
        rows = self.log.read()
        self.assertEqual((len(rows), rows[-1]["command"]), (off.FIT_KEEP_MAX, f"cmd{off.FIT_KEEP_MAX + 19}"))
        self.assertEqual(oct(self.log.path.stat().st_mode & 0o777), "0o600")                       # still private after a rewrite

    def test_filtering_clearing_and_damaged_lines(self):
        self.add("eslint .", "registered", **{"class": "lint"})
        self.add("make big", "excluded", reasons="too big")
        with open(self.log.path, "a") as f:
            f.write("not json\n")
        self.assertEqual(len(self.log.read()), 2)
        self.assertEqual([r["command"] for r in self.log.filtered(verdict="excluded")], ["make big"])
        self.assertEqual([r["command"] for r in self.log.filtered(command="ESLINT")], ["eslint ."])
        self.assertEqual(self.log.clear(), 2)
        self.assertEqual(self.log.read(), [])

    def test_a_history_problem_never_breaks_a_check(self):
        broken = off.FitLog(Path("/dev/null/x"))
        broken.record(["x"], {"verdict": "unknown"}, "fit", 1, None)                                # must not raise


class Names(unittest.TestCase):
    def test_a_readable_name_from_the_command(self):
        for argv, want in ((["ssh", "-V"], "ssh-v"), (["npm", "run", "build"], "npm-run-build"), (["eslint", ".", "--max-warnings", "0"], "eslint-max-warnings"),
                           (["/usr/bin/python3", "--version"], "python3-version"), (["x"], "x"), (["./a b"], "a-b"), ([""], "adopted")):
            self.assertEqual(off.derive_name(argv), want, argv)
        self.assertLessEqual(len(off.derive_name(["tool"] + [f"word{i}" for i in range(30)])), 38)

    def test_suggestions_are_unique_stable_and_prefer_the_chosen_name(self):
        taken = {"ssh-v"}
        a = off.suggest_name(["ssh", "-V"], taken)
        self.assertNotEqual(a, "ssh-v")
        self.assertRegex(a, r"^ssh-v-[0-9a-f]{4}$")
        self.assertEqual(a, off.suggest_name(["ssh", "-V"], taken))                                 # the same command always gets the same name
        self.assertEqual(off.suggest_name(["ssh", "-V"], taken, preferred="my-ssh"), "my-ssh")
        self.assertEqual(off.suggest_name(["ssh", "-V"], taken, preferred="ssh-v"), a)              # a taken preference falls back
        self.assertEqual(off.suggest_name(["ssh", "-V"], taken | {a}), off.suggest_name(["ssh", "-V"], taken | {a}))

    def test_what_cannot_be_a_class_name(self):
        for bad in ("A", "x", "has space", "-lead", "x" * 42, "up_per"):
            self.assertIsNotNone(off.class_name_problem(bad, set()), bad)
        self.assertIsNone(off.class_name_problem("ok-name", set()))
        self.assertIn("already installed or proposed", off.class_name_problem("ok-name", {"ok-name"}))

    def test_the_name_book_remembers_the_most_recent_200(self):
        nb = off.NameBook(Path(tempfile.mkdtemp()))
        self.assertIsNone(nb.get("profile-aaa111"))
        for i in range(210):
            nb.remember(f"profile-{i:06d}", f"name-{i}")
        self.assertIsNone(nb.get("profile-000000"))
        self.assertEqual(nb.get("profile-000209"), "name-209")


class Profile(unittest.TestCase):
    def test_a_spec_file(self):
        spec = off.load_spec("# a comment\ncommand: eslint . --max-warnings 0\nname: lint\nWhat: lints it\nest-time-s: 4\nflag --max-warnings: fail over N\npackage: npm:eslint@9.1.0, apt:jq\n")
        self.assertEqual((spec["command"], spec["name"], spec["what"], spec["est_time_s"]), ("eslint . --max-warnings 0", "lint", "lints it", "4"))
        self.assertEqual((spec["flags"], spec["packages"]), ({"--max-warnings": "fail over N"}, ["npm:eslint@9.1.0", "apt:jq"]))
        for bad, needle in (("no colon here\n", "A line is not `key: value`"), ("bogus: 1\n", "Unknown field")):
            with self.assertRaises(ClientError) as cm:
                off.load_spec(bad, "my.spec")
            self.assertIn(needle, cm.exception.title)
            self.assertIn("my.spec line 1", str(cm.exception.facts))                       # where it is is a labelled row, not part of the title

    def test_the_required_details_are_named_and_the_fix_is_a_command_to_copy(self):
        with self.assertRaises(ClientError) as cm:
            off.build_profile_job(["eslint", "."], None, "fast", {"est_time_s": 5, "est_mem_mb": None}, retry_example=lambda extra: "ct-run offload profile --name lint " + extra)
        e = cm.exception
        self.assertEqual(e.title, "This profiling run is missing required details")
        self.assertEqual([s for s in e.steps if s.startswith("Missing")][0][:28], "Missing: --what  \"what the c")
        self.assertEqual(len([s for s in e.steps if s.startswith("Missing")]), 2)
        self.assertEqual(e.example, "ct-run offload profile --name lint --what 'what the command does' --est-mem-mb 100")
        with self.assertRaises(ClientError):
            off.build_profile_job([], "w", "e", {"est_time_s": 1, "est_mem_mb": 1})

    def test_a_good_request_and_what_is_refused(self):
        job = off.build_profile_job(["ls", "-l"], "lists", "instant", {"est_time_s": 1, "est_mem_mb": 10, "est_cpu_cores": None}, flags={"-l": "long"},
                                    packages=["pip:b==1", "apt:a", "apt:a"], why="to check", timeout_s=60, files={"a.txt": "x"})
        self.assertEqual(job, {"class": "profile-run", "argv": ["ls", "-l"], "what": "lists", "expect": "instant", "packages": ["apt:a", "pip:b==1"], "why": "to check",
                               "est_time_s": 1, "est_mem_mb": 10, "flags": {"-l": "long"}, "timeout_s": 60, "files": {"a.txt": "x"}})
        for argv in (["sudo", "id"], ["/usr/bin/su", "x"], ["doas", "x"]):
            with self.assertRaises(ClientError, msg=str(argv)):
                off.build_profile_job(argv, "w", "e", {"est_time_s": 1, "est_mem_mb": 1})
        for bad in ("jq", "gem:rails", "apt:", "apt:a b", "apt:x; id"):
            with self.assertRaises(ClientError, msg=bad):
                off.build_profile_job(["x"], "w", "e", {"est_time_s": 1, "est_mem_mb": 1}, packages=[bad])
        self.assertEqual(off.parse_flags(["-n=null input", "--max=2=3"]), {"-n": "null input", "--max": "2=3"})
        with self.assertRaises(ClientError):
            off.parse_flags(["nothing"])

    def test_the_example_command_keeps_the_double_dash_part_intact(self):
        self.assertEqual(off.own_command_with(["offload", "profile", "--name", "x", "--", "ssh", "-V", "a b"], "--what 'w'"), "ct-run offload profile --name x --what 'w' -- ssh -V 'a b'")
        self.assertEqual(off.own_command_with(["offload", "profile"], ""), "ct-run offload profile")


class RegistryEditing(unittest.TestCase):
    def test_a_class_goes_at_the_end_of_the_classes_section(self):
        text = "limits:\n  files: {}\nclasses:\n  one:\n    run: [a]\n\n  two:\n    run: [b]\n\n# trailing comment\nprofiling:\n  x: 1\n"
        out = off.add_class_to_registry(text, ["  # note", "  three:", "    run: [c]"])
        self.assertEqual(out, "limits:\n  files: {}\nclasses:\n  one:\n    run: [a]\n\n  two:\n    run: [b]\n\n  # note\n  three:\n    run: [c]\n\n# trailing comment\nprofiling:\n  x: 1\n")
        self.assertTrue(off.add_class_to_registry("classes:\n", ["  x:", "    run: [c]"]).endswith("  x:\n    run: [c]\n"))
        with self.assertRaises(ClientError):
            off.add_class_to_registry("limits: {}\n", ["  x:"])

    def test_class_lines_carry_their_provenance_and_valid_values(self):
        pr = {"what": "Prints one", "reasons": ["1.0s, 40 MB"], "files": ["in.txt"]}
        lines = off.class_yaml_lines("my-ls", {"tier": "auto", "cost": "light", "run": ["/usr/bin/ls", "-l"], "timeout_s": 30, "accepts_files": True, "packages": ["apt:coreutils"]}, pr, "profile-abc123")
        self.assertEqual(lines[0], "  # Prints one")
        self.assertIn("  # profile run profile-abc123: 1.0s, 40 MB", lines)
        self.assertIn("  # profiled with input files: in.txt (the command expects files with these names)", lines)
        self.assertIn('    run: ["/usr/bin/ls", "-l"]', lines)
        self.assertIn("    accepts_files: true", lines)
        self.assertIn('    packages: ["apt:coreutils"]', lines)


class PackRecipe(unittest.TestCase):
    """List item 0b: an adopted class keeps how its files were packaged, so `ct-run submit CLASS` repeats the profiled invocation."""

    def test_a_recipe_is_relative_to_where_the_command_was_typed(self):
        cwd = Path(tempfile.mkdtemp()).resolve()
        (cwd / "src").mkdir()
        recipe, note = off.make_recipe(str(cwd / "src"), ["**/*.js"], ["notes.txt"], cwd)
        self.assertEqual(recipe, {"dir": "src", "include": ["**/*.js"], "files": ["notes.txt"]})
        self.assertIsNone(note)
        self.assertEqual(off.make_recipe(".", None, None, cwd)[0], {"dir": "."})

    def test_a_folder_outside_the_working_folder_is_not_saved_and_says_so(self):
        cwd = Path(tempfile.mkdtemp()).resolve()
        other = Path(tempfile.mkdtemp()).resolve()
        recipe, note = off.make_recipe(str(other), None, None, cwd)
        self.assertIsNone(recipe)
        self.assertIn("outside", note)

    def test_no_files_means_no_recipe(self):
        self.assertEqual(off.make_recipe(None, None, None, Path("."))[0], None)

    def test_the_pack_book_keeps_a_recipe_per_run(self):
        pb = off.PackBook(Path(tempfile.mkdtemp()))
        self.assertIsNone(pb.get("profile-aaa111"))
        pb.remember("profile-aaa111", {"dir": "src"})
        self.assertEqual(pb.get("profile-aaa111"), {"dir": "src"})

    def test_the_class_lines_carry_the_recipe_and_the_worker_ignores_it(self):
        entry = {"tier": "auto", "run": ["x"], "accepts_files": True, "submit": {"dir": "src", "include": ["*.js"]}}
        lines = off.class_yaml_lines("lint", entry, {"reasons": ["ok"]}, "profile-aaa111")
        self.assertIn('    submit: {"dir": "src", "include": ["*.js"]}', lines)

    def test_a_proposal_adds_the_recipe_only_to_a_class_that_takes_files(self):
        pb = off.PackBook(Path(tempfile.mkdtemp()))
        pb.remember("profile-aaa111", {"dir": "src"})
        self.assertEqual(off.with_recipe({"accepts_files": True}, "profile-aaa111", pb), {"accepts_files": True, "submit": {"dir": "src"}})
        self.assertNotIn("submit", off.with_recipe({}, "profile-aaa111", pb))
        self.assertNotIn("submit", off.with_recipe({"accepts_files": True}, "profile-zzz999", pb))

    def test_a_submit_uses_the_saved_recipe_only_when_nothing_was_typed(self):
        cwd = Path(tempfile.mkdtemp()).resolve()
        r = {"dir": "src", "include": ["*.js"], "files": ["a.txt"]}
        d, f, inc, exc, used = off.use_recipe(r, None, None, None, cwd)
        self.assertEqual((d, f, inc, exc, used), (str(cwd / "src"), [str(cwd / "a.txt")], ["*.js"], None, True))
        self.assertEqual(off.use_recipe(r, "other", None, None, cwd), ("other", None, None, None, False))
        self.assertEqual(off.use_recipe(r, None, ["x"], None, cwd)[4], False)
        self.assertEqual(off.use_recipe(None, None, None, None, cwd)[4], False)

    def test_an_unsafe_recipe_is_refused_not_followed(self):
        for bad in ({"dir": "/etc"}, {"dir": "../x"}, {"files": ["../../secret"]}):
            with self.assertRaises(ClientError) as cm:
                off.use_recipe(bad, None, None, None, Path("."))
            self.assertIn("not safe", str(cm.exception))


class NeedsLonger(unittest.TestCase):
    def test_the_rerun_command_replaces_the_old_limit_and_keeps_the_rest(self):
        raw = ["offload", "profile", "--what", "w", "--timeout-s", "300", "--dir", ".", "--", "env", "A=1", "python3", "-m", "unittest"]
        got = off.rerun_with_limit(raw, 600)
        self.assertIn("--timeout-s 600", got)
        self.assertNotIn("300", got)
        self.assertTrue(got.endswith("-- env A=1 python3 -m unittest"), got)
        self.assertIn("--dir .", got)
        self.assertIn("--timeout-s 90", off.rerun_with_limit(["offload", "profile", "--timeout-s=30", "--", "ls"], 90))

    def test_a_result_that_needs_longer_is_not_adopted_and_not_recorded_as_an_exclusion(self):
        pr = {"verdict": "needs-longer", "argv": ["x"], "reasons": ["stopped"], "limiting": ["time"], "next_limit_s": 600}
        with self.assertRaises(ClientError) as cm:
            off.propose(None, "profile-aaa111", {"profile": pr})
        self.assertIn("longer time limit", str(cm.exception))

    def test_the_result_card_offers_the_command_or_the_ceiling(self):
        from ct.client import view
        res = {"id": "profile-aaa111", "status": "ok", "profile": {"verdict": "needs-longer", "argv": ["x"], "reasons": ["stopped"], "limiting": ["time"], "next_limit_s": 600}}
        card = view.result_card(res, rerun="ct-run offload profile --timeout-s 600 -- x")
        self.assertEqual((card["run"], card["run_label"]), ("ct-run offload profile --timeout-s 600 -- x", "Profile it again with a limit of 600 s"))
        res["profile"].update(next_limit_s=None, at_ceiling=True, ceiling_s=600)
        card = view.result_card(res)
        self.assertIsNone(card["run"])
        self.assertTrue(any("isolation.purpose" in str(r) for r in card["head"]["details"]))


if __name__ == "__main__":
    unittest.main()
