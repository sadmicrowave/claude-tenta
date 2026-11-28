"""Part 1 of 2 of the test_client tests (FilesAndBatches, IdsAndJobs, Human, DirectPace, RoundTrip, ProgressLine, NoBackgroundSsh)."""
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct.client import submit, view  # noqa: E402
from ct.client.repo import ClientError  # noqa: E402


NOW = datetime(2026, 1, 2, 3, 0, 0, tzinfo=timezone.utc)


LIMITS = submit.file_limits(None)


class FilesAndBatches(unittest.TestCase):
    def test_globs(self):
        for rel, pat, want in (("src/a/b.js", "src/**/*.js", True), ("src/b.js", "src/**/*.js", True), ("lib/b.js", "src/**/*.js", False),
                               ("deep/x/y.py", "*.py", True), ("a.py", "src/*.py", False), ("src/a/b.js", "src/*.js", False), ("a.js", "?.js", True),
                               ("ab.js", "?.js", False), ("src/x.min.js", "src/**/*.js", True)):
            self.assertEqual(submit.glob_match(rel, pat), want, (rel, pat))

    def tree(self):
        d = Path(tempfile.mkdtemp())
        for rel, text in {"a.py": "x", "sub/b.py": "y", ".git/config": "no", ".git/HEAD": "no", ".env": "no", ".gitignore": "ig", "node_modules/m/i.js": "no", "dist/out.js": "no",
                          "big.txt": "x" * 2000, "bad name.txt": "ok", "tmpl/helper@.service.in": "t", "keep.md": "z"}.items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        (d / "binary.bin").write_bytes(b"\xff\xfe\x00")
        return d

    def test_collecting_sends_what_can_be_sent_and_accounts_for_every_file_it_does_not(self):
        d = self.tree()
        left = submit.LeftOut()
        files = submit.collect_dir(d, {**LIMITS, "max_file_kb": 1}, left=left)
        self.assertEqual(sorted(files), ["a.py", "bad name.txt", "keep.md", "sub/b.py", "tmpl/helper@.service.in"])         # a space and an @ are fine now
        self.assertEqual(sorted(left.excluded), [".env", ".git", ".gitignore", "dist", "node_modules"])                       # left out by rule, each named
        self.assertEqual(sorted((n, c) for n, c, _ in left.unsent), [("big.txt", "files.too_big"), ("binary.bin", "files.not_text")])
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, include=["*.py"])), ["a.py", "sub/b.py"])

    def test_for_a_windows_worker_names_it_would_refuse_are_left_out_here_and_named_with_the_reason(self):
        d = Path(tempfile.mkdtemp())
        for rel in ("ok.py", "con.txt", "sub/Nul.py", "COM1", "stream:x.txt", "dots.", "dir /f.txt"):
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left, windows=True)
        self.assertEqual(sorted(files), ["ok.py"])
        self.assertEqual(sorted(n for n, c, _ in left.unsent), ["COM1", "con.txt", "dir /f.txt", "dots.", "stream:x.txt", "sub/Nul.py"])
        self.assertTrue(all(c == "files.name.path" and "Windows" in why for _, c, why in left.unsent))
        plain = submit.collect_dir(d, LIMITS, left=submit.LeftOut())                          # a Linux or Mac worker: ordinary names
        self.assertIn("con.txt", plain)

    def test_the_workers_published_platform_decides_whether_windows_rules_apply(self):
        self.assertTrue(submit.worker_is_windows({"platform": "windows"}))
        for state in (None, {}, {"platform": "linux"}, {"platform": "macos"}):
            self.assertFalse(submit.worker_is_windows(state))

    def repo(self):
        import subprocess
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        for rel, text in {"a.py": "x", ".gitignore": "dist/\n*.log\n", ".tenta.toml": "t", "dist/out.js": "no", "x.log": "no", "new.py": "n", "sub/c.py": "c"}.items():
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        subprocess.run(["git", "add", "a.py", ".gitignore", ".tenta.toml", "sub/c.py"], cwd=d, check=True)
        return d

    def test_in_a_git_folder_the_projects_own_ignore_rules_decide_what_is_sent(self):
        d = self.repo()
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertEqual(sorted(files), [".gitignore", ".tenta.toml", "a.py", "new.py", "sub/c.py"])         # tracked, and untracked but not ignored; a tracked dotfile is the project's own
        self.assertEqual(sorted(left.excluded), ["dist", "x.log"])                                           # what the project's own rules ignore is named, never silent
        self.assertNotIn(".git", left.excluded)                                                              # git's own folder is never part of the project

    def test_a_project_that_is_not_a_git_folder_is_cut_by_its_languages_ignore_template(self):
        d = Path(tempfile.mkdtemp())
        for rel in ("pyproject.toml", "app.py", "__pycache__/x.pyc", ".venv/lib/z.py", "build/out.txt", ".DS_Store", ".gitignore", ".tenta.toml", "docs/readme.md", ".idea/workspace.xml"):
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertEqual(sorted(files), [".gitignore", ".tenta.toml", "app.py", "docs/readme.md", "pyproject.toml"])          # dotfiles the template does not ignore are the project's own
        self.assertEqual(sorted(left.excluded), [".DS_Store", ".idea", ".venv", "__pycache__", "build"])
        self.assertIn("Python", left.source)
        self.assertIn("not a git repository", left.source)

    def test_a_folder_with_no_recognisable_language_keeps_the_interim_rule(self):
        d = Path(tempfile.mkdtemp())
        for rel in ("notes.txt", ".hidden", "node_modules/m.js"):
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        left = submit.LeftOut()
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, left=left)), ["notes.txt"])
        self.assertEqual(sorted(left.excluded), [".hidden", "node_modules"])
        self.assertEqual(left.source, "")

    def test_the_templates_are_vendored_with_their_licence_and_every_named_one_exists(self):
        t = submit.TEMPLATES
        for lang in {n for n in submit.LANGUAGES}:
            self.assertGreater(len((t / "lang" / f"{lang}.gitignore").read_text()), 20, lang)
        for g in submit.GLOBALS:
            self.assertGreater(len((t / "global" / f"{g}.gitignore").read_text()), 5, g)
        self.assertIn("CC0", (t / "LICENSE").read_text())
        self.assertRegex((t / "SOURCE").read_text(), r"commit [0-9a-f]{40}")

    def test_a_link_is_never_followed_so_a_file_outside_the_project_is_never_sent(self):
        outside = Path(tempfile.mkdtemp())
        (outside / "secret.txt").write_text("OUTSIDE-SECRET")
        for git in (False, True):
            d = self.repo() if git else Path(tempfile.mkdtemp())
            (d / "real.txt").write_text("hello")
            (d / "notes.txt").symlink_to(outside / "secret.txt")                              # a link whose target is outside the folder
            (d / "linkdir").symlink_to(outside)                                                # and a link to a whole folder outside
            (d / "inside.txt").symlink_to(d / "real.txt")                                      # even a link that points at something inside is not followed
            left = submit.LeftOut()
            files = submit.collect_dir(d, LIMITS, left=left)
            self.assertNotIn("notes.txt", files, git)
            self.assertNotIn("inside.txt", files, git)
            self.assertFalse(any("OUTSIDE-SECRET" in v for v in files.values()), git)
            self.assertFalse(any(k.startswith("linkdir") for k in files), git)
            self.assertIn("real.txt", files)
            self.assertIn("notes.txt", [n for n, _, _ in left.links])                          # said, not silent
            self.assertTrue(all(c == "files.link.skipped" for _, c, _ in left.links))

    def test_git_is_run_with_its_command_options_switched_off_so_a_hostile_repository_cannot_run_one(self):
        import subprocess
        d = self.repo()
        marker = d.parent / (d.name + "-PWNED")
        hook = d.parent / (d.name + "-hook.sh")
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=d, check=True)
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertFalse(marker.exists(), "the repository's own core.fsmonitor ran a command")
        self.assertIn("a.py", files)                                                           # and the project's own ignore rules still decide what is sent
        self.assertEqual(sorted(left.excluded), ["dist", "x.log"])

    def test_a_file_that_usually_holds_a_secret_is_held_back_unless_an_include_names_it(self):
        d = Path(tempfile.mkdtemp())
        for rel in ("ok.py", "server.pem", "deploy/id_rsa", "creds/.npmrc", "app.env.example", "keys.tfstate"):
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertEqual(sorted(files), ["app.env.example", "ok.py"])
        self.assertEqual(sorted(n for n, _, _ in left.held), ["deploy/id_rsa", "keys.tfstate", "server.pem"])        # (.npmrc is hidden: left out by rule here)
        self.assertTrue(all(c == "files.held.secret_name" for _, c, _ in left.held))
        sent = submit.collect_dir(d, LIMITS, include=["server.pem", "ok.py"], left=submit.LeftOut())
        self.assertEqual(sorted(sent), ["ok.py", "server.pem"])                                                          # named exactly: the person chose it
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, include=["**/*"], left=submit.LeftOut())), ["app.env.example", "ok.py"])   # a wildcard never sends a held file

    def test_a_file_whose_text_looks_like_a_secret_is_sent_and_the_person_is_told(self):
        d = Path(tempfile.mkdtemp())
        (d / "notes.txt").write_text("token " + "gh" + "p_" + "a" * 24)
        (d / "plain.txt").write_text("hello")
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left)
        self.assertEqual(sorted(files), ["notes.txt", "plain.txt"])                                                      # not held: source and test files often hold examples
        self.assertEqual([(n, c) for n, c, _ in left.warn], [("notes.txt", "files.warn.secret_content")])

    def test_secret_looking_text_is_held_on_github_and_only_warned_on_direct(self):
        d = Path(tempfile.mkdtemp())
        (d / "notes.txt").write_text("token " + "gh" + "p_" + "a" * 24)
        (d / "plain.txt").write_text("hello")
        left = submit.LeftOut()
        files = submit.collect_dir(d, LIMITS, left=left, hold_secret_text=True)
        self.assertEqual(sorted(files), ["plain.txt"])
        self.assertEqual([(n, c) for n, c, _ in left.held], [("notes.txt", "files.held.secret_content")])
        self.assertEqual(left.warn, [])
        named = submit.collect_dir(d, LIMITS, include=["notes.txt", "plain.txt"], left=submit.LeftOut(), hold_secret_text=True)
        self.assertEqual(sorted(named), ["notes.txt", "plain.txt"])                                                      # named exactly: the person chose it
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, left=submit.LeftOut(), hold_secret_text=False)), ["notes.txt", "plain.txt"])

    def test_the_summary_totals_and_the_size_words(self):
        self.assertEqual(submit.totals({"a": "xy", "b": "é"}), (2, 4))
        self.assertEqual([submit.size_text(n) for n in (5, 1500, 2_500_000, 3_000_000_000)], ["5 bytes", "1.5 KB", "2.5 MB", "3.0 GB"])

    def test_a_hidden_file_or_folder_is_sent_when_the_include_names_it(self):
        d = self.tree()
        files = submit.collect_dir(d, LIMITS, include=[".gitignore", ".git/HEAD"], left=submit.LeftOut())
        self.assertEqual(sorted(files), [".git/HEAD", ".gitignore"])
        self.assertEqual(sorted(submit.collect_dir(d, LIMITS, include=["**/*.js"])), [])                                       # a wildcard does not reach into a skipped folder

    def test_a_job_that_would_run_on_an_incomplete_set_is_refused_unless_the_person_chooses(self):
        d = self.tree()
        left = submit.LeftOut()
        submit.collect_dir(d, {**LIMITS, "max_file_kb": 1}, left=left)
        with self.assertRaises(ClientError) as cm:
            submit.check_complete(left, allow=False)
        self.assertEqual(cm.exception.title, "Some files could not be sent")
        self.assertIn("--send-incomplete", " ".join(cm.exception.steps))
        self.assertEqual(cm.exception.facts[-1], ("Code", "files.incomplete"))
        submit.check_complete(left, allow=True)
        clean = submit.LeftOut()
        clean.exclude(".git")
        submit.check_complete(clean, allow=False)                                                                              # left out by rule is not incomplete

    def test_batches_follow_the_workers_published_limits(self):
        files = {f"f{i}.txt": "x" for i in range(120)}
        self.assertEqual(len(submit.plan_batches({"a": "x"}, False, LIMITS)), 1)
        info = {"limits": {"files": {"max_count": 50, "max_total_kb": 2000, "max_batches": 3}}}
        lim = submit.file_limits(info)
        self.assertEqual((lim["max_count"], lim["max_batches"], lim["max_file_kb"]), (50, 3, 100))     # unpublished keys keep the conservative default
        parts = submit.plan_batches(files, True, lim)
        self.assertEqual([len(p) for p in parts], [40, 40, 40])
        self.assertEqual(sorted(k for p in parts for k in p), sorted(files))                           # every file exactly once
        with self.assertRaises(ClientError) as cm:
            submit.plan_batches(files, False, lim)
        self.assertIn("needs its files together", cm.exception.why)
        with self.assertRaises(ClientError) as cm:
            submit.plan_batches({f"f{i}": "x" for i in range(200)}, True, lim)
        self.assertIn("would need 4 jobs", cm.exception.why)

    def test_a_huge_tree_is_never_split_into_many_jobs(self):
        with self.assertRaises(ClientError) as cm:
            submit.plan_batches({f"f{i}": "x" * 50_000 for i in range(10)}, True, submit.file_limits({"limits": {"files": {"max_total_kb": 100}}}))
        self.assertIn("never split", cm.exception.why)

    def test_old_defaults_apply_until_the_worker_publishes_its_limits(self):
        self.assertEqual(submit.file_limits(None), submit.OLD_FILE_LIMITS)
        self.assertEqual(submit.file_limits({"limits": {"files": {"bogus": 1}}}), submit.OLD_FILE_LIMITS)


