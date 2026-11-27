"""The interactive offload review (slice 7 of theme/offload-value): one y/n per recommended command, profile on every worker, register where it passes, a remembered no.
Everything the flow reads about time, workers and answers is passed in, so no test depends on the machine running it. The fakes are checked against the real parser, `place.view_of`
and `sched` in `Real` below."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import config, refusals, sched  # noqa: E402
from ct.client import cli, place, review_flow as rf  # noqa: E402
from ct.client.repo import ClientRepos  # noqa: E402
from offload_support import OffloadFixture  # noqa: E402

NOW = 1_800_000_000
DAY = 86400
OWN = ("ct-run", "homect")
REG_OK = {"profile_time": {"factor": 1.0, "margin": 3}, "cache": {"version": 1}, "classes": {}}


def rec(shape="scripts/t", example="scripts/t test_a test_b", count=30, median=60.0, total=1800.0):
    return {"shape": shape, "example": example, "count": count, "median": median, "total": total, "untimed": 0, "rung": "long", "headline": "takes long", "why": "x"}


def target(name, state="ready", why="", registry=None, excluded=()):
    return rf.Target(name, state, why, registry if registry is not None else dict(REG_OK, classes={}), list(excluded))


def fits(jid):
    return {"id": jid, "status": "ok", "profile": {"verdict": "fits", "cost": "light", "argv": ["scripts/t", "test_a", "test_b"]}}


def nofit(jid, why="needs 9000 MB of memory and the worker has 2000 MB"):
    return {"id": jid, "status": "ok", "profile": {"verdict": "does-not-fit", "reasons": [why], "limiting": ["memory"], "argv": ["scripts/t"]}}


class FakeFleet:
    """Stands in for the live fleet: same method names as `rf.LiveFleet` (checked in Real)."""

    def __init__(self, results=None, send_fail=(), approve_rc=None, adopt=None, folder=None):
        self.results, self.send_fail, self.approve_rc, self.adopt_res, self.calls = results or {}, dict(send_fail), approve_rc or {}, adopt or {}, []

    def profile(self, worker, job):
        self.calls.append(("profile", worker, job))
        if worker in self.send_fail:
            raise rf.Failed(self.send_fail[worker])
        return f"profile-{worker}"

    def approve(self, worker, jid):
        self.calls.append(("approve", worker, jid))
        return self.approve_rc.get(worker, 0)

    def result(self, worker, jid, limit_s):
        self.calls.append(("result", worker, jid, limit_s))
        r = self.results.get(worker)
        return dict(r, id=jid) if r else None

    def adopt(self, worker, res, job):
        self.calls.append(("adopt", worker, res["id"]))
        return self.adopt_res.get(worker, "adopted")

    def limit_s(self, worker, est_s):
        return 90

    def names(self, calls, kind):
        return [c[1] for c in calls if c[0] == kind]


def answers_to(*given):
    it = iter(given)

    def ask(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return ask


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.answers = rf.Answers(self.home, clock=lambda: NOW)

    def run_flow(self, recs, targets, fleet, *given, tty=True, folder=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            res = rf.review(recs, targets, fleet, self.answers, ask=answers_to(*given), is_tty=lambda: tty, own_tools=OWN, now=NOW, ask_again_days=30, folder_of=lambda r: folder, **kw)
        return res, out.getvalue() + err.getvalue()


class AnswerBook(Base):
    def test_not_now_is_quiet_for_the_period_then_asked_again(self):
        self.answers.decline("scripts/t", "not-now")
        self.assertEqual(self.answers.silenced("scripts/t", 30), "not-now")
        later = rf.Answers(self.home, clock=lambda: NOW + 31 * DAY)
        self.assertIsNone(later.silenced("scripts/t", 30))

    def test_never_lasts_until_undone_and_survives_a_restart(self):
        self.answers.decline("scripts/t", "never")
        again = rf.Answers(self.home, clock=lambda: NOW + 900 * DAY)
        self.assertEqual(again.silenced("scripts/t", 30), "never")
        self.assertTrue(again.forget("1"))
        self.assertIsNone(rf.Answers(self.home, clock=lambda: NOW).silenced("scripts/t", 30))

    def test_forget_by_number_or_by_command_and_a_wrong_one_is_false(self):
        self.answers.decline("a x", "never")
        self.answers.decline("b y", "not-now")
        self.assertFalse(self.answers.forget("9"))
        self.assertTrue(self.answers.forget("b y"))
        self.assertEqual([r[1] for r in self.answers.rows()], ["a x"])

    def test_a_damaged_file_is_treated_as_empty_and_does_not_stop_the_review(self):
        (self.home / rf.ANSWERS_FILE).write_text("{not json")
        self.assertIsNone(self.answers.silenced("scripts/t", 30))
        self.answers.decline("scripts/t", "never")
        self.assertEqual(self.answers.silenced("scripts/t", 30), "never")

    def test_the_file_is_private(self):
        self.answers.decline("scripts/t", "never")
        self.assertEqual((self.home / rf.ANSWERS_FILE).stat().st_mode & 0o077, 0)


class Workers(unittest.TestCase):
    def view(self, **kw):
        base = dict(name="w", connection="direct", state={"updated": "x", "locked": False}, registry=dict(REG_OK), age_s=60.0, pickup_s=5)
        base.update(kw)
        return sched.worker_view(base.pop("name"), base.pop("connection"), base.pop("state"), base.pop("registry"), base.pop("age_s"), base.pop("pickup_s"), **base)

    def test_each_way_a_worker_cannot_be_asked_says_so_with_a_reason(self):
        self.assertEqual(rf.worker_state(self.view())[0], "ready")
        st, why = rf.worker_state(self.view(reachable=False, why_not="connection refused"))
        self.assertEqual(st, "offline")
        self.assertIn("connection refused", why)
        self.assertEqual(rf.worker_state(self.view(age_s=sched.STALE_S + 600))[0], "silent")
        self.assertEqual(rf.worker_state(self.view(age_s=None))[0], "silent")
        self.assertEqual(rf.worker_state(self.view(state={"updated": "x", "locked": True}))[0], "locked")
        st, why = rf.worker_state(self.view(registry={"classes": {}}))
        self.assertEqual(st, "old")
        self.assertIn("older release", why)

    def test_a_worker_that_never_published_a_registry_is_old_not_ready(self):
        self.assertEqual(rf.worker_state(self.view(registry=None))[0], "old")


class Plan(Base):
    def plan(self, recs, targets):
        return rf.plan(recs, targets, self.answers, own_tools=OWN, now=NOW, ask_again_days=30)

    def test_every_ready_worker_that_lacks_the_class_is_a_target(self):
        p = self.plan([rec()], [target("a"), target("b")])
        self.assertEqual([(i.rec["shape"], i.ready) for i in p.items], [("scripts/t", ["a", "b"])])

    def test_declined_commands_are_not_asked_again(self):
        self.answers.decline("scripts/t", "not-now")
        p = self.plan([rec()], [target("a")])
        self.assertEqual((p.items, p.skipped[0][0]), ([], "scripts/t"))
        self.assertIn("not now", p.skipped[0][1])

    def test_a_class_already_on_every_worker_is_not_offered(self):
        reg = dict(REG_OK, classes={"suite": {"run": ["scripts/t", "test_a", "test_b"], "cost": "light", "tier": "auto"}})
        p = self.plan([rec()], [target("a", registry=reg), target("b", registry=reg)])
        self.assertEqual(p.items, [])
        self.assertIn("already", p.skipped[0][1])

    def test_a_class_on_one_worker_is_offered_for_the_others_only(self):
        reg = dict(REG_OK, classes={"suite": {"run": ["scripts/t", "test_a", "test_b"], "cost": "light", "tier": "auto"}})
        p = self.plan([rec()], [target("a", registry=reg), target("b")])
        self.assertEqual(p.items[0].ready, ["b"])
        self.assertEqual(p.items[0].have, ["a"])

    def test_workers_that_cannot_be_asked_are_kept_with_their_reason_never_dropped(self):
        p = self.plan([rec()], [target("a"), target("off", "offline", "connection refused"), target("old", "old", "run ct-admin apply")])
        self.assertEqual(p.items[0].ready, ["a"])
        self.assertEqual({w: s for w, s, _ in p.items[0].held}, {"off": "offline", "old": "old"})

    def test_no_worker_can_be_asked_is_a_skip_that_says_why(self):
        p = self.plan([rec()], [target("off", "offline", "connection refused")])
        self.assertEqual(p.items, [])
        self.assertIn("connection refused", p.skipped[0][1])

    def test_a_command_excluded_on_a_worker_is_held_there_not_profiled_again(self):
        ex = [{"argv": ["scripts/t", "test_a", "test_b"], "reasons": ["too much memory"], "limiting": ["memory"], "profiled": {}}]
        p = self.plan([rec()], [target("a", excluded=ex), target("b")])
        self.assertEqual(p.items[0].ready, ["b"])
        self.assertIn("too much memory", p.items[0].held[0][2])

    def test_unsafe_commands_are_never_offered_even_if_they_reach_the_list(self):
        for ex in ("git push origin main", "curl https://x.example/a", "sudo systemctl restart x", "deploy --token abc123", "npm install", "ct-run submit x"):
            p = self.plan([rec(shape=ex.split()[0], example=ex)], [target("a")])
            self.assertEqual((ex, p.items), (ex, []))
            self.assertTrue(p.skipped, ex)

    def test_the_command_is_split_the_way_a_shell_would(self):
        p = self.plan([rec(example="pytest -k 'a b' tests")], [target("a")])
        self.assertEqual(p.items[0].argv, ["pytest", "-k", "a b", "tests"])


class Flow(Base):
    def test_yes_profiles_approves_waits_and_adopts_on_every_worker(self):
        fl = FakeFleet(results={"a": fits("x"), "b": fits("x")})
        res, text = self.run_flow([rec()], [target("a"), target("b")], fl, "y")
        self.assertEqual(fl.names(fl.calls, "profile"), ["a", "b"])
        self.assertEqual(fl.names(fl.calls, "approve"), ["a", "b"])
        self.assertEqual(fl.names(fl.calls, "adopt"), ["a", "b"])
        self.assertEqual([(o.worker, o.status) for o in res[0].outcomes], [("a", "registered"), ("b", "registered")])
        self.assertIsNone(self.answers.silenced("scripts/t", 30))

    def test_every_job_carries_the_same_class_name_and_the_estimate_from_the_log(self):
        fl = FakeFleet(results={"a": fits("x"), "b": fits("x")})
        self.run_flow([rec(median=75.0)], [target("a"), target("b")], fl, "y")
        jobs = [c[2] for c in fl.calls if c[0] == "profile"]
        self.assertEqual(len({j.name for j in jobs}), 1)
        self.assertEqual({j.est_s for j in jobs}, {75.0})
        self.assertEqual(jobs[0].argv, ["scripts/t", "test_a", "test_b"])

    def test_mixed_results_register_where_it_passes_and_say_why_elsewhere_with_the_real_text(self):
        fl = FakeFleet(results={"a": fits("x"), "b": nofit("x")})
        res, text = self.run_flow([rec()], [target("a"), target("b")], fl, "y")
        self.assertEqual([(o.worker, o.status) for o in res[0].outcomes], [("a", "registered"), ("b", "does-not-fit")])
        self.assertIn("needs 9000 MB of memory and the worker has 2000 MB", text)
        self.assertEqual(fl.names(fl.calls, "adopt"), ["a"])

    def test_an_offline_worker_is_named_and_the_others_still_run(self):
        fl = FakeFleet(results={"a": fits("x")})
        res, text = self.run_flow([rec()], [target("a"), target("off", "offline", "connection refused")], fl, "y")
        self.assertIn("connection refused", text)
        self.assertEqual(fl.names(fl.calls, "profile"), ["a"])
        self.assertIn("off", [h[0] for h in res[0].item.held])

    def test_workers_that_cannot_be_asked_are_said_once_with_the_fix_not_on_every_command(self):
        fl = FakeFleet(results={"a": fits("x")})
        recs = [rec(), rec(shape="make lint", example="make lint")]
        res, text = self.run_flow(recs, [target("a"), target("old", "old", "runs an older release that cannot take the recipe")], fl, "", "")
        self.assertEqual(text.count("older release"), 1)
        self.assertIn("ct-admin apply --worker old", text)

    def test_the_same_list_is_said_without_a_terminal(self):
        fl = FakeFleet()
        res, text = self.run_flow([rec()], [target("a"), target("off", "offline", "connection refused")], fl, tty=False)
        self.assertIn("connection refused", text)

    def test_a_worker_that_fails_to_take_the_job_does_not_stop_the_others(self):
        fl = FakeFleet(results={"b": fits("x")}, send_fail={"a": "the queue branch is locked"})
        res, text = self.run_flow([rec()], [target("a"), target("b")], fl, "y")
        self.assertEqual([(o.worker, o.status) for o in res[0].outcomes], [("a", "not-sent"), ("b", "registered")])
        self.assertIn("the queue branch is locked", text)

    def test_a_refused_approval_is_reported_and_not_waited_for(self):
        fl = FakeFleet(results={"a": fits("x")}, approve_rc={"a": 1})
        res, text = self.run_flow([rec()], [target("a")], fl, "y")
        self.assertEqual(res[0].outcomes[0].status, "not-approved")
        self.assertEqual(fl.names(fl.calls, "result"), [])
        self.assertEqual(fl.names(fl.calls, "adopt"), [])

    def test_no_result_in_time_is_said_with_the_command_to_read_it_later(self):
        fl = FakeFleet(results={})
        res, text = self.run_flow([rec()], [target("a")], fl, "y")
        self.assertEqual(res[0].outcomes[0].status, "no-result")
        self.assertIn("ct-run result profile-a --wait", text)

    def test_ran_out_of_time_is_not_a_no_fit(self):
        r = {"status": "ok", "profile": {"verdict": "needs-longer", "reasons": ["did not finish in 90 s"], "next_limit_s": 180}}
        res, text = self.run_flow([rec()], [target("a")], FakeFleet(results={"a": r}), "y")
        self.assertEqual(res[0].outcomes[0].status, "needs-longer")
        self.assertIn("did not finish in 90 s", text)

    def test_when_adopting_is_left_to_the_person_the_exact_command_is_shown(self):
        fl = FakeFleet(results={"a": fits("x")}, adopt={"a": "manual"})
        res, text = self.run_flow([rec()], [target("a")], fl, "y")
        self.assertEqual(res[0].outcomes[0].status, "profiled")
        self.assertIn("ct-admin adopt profile-a", text)

    def test_no_is_remembered_and_nothing_is_sent(self):
        fl = FakeFleet()
        self.run_flow([rec()], [target("a")], fl, "n")
        self.assertEqual(fl.calls, [])
        self.assertEqual(self.answers.silenced("scripts/t", 30), "not-now")

    def test_never_is_remembered_for_good(self):
        fl = FakeFleet()
        self.run_flow([rec()], [target("a")], fl, "never")
        self.assertEqual(fl.calls, [])
        self.assertEqual(self.answers.silenced("scripts/t", 30), "never")

    def test_enter_skips_for_now_and_remembers_nothing(self):
        fl = FakeFleet()
        self.run_flow([rec()], [target("a")], fl, "")
        self.assertEqual(fl.calls, [])
        self.assertIsNone(self.answers.silenced("scripts/t", 30))

    def test_an_unclear_answer_is_asked_again_then_skipped_never_taken_as_yes(self):
        fl = FakeFleet(results={"a": fits("x")})
        self.run_flow([rec()], [target("a")], fl, "sure", "ok")
        self.assertEqual(fl.calls, [])
        self.assertIsNone(self.answers.silenced("scripts/t", 30))

    def test_each_command_needs_its_own_yes_and_there_is_no_yes_to_all(self):
        fl = FakeFleet(results={"a": fits("x")})
        recs = [rec(), rec(shape="make lint", example="make lint")]
        res, _ = self.run_flow(recs, [target("a")], fl, "y", "all")
        self.assertEqual(len(res), 1)
        self.assertEqual(len([c for c in fl.calls if c[0] == "profile"]), 1)

    def test_ctrl_c_stops_the_loop_and_changes_nothing(self):
        def ask(prompt):
            raise KeyboardInterrupt
        fl = FakeFleet()
        out = io.StringIO()
        with redirect_stderr(out), redirect_stdout(out):
            res = rf.review([rec()], [target("a")], fl, self.answers, ask=ask, is_tty=lambda: True, own_tools=OWN, now=NOW, ask_again_days=30, folder_of=lambda r: None)
        self.assertEqual((res, fl.calls), ([], []))
        self.assertIsNone(self.answers.silenced("scripts/t", 30))

    def test_a_folder_is_confirmed_and_can_be_changed_or_dropped(self):
        fl = FakeFleet(results={"a": fits("x")})
        self.run_flow([rec()], [target("a")], fl, "y", "", folder="/work/proj")
        self.assertEqual([c[2].folder for c in fl.calls if c[0] == "profile"], ["/work/proj"])
        fl = FakeFleet(results={"a": fits("x")})
        self.run_flow([rec()], [target("a")], fl, "y", "-", folder="/work/proj")
        self.assertEqual([c[2].folder for c in fl.calls if c[0] == "profile"], [None])
        fl = FakeFleet(results={"a": fits("x")})
        self.run_flow([rec()], [target("a")], fl, "y", "/other", folder="/work/proj")
        self.assertEqual([c[2].folder for c in fl.calls if c[0] == "profile"], ["/other"])

    def test_packages_the_card_showed_are_sent_only_for_the_folder_the_card_showed(self):
        for typed, want in (("", True), ("/other", False)):
            fl = FakeFleet(results={"a": fits("x")})
            self.run_flow([rec()], [target("a")], fl, "y", typed, folder="/work/proj", proposal_of=lambda f: (["Found: requirements.txt"], True))
            self.assertEqual([c[2].packages for c in fl.calls if c[0] == "profile"], [want], typed)

    def test_the_wait_limit_comes_from_each_workers_own_speed(self):
        fl = FakeFleet(results={"a": fits("x")})
        self.run_flow([rec()], [target("a")], fl, "y")
        self.assertEqual([c[3] for c in fl.calls if c[0] == "result"], [90])


class NoTerminal(Base):
    def test_without_a_terminal_it_lists_and_prints_exact_commands_and_does_nothing(self):
        fl = FakeFleet()
        asked = []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            res = rf.review([rec()], [target("a")], fl, self.answers, ask=lambda p: asked.append(p), is_tty=lambda: False, own_tools=OWN, now=NOW, ask_again_days=30, folder_of=lambda r: None)
        self.assertEqual((res, fl.calls, asked), ([], [], []))
        self.assertIn("ct-run offload profile", out.getvalue() + err.getvalue())
        self.assertIn("scripts/t test_a test_b", out.getvalue() + err.getvalue())
        self.assertFalse((self.home / rf.ANSWERS_FILE).exists())


class Command(unittest.TestCase):
    """`ct-run offload review` itself: the same entry the person types."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        cfg_path = self.home / "c.toml"
        cfg_path.write_text('[identity]\nname = "homect"\n[server]\nhost = "h.example"\nadmin_user = "agent"\n')
        self.cfg = config.load(str(cfg_path), env={"CT_CONFIG": ""})
        self.fleet = FakeFleet(results={"a": fits("x")})
        self.ctx = cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), product="homect", own_tools=OWN, home=self.home, clock=lambda: NOW,
                               host_load=lambda: {"load": 0.1, "cores": 4, "ratio": 0.03}, is_tty=lambda: False)
        self.ctx.review_targets = lambda cfg: [target("a")]
        self.ctx.review_fleet = lambda cfg: self.fleet
        cmdlog_path = self.home / "command-log.jsonl"
        lines = [{"ts": NOW - 2 * DAY + i, "cmd": "scripts/t test_a test_b", "secs": 60.0, "cwd": "/x"} for i in range(30)]
        lines += [{"ts": NOW - 5 * DAY + i, "cmd": f"ls d{i}", "secs": 0.1, "cwd": "/x"} for i in range(20)]
        cmdlog_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    def run_main(self, *args, tty=False, given=()):
        self.ctx.is_tty = lambda: tty
        self.ctx.ask = answers_to(*given)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["offload", "review", *args], ctx=self.ctx, cfg=self.cfg)
        return code, out.getvalue() + err.getvalue()

    def test_without_a_terminal_nothing_is_asked_or_registered(self):
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.fleet.calls, [])
        self.assertIn("ct-run offload profile", text)

    def test_at_a_terminal_a_yes_registers_the_class(self):
        code, text = self.run_main(tty=True, given=("y", ""))
        self.assertEqual(code, 0)
        self.assertIn(("adopt", "a", "profile-a"), [c[:3] for c in self.fleet.calls])

    def test_the_remembered_answers_can_be_listed_and_undone(self):
        self.run_main(tty=True, given=("never",))
        code, text = self.run_main("--declined")
        self.assertIn("scripts/t", text)
        code, text = self.run_main("--forget", "1")
        self.assertEqual(code, 0)
        self.assertEqual(rf.Answers(self.home).rows(), [])

    def test_forgetting_a_number_that_is_not_there_says_so_and_fails(self):
        code, text = self.run_main("--forget", "7")
        self.assertEqual(code, 2)
        self.assertIn("ct-run offload review --declined", text)


