"""Placing a job on one of several workers: the wiring around ct.sched (reading each worker over its own connection, explaining, remembering where a job went)."""
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from ct import config, sched
from ct.client import cli, place
from ct.client.repo import ClientError


def cfg_file(d, name, kind="direct"):
    p = Path(d) / f"{name}.toml"
    p.write_text(f'[identity]\nname = "{name}"\n[server]\nhost = "{name}.example"\nadmin_user = "agent"\n[transport]\nkind = "{kind}"\n')
    return config.load(str(p), env={"CT_CONFIG": ""})


class FakeRepos:
    """A worker's results clone: real files, real readers; `fail` makes the look at the worker fail the way an unreachable one does."""

    def __init__(self, d, *, running=0, waiting=0, capacity=4, fail=None, locked=False, classes=("sys-status",)):
        self.results = Path(d)
        self.fail = fail
        (self.results / "worker-state.json").write_text(json.dumps({"updated": "2999-01-01T00:00:00Z", "name": "x", "hardware": {"cores": 4, "capacity": capacity}, "isolation": {"ready": True},
                                                                  "locked": locked, "classes": list(classes), "observed": {"running": running, "waiting": waiting, "load_1m": 0.1}}))
        (self.results / "registry.json").write_text(json.dumps({"classes": {c: {"kind": "command", "cost": "light", "tier": "auto", "run": [c]} for c in classes}, "capacity": capacity}))

    def pull_results(self):
        if self.fail:
            raise OSError(self.fail)

    def need_setup(self):
        pass

    def registry_info(self):
        return json.loads((self.results / "registry.json").read_text())

    def read_result(self, jid):
        try:
            return json.loads((self.results / "results" / f"{jid}.json").read_text())
        except OSError:
            return None


class Ctx:
    def __init__(self, name, home):
        self.product, self.home, self.fleet, self.max_in_flight = name, Path(home), (lambda: []), 8


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfgs = {n: cfg_file(self.tmp, n, k) for n, k in (("mini", "direct"), ("vmx", "direct"), ("ghx", "github"))}
        self.repos = {}
        self.ents = lambda cfg: [(n, c, None) for n, c in self.cfgs.items()]

    def add(self, name, **kw):
        d = self.tmp / f"r-{name}"
        d.mkdir(exist_ok=True)
        self.repos[name] = FakeRepos(d, **kw)

    def route(self, forced=None, cls="sys-status", say=None):
        main = Ctx("mini", self.tmp / "home")
        made = []
        r = place.route(main, self.cfgs["mini"], cls, forced, entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]],
                        make_ctx=lambda e: made.append(e[0]) or (main if e[0] == "mini" else Ctx(e[0], self.tmp / f"home-{e[0]}")), say=say)
        return r, main, made


class Dedicated(Setup):
    """A command dedicated to a worker in ~/.tenta.toml goes only there. The worker is NOT reserved: it stays in the running for every other command."""

    def tearDown(self):
        from ct.client import here
        here.global_path().unlink(missing_ok=True)

    def test_the_dedicated_command_goes_there_and_the_worker_still_takes_other_work(self):
        from ct.client import here
        for n in ("vmx", "ghx"):
            self.add(n, classes=("sys-status", "nmap"))
        self.add("mini", classes=("sys-status", "nmap"), running=3)                      # the dedicated worker is the busiest: it is still not left out
        here.save(here.Pins(here.global_path(), commands={"nmap": "mini"}))
        self.assertEqual(here.pin_for("nmap")[0], "mini")                                # sending nmap is forced there (cli.route_submit), however busy it is
        (_, ch), _, _ = self.route(cls="sys-status")
        self.assertEqual(ch.worker, "vmx")
        self.assertEqual(dict((n, v) for n, v, _ in ch.lines)["mini"], "not chosen")      # still eligible for the other command, not left out