class IdsAndJobs(unittest.TestCase):
    def test_ids_are_short_readable_and_unique(self):
        known = set()
        for _ in range(200):
            jid = submit.new_job_id("echo", known)
            self.assertRegex(jid, r"^echo-[2-9a-hj-km-np-z]{6}$")
            self.assertNotIn(jid, known)
            known.add(jid)
        self.assertEqual(len(submit.new_job_id("x" * 80)), 40 + 7)
        self.assertEqual(submit.job_ids("base", 1), ["base"])
        self.assertEqual(submit.job_ids("base", 3), ["base-b1", "base-b2", "base-b3"])

    def test_params_and_the_job_file(self):
        self.assertEqual(submit.parse_params(["n=3", "dir=src", "neg=-2", "ver=1.5", "eq=a=b"]), {"n": 3, "dir": "src", "neg": -2, "ver": "1.5", "eq": "a=b"})
        with self.assertRaises(ClientError):
            submit.parse_params(["nothing"])
        job = submit.stamp("job-0001", submit.build_job("echo", {"w": "hi"}, "a prompt", {"a.txt": "x"}), clock=lambda: NOW)
        self.assertEqual(job, {"id": "job-0001", "class": "echo", "params": {"w": "hi"}, "prompt": "a prompt", "files": {"a.txt": "x"}, "created": "2026-01-02T03:00:00Z"})
        self.assertNotIn("prompt", submit.build_job("echo"))


