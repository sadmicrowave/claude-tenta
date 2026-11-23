import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from ct import guard, jobs, registry, util  # noqa: E402

LIMITS = registry.limits_of({})


class Ids(unittest.TestCase):
    def test_ids(self):
        for ok in ("job-0001", "a" * 64, "profile-abc123"):
            self.assertEqual(jobs.check_id(ok), ok)
        for bad in ("abc", "A-job-1", "-job-001", "job_0001", "a" * 65, "../etc", "", None, 5):
            with self.assertRaises(jobs.Reject, msg=str(bad)):
                jobs.check_id(bad)
        with self.assertRaises(jobs.Reject):
            jobs.check_id("job-0001", filename_id="job-0002")


class FileNames(unittest.TestCase):
    CMD = {"accepts_files": True}
    AI = {"accepts_files": True, "kind": "claude"}
    LIM = {"files": {"max_count": 200, "max_file_kb": 500, "max_total_kb": 2000}}

    def test_a_name_is_checked_only_for_what_could_leave_the_job_folder(self):
        for ok in ("helper@.service.in", "a b.txt", "src/a+b/c=d,e.py", ".gitignore", ".tenta.toml", "dir/.hidden/x", "ünï.txt", "x" * 255):
            self.assertIsNone(jobs.file_name_problem(ok), ok)
        for bad, why in (("", "empty"), ("/etc/passwd", "relative"), ("../x", "relative"), ("a/../b", "relative"), ("a//b", "relative"), ("a/", "relative"), ("./a", "relative"),
                         ("a\\b", "backslash"), ("a\x00b", "control"), ("a\nb", "control"), ("a" * 256, "long"), ("a/" * 600, "long")):
            problem = jobs.file_name_problem(bad)
            self.assertEqual(problem[0], "files.name.path", repr(bad))
            self.assertIn(why, problem[1], repr(bad))

    def test_check_files_accepts_the_names_the_old_character_list_refused(self):
        files = {"templates/systemd/helper@.service.in": "x", ".gitignore": "y", "my notes.txt": "z"}
        self.assertEqual(jobs.check_files(files, self.CMD, self.LIM), files)

    def test_a_leading_dot_is_refused_only_for_a_class_whose_tool_reads_settings_from_its_folder(self):
        jobs.check_files({".claude/settings.json": "{}"}, self.CMD, self.LIM)               # a command class: just a file
        with self.assertRaises(jobs.Reject) as cm:
            jobs.check_files({"docs/.claude/settings.json": "{}"}, self.AI, self.LIM)
        self.assertEqual(cm.exception.code, "files.name.hidden")
        with self.assertRaises(jobs.Reject) as cm:
            jobs.check_files({"../x": "1"}, self.AI, self.LIM)
        self.assertEqual(cm.exception.code, "files.name.path")                              # the path rule still applies to every kind


class Age(unittest.TestCase):
    def test_an_expired_job_says_so_in_words_and_how_to_go_on(self):
        with self.assertRaises(jobs.Reject) as cm:
            jobs.check_age(self.job(timedelta(days=-2)))
        self.assertIn("more than 24 hours old", str(cm.exception))
        self.assertIn("send it again", str(cm.exception))

    def test_the_client_and_the_worker_use_one_definition_of_expired(self):
        self.assertTrue(jobs.expired(self.job(timedelta(days=-2))))
        self.assertFalse(jobs.expired(self.job(timedelta(hours=-23))))
        self.assertFalse(jobs.expired({}))                                             # no readable time: not called expired here (the worker rejects it for that reason)
        self.assertFalse(jobs.expired({"created": "yesterday"}))

    def job(self, delta):
        return {"created": util.iso(util.now() + delta)}

    def test_fresh_old_future_and_malformed(self):
        jobs.check_age(self.job(timedelta(seconds=-10)))
        jobs.check_age(self.job(timedelta(seconds=120)))                                  # a little clock skew is fine
        for delta, needle in ((timedelta(days=-2), "expired"), (timedelta(minutes=30), "future")):
            with self.assertRaises(jobs.Reject) as cm:
                jobs.check_age(self.job(delta))
            self.assertIn(needle, str(cm.exception))
        for bad, needle in (({}, "missing"), ({"created": "yesterday"}, "invalid"), ({"created": "2026-01-01T00:00:00"}, "timezone"),
                            ({"created": None}, "invalid"), ({"created": 5}, "invalid")):
            with self.assertRaises(jobs.Reject, msg=str(bad)) as cm:
                jobs.check_age(bad)
            self.assertIn(needle, str(cm.exception))

    def test_the_clock_and_maximum_age_are_injectable(self):
        fixed = util.parse_iso("2026-01-02T00:00:00Z")
        jobs.check_age({"created": "2026-01-01T23:00:00Z"}, max_age_s=7200, clock=lambda: fixed)
        with self.assertRaises(jobs.Reject):
            jobs.check_age({"created": "2026-01-01T20:00:00Z"}, max_age_s=7200, clock=lambda: fixed)