class Routing(Setup):
    def test_with_one_worker_nothing_is_read_and_the_given_context_is_used(self):
        main = Ctx("mini", self.tmp)
        ctx, ch = place.route(main, self.cfgs["mini"], "sys-status", None, entries_fn=lambda cfg: [("mini", cfg, None)], repos_of=lambda e: 1 / 0)
        self.assertIs(ctx, main)
        self.assertIsNone(ch)

    def test_the_freest_worker_is_chosen_after_reading_each_over_its_own_connection(self):
        self.add("mini", running=3)
        self.add("vmx", running=1)
        self.add("ghx", running=2)
        (ctx, ch), main, made = self.route()
        self.assertEqual((ch.worker, ctx.product), ("vmx", "vmx"))
        self.assertIs(ctx.fleet, main.fleet)                                  # the job cap counts across the whole fleet, whichever worker got the job
        self.assertEqual(ctx.home_main, main.home)

    def test_an_unreachable_worker_is_left_out_with_what_was_said(self):
        self.add("mini")
        self.add("vmx", fail="ssh: connect timed out")
        self.add("ghx", running=4)
        (ctx, ch), _, _ = self.route()
        self.assertEqual(ch.worker, "mini")
        self.assertIn("connect timed out", dict((n, r) for n, _, r in ch.lines)["vmx"])

    def test_a_forced_worker_is_used_without_reading_the_others_and_an_unknown_name_is_refused(self):
        (ctx, ch), _, made = self.route(forced="ghx")
        self.assertEqual((ctx.product, ch), ("ghx", None))
        with self.assertRaises(ClientError):
            self.route(forced="nope")

    def test_when_nobody_can_take_it_the_error_says_why_for_each(self):
        self.add("mini", locked=True)
        self.add("vmx", classes=("other",))
        self.add("ghx", fail="no route")
        with self.assertRaises(ClientError) as e:
            self.route()
        text = " ".join(e.exception.steps)
        for word in ("LOCKED", "does not offer", "no route"):
            self.assertIn(word, text)

    def test_the_reason_is_said_before_sending(self):
        self.add("mini")
        self.add("vmx", running=1)
        self.add("ghx", running=1)
        said = []
        self.route(say=said.append)
        self.assertEqual(said[0].worker, "mini")


class Placements(Setup):
    def test_where_a_job_went_is_remembered_and_the_result_is_looked_up_there(self):
        home = self.tmp / "home"
        place.record(home, ["j-1", "j-2"], "vmx")
        self.assertEqual(place.worker_of(home, "j-1"), "vmx")
        self.assertIsNone(place.worker_of(home, "unknown"))
        main = Ctx("mini", home)
        ctx = place.for_job(main, self.cfgs["mini"], "j-1", make_ctx=lambda c: Ctx(c.identity.name, self.tmp), entries_fn=self.ents)
        self.assertEqual(ctx.product, "vmx")
        self.assertIs(place.for_job(main, self.cfgs["mini"], "never-sent", entries_fn=self.ents), main)

    def test_a_damaged_placements_file_is_not_a_crash(self):
        home = self.tmp / "home"
        home.mkdir()
        (home / "placements.json").write_text("{nope")
        self.assertIsNone(place.worker_of(home, "j-1"))
        place.record(home, ["j-1"], "vmx")
        self.assertEqual(place.worker_of(home, "j-1"), "vmx")


class TheWhereCommand(Setup):
    def test_where_explains_every_worker_and_sends_nothing(self):
        self.add("mini", running=3)
        self.add("vmx", running=0)
        self.add("ghx", fail="down")
        main = mock.Mock(repos=self.repos["mini"], product="mini")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ct.client.place.entries", self.ents), mock.patch("ct.client.cli.ClientRepos.from_config", side_effect=lambda c: self.repos[c.identity.name]), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["where", "sys-status"], ctx=main, cfg=self.cfgs["mini"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 0)
        for word in ("vmx", "CHOSEN", "more capacity free", "down", "--worker vm"):
            self.assertIn(word, text)


