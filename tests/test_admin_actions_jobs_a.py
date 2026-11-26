"""Part 1 of 2 of the test_admin_actions_jobs tests (JobTests, ProposalTests)."""
import unittest
from datetime import timedelta
from admin_support import AdminBase, REGISTRY, sh  # noqa: F401  (also points HOME at a throwaway folder)
from ct import util
from ct.admin import jobs


class JobTests(AdminBase):
    def test_review_marks_what_you_saw_and_approve_all_sends_pinned_digests(self):
        self.put_job("job-a")
        self.put_result("job-a", status="awaiting_approval")
        code, out, _ = self.admin("review")
        self.assertEqual(code, 0)
        self.assertIn("job-a", out)
        digest = jobs.digest_of(jobs.job_bytes(self.repos, "job-a"))
        self.assertEqual(jobs.Viewed(self.home).load()["job-a"], digest)
        self.answers.append("y")
        self.tty = True
        code, _, _ = self.admin("approve", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [f"ct-worker approve --many job-a:{digest}"])

    def test_a_job_that_changed_after_you_viewed_it_is_not_approved_in_bulk(self):
        self.put_job("job-a")
        self.put_result("job-a", status="awaiting_approval")
        self.admin("review")
        self.put_job("job-a", params={"x": "changed"})
        self.tty = True
        code, out, err = self.admin("approve", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("changed since you viewed it", out)
        self.assertEqual(self.ssh.calls, [])

    def test_approve_all_skips_unviewed_jobs_and_asks_nothing_for_them(self):
        self.put_job("job-a")
        self.put_result("job-a", status="awaiting_approval")
        code, out, err = self.admin("approve", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("not viewed yet", out)
        self.assertEqual(self.ssh.calls, [])

    def test_approve_one_pins_the_digest_and_follows_the_result_in_a_terminal(self):
        self.put_job("job-b")
        digest = jobs.digest_of(jobs.job_bytes(self.repos, "job-b"))
        self.tty = True
        self.answers.append("y")
        code, out, _ = self.admin("approve", "job-b")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, [f"ct-worker approve job-b --sha256 {digest}"])
        self.assertEqual(self.results_followed, ["job-b"])
        self.assertIn(digest[:12], out)

    def test_approve_one_refuses_an_expired_job_before_showing_or_signing_anything(self):
        self.put_job("job-old", created=util.iso(util.now() - timedelta(days=2)))
        self.tty = True
        self.answers.append("y")
        code, out, err = self.admin("approve", "job-old")
        self.assertNotEqual(code, 0)
        self.assertIn("That job has expired", out + err)
        self.assertIn("job-old", out + err)
        self.assertEqual(self.ssh.calls, [])
        self.assertEqual(self.answers, ["y"])                                          # it asked nothing: the answer was never used
        self.assertNotIn("job-old", jobs.Viewed(self.home).load())

    def test_approve_all_skips_an_expired_job_and_says_why(self):
        self.put_job("job-old", created=util.iso(util.now() - timedelta(days=2)))
        self.put_result("job-old", status="awaiting_approval")
        self.admin("review")
        code, out, err = self.admin("approve", "--all")
        self.assertNotEqual(code, 0)
        self.assertIn("expired", out)
        self.assertEqual(self.ssh.calls, [])

    def test_answering_no_approves_nothing(self):
        self.put_job("job-b")
        self.tty = True
        self.answers.append("n")
        code, _, _ = self.admin("approve", "job-b")
        self.assertEqual(code, 1)
        self.assertEqual(self.ssh.calls, [])

    def test_without_a_terminal_it_refuses_to_guess_an_answer(self):
        self.put_job("job-b")
        code, _, err = self.admin("approve", "job-b")
        self.assertEqual(code, 2)
        self.assertIn("needs your answer", err)
        self.assertEqual(self.ssh.calls, [])

    def test_yes_skips_the_question_and_no_follow_returns_at_once(self):
        self.put_job("job-b")
        code, _, _ = self.admin("approve", "job-b", "--yes", "--no-follow")
        self.assertEqual(code, 0)
        self.assertEqual(self.results_followed, [])

    def test_a_job_gone_from_github_is_approved_on_the_worker(self):
        self.put_result("old-job", status="awaiting_approval")
        code, _, _ = self.admin("approve", "old-job", "--yes", "--no-follow")
        self.assertEqual(code, 0)
        self.assertEqual(self.ssh.calls, ["ct-worker approve old-job --yes"])
        self.assertEqual(jobs.Viewed(self.home).load()["old-job"], "")

    def test_a_job_that_is_nowhere_is_an_error(self):
        code, _, err = self.admin("approve", "nothing-here")
        self.assertEqual(code, 2)
        self.assertIn("No such job", err)
        self.assertIn("nothing-here", err)

    def test_bad_ids_are_refused_before_any_command_is_built(self):
        for cmd in (["approve", "A;rm"], ["cancel", "x y"], ["kill", "../x"]):
            code, _, _ = self.admin(*cmd)
            self.assertEqual(code, 2)
        self.assertEqual(self.ssh.calls, [])

    def test_passthrough_commands_call_the_workers_own_commands(self):
        for args, expect in ((["status", "--hours", "48"], "ct-worker status --hours 48"), (["unlock"], "ct-worker unlock"), (["quarantine"], "ct-worker quarantine"),
                             (["quarantine", "job-x"], "ct-worker quarantine job-x"), (["output", "job-x"], "ct-worker output job-x"), (["cancel", "job-x"], "ct-worker cancel job-x"),
                             (["kill", "job-x", "--hard"], "ct-worker kill job-x --hard")):
            self.ssh.calls.clear()
            self.assertEqual(self.admin(*args)[0], 0)
            self.assertEqual(self.ssh.calls, [expect])


class ProposalTests(AdminBase):
    def propose(self, name, text=None):
        reg = text or (REGISTRY + f"  {name}:\n    tier: auto\n    cost: light\n    run: [/bin/echo, {name}]\n")
        self.repos.push_files(f"registry/{name}", {"registry.yaml": reg}, f"feat(registry): add {name} class", base="main")

    def test_listing_shows_pending_and_merged_branches(self):
        self.propose("hello")
        code, out, _ = self.admin("proposals")
        self.assertEqual(code, 0)
        self.assertIn("registry/hello", out)
        self.assertIn("1 to review", out)

    def test_merge_squashes_into_main_and_deletes_the_branch(self):
        self.propose("hello")
        self.tty = True
        self.answers.append("y")
        code, _, err = self.admin("proposals", "merge", "registry/hello")
        self.assertEqual(code, 0)
        self.repos.fetch_queue()
        self.assertIn("  hello:", self.repos.show_file("origin/main", "registry.yaml"))
        self.assertFalse(self.repos.remote_branch_exists("registry/hello"))
        self.assertIn("Merged-Branch: registry/hello@", sh("git", "-C", str(self.repos.queue), "log", "-1", "--format=%B", "origin/main"))

    def test_an_invalid_registry_is_refused_before_it_is_merged(self):
        self.propose("bad", REGISTRY + "  bad:\n    tier: nonsense\n    run: not-a-list\n")
        self.tty = True
        code, _, err = self.admin("proposals", "merge", "registry/bad")
        self.assertEqual(code, 2)
        self.assertIn("invalid registry", err)
        self.assertTrue(self.repos.remote_branch_exists("registry/bad"))

    def test_main_jobs_and_data_cannot_be_merged(self):
        for b in ("main", "jobs", "data"):
            self.assertEqual(self.admin("proposals", "merge", b, "--yes")[0], 2)

    def test_merge_all_takes_every_pending_branch(self):
        self.propose("one")
        self.repos.fetch_queue()
        self.admin("proposals", "merge", "registry/one", "--yes")
        self.propose("two")
        code, _, _ = self.admin("proposals", "merge", "--all", "--yes")
        self.assertEqual(code, 0)
        self.repos.fetch_queue()
        text = self.repos.show_file("origin/main", "registry.yaml")
        self.assertIn("  two:", text)

    def test_tidy_deletes_only_merged_branches(self):
        self.propose("one")
        self.admin("proposals", "merge", "registry/one", "--yes")
        self.propose("two")
        code, out, _ = self.admin("proposals", "tidy", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(self.repos.remote_branch_exists("registry/two"))


if __name__ == "__main__":
    unittest.main()