class Params(unittest.TestCase):
    cls = {"run": ["tool", "--n", "{n}", "{mode}", "{host}"],
           "params": {"n": {"int": [1, 10]}, "mode": {"enum": ["a", "b"]}, "host": {"regex": r"[a-z]{1,10}", "optional": True}}}

    def test_a_valid_job_builds_an_argv_list(self):
        self.assertEqual(jobs.build_argv(self.cls, {"n": 3, "mode": "a", "host": "abc"}), ["tool", "--n", "3", "a", "abc"])
        self.assertEqual(jobs.build_argv(self.cls, {"n": 3, "mode": "b"}), ["tool", "--n", "3", "b"])        # optional placeholder dropped

    def test_nothing_unvalidated_reaches_the_command(self):
        bad = [{"n": 11, "mode": "a"}, {"n": 0, "mode": "a"}, {"n": True, "mode": "a"}, {"n": "3", "mode": "a"}, {"n": 3.0, "mode": "a"},
               {"n": 3, "mode": "c"}, {"n": 3, "mode": "a", "host": "x; id"}, {"n": 3, "mode": "a", "host": "ABC"},
               {"n": 3, "mode": "a", "host": "abc\n"}, {"n": 3}, {"mode": "a"}, {"n": 3, "mode": "a", "extra": 1}]
        for params in bad:
            with self.assertRaises(ValueError, msg=str(params)):
                jobs.build_argv(self.cls, params)
        with self.assertRaises(ValueError):
            jobs.build_argv(self.cls, ["not", "an", "object"])

    def test_the_regex_must_match_the_whole_value(self):
        with self.assertRaises(ValueError):
            jobs.check_param("p", {"regex": r"[0-9]+"}, "123abc")
        with self.assertRaises(ValueError):
            jobs.check_param("p", {}, "x")                                             # a spec with no type is a registry bug, refused