class TheClassesCommand(Setup):
    def test_with_several_workers_each_class_says_which_workers_offer_it(self):
        self.add("mini", classes=("sys-status", "nmap"))
        self.add("vmx", classes=("sys-status",))
        self.add("ghx", fail="down")
        main = mock.Mock(repos=self.repos["mini"], product="mini", home=self.tmp / "home", admin="ct-admin")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ct.client.place.entries", self.ents), mock.patch("ct.client.cli.ClientRepos.from_config", side_effect=lambda c: self.repos[c.identity.name]), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["classes"], ctx=main, cfg=self.cfgs["mini"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 0, text)
        import re
        plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
        block = lambda c: re.split(rf"CLASS\s+{c}\n", plain)[1].split("COST")[0]           # noqa: E731  (the rows of one class)
        self.assertRegex(block("sys-status"), r"RUNS ON\s+mini\n\s+vmx\n")                  # each worker on its own line under the value column
        self.assertIn("mini", block("nmap"))
        self.assertNotIn("vmx", block("nmap"))
        self.assertIn("ghx could not be read", text)

    def test_an_unknown_worker_name_is_refused_with_the_list(self):
        self.add("mini")
        main = mock.Mock(repos=self.repos["mini"], product="mini")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ct.client.place.entries", self.ents), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["classes", "--worker", "nope"], ctx=main, cfg=self.cfgs["mini"])
        self.assertEqual(rc, 2)
        self.assertIn("Yours: mini, vmx, ghx", out.getvalue() + err.getvalue())