class Human(unittest.TestCase):
    def test_durations_and_ages(self):
        self.assertEqual([view.human_dur(s) for s in (45, 120, 840, 7500, 200000)], ["45 s", "2 min", "14 min", "2 h 05 min", "2 days 7 h"])
        self.assertEqual(view.human_age(None), "-")
        self.assertEqual([view.human_age(NOW - timedelta(seconds=s), NOW) for s in (10, 300, 7200, 200000)], ["just now", "5 min", "2 h 00 min", "2 days"])

    def test_phases(self):
        p = lambda res, **kw: view.phase_of(res, "job-1", "tct-admin", product="tct", **kw)[0]    # noqa: E731
        self.assertEqual(p(None), "sent: waiting for tct to notice it (an idle worker looks every 90 s)")
        self.assertEqual(p({"status": "queued"}), "queued on tct: waiting for a free slot")
        self.assertIn("2 min so far", p({"status": "queued"}, waited=130))
        self.assertIn("has not picked it up", p(None, waited=400))               # a job nobody picks up is no longer described as "within a minute"
        self.assertIn("tct-admin wake", p(None, waited=400))
        self.assertNotIn("has not picked it up", p(None, waited=130))            # a sleeping worker looks every 5 min: two minutes is not late
        self.assertEqual(p({"status": "awaiting_approval"}), "waiting for your approval (tct-admin approve job-1)")
        self.assertEqual(p({"status": "awaiting_approval"}, approved=True), "approved: waiting for tct to pick it up")
        self.assertEqual(p({"status": "running", "note": "fetching packages on tct"}), "fetching packages on tct")
        self.assertEqual(p({"status": "running"}), "running on tct")

    def test_the_job_list_orders_by_the_jobs_own_time_and_shows_waiting_jobs(self):
        results = {"a": {"id": "a", "status": "ok", "finished": "2026-01-01T00:00:00Z", "started": "2026-01-01T00:00:00Z"},
                   "b": {"id": "b", "status": "error", "finished": "2026-01-02T00:00:00Z", "note": "boom"},
                   "w": {"id": "w", "status": "awaiting_approval", "class": "careful"}}
        rows = view.status_rows(results, lambda jid: NOW if jid == "w" else None, None)
        self.assertEqual([r[2] for r in rows], ["w", "b", "a"])
        self.assertEqual(view.run_durations({"a": {"id": "a", "started": "2026-01-01T00:00:00Z", "finished": "2026-01-01T00:14:00Z", "status": "ok"},
                                             "b": {"id": "b", "status": "awaiting_approval"}}), {"a": "14 min"})

    def test_a_job_waiting_for_approval_for_more_than_a_day_is_listed_as_expired_not_waiting(self):
        results = {"w": {"id": "w", "status": "awaiting_approval", "class": "careful"}, "f": {"id": "f", "status": "awaiting_approval", "class": "careful"}}
        created = {"w": NOW - timedelta(hours=30), "f": NOW - timedelta(hours=2)}
        rows = view.status_rows(results, lambda jid: created[jid], None, now=NOW)
        status = {r[2]: r[1] for r in rows}
        self.assertEqual(status, {"w": "expired", "f": "awaiting_approval"})
        self.assertIn("send it again", [r for r in rows if r[2] == "w"][0][3])
        out = io.StringIO()
        view.render_status(rows, results, lambda jid, cls: cls, [], "ct-admin", out=out, now=NOW)
        self.assertIn("ct-admin approve f", out.getvalue())                              # the fresh one is still offered
        self.assertNotIn("ct-admin approve w", out.getvalue())                            # no command offered for a job that can no longer be approved

    def test_a_job_the_worker_has_not_picked_up_is_a_queued_row_with_a_nudge_after_five_minutes(self):
        listing = [("new", {"created": "2026-01-02T02:58:00Z", "class": "echo"}), ("stuck", {"created": "2026-01-02T02:00:00Z", "class": "echo"}),
                   ("old", {"created": "2025-12-01T00:00:00Z"}), ("known", {"created": "2026-01-02T02:59:00Z"})]
        rows = view.queued_rows({"known"}, listing, at=NOW, admin="tct-admin")
        self.assertEqual([r[2] for r in rows], ["new", "stuck"])                                  # an old one with no result is not shown as queued
        self.assertIn("looks about once a minute", rows[0][3])
        self.assertIn("has not picked this up in 60 min", rows[1][3])
        self.assertIn("tct-admin check", rows[1][3])

    def test_render_status_and_classes(self):
        out = io.StringIO()
        results = {"w": {"id": "w", "status": "awaiting_approval", "class": "careful"}, "a": {"id": "a", "status": "ok", "class": "echo", "finished": "2026-01-01T00:00:00Z"}}
        rows = view.status_rows(results, lambda jid: NOW, None, now=NOW)
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            view.render_status(rows, results, lambda jid, cls: cls or "-", [("on your computer", "tct-admin approve {}"), ("on tct", "tct approve {}")], "tct-admin", out=out, now=NOW)
        text = out.getvalue()
        for needle in ("Your most recent jobs, newest first", "Waiting for you", "tct-admin approve w", "echo"):
            self.assertIn(needle, text)
        self.assertNotIn("macbook", text)
        out = io.StringIO()
        info = {"updated": "2026-01-02T00:00:00Z", "mem_total_mb": 8000, "capacity": 4,
                "classes": {"echo": {"kind": "command", "cost": "light", "tier": "auto", "run": ["/bin/echo", "{word}"], "params": ["word"]},
                            "profile-run": {"kind": "profile", "cost": "heavy", "tier": "approve"}}}
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            view.render_classes(info, pending=["newclass"], excluded=2, out=out, product="tct")
        text = out.getvalue()
        for needle in ("What this worker can run for you today", "tct", "echo", "/bin/echo {word}", "word", "profile one exact command", "Proposed, NOT installed yet", "newclass", "Excluded after profiling: 2"):
            self.assertIn(needle, text)

    def test_result_formatting_for_a_profile_run_shows_verdict_and_estimate_comparison(self):
        res = {"id": "profile-abc123", "status": "ok", "output": "done", "note": "", "metrics": {"wall_s": 3.0, "cpu_s": 2.5, "peak_rss_mb": 300, "mem_total_mb": 8000, "peak_temp_c": 60, "disk_mb": 1},
               "profile": {"argv": ["ls", "-l"], "what": "lists", "verdict": "fits", "cost": "light", "reasons": ["3.0s, 300 MB"], "estimates": {"time_s": 1, "mem_mb": 100}}}
        with mock.patch.dict(os.environ, {"CT_COLOR": "never"}):
            head, output, tail = view.format_result(res, admin="tct-admin", suggest_name="ls-long")
        text = "\n".join(head + tail)
        for needle in ("JOB     profile-abc123", "STATUS  ok", "Command", "ls -l", "MEASURED", "VERDICT  fits   cost light", "x3.0 over the estimate",
                       "tct-admin adopt profile-abc123 --name ls-long"):
            self.assertIn(needle, text)
        self.assertEqual(output, "done")


