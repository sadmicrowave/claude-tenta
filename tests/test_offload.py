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
from ct.client import offload as off  # noqa: E402
from ct.client.repo import ClientError  # noqa: E402
from support import RepoFixture, sh  # noqa: E402

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


class EndToEnd(RepoFixture):
    def push_main_registry(self, text="classes:\n  existing-one:\n    tier: auto\n    cost: light\n    run: [\"/bin/true\"]\n"):
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "-B", "main", "origin/main", cwd=req)
        (req / "registry.yaml").write_text(text)
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "registry", cwd=req)
        sh("git", "push", "-q", "origin", "main", cwd=req)

    def profile(self, *extra, argv=("ls", "-l")):
        args = ["offload", "profile", "--what", "lists files", "--expect", "instant", "--est-time-s", "1", "--est-mem-mb", "10", *extra, "--", *argv]
        return self.run_cli(*args)

    def run_profile_to_a_result(self, *extra):
        code, out, err = self.profile(*extra)
        self.assertEqual(code, 0, out + err)
        jid = out.strip()
        self.w.fetch()
        self.assertEqual(self.result(jid)["status"], "awaiting_approval")
        self.assertEqual(self.w.approve(jid), (True, ""))
        self.w.fetch()
        self.w.execute(jid)
        return jid

    def test_fit_answers_registered_unknown_excluded_and_retry_with_the_right_exit_codes(self):
        self.w.fetch()                                                                              # publishes the registry (classes and RAM)
        code, out, err = self.run_cli("offload", "fit", "--", "/bin/echo", "hello")
        self.assertEqual(code, 0)
        self.assertIn("REGISTERED as class echo (light, tier auto): ct-run submit echo --wait", out)
        self.assertEqual(self.run_cli("offload", "fit", "--", "nosuchtool")[0], 4)
        req = self.tmp / "requester"
        sh("git", "checkout", "-q", "--orphan", "data", cwd=req)
        sh("git", "rm", "-rfq", ".", cwd=req)
        entry = {"argv": ["bigtool"], "reasons": ["needs 6000 MB"], "limiting": ["memory"], "profiled": {"mem_total_mb": 4000}}
        (req / "exclusions.json").write_text(json.dumps({"excluded": [entry, dict(entry, argv=["slowtool"], limiting=["time"], reasons=["too slow"])]}))
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", "exclusions", cwd=req)
        sh("git", "push", "-q", "origin", "data", cwd=req)
        code, out, err = self.run_cli("offload", "fit", "--", "slowtool")
        self.assertEqual(code, 3)
        self.assertIn("EXCLUDED: too slow. Do not offload this", out)
        code, out, err = self.run_cli("offload", "fit", "--", "bigtool")
        self.assertEqual(code, 5)                                                                   # the worker now has 8000 MB, it had 4000
        self.assertIn("it has 8000 mb now", out.lower())
        self.assertEqual(self.run_cli("offload", "fit")[0], 2)                                      # no command given

    def test_every_check_is_in_the_history(self):
        self.w.fetch()
        self.run_cli("offload", "fit", "--", "/bin/echo", "hello")
        self.run_cli("offload", "fit", "--", "nosuchtool")
        self.run_cli("offload", "fit", "--", "nosuchtool")
        code, out, err = self.run_cli("offload", "history")
        self.assertEqual(code, 0)
        for needle in ("Every check of whether a command suits testct", "3 checks", "(1 registered, 2 unknown)", "Worth a look", "2x nosuchtool", "echo", "nosuchtool"):
            self.assertIn(needle, out)
        rows = json.loads(self.run_cli("offload", "history", "--json", "--verdict", "unknown")[1])
        self.assertEqual([r["command"] for r in rows], ["nosuchtool", "nosuchtool"])
        self.assertEqual(len(json.loads(self.run_cli("offload", "history", "--json", "-n", "1")[1])), 1)
        self.assertIn("No fit checks recorded for that filter", "".join(self.run_cli("offload", "history", "--command", "zzz")[1:]))
        self.assertIn("Removed", "".join(self.run_cli("offload", "history", "--clear")[1:]))
        self.assertIn("No fit checks recorded yet", "".join(self.run_cli("offload", "history")[1:]))

    def test_profile_to_a_proposal_that_the_registry_accepts(self):
        self.push_main_registry()
        jid = self.run_profile_to_a_result("--name", "my-ls")
        code, out, err = self.run_cli("result", jid)
        self.assertIn("VERDICT  fits", err)
        self.assertIn(f"tct-admin adopt {jid} --name my-ls", err)                                        # the remembered name is the suggestion
        code, out, err = self.run_cli("offload", "propose", jid, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Would register a class", out + err)
        self.assertIn("my-ls", out + err)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("registry/my-ls"))                         # a preview pushes nothing
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 0)
        self.assertIn("Proposed a class", out + err)
        self.assertIn("my-ls", out + err)
        self.assertIn("tct-admin apply registry/my-ls", out + err)
        text = sh("git", "show", "registry/my-ls:registry.yaml", cwd=self.tmp / "queue.git")
        self.assertIn("my-ls:", text)
        self.assertIn("existing-one:", text)
        import yaml
        reg = yaml.safe_load(text)
        self.assertEqual(registry.validate(reg, "/opt/x/tools"), [])                                # a proposal is always a valid registry
        self.assertEqual(reg["classes"]["my-ls"]["tier"], "auto")
        self.assertEqual(reg["classes"]["my-ls"]["run"][1:], ["-l"])
        self.assertIn("feat(registry): add my-ls class", sh("git", "log", "-1", "--format=%s", "registry/my-ls", cwd=self.tmp / "queue.git"))
        again = self.run_cli("offload", "propose", jid)
        self.assertEqual(again[0], 2)
        self.assertIn("already waiting", again[1] + again[2])
        self.assertEqual(sh("git", "rev-parse", "main", cwd=self.tmp / "queue.git"), sh("git", "rev-parse", "origin/main", cwd=self.repos.queue))   # main is untouched

    def test_the_tier_and_batchable_options(self):
        self.push_main_registry()
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        jid = self.run_profile_to_a_result("--name", "files-ls", "--dir", str(d))
        code, out, err = self.run_cli("offload", "propose", jid, "--tier", "approve", "--batchable")
        self.assertEqual(code, 0, out + err)
        import yaml
        entry = yaml.safe_load(sh("git", "show", "registry/files-ls:registry.yaml", cwd=self.tmp / "queue.git"))["classes"]["files-ls"]
        self.assertEqual((entry["tier"], entry["batchable"], entry["accepts_files"]), ("approve", True, True))
        no_files = self.run_profile_to_a_result("--name", "plain-ls")
        code, out, err = self.run_cli("offload", "propose", no_files, "--batchable")
        self.assertEqual(code, 2)
        self.assertIn("--batchable only makes sense", out + err)

    def test_a_command_that_does_not_fit_becomes_an_exclusion_and_is_never_offered_again(self):
        self.push_main_registry()
        self.runner.reply = ("ok", 0, "x", {"wall_s": 2.0, "peak_rss_mb": 6000, "mem_total_mb": 8000, "cores_busy": 1.0, "peak_temp_c": 50, "disk_mb": 0}, "")
        jid = self.run_profile_to_a_result()
        code, out, err = self.run_cli("result", jid)
        self.assertIn("VERDICT  does-not-fit", err)
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 0)
        self.assertIn("Recorded: this command does not fit", out + err)
        self.assertIn("testct", out + err)
        self.assertIn("Claude will not offload it again", out + err)
        data = json.loads(sh("git", "show", "data:exclusions.json", cwd=self.tmp / "queue.git"))
        (e,) = data["excluded"]
        self.assertEqual((e["argv"], e["limiting"], e["profiled"]["job"], e["profiled"]["mem_total_mb"]), (["ls", "-l"], ["memory"], jid, 8000))
        code, out, err = self.run_cli("offload", "fit", "--", "ls", "-l")
        self.assertEqual(code, 3)
        self.assertIn("EXCLUDED", out)
        code, out, err = self.run_cli("offload", "exclusions")
        self.assertIn("ls -l", out)
        self.assertRegex(out, r"Limited by\s+memory")
        self.assertIn(f"(job {jid})", out)
        code, out, err = self.run_cli("offload", "profile", "--what", "w", "--expect", "e", "--est-time-s", "1", "--est-mem-mb", "1", "--", "ls", "-l")   # asking again is refused...
        self.assertEqual(code, 1)
        self.assertIn("EXCLUDED", out)
        code, out, err = self.profile("--retry")                                                    # ...unless you say it is worth trying again
        self.assertEqual(code, 0)

    def test_a_fitting_profile_lifts_an_old_exclusion(self):
        self.push_main_registry()
        self.runner.reply = ("ok", 0, "x", {"wall_s": 2.0, "peak_rss_mb": 6000, "mem_total_mb": 8000, "cores_busy": 1.0, "peak_temp_c": 50}, "")
        jid = self.run_profile_to_a_result()
        self.run_cli("offload", "propose", jid)
        self.runner.reply = ("ok", 0, "x", {"wall_s": 1.0, "peak_rss_mb": 40, "mem_total_mb": 16000, "cores_busy": 0.5, "peak_temp_c": 50}, "")
        self.w.io.publish({"id": "x", "status": "ok"})
        jid2 = self.run_profile_to_a_result("--retry")
        self.assertEqual(self.run_cli("offload", "propose", jid2, "--name", "ls-again")[0], 0)
        self.assertEqual(json.loads(sh("git", "show", "data:exclusions.json", cwd=self.tmp / "queue.git"))["excluded"], [])

    def test_an_inconclusive_run_proposes_nothing(self):
        self.push_main_registry()
        self.runner.reply = ("error", 2, "boom", {"wall_s": 1.0}, "")
        jid = self.run_profile_to_a_result()
        code, out, err = self.run_cli("offload", "propose", jid)
        self.assertEqual(code, 2)
        self.assertIn("the profiling run was inconclusive", out + err)

    def test_propose_needs_a_finished_profile_run(self):
        self.assertEqual(self.run_cli("offload", "propose", "profile-nosuch1")[0], 2)
        code, out, err = self.profile()
        jid = out.strip()
        self.w.fetch()
        code, out, err = self.run_cli("offload", "propose", jid)                                     # waiting for approval: no profile yet
        self.assertEqual(code, 2)
        self.assertIn("No profile result yet", out + err)

    def test_missing_details_stop_the_request_before_anything_is_pushed(self):
        code, out, err = self.run_cli("offload", "profile", "--what", "lists", "--", "ls", "-l")
        self.assertEqual(code, 2)
        self.assertIn("This profiling run is missing required details", out + err)
        self.assertIn("--est-time-s", out + err)
        self.assertIn("ct-run offload profile --what lists --expect 'a few seconds, tiny, prints one line' --est-time-s 10 --est-mem-mb 100 -- ls -l", out + err)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("jobs"))

    def test_the_approval_instructions_name_the_configured_admin_command(self):
        code, out, err = self.profile("--name", "my-ls", "--why", "to see")
        self.assertEqual(code, 0)
        jid = out.strip()
        for needle in ("This profiling run needs YOUR approval", "ls -l", jid, "my-ls", f"tct-admin approve {jid}", f"ct-run result {jid}"):
            self.assertIn(needle, err)
        self.assertEqual(self.ctx.names.get(jid), "my-ls")                                          # remembered for the job list and the proposal
        self.assertIn("my-ls", self.run_cli("jobs")[1])

    def test_a_taken_or_bad_name_is_refused_up_front(self):
        self.w.fetch()
        code, out, err = self.profile("--name", "echo")
        self.assertEqual(code, 2)
        self.assertIn("already installed or proposed", out + err)
        self.assertEqual(self.profile("--name", "Bad Name")[0], 2)

    def test_a_spec_file_fills_in_the_details(self):
        spec = Path(tempfile.mkdtemp()) / "ls.spec"
        spec.write_text("command: ls -l\nname: spec-ls\nwhat: lists files\nexpect: instant\nest-time-s: 1\nest-mem-mb: 10\nflag -l: long format\n")
        code, out, err = self.run_cli("offload", "profile", "--spec", str(spec))
        self.assertEqual(code, 0, out + err)
        jid = out.strip()
        self.repos.fetch_queue()
        job = json.loads(sh("git", "show", f"jobs:jobs/{jid}.json", cwd=self.tmp / "queue.git"))
        self.assertEqual((job["argv"], job["what"], job["flags"], job["est_time_s"]), (["ls", "-l"], "lists files", {"-l": "long format"}, 1.0))

    def test_profiling_a_command_that_is_already_registered_says_so_instead_of_queuing(self):
        self.w.fetch()
        code, out, err = self.run_cli("offload", "profile", "--what", "w", "--expect", "e", "--est-time-s", "1", "--est-mem-mb", "1", "--", "/bin/echo", "hello")
        self.assertEqual(code, 0)
        self.assertIn("REGISTERED as class echo", out)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("jobs"))

    def test_classes_shows_proposals(self):
        self.w.fetch()
        self.assertNotIn("Proposed, NOT installed yet", self.run_cli("classes")[1])                    # nothing proposed yet
        self.push_main_registry("classes:\n  echo:\n    tier: auto\n    cost: light\n    run: [\"/bin/echo\", \"{word}\"]\n"
                                "  not-yet-installed:\n    tier: auto\n    cost: light\n    run: [\"/bin/true\"]\n")
        code, out, err = self.run_cli("classes")
        self.assertEqual(code, 0)
        self.assertIn("Proposed, NOT installed yet", out)
        self.assertIn("not-yet-installed", out)
        self.assertNotIn("echo, not-yet", out)                                                     # echo is already installed, so it is not "proposed"


if __name__ == "__main__":
    unittest.main()