class Files(unittest.TestCase):
    cls = {"accepts_files": True}

    def test_limits_follow_the_registry(self):
        jobs.check_files({f"f{i}.txt": "x" for i in range(200)}, self.cls, LIMITS)
        with self.assertRaises(ValueError):
            jobs.check_files({f"f{i}.txt": "x" for i in range(201)}, self.cls, LIMITS)
        jobs.check_files({"big.txt": "x" * 500_000}, self.cls, LIMITS)
        with self.assertRaises(ValueError):
            jobs.check_files({"big.txt": "x" * 500_001}, self.cls, LIMITS)
        with self.assertRaises(ValueError):
            jobs.check_files({f"f{i}.txt": "x" * 450_000 for i in range(5)}, self.cls, LIMITS)             # over the 2 MB total
        small = registry.limits_of({"limits": {"files": {"max_count": 2, "max_file_kb": 1}}})
        with self.assertRaises(ValueError):
            jobs.check_files({"a": "x", "b": "x", "c": "x"}, self.cls, small)
        with self.assertRaises(ValueError):
            jobs.check_files({"a": "x" * 1001}, self.cls, small)

    def test_names_cannot_escape_or_plant_settings(self):
        for bad in ("../x", "/etc/passwd", "a/../b", "a//b", "", "x" * 1100, "a\n"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                jobs.check_files({bad: "x"}, self.cls, LIMITS)
        for ok in ("a.txt", "dir/sub/file-1_2.js", "_x", "a.b.c", ".claude/settings.json", ".hidden", "a b", "a;b", "dir/.dot", "x" * 201):           # (the old character list refused the last six: see FileNames)
            jobs.check_files({ok: "x"}, self.cls, LIMITS)

    def test_content_must_be_text_and_the_class_must_accept_files(self):
        with self.assertRaises(ValueError):
            jobs.check_files({"a": b"bytes"}, self.cls, LIMITS)
        with self.assertRaises(ValueError):
            jobs.check_files({"a": "x"}, {}, LIMITS)
        with self.assertRaises(ValueError):
            jobs.check_files(["a"], self.cls, LIMITS)
        self.assertEqual(jobs.check_files(None, {}, LIMITS), {})


class Timeouts(unittest.TestCase):
    def test_defaults_and_profile_caps(self):
        self.assertEqual(jobs.job_timeout({}, {}), 60)
        self.assertEqual(jobs.job_timeout({"kind": "claude"}, {}), 600)
        self.assertEqual(jobs.job_timeout({"timeout_s": 5}, {"timeout_s": 999}), 5)             # a job cannot raise a class's timeout
        prof = {"kind": "profile", "max_timeout_s": 300}
        self.assertEqual(jobs.job_timeout(prof, {}), 300)
        self.assertEqual(jobs.job_timeout(prof, {"timeout_s": 60}), 60)
        for bad in (5, 301, True, "60"):
            self.assertEqual(jobs.job_timeout(prof, {"timeout_s": bad}), 300)


class Profile(unittest.TestCase):
    cls = {"kind": "profile", "accepts_files": True, "max_timeout_s": 600}
    good = {"argv": ["jq", "-n", "1"], "what": "prints one", "expect": "instant", "est_time_s": 1, "est_mem_mb": 10}

    def build(self, **over):
        job = {**self.good, **over}
        return jobs.build_profile(job, self.cls, LIMITS, find_program=lambda n: n in ("jq", "ls"), is_executable=lambda p: p == "/usr/bin/ok")

    def test_a_good_request(self):
        argv, files, meta = self.build(flags={"-n": "no input"}, why="to check", packages=["apt:jq"])
        self.assertEqual((argv, files), (["jq", "-n", "1"], {}))
        self.assertEqual(meta["estimates"], {"time_s": 1, "mem_mb": 10})
        self.assertEqual((meta["flags"], meta["why"], meta["packages"]), ({"-n": "no input"}, "to check", ["apt:jq"]))

    def test_the_command_must_exist_or_come_from_a_package(self):
        self.assertEqual(self.build(argv=["ls"])[0], ["ls"])
        with self.assertRaises(ValueError) as cm:
            self.build(argv=["nope"])
        self.assertIn("packages", str(cm.exception))
        self.build(argv=["nope"], packages=["apt:nope"])                                        # a package may provide it
        self.assertEqual(self.build(argv=["/usr/bin/ok", "x"])[0], ["/usr/bin/ok", "x"])
        for bad in (["/usr/bin/missing"], ["relative/path"], ["./x"]):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.build(argv=bad)
        with self.assertRaises(ValueError):
            self.build(argv=["/usr/bin/ok"], packages=["apt:jq"])                               # with packages, give the command by name

    def test_privilege_tools_are_refused_however_they_are_spelled(self):
        for name in ("sudo", "su", "doas", "pkexec", "/usr/bin/sudo"):
            with self.assertRaises(ValueError, msg=name):
                self.build(argv=[name, "id"], packages=["apt:x"])

    def test_argv_shape_and_required_descriptions(self):
        for bad in ([], [""], ["a"] * 41, "ls", ["ls", 5], ["ls", "a\0b"], ["ls", "x" * 501]):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.build(argv=bad)
        for field in ("what", "expect", "est_time_s", "est_mem_mb"):
            job = {k: v for k, v in self.good.items() if k != field}
            with self.assertRaises(ValueError, msg=field):
                jobs.build_profile(job, self.cls, LIMITS, find_program=lambda n: True)
        for bad in ({"what": "  "}, {"what": "x" * 401}, {"why": "x" * 301}, {"est_time_s": -1}, {"est_mem_mb": True}, {"est_cpu_cores": "2"},
                    {"flags": {"a": 5}}, {"flags": {f"-{i}": "x" for i in range(21)}}, {"timeout_s": 5}, {"timeout_s": 601}, {"timeout_s": "60"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self.build(**bad)

    def test_files_follow_the_class_and_limits(self):
        self.assertEqual(self.build(files={"in.txt": "x"})[1], {"in.txt": "x"})
        with self.assertRaises(ValueError):
            self.build(files={"../x": "x"})


class Scrub(unittest.TestCase):
    def test_secrets_are_redacted_and_text_is_capped(self):
        token = "ghp" + "_" + "a" * 20
        out = guard.scrub(f"key {token} and sk-ant-" + "api03-abcdef end", 1000)
        self.assertEqual(out, "key [redacted] and [redacted] end")
        out = guard.scrub("x" * 50, 10)
        self.assertEqual(out, "x" * 10 + "\n[truncated]")
        self.assertEqual(guard.scrub(None, 10), "")


if __name__ == "__main__":
    unittest.main()