class DirectPace(unittest.TestCase):
    def test_what_a_person_is_told_to_expect_matches_the_pace_of_the_connection(self):
        from ct import config
        cfg = config.Config(config.validate({"server": {"admin_user": "me", "host": "h"}, "github": {"owner": "o"}, "transport": {"kind": "direct"}})[0])
        try:
            view.set_pace(cfg.schedule, direct=True)
            self.assertNotIn("90 s", view.pace_text())
            self.assertIn("every few seconds", view.pace_text())
            view.set_pace(cfg.schedule)
            self.assertIn("90 s", view.pace_text())
        finally:
            view.set_pace(cfg.schedule)


class RoundTrip(unittest.TestCase):
    RES = {"id": "j", "status": "ok", "created": "2026-11-17T04:49:00Z", "started": "2026-11-17T04:49:30Z", "finished": "2026-11-17T04:49:36Z",
           "metrics": {"wall_s": 0.02, "cpu_s": 0.01, "peak_rss_mb": 0, "mem_total_mb": 15219, "peak_temp_c": 52, "disk_mb": 0.0}}

    SEEN = datetime(2026, 11, 17, 4, 49, 39, tzinfo=timezone.utc)          # three seconds after the worker published it

    def test_the_round_trip_ends_when_the_result_is_in_hand_not_when_the_worker_finished(self):
        self.assertEqual(view.round_trip(self.RES, self.SEEN), (39, 30, 6, 3, None))
        tail = "\n".join(view.format_result(self.RES, received=self.SEEN)[2])
        for needle in ("ROUND TRIP", "total", "39 s  from sending the job to having its result here", "waiting", "30 s  until the worker started it", "running", "6 s  on the worker", "coming back", "3 s  the result published"):
            self.assertIn(needle, tail)

    def test_a_worker_that_says_when_it_saw_the_job_splits_the_waiting_into_noticing_and_starting(self):
        res = {**self.RES, "seen": "2026-11-17T04:49:25Z"}
        self.assertEqual(view.round_trip(res, self.SEEN), (39, 30, 6, 3, 25))
        rows = dict(view.result_card(res, received=self.SEEN)["meta"])["Round trip"]
        labels = [r[0] for r in rows]
        self.assertIn("Noticing", labels)
        self.assertIn("Starting", labels)
        self.assertNotIn("Waiting", labels)
        self.assertIn("25 s  until the worker's next look at GitHub found it", [r[1] for r in rows])
        self.assertIn("5 s  your approval, a free slot, and the next start pass", [r[1] for r in rows])

    def test_the_card_says_the_same(self):
        rows = dict(view.result_card(self.RES, received=self.SEEN)["meta"])["Round trip"]
        self.assertEqual([r[0] for r in rows], ["Total", "Waiting", "Running", "Coming back"])
        self.assertIn(("Total", "39 s  from sending the job to having its result here"), rows)

    def test_without_a_moment_of_receiving_it_is_measured_to_now(self):
        self.assertGreater(view.round_trip(self.RES)[0], 36)               # the sample result is long past: the trip is as long as it has been since it was sent

    def test_an_older_result_without_a_send_time_just_leaves_the_row_out(self):
        res = {k: v for k, v in self.RES.items() if k != "created"}
        self.assertIsNone(view.round_trip(res, self.SEEN))
        self.assertNotIn("round trip", "\n".join(view.format_result(res, received=self.SEEN)[2]))

    def test_two_clocks_that_disagree_never_give_a_negative_time(self):
        self.assertEqual(view.round_trip({**self.RES, "created": "2026-11-17T04:50:00Z"}, self.SEEN)[0], 0)