class TheJobsList(Setup):
    """ct-run jobs with several workers: every worker's jobs together, newest first, each with its worker; one that cannot be read is named."""

    def rows(self, when, jid):
        from datetime import datetime, timezone
        return {jid: {"id": jid, "status": "ok", "class": "sys-status"}}, [(datetime(2026, 11, 19, 12, when, tzinfo=timezone.utc), "ok", jid, "", "sys-status")]

    def run_jobs(self, by_repo):
        main = mock.Mock(repos=self.repos["mini"], product="mini", home=self.tmp / "home", admin="ct-admin", label_of=lambda jid, cls: cls, hints=[])
        out, err = io.StringIO(), io.StringIO()

        def jobs_of(repos, admin):
            r = by_repo[next(n for n, x in self.repos.items() if x is repos)]
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("ct.client.place.entries", self.ents), mock.patch("ct.client.cli.ClientRepos.from_config", side_effect=lambda c: self.repos[c.identity.name]), \
                mock.patch("ct.client.cli.jobs_of", jobs_of), redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["jobs"], ctx=main, cfg=self.cfgs["mini"])
        return rc, re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue() + err.getvalue())

    def test_jobs_of_every_worker_are_listed_newest_first_with_the_worker(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        rc, text = self.run_jobs({"mini": self.rows(5, "job-aaa111"), "vmx": self.rows(9, "job-bbb222"), "ghx": self.rows(7, "job-ccc333")})
        self.assertEqual(rc, 0, text)
        order = [text.index(j) for j in ("job-bbb222", "job-ccc333", "job-aaa111")]
        self.assertEqual(order, sorted(order))
        for jid, worker in (("job-bbb222", "vmx"), ("job-ccc333", "ghx"), ("job-aaa111", "mini")):
            block = re.search(rf"JOB\s+{jid}\n((?:  .+\n)+)", text).group(1)                     # the job's own rows, up to the blank line
            self.assertRegex(block, rf"WORKER\s+{worker}")

    def test_a_worker_that_cannot_be_read_is_named_and_the_others_still_listed(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        rc, text = self.run_jobs({"mini": self.rows(5, "job-aaa111"), "vmx": ClientError("Could not fetch", "network down"), "ghx": self.rows(7, "job-ccc333")})
        self.assertEqual(rc, 0, text)
        self.assertIn("could not be read", text)
        self.assertIn("vmx", text)
        self.assertIn("job-ccc333", text)


if __name__ == "__main__":
    unittest.main()


class TheConnectionReallyUsed(Setup):
    """Found on the real fleet: the configuration said Direct, the copies of the queue pointed at GitHub, and the worker listened on GitHub. Jobs go where the copy points."""

    def repo_with_origin(self, url):
        import subprocess
        d = self.tmp / "clone"
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True)
        return d

    def test_the_copy_decides_not_the_configuration(self):
        class R:
            queue = self.repo_with_origin("git@github.com:someone/q.git")
        self.assertEqual(place.actual_connection(R, "direct"), "github")
        import subprocess
        subprocess.run(["git", "-C", str(R.queue), "remote", "set-url", "origin", "ssh://" + "tenta-mail" + "@" + "box.example/var/lib/x/queue.git"], check=True)
        self.assertEqual(place.actual_connection(R, "github"), "direct")

    def test_with_no_copy_to_look_at_the_configuration_is_used(self):
        class R:
            queue = self.tmp / "nowhere"
        self.assertEqual(place.actual_connection(R, "direct"), "direct")


class PendingJobsAreLoad(Setup):
    def test_jobs_sent_to_a_worker_without_a_final_result_are_counted_and_old_or_finished_ones_are_not(self):
        self.add("vmx")
        (self.repos["vmx"].results / "results").mkdir()
        (self.repos["vmx"].results / "results" / "j-done.json").write_text(json.dumps({"status": "ok"}))
        (self.repos["vmx"].results / "results" / "j-running.json").write_text(json.dumps({"status": "running"}))
        home = self.tmp / "home"
        place.record(home, ["j-done", "j-running", "j-nothing-yet"], "vmx")
        place.record(home, ["j-elsewhere"], "mini")
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"]), 2)
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"], now=__import__("time").time() + 3600), 0)           # an hour later: too old to say anything about now
        self.assertEqual(place.pending_sent(self.tmp / "nowhere", "vmx", self.repos["vmx"]), 0)

    def test_a_burst_through_route_spreads_because_each_placement_is_remembered(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        placed = []
        for _ in range(9):
            (ctx, ch), main, _ = self.route()
            place.record(main.home, [f"j-{len(placed)}"], ch.worker)
            placed.append(ch.worker)
        self.assertEqual({w: placed.count(w) for w in ("mini", "vmx", "ghx")}, {"mini": 3, "vmx": 3, "ghx": 3})


class ManyPlacementsAtOnce(Setup):
    """Found by the second load test: eight simultaneous submits each looked at the workers before any had recorded where it went, so all eight chose the same one."""

    def test_simultaneous_placements_spread_because_the_choice_and_its_reservation_are_one_step(self):
        import threading
        import time
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        main = Ctx("mini", self.tmp / "home")
        placed, start = [], threading.Barrier(9)

        def slow_fetch(repos):
            time.sleep(0.15)                                 # a real look takes a second or two: the window in which every process saw the same picture

        def one():
            start.wait()
            ctx, ch = place.route(main, self.cfgs["mini"], "sys-status", None, entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], make_ctx=lambda e: main if e[0] == "mini" else Ctx(e[0], self.tmp / f"h-{e[0]}"), fetch=slow_fetch)
            placed.append(ch.worker)
        threads = [threading.Thread(target=one) for _ in range(9)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual({w: placed.count(w) for w in ("mini", "vmx", "ghx")}, {"mini": 3, "vmx": 3, "ghx": 3})

    def test_a_reservation_counts_as_load_until_released_and_expires_on_its_own(self):
        self.add("vmx")
        home = self.tmp / "home"
        tok = place.reserve(home, "vmx")
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"]), 1)
        place.release(home, tok)
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"]), 0)
        place.reserve(home, "vmx")
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"], now=__import__("time").time() + 300), 0)        # a send that died leaves nothing behind for long

    def test_records_written_at_the_same_moment_are_all_kept(self):
        import threading
        home = self.tmp / "home"
        ts = [threading.Thread(target=place.record, args=(home, [f"j-{i}"], "vmx")) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(json.loads((home / "placements.json").read_text())), 20)


class PendingJobsCountInCapacityPoints(Setup):
    def test_a_pending_heavy_job_weighs_four_a_light_one_and_an_unknown_class_one(self):
        self.add("vmx", classes=("sys-status", "profile-run"))
        (self.repos["vmx"].results / "registry.json").write_text(json.dumps({"classes": {"sys-status": {"cost": "light"}, "profile-run": {"cost": "heavy"}, "claude-read": {"cost": "medium"}}, "capacity": 4}))
        home = self.tmp / "home"
        place.record(home, ["j-heavy"], "vmx", "profile-run")
        place.record(home, ["j-medium"], "vmx", "claude-read")
        place.record(home, ["j-light"], "vmx", "sys-status")
        place.record(home, ["j-old-style"], "vmx")                            # recorded before classes were: counts as one
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"]), 4 + 2 + 1 + 1)

    def test_a_reservation_for_a_class_weighs_what_the_class_weighs(self):
        self.add("vmx")
        (self.repos["vmx"].results / "registry.json").write_text(json.dumps({"classes": {"profile-run": {"cost": "heavy"}}, "capacity": 4}))
        home = self.tmp / "home"
        place.reserve(home, "vmx", "profile-run")
        self.assertEqual(place.pending_sent(home, "vmx", self.repos["vmx"]), 4)

    def test_a_heavy_burst_goes_one_per_worker_because_each_takes_all_its_capacity(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n, classes=("profile-run",))
            (self.repos[n].results / "registry.json").write_text(json.dumps({"classes": {"profile-run": {"kind": "profile", "cost": "heavy"}}, "capacity": 4}))
        placed = []
        for _ in range(3):
            (ctx, ch), main, _ = self.route(cls="profile-run")
            placed.append(ch.worker)
        self.assertEqual(sorted(placed), ["ghx", "mini", "vmx"])


class ABurstReadsEachWorkerOnce(Setup):
    """The second load test: eight simultaneous sends each re-read every worker over the network while holding the placement lock, so the sends took 18 s. A snapshot for a few seconds lets them share one look;
    the load from this computer's own record is still worked out fresh for each, so the burst still spreads."""

    def route_n(self, n, fetch):
        out = []
        for _ in range(n):
            (ctx, ch), main, _ = self.route_with(fetch)
            place.record(main.home, [f"j-{len(out)}"], ch.worker, "sys-status")
            out.append(ch.worker)
        return out

    def route_with(self, fetch):
        main = Ctx("mini", self.tmp / "home")
        r = place.route(main, self.cfgs["mini"], "sys-status", None, entries_fn=self.ents, repos_of=lambda e: self.repos[e[0]], make_ctx=lambda e: main if e[0] == "mini" else Ctx(e[0], self.tmp / f"h-{e[0]}"), fetch=fetch)
        return r, main, None

    def test_placements_within_a_few_seconds_share_one_look_at_the_workers(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        looks = []
        placed = self.route_n(6, lambda repos: looks.append(1))
        self.assertEqual(len(looks), 3)                                         # one look per worker, not one per placement
        self.assertEqual({w: placed.count(w) for w in ("mini", "vmx", "ghx")}, {"mini": 2, "vmx": 2, "ghx": 2})      # and the burst still spreads

    def test_an_old_snapshot_is_not_used(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        looks = []
        self.route_n(1, lambda repos: looks.append(1))
        with mock.patch("ct.client.place.time.time", return_value=__import__("time").time() + 60):
            self.route_n(1, lambda repos: looks.append(1))
        self.assertEqual(len(looks), 6)

    def test_a_snapshot_never_hides_a_worker_that_was_added_or_removed(self):
        for n in ("mini", "vmx", "ghx"):
            self.add(n)
        looks = []
        self.route_n(1, lambda repos: looks.append(1))
        drop = self.ents
        self.ents = lambda cfg: [e for e in drop(cfg) if e[0] != "ghx"]
        self.route_n(1, lambda repos: looks.append(1))
        self.assertEqual(len(looks), 5)                                         # the set of workers changed: looked at again