class AtTheEndOfSetup(unittest.TestCase):
    """`ct-setup install` offers the same flow once; a no changes nothing and says how to come back."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        lines = [{"ts": NOW - 2 * DAY + i, "cmd": "scripts/t test_a test_b", "secs": 60.0, "cwd": "/x"} for i in range(30)]
        lines += [{"ts": NOW - 5 * DAY + i, "cmd": f"ls d{i}", "secs": 0.1, "cwd": "/x"} for i in range(20)]
        (self.home / "command-log.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        p = self.home / "c.toml"
        p.write_text(f'[identity]\nname = "homect"\n[server]\nhost = "h.example"\nadmin_user = "agent"\n[paths]\nclient_home = "{self.home}"\n')
        self.cfg = config.load(str(p), env={"CT_CONFIG": ""})

    def run_it(self, answer):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        err, calls = Tty(), []
        from unittest import mock
        with mock.patch.object(cli, "run_review_flow", lambda *a: calls.append(a)), mock.patch.object(cli.Context, "from_config", classmethod(lambda c, cfg: cli.Context(ClientRepos(self.home / "q", self.home / "r", "x", "y"), home=self.home))):
            cli.after_install_review(self.cfg, stream=err, host_load=lambda: {"load": 0.1, "cores": 4, "ratio": 0.03}, now=NOW, ask=lambda p: answer)
        return calls, err.getvalue()

    def test_yes_runs_the_flow(self):
        calls, _ = self.run_it("y")
        self.assertEqual(len(calls), 1)

    def test_no_changes_nothing_and_says_how_to_come_back(self):
        calls, text = self.run_it("n")
        self.assertEqual(calls, [])
        self.assertIn("ct-run offload review", text)


class LiveEndToEnd(OffloadFixture):
    """The live fleet against a real queue repository and a real worker fixture: the run it sends is the one `ct-run offload profile` would send."""

    def test_the_run_it_sends_is_a_real_profile_run_that_waits_for_approval_then_returns_a_result(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        (proj / "a.txt").write_text("hello\n")
        fleet = rf.LiveFleet(self.ctx, None)
        job = rf.Job("ls", ["ls", "-l"], str(proj), 5.0, "my-ls")
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            jid = fleet.profile("x", job)
        self.w.fetch()
        self.assertEqual(self.result(jid)["status"], "awaiting_approval")
        self.assertEqual(self.ctx.names.get(jid), "my-ls")
        self.assertEqual(self.w.approve(jid), (True, ""))
        self.w.fetch()
        self.w.execute(jid)
        res = fleet.result("x", jid, 5)
        self.assertEqual(res["id"], jid)
        self.assertIn(res["profile"]["verdict"], ("fits", "does-not-fit", "needs-longer", "inconclusive"))
        self.assertEqual(os.getcwd(), self._cwd)                       # the folder change is undone

    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()

    def test_a_command_the_worker_already_answers_for_is_not_reported_as_sent(self):
        (self.repos.results / "registry.json").write_text(json.dumps({"classes": {"always": {"run": ["/bin/true"], "cost": "light", "tier": "auto"}}}))
        fleet = rf.LiveFleet(self.ctx, None)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            with self.assertRaises(rf.Failed):
                fleet.profile("x", rf.Job("x", ["/bin/true"], None, 5.0, "other"))

    def test_the_unattended_worker_needs_no_approval_and_a_waiting_one_asks_ct_admin(self):
        calls = []
        self.ctx.run_admin = lambda args: calls.append(args) or 0
        fleet = rf.LiveFleet(self.ctx, None)
        (self.repos.results / "worker-state.json").write_text(json.dumps({"profile_approval": "none"}))
        self.assertEqual(fleet.approve("x", "profile-aaaaaa"), 0)
        self.assertEqual(calls, [])
        (self.repos.results / "worker-state.json").write_text(json.dumps({"profile_approval": "required"}))
        fleet.approve("x", "profile-aaaaaa")
        self.assertEqual(calls, [["approve", "profile-aaaaaa", "--worker", "x"]])


class Real(unittest.TestCase):
    """The fakes against the real thing."""

    def test_the_fake_fleet_has_the_methods_of_the_live_one(self):
        for name in ("profile", "approve", "result", "adopt", "limit_s"):
            self.assertTrue(callable(getattr(rf.LiveFleet, name, None)), name)
            self.assertTrue(callable(getattr(FakeFleet, name)), name)

    def test_the_profile_command_line_parses_with_the_real_parser(self):
        job = rf.Job(shape="scripts/t", argv=["scripts/t", "test_a", "x y"], folder="/work/proj", est_s=75.0, name="suite-t", packages=True)
        argv = rf.profile_args(job, "profile-k7m2xq")
        ap, _ = cli.build_parser()
        a = ap.parse_args(argv[:argv.index("--")])
        self.assertEqual((a.offload_cmd, a.name, a.id, a.dir, a.est_time_s, a.use_detected_packages), ("profile", "suite-t", "profile-k7m2xq", "/work/proj", 75.0, True))
        self.assertEqual(argv[argv.index("--") + 1:], ["scripts/t", "test_a", "x y"])
        self.assertEqual((a.est_mem_mb, a.what is not None, "placeholder" in a.expect), (rf.MEM_PLACEHOLDER_MB, True, True))     # the approver is told the memory is not a measurement

    def test_the_command_printed_without_a_terminal_carries_every_detail_the_real_command_requires(self):
        argv = rf.profile_args(rf.Job("make lint", ["make", "lint"], None, 45.0, "make-lint"))
        ap, _ = cli.build_parser()
        a = ap.parse_args(argv[:argv.index("--")])
        self.assertTrue(all([a.what, a.expect, a.est_time_s, a.est_mem_mb]))

    def test_no_folder_means_no_dir_argument(self):
        argv = rf.profile_args(rf.Job("scripts/t", ["x"], None, 30.0, "n"), "profile-aaaaaa")
        self.assertNotIn("--dir", argv)

    def test_targets_from_real_worker_files_and_the_real_view(self):
        d = Path(tempfile.mkdtemp())
        (d / "worker-state.json").write_text(json.dumps({"updated": "2999-01-01T00:00:00Z", "locked": False, "classes": []}))
        (d / "registry.json").write_text(json.dumps(dict(REG_OK, classes={})))

        class R:
            results = d

            def pull_results(self):
                pass

            def registry_info(self):
                return json.loads((d / "registry.json").read_text())
        cfgp = d / "w.toml"
        cfgp.write_text('[identity]\nname = "wrk"\n[server]\nhost = "wrk.example"\nadmin_user = "agent"\n[transport]\nkind = "direct"\n')
        v = place.view_of("wrk", config.load(str(cfgp), env={"CT_CONFIG": ""}), R(), now=NOW)
        self.assertEqual(rf.worker_state(v)[0], "ready")
        (d / "registry.json").write_text(json.dumps({"classes": {}}))
        self.assertEqual(rf.worker_state(place.view_of("wrk", config.load(str(cfgp), env={"CT_CONFIG": ""}), R(), now=NOW))[0], "old")

    def test_the_keys_that_mark_a_current_worker_are_ones_the_worker_really_publishes(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "ct" / "worker.py").read_text()
        for key in rf.RELEASE_KEYS:
            self.assertIn(f'"{key}"', src, key)

    def test_the_settings_and_refusals_exist(self):
        self.assertEqual(config.SCHEMA["offload"]["review_ask_again_days"][0], 30)
        self.assertIn("review.answers.unreadable", refusals.CATALOG)
        self.assertIn("review.forget.unknown", refusals.CATALOG)


if __name__ == "__main__":
    unittest.main()