class ProgressLine(unittest.TestCase):
    def test_the_live_line_is_one_line_cut_with_a_mark_never_wrapped(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
            def fileno(self):
                raise OSError
        out = Tty()
        with mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((60, 24))), mock.patch.dict(os.environ, {"CT_COLOR": "never"}) if False else mock.patch.dict(os.environ, {}):
            p = view.Progress(stream=out, clock=lambda: 0)
            p.draw("sent: waiting for a worker to notice it " * 4, view.YELLOW)
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", out.getvalue())
        self.assertLessEqual(len(line), 59)
        self.assertIn("…", line)
        self.assertTrue(line.endswith("0:00"))


class NoBackgroundSsh(unittest.TestCase):
    """A shared ssh connection (ControlMaster) leaves a background process holding the captured output pipes of the git command, so every pass or fetch hangs until it exits.
    The end-to-end test found this on a real machine; keep it out."""
    def test_nothing_in_the_package_starts_a_background_ssh_master_for_git(self):
        src = Path(__file__).resolve().parents[1] / "src" / "ct"
        for f in src.rglob("*.py"):
            text = f.read_text()
            if ("ControlMaster auto" in text or "ControlMaster=auto" in text) and f.name != "ssh.py":                                  # admin/ssh.py's own use is for interactive commands, not captured git output
                self.fail(f"{f.relative_to(src)} turns on ssh connection sharing")


if __name__ == "__main__":
    unittest.main()
