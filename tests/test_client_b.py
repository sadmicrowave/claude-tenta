"""Part 2 of 2 of the test_client tests (Repo, Prune, EndToEnd, Waiting, Config, WaitingChip, FasterPolling)."""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ct.client import cli, submit, view  # noqa: E402
from ct.client.repo import ClientError, ClientRepos  # noqa: E402
from support import RepoFixture, sh  # noqa: E402


class Repo(RepoFixture):
    def test_clone_is_idempotent(self):
        self.assertEqual([w for w, _ in self.repos.clone_all(self.home)], ["exists", "exists"])
        self.assertTrue(self.repos.is_set_up())

    def test_a_clone_that_is_already_there_follows_the_address_the_configuration_now_says(self):
        """Seen on a real machine: set up again for another connection, the clones kept the old address, so jobs were sent and read the old way."""
        import subprocess
        new = self.tmp / "elsewhere"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repos.queue_url), str(new)], check=True)
        moved = ClientRepos(self.repos.queue, self.repos.results, str(new), self.repos.results_url)
        self.assertEqual([w for w, _ in moved.clone_all(self.home)], ["repointed", "exists"])
        got = subprocess.run(["git", "-C", str(self.repos.queue), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(got, str(new))
        self.assertEqual([w for w, _ in moved.clone_all(self.home)], ["exists", "exists"])           # and a second run changes nothing

    def test_a_bad_address_is_explained(self):
        bad = ClientRepos(self.tmp / "x" / "queue", self.tmp / "x" / "results", str(self.tmp / "nope.git"), str(self.tmp / "nope2.git"))
        with self.assertRaises(ClientError) as cm:
            bad.clone_all(self.tmp / "x")
        self.assertEqual(cm.exception.title, "Could not clone a repository")
        self.assertIn("GitHub", str(cm.exception.facts))
        self.assertIn("ssh -T git@github.com", cm.exception.steps[0])
        with self.assertRaises(ClientError):
            bad.need_setup()

    def test_jobs_are_pushed_with_plumbing_to_an_orphan_branch_then_on_top_of_it(self):
        (self.repos.queue / "scratch.txt").write_text("half-edited, never committed")
        c1 = self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job: a")
        c2 = self.repos.push_files("jobs", {"jobs/b.json": "{}\n"}, "job: b")
        log = sh("git", "log", "--format=%h|%an|%s", "jobs", cwd=self.tmp / "queue.git").splitlines()
        self.assertEqual([l.split("|")[1:] for l in log], [["ct-client", "job: b"], ["ct-client", "job: a"]])
        self.assertEqual(sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git").splitlines(), ["jobs/a.json", "jobs/b.json"])
        self.assertEqual(sh("git", "rev-parse", "origin/jobs", cwd=self.repos.queue), c2)
        self.assertNotIn("scratch.txt", sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git"))     # the working tree is never read
        self.assertNotEqual(c1, c2)
        self.assertFalse((self.repos.queue / ".git" / "ct-client.index").exists())

    def test_a_push_that_loses_a_race_is_fetched_and_retried_never_forced(self):
        self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job: a")
        other = self.tmp / "other"
        sh("git", "clone", "-q", str(self.tmp / "queue.git"), str(other))
        sh("git", "checkout", "-q", "jobs", cwd=other)
        (other / "jobs" / "x.json").write_text("{}\n")
        sh("git", "add", "-A", cwd=other)
        sh("git", "commit", "-q", "-m", "someone else's job", cwd=other)
        sh("git", "push", "-q", "origin", "jobs", cwd=other)
        self.repos.push_files("jobs", {"jobs/b.json": "{}\n"}, "job: b")                        # fetches first, so it builds on the new tip
        names = sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git").splitlines()
        self.assertEqual(sorted(names), ["jobs/a.json", "jobs/b.json", "jobs/x.json"])           # nobody's job was lost

    def test_a_dead_remote_raises_after_the_retries(self):
        sh("git", "remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=self.repos.queue)
        with self.assertRaises(ClientError) as cm:
            self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job: a")
        self.assertIn("Could not push", cm.exception.title)
        self.assertEqual(self.sleeps, [2] * 8)             # eight tries now: a burst of simultaneous sends loses races and must win on a later try

    def test_results_are_mirrored_even_after_the_worker_rewrites_history(self):
        self.job("job-0001")
        self.w.fetch()
        self.w.execute("job-0001")
        self.assertIsNone(self.repos.pull_results())
        self.assertEqual(self.repos.read_result("job-0001")["status"], "ok")
        self.w.io.prune_results(days=14, force=True)                                            # history is squashed: a rebase would break
        self.assertIsNone(self.repos.pull_results())
        self.assertEqual(self.repos.read_result("job-0001")["status"], "ok")
        self.assertEqual(self.repos.result_ids(), {"job-0001"})

    def test_an_empty_results_repo_is_not_an_error(self):
        self.assertIsNone(self.repos.pull_results())
        self.assertEqual((self.repos.result_ids(), self.repos.registry_info(), self.repos.read_result("x")), (set(), None, None))

    def test_a_lock_is_reported_with_its_details(self):
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.w.publish_security()
        sec = self.repos.pull_results()
        self.assertEqual((sec["locked"], sec["job"], sec["rules"]), (True, "job-0001", ["mac"]))
        self.assertNotIn("detail", sec)

    def test_job_known(self):
        sub_job = submit.stamp("job-0009", submit.build_job("echo"))
        self.repos.push_files("jobs", {"jobs/job-0009.json": json.dumps(sub_job)}, "job")
        self.assertTrue(self.repos.job_known("job-0009"))
        self.assertFalse(self.repos.job_known("job-0008"))


class Prune(RepoFixture):
    def test_old_finished_jobs_go_but_new_and_unfinished_ones_are_kept(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        files = {f"jobs/{n}.json": json.dumps({"id": n, "created": c}) for n, c in (("old-done", old), ("old-waiting", old), ("new-done", new))}
        self.repos.push_files("jobs", files, "jobs")
        for n, st in (("old-done", "ok"), ("old-waiting", "awaiting_approval"), ("new-done", "ok")):
            self.w.io.publish({"id": n, "status": st})
        msg = self.repos.prune_jobs(days=14, force=True)
        self.assertIn("1 commit -> 1, dropped 1 job", msg)
        names = sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git").splitlines()
        self.assertEqual(sorted(names), ["jobs/new-done.json", "jobs/old-waiting.json"])          # the unfinished job is never dropped

    def test_nothing_happens_below_the_threshold_or_without_a_branch(self):
        self.assertEqual(self.repos.prune_jobs(), "no jobs branch yet")
        self.repos.push_files("jobs", {"jobs/a.json": "{}\n"}, "job")
        self.assertIn("no prune needed (threshold 150)", self.repos.prune_jobs())

    def test_a_job_pushed_during_the_prune_is_never_lost(self):
        self.repos.push_files("jobs", {"jobs/a.json": json.dumps({"created": "2020-01-01T00:00:00Z"})}, "job")
        self.w.io.publish({"id": "a", "status": "ok"})
        orig = self.repos.show_file

        def racing(ref, path):
            if not getattr(self, "raced", False):
                self.raced = True
                other = self.tmp / "other"
                sh("git", "clone", "-q", "-b", "jobs", str(self.tmp / "queue.git"), str(other))
                (other / "jobs" / "late.json").write_text("{}\n")
                sh("git", "add", "-A", cwd=other)
                sh("git", "commit", "-q", "-m", "late job", cwd=other)
                sh("git", "push", "-q", "origin", "jobs", cwd=other)
            return orig(ref, path)
        self.repos.show_file = racing
        self.assertIn("refused", self.repos.prune_jobs(days=1, force=True))
        self.assertIn("jobs/late.json", sh("git", "ls-tree", "--name-only", "-r", "jobs", cwd=self.tmp / "queue.git"))


class EndToEnd(RepoFixture):
    def test_submit_through_the_worker_and_back(self):
        code, out, err = self.run_cli("submit", "echo", "-p", "word=hello", "--no-wait")
        jid = out.strip()
        self.assertEqual(code, 0)
        self.assertRegex(jid, r"^echo-[2-9a-hj-km-np-z]{6}$")
        self.assertIn("Queued", out + err)
        self.assertIn(jid, out + err)
        self.w.fetch()
        self.assertEqual(self.spawned, [jid])
        self.w.execute(jid)
        code, out, err = self.run_cli("result", jid)
        self.assertEqual(code, 0)
        self.assertIn("JOB     " + jid, err)
        self.assertIn("STATUS  ok", err)
        self.assertIn("fine", out)
        self.assertEqual(self.runner.calls[0]["argv"], ["/bin/echo", "hello"])
        code, out, err = self.run_cli("result", jid, "--json")
        self.assertEqual(json.loads(out)["status"], "ok")

    def test_the_command_says_which_links_it_did_not_follow_and_still_runs(self):
        outside = Path(tempfile.mkdtemp())
        (outside / "s.txt").write_text("OUTSIDE-SECRET")
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        (d / "link.txt").symlink_to(outside / "s.txt")
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Links not followed", out + err)
        self.assertIn("link.txt", out + err)
        self.w.fetch()
        self.w.execute(out.strip())
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt"])

    def test_the_command_says_what_was_held_back_and_still_runs(self):
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        (d / "server.pem").write_text("x")
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Files held back", out + err)
        self.assertIn("server.pem", out + err)
        self.w.fetch()
        self.w.execute(out.strip())
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt"])

    def test_files_travel_in_the_job(self):
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 0)
        self.w.fetch()
        self.w.execute(out.strip())
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt"])

    def test_the_command_lists_what_it_left_out_and_stops_on_a_file_it_could_not_send(self):
        d = Path(tempfile.mkdtemp())
        (d / "in.txt").write_text("hello")
        (d / ".git").mkdir()
        (d / ".git" / "HEAD").write_text("ref")
        (d / "blob.bin").write_bytes(b"\xff\xfe\x00")
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 2)
        text = out + err
        self.assertIn("Files left out by rule", text)
        self.assertIn(".git", text)
        self.assertIn("Some files could not be sent", text)
        self.assertIn("blob.bin", text)
        self.assertIn("files.incomplete", text)
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--send-incomplete", "--no-wait")
        self.assertEqual(code, 0, out + err)
        self.w.fetch()
        self.w.execute(out.strip())
        self.assertEqual(self.runner.calls[0]["files"], ["in.txt"])

    def test_a_set_of_files_too_big_for_the_class_is_refused_with_the_fix(self):
        d = Path(tempfile.mkdtemp())
        for i in range(210):                                                                    # the worker's published limit is 200 files per job
            (d / f"f{i}.txt").write_text("x")
        self.w.fetch()                                                                          # publishes the registry info (limits)
        self.repos.pull_results()
        info = json.loads((self.repos.results / "registry.json").read_text())
        info.pop("cache")                                                                        # a worker that does not keep files by hash (an older release) takes the whole set in the job: the per-job limit applies
        (self.repos.results / "registry.json").write_text(json.dumps(info))
        code, out, err = self.run_cli("submit", "files", "--dir", str(d), "--no-wait")
        self.assertEqual(code, 2)
        self.assertIn("These files do not fit in one job", out + err)
        self.assertIn("--include", out + err)

    def test_a_locked_worker_refuses_new_jobs_before_anything_is_pushed(self):
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.w.publish_security()
        code, out, err = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        self.assertEqual(code, 2)
        self.assertIn("The worker is locked", out + err)
        self.assertIn("ct-admin unlock", out + err)
        self.repos.fetch_queue()
        self.assertFalse(self.repos.remote_branch_exists("jobs"))

    def test_classes_and_jobs(self):
        self.assertEqual(self.run_cli("classes")[0], 1)
        self.w.fetch()
        code, out, err = self.run_cli("classes")
        self.assertEqual(code, 0)
        self.assertIn("What this worker can run for you today", out)
        self.assertIn("testct", out)
        self.assertIn("careful", out)
        code, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        jid = out.strip()
        code, out, err = self.run_cli("jobs")
        self.assertIn(jid, out)
        self.assertIn("queued", out)
        self.w.fetch()
        self.w.execute(jid)
        self.assertRegex(self.run_cli("jobs")[1], rf"JOB\s+{jid}\n\s+STATUS\s+OK")

    def test_an_approval_job_shows_where_to_approve_it(self):
        code, out, err = self.run_cli("submit", "careful", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        code, out, err = self.run_cli("jobs")
        self.assertIn("Waiting for you", out)
        self.assertIn(f"tct-admin approve {jid}", out)
        code, out, err = self.run_cli("result", jid)
        self.assertEqual(code, 0)
        self.assertIn("Not approved yet", out + err)
        self.assertIn(f"tct-admin approve {jid}", out + err)
        self.assertEqual(self.admin_calls, [])                                                   # a captured run is never offered the choice

    def test_at_a_terminal_a_waiting_job_can_be_approved_on_the_spot(self):
        _, out, _ = self.run_cli("submit", "careful", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        self.ctx.is_tty = lambda: True
        self.answers = ["y"]
        self.run_cli("result", jid)
        self.assertEqual(self.admin_calls, [["approve", jid]])
        self.answers = ["n"]
        self.admin_calls.clear()
        code, out, err = self.run_cli("result", jid)
        self.assertEqual(self.admin_calls, [])
        self.assertIn(f"tct-admin approve {jid}", out + err)

    def test_unknown_and_not_yet_picked_up_jobs(self):
        code, out, err = self.run_cli("result", "job-nosuch1")
        self.assertEqual(code, 1)
        self.assertIn("No such job", out + err)
        self.assertIn("job-nosuch1", out + err)
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        code, out2, err = self.run_cli("result", out.strip())
        self.assertEqual(code, 0)
        self.assertIn("No result yet", out2 + err)

    def test_prune_and_help_and_bad_commands(self):
        code, out, err = self.run_cli("prune")
        self.assertIn("no jobs branch yet", out + err)
        code, out, err = self.run_cli("help")
        self.assertEqual(code, 0)
        for word in ("setup", "submit", "result", "jobs", "classes", "prune", "offload"):
            self.assertIn(word, out)
        self.assertEqual(self.run_cli("nonsense")[0], 2)
        self.assertEqual(self.run_cli()[0], 0)                                                   # no arguments is help

    def test_not_set_up_is_explained(self):
        lonely = cli.Context(ClientRepos(self.tmp / "no" / "q", self.tmp / "no" / "r", "x", "y"), product="testct")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(["jobs"], ctx=lonely), 2)
        self.assertIn("not set up yet", out.getvalue() + err.getvalue())
        self.assertIn("ct-run setup", out.getvalue() + err.getvalue())


class Waiting(RepoFixture):
    def clock(self):
        t = [1000.0]
        self.ctx.clock = lambda: t[0]
        self.ctx.sleep = lambda s: t.__setitem__(0, t[0] + s)

    def test_it_returns_the_result_as_soon_as_the_job_finishes(self):
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        self.w.execute(jid)
        code, out, err = self.run_cli("result", jid, "--wait")
        self.assertEqual(code, 0)
        self.assertIn("STATUS  ok", err)

    def test_a_job_that_never_finishes_times_out_politely(self):
        self.clock()
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        code, out, err = self.run_cli("result", out.strip(), "--wait", "--timeout", "3")
        self.assertEqual(code, 1)
        self.assertIn("Stopped waiting", out + err)
        self.assertIn("3 s", out + err)
        self.assertIn("The job may still run", out + err)

    def test_waiting_stops_when_the_worker_locks_itself(self):
        self.clock()
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        self.lockdown.engage("quarantine", "job-0001", {"mac"}, cls="c")
        self.w.publish_security()
        code, out2, err = self.run_cli("result", out.strip(), "--wait", "--timeout", "30")
        self.assertEqual(code, 3)
        self.assertIn("the worker is locked", out2 + err)
        self.assertIn("tct-admin unlock", out2 + err)

    def test_the_approval_instruction_is_given_once_while_waiting(self):
        self.clock()
        _, out, _ = self.run_cli("submit", "careful", "--no-wait")
        jid = out.strip()
        self.w.fetch()
        code, out, err = self.run_cli("result", jid, "--wait", "--timeout", "12")
        self.assertEqual(code, 1)
        self.assertEqual((out + err).count("Waiting for your approval"), 1)

    def test_a_failed_job_returns_a_failing_exit_code(self):
        _, out, _ = self.run_cli("submit", "echo", "-p", "word=hi", "--no-wait")
        jid = out.strip()
        self.runner.reply = ("error", 1, "ValueError: bad\n", {}, "")
        self.w.fetch()
        self.w.execute(jid)
        self.assertEqual(self.run_cli("result", jid)[0], 1)

    def test_submit_waits_by_default_in_a_terminal_and_not_when_captured(self):
        self.runner.reply = ("ok", 0, "fine\n", {}, "")
        self.ctx.is_tty = lambda: False
        code, out, err = self.run_cli("submit", "echo", "-p", "word=hi")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^echo-")                                               # captured: prints the id and returns
        self.assertNotIn("STATUS", err)


class Config(unittest.TestCase):
    def test_the_context_is_built_from_the_configuration(self):
        from ct import config
        cfg = config.load(None, env={"CT_CONFIG": "", "CT_IDENTITY_NAME": "homect", "CT_GITHUB_OWNER": "alice", "CT_PATHS_CLIENT_HOME": "~/.homect"})
        ctx = cli.Context.from_config(cfg)
        self.assertEqual((ctx.product, ctx.admin), ("homect", "ct-admin"))
        self.assertEqual((ctx.repos.queue_url, ctx.repos.results_url), ("git@github.com:alice/homect-queue.git", "git@github.com:alice/homect-results.git"))          # derived from the install name
        self.assertEqual(str(ctx.repos.queue), str(Path("~/.homect/queue").expanduser()))
        self.assertEqual(ctx.hints, [("on your computer", "ct-admin approve {}"), ("on the worker", "ct-worker approve {}")])
        self.assertIn("ct-worker", ctx.own_tools)
        self.assertIn("ct-admin", ctx.own_tools)
        self.assertNotIn("example-owner", json.dumps([ctx.repos.queue_url, ctx.repos.results_url]))


class WaitingChip(unittest.TestCase):
    def test_every_live_line_starts_with_its_status_as_a_capitals_chip_in_the_colour_of_that_status(self):
        from ct import theme
        class Tty(io.StringIO):
            def isatty(self):
                return True
            def fileno(self):
                raise OSError
        for colour, role, text, word in ((view.YELLOW, "wait_chip", "sent: waiting for the worker to notice it", " SENT "), (view.CYAN, "primary_chip", "running on the worker", " RUNNING "),
                                          (view.RED, "bad_chip", "approved, but it was not picked up", " APPROVED, ")):
            out = Tty()
            with mock.patch.dict(os.environ, {"CT_COLOR": "always"}):
                view.Progress(stream=out, clock=lambda: 0).draw(text, colour)
            line = out.getvalue()
            self.assertIn(theme.code(role), line, text)
            self.assertIn(word.strip().rstrip(","), re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).upper(), text)
            self.assertIn(text.partition(" ")[2], re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line), text)


class FasterPolling(unittest.TestCase):
    def test_a_waiting_client_asks_github_every_two_seconds_not_five(self):
        src = Path(cli.__file__).read_text()
        self.assertIn("for _ in range(10):", src)
        self.assertNotIn("for _ in range(25):", src)


if __name__ == "__main__":
    unittest.main()
