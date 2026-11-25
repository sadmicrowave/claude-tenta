"""ct-admin: the workers command, locating a worker, and the commands that act on the whole fleet."""
import io
import re
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from admin_support import AdminBase  # noqa: F401  (also points HOME at a throwaway folder)

from ct.admin import cli, release
from ct.admin import workers as workers_mod
from ct.client.repo import ClientError


class WakeAndAudit(AdminBase):
    """One short command reaches every worker, or the one named, with nothing but its name: its address and account come from its own configuration."""

    def test_wake_and_audit_reach_the_main_worker_with_no_paths_or_options(self):
        self.ctx.workers_file = self.tmp / "workers.toml"
        self.assertEqual(self.admin("wake")[0], 0)
        self.assertEqual(self.admin("audit")[0], 0)
        self.assertEqual(self.admin("tripwire-test")[0], 0)
        self.assertEqual(self.ssh.calls, ["ct-worker wake", "sudo ct-update --audit", "sudo ct-update --tripwire-test"])

    def test_a_worker_is_named_once_and_found_by_that_name(self):
        self.ctx.workers_file = self.tmp / "workers.toml"
        code, out, err = self.admin("wake", "--worker", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("There is no such worker", out + err)
        self.assertIn("nobody", out + err)
        self.assertEqual(self.ssh.calls, [])
        self.assertIs(self.ctx.for_worker("testct"), self.ctx)                    # the main worker answers to its own name


def state_doc(**over):
    base = {"v": 1, "updated": "2026-11-16T12:00:00Z", "name": "w", "release": "9032dfbd1aaaa", "os": {"id": "ubuntu", "version": "26.04"},
            "hardware": {"cores": 2, "mem_total_mb": 15219, "cpu": "X", "native_claude": True, "capacity": 4},
            "claude": {"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}}, "isolation": {"ready": True, "method": "bwrap"},
            "classes": ["a", "b"], "packages": ["apt:jq==1.7"], "approvers": [], "locked": False,
            "observed": {"load_1m": 0.3, "temp_c": 52, "mem_available_mb": 14000, "running": 0, "waiting": 0}}
    base.update(over)
    return base


class Fleet:
    """Stands in for a second worker's repositories: just its results folder."""

    def __init__(self, results, fail=False):
        self.results, self.fail = results, fail

    def pull_results(self):
        if self.fail:
            raise RuntimeError("network down")

    def registry_info(self):
        return None                                   # the real ClientRepos reads registry.json from the results clone; a stand-in worker published none


class WorkersCommand(AdminBase):
    NOW = datetime_now = None

    def setUp(self):
        super().setUp()
        self.other = self.tmp / "other-results"
        self.other.mkdir()
        self.ctx.fleet = lambda: [("testct", self.repos), ("studio", Fleet(self.other, False))]
        self.ctx.workers_file = self.tmp / "workers.toml"
        self.put_state(self.repos.results, state_doc(updated=self.fresh()))
        self.put_state(self.other, state_doc(updated=self.fresh(), claude={"auth": "none"}, hardware={"cores": 8, "mem_total_mb": 32000, "native_claude": True}))

    def fresh(self):
        import time as _t
        return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())

    def put_state(self, folder, doc):
        (Path(folder) / "worker-state.json").write_text(json.dumps(doc))

    def test_every_worker_is_a_row_with_hardware_and_claude_state_side_by_side(self):
        code, out, err = self.admin("workers")
        self.assertEqual(code, 0, err)
        for needle in ("TESTCT", "STUDIO", "account: 2.1.325", "none", "ubuntu 26.04", "2 cores", "8 cores", "jail ON"):
            self.assertIn(needle, out)

    def test_claude_filter_keeps_only_workers_with_working_claude_access(self):
        code, out, _ = self.admin("workers", "--claude")
        self.assertEqual(code, 0)
        self.assertIn("TESTCT", out)
        self.assertNotIn("STUDIO", out)

    def test_a_locked_worker_or_one_whose_claude_tool_is_missing_is_not_a_claude_candidate(self):
        self.put_state(self.repos.results, state_doc(updated=self.fresh(), locked=True))
        self.put_state(self.other, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "missing", "version": ""}}))
        code, out, err = self.admin("workers", "--claude")
        self.assertEqual(code, 1)
        self.assertIn("No worker matches", out + err)

    def test_the_older_pinned_release_is_labelled_so_it_can_be_compared(self):
        self.put_state(self.other, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "legacy", "version": "2.1.112"}},
                                             hardware={"cores": 2, "mem_total_mb": 15000, "native_claude": False}))
        _, out, _ = self.admin("workers")
        self.assertIn("2.1.112 (older release, pinned)", out)
        self.assertIn("old CPU", out)

    def test_a_token_near_its_end_is_visible_and_an_expired_one_is_not_a_claude_candidate(self):
        self.put_state(self.repos.results, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}, "token_days": 340}))
        _, out, _ = self.admin("workers")
        self.assertIn("token 340 days old (renew soon)", out)
        self.put_state(self.repos.results, state_doc(updated=self.fresh(), claude={"auth": "account", "tool": {"kind": "native", "version": "2.1.325"}, "token_days": 400}))
        code, out, _ = self.admin("workers", "--claude")
        self.assertNotIn("TESTCT", out)

    def test_json_is_machine_readable_for_a_future_scheduler(self):
        code, out, _ = self.admin("workers", "--json")
        rows = json.loads(out)
        self.assertEqual([r["name"] for r in rows], ["testct", "studio"])
        self.assertEqual((rows[0]["has_claude"], rows[1]["has_claude"]), (True, False))

    def test_a_worker_that_published_nothing_is_a_row_not_a_crash(self):
        (self.other / "worker-state.json").unlink()
        code, out, _ = self.admin("workers")
        self.assertEqual(code, 0)
        self.assertIn("no state published", out)

    def test_an_old_reading_is_marked_stale(self):
        self.put_state(self.other, state_doc(updated="2020-01-01T00:00:00Z"))
        _, out, _ = self.admin("workers")
        self.assertIn("(STALE)", out)

    def test_a_worker_that_cannot_be_refreshed_is_noted_and_its_last_state_still_shown(self):
        self.ctx.fleet = lambda: [("testct", self.repos), ("studio", Fleet(self.other, True))]
        code, out, err = self.admin("workers")
        self.assertEqual(code, 0)
        self.assertIn("studio: could not refresh", out + err)
        self.assertIn("STUDIO", out)

    def test_adding_and_removing_a_worker_edits_the_list_and_validates_it(self):
        cfg = self.tmp / "studio.toml"
        cfg.write_text("[identity]\nname = \"studio\"\n")
        self.assertEqual(self.admin("workers", "add", "studio", "--config", str(cfg))[0], 0)
        self.assertEqual(workers_mod.load_list(self.ctx.workers_file), {"studio": str(cfg)})
        self.assertEqual(self.admin("workers", "remove", "studio")[0], 0)
        self.assertEqual(workers_mod.load_list(self.ctx.workers_file), {})
        self.assertEqual(self.admin("workers", "add", "Bad Name", "--config", str(cfg))[0], 2)
        self.assertEqual(self.admin("workers", "add", "x", "--config", str(self.tmp / "nope.toml"))[0], 2)
        self.assertEqual(self.admin("workers", "remove", "ghost")[0], 2)


class Locate(AdminBase):
    """ct-admin locate: the worker's address changed; it is found by the SSH host key this computer recorded for it, and a name is never proof."""
    KEY = {("ssh-ed25519", "AAAA-the-worker")}

    def setUp(self):
        super().setUp()
        self.conf = self.tmp / "testct.toml"
        self.conf.write_text('[identity]\nname = "testct"\n[server]\nhost = "worker.example"\nadmin_user = "admin"\n')
        self.ctx.cfg = mock.Mock(source=str(self.conf))
        self.ctx.ssh.port = 22
        from ct.setup import discover
        self.discover = discover
        self.devs = [discover.Device("192.0.2.31", name="worker.example"), discover.Device("192.0.2.32", name="another.example")]

    def locate(self, *args, shown=None, recorded=None, answers=("y",)):
        self.answers = list(answers)
        self.tty = True
        keys = shown or {"192.0.2.31": {("ssh-ed25519", "IMPOSTOR")}, "192.0.2.32": self.KEY}            # the machine with the worker's NAME shows another key
        with mock.patch("ct.rediscover.recorded_keys", return_value=self.KEY if recorded is None else recorded), \
             mock.patch("ct.rediscover.shown_keys", side_effect=lambda ip, port=22, run=None: keys.get(ip, set())), \
             mock.patch("ct.rediscover.resolve_mdns", return_value=None), mock.patch("ct.rediscover._arp", return_value={}), \
             mock.patch.object(self.discover, "local_networks", return_value=[("en0", __import__("ipaddress").IPv4Interface("192.0.2.5/24"))]), mock.patch.object(self.discover, "scan", return_value=(self.devs, [])):
            return self.admin("locate", *args)

    def test_the_machine_with_the_recorded_key_is_found_and_its_address_saved_after_asking(self):
        code, out, err = self.locate()
        self.assertEqual(code, 0, out + err)
        self.assertIn("192.0.2.32", out)
        self.assertIn('host = "192.0.2.32"  # was: worker.example', self.conf.read_text())

    def test_saying_no_changes_nothing(self):
        code, out, _ = self.locate(answers=("n",))
        self.assertEqual(code, 0)
        self.assertIn('host = "worker.example"', self.conf.read_text())
        self.assertIn("Nothing was changed", out)

    def test_a_worker_that_never_connected_here_cannot_be_found_and_nothing_is_guessed(self):
        code, out, _ = self.locate(recorded=set())
        self.assertEqual(code, 1)
        self.assertIn("NOT FOUND", out.upper())
        self.assertIn('host = "worker.example"', self.conf.read_text())

    def test_a_worker_still_at_its_address_is_left_alone_with_no_scan(self):
        keys = {"worker.example": self.KEY}
        with mock.patch.object(self.discover, "scan", side_effect=AssertionError("no scan needed")):
            code, out, _ = self.locate(shown=keys)
        self.assertEqual(code, 0)
        self.assertIn("is where its configuration says", out)


class RememberingWhereTheWorkerAnswered(AdminBase):
    """Found testing on a real machine: the last address that worked was only written by `watch` and `locate`, so a user who never ran them had nothing for that rung."""

    def test_an_ordinary_successful_command_notes_the_address_and_a_failed_one_does_not(self):
        from ct import rediscover
        with mock.patch("socket.gethostbyname", return_value="192.0.2.44"), mock.patch("ct.rediscover._arp", return_value={}):
            self.ssh._run = lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", "no route")
            self.admin("check")
            self.assertEqual(rediscover.last_seen(self.home, "testct"), {})
            self.ssh._run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")
            self.admin("check")
        self.assertEqual(rediscover.last_seen(self.home, "testct")["ip"], "192.0.2.44")


class ALostWorkerThatWasReinstalled(Locate):
    def test_a_machine_with_no_matching_key_says_a_reinstalled_worker_has_a_new_key_and_how_to_trust_it(self):
        code, out, _ = self.locate(shown={"192.0.2.31": {("ssh-ed25519", "OTHER")}, "192.0.2.32": {("ssh-ed25519", "OTHER2")}})
        self.assertEqual(code, 1)
        self.assertIn("ssh-keygen -R worker.example", out)
        self.assertIn("new SSH key", out)


class ACommandOnAJobSentToAnotherWorker(AdminBase):
    """A job the scheduler placed on another worker is approved, read and stopped THERE, by the same command with the same id."""

    def test_the_context_of_the_worker_it_was_sent_to_is_used(self):
        from ct.client import place
        place.record(self.ctx.home, ["j-elsewhere"], "other")
        seen = []
        other = mock.Mock(name="other-ctx")
        other.name = "other"
        with mock.patch.object(self.ctx, "for_worker", side_effect=lambda n: seen.append(n) or other), mock.patch.dict(cli.DISPATCH, {"output": lambda c, a: seen.append(("ran-on", c.name)) or 0}):
            self.admin("output", "j-elsewhere")
        self.assertEqual(seen, ["other", ("ran-on", "other")])

    def test_a_job_with_no_recorded_placement_stays_on_the_main_worker(self):
        seen = []
        with mock.patch.dict(cli.DISPATCH, {"output": lambda c, a: seen.append(c.name) or 0}):
            self.admin("output", "j-here")
        self.assertEqual(seen, ["testct"])


class EveryCommandKnowsWhichWorkersItActsOn(AdminBase):
    """The standing rule: a command about one worker is never scheduled, and a command that acts on workers acts on every one unless --worker narrows it. Every ct-admin command is classified here,
    so a new command cannot be added without saying which it is."""
    FLEET = {"apply", "diff", "check", "status", "quarantine", "unlock"}                  # every worker, one after the other, each asked its own questions
    PICK = {"update", "clean", "adopt", "retire", "class", "action", "proposals"}                   # one worker: the main one, or the one named with --worker
    OWN_FLAG = {"wake", "audit", "tripwire-test", "locate"}                                # already every worker (or the one named) with their own --worker
    BY_NATURE = {"watch", "workers"}                                                       # look at the whole fleet by definition
    BY_JOB = {"approve", "review", "cancel", "kill", "output"}                             # follow the job to the worker it was sent to (placements)
    LOCAL = {"theme", "action-hash"}                                                       # about this computer only

    def test_every_command_is_in_exactly_one_class_and_the_flag_exists_where_it_should(self):
        ap, sp = cli.build_parser()
        classes = [self.FLEET, self.PICK, self.OWN_FLAG, self.BY_NATURE, self.BY_JOB, self.LOCAL]
        every = set(sp.choices)
        self.assertEqual(set().union(*classes), every, "classify the new command above")
        self.assertEqual(sum(len(c) for c in classes), len(every), "a command is in two classes")
        for name in self.FLEET | self.PICK | self.OWN_FLAG:
            self.assertTrue(any("--worker" in a.option_strings for a in sp.choices[name]._actions), f"{name} has no --worker")
        self.assertEqual(set(cli.FLEET_COMMANDS), self.FLEET)
        self.assertEqual(set(cli.PICK_COMMANDS), self.PICK)

    def test_no_internal_send_is_left_to_the_scheduler(self):
        """The checks that are ABOUT a worker (the install check, the wizard's first job) must say so: --here."""
        import re
        root = Path(__file__).resolve().parents[1] / "src" / "ct"
        for f in root.rglob("*.py"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r'\.main\(\["submit"', line):
                    self.assertIn('"--here"', line, f"{f.name}:{n} sends a job without pinning it to the worker it is about")


class FleetWideCommands(AdminBase):
    def two(self):
        a, b = mock.Mock(name="a"), mock.Mock(name="b")
        a.name, b.name = "first", "second"
        return a, b

    def run_diff(self, *extra, targets=None, inner=None):
        a, b = self.two()
        seen = []
        with mock.patch.object(self.ctx, "targets", side_effect=lambda n=None: seen.append(n) or (targets or [("first", a), ("second", b)])), \
                mock.patch.dict(cli.DISPATCH, {"diff": inner or (lambda c, args: calls.append(c.name) or 0)}):
            calls = []
            out = self.admin("diff", *extra)
        return out, calls, seen

    def test_every_worker_is_visited_with_a_heading_each_when_there_are_several(self):
        (code, out, err), calls, seen = self.run_diff()
        self.assertEqual((code, calls, seen), (0, ["first", "second"], [None]))
        self.assertIn("first", out + err)
        self.assertIn("second", out + err)

    def test_the_next_steps_footer_is_said_once_after_the_last_worker_not_under_every_card(self):
        """Seen on `ct-admin status` with three workers: 'From here you can' and its three commands under every card."""
        from ct import ui as _ui

        def inner(c, args):
            print(f"card for {c.name}")
            _ui.related(("Jobs waiting for you", "ct-admin review"), ("Compare every worker you run", "ct-admin workers"), force=True)
            return 0
        (code, out, err), calls, seen = self.run_diff(inner=inner)
        text = out + err
        self.assertEqual(code, 0)
        self.assertEqual(text.count("From here you can"), 1, text)
        self.assertEqual(text.count("ct-admin review"), 1, text)
        self.assertEqual(text.count("card for"), 2)                                     # the content is still repeated for every worker
        self.assertGreater(text.index("From here you can"), text.rindex("card for"))              # and the footer comes after the last of them

    def test_a_single_worker_still_gets_its_footer_straight_after_its_card(self):
        from ct import ui as _ui
        a, b = self.two()
        (code, out, err), calls, seen = self.run_diff(targets=[("first", a)], inner=lambda c, args: _ui.related(("Jobs waiting for you", "ct-admin review"), force=True) and 0)
        self.assertEqual((out + err).count("From here you can"), 1)

    def test_worker_narrows_it_to_the_one_named(self):
        a, b = self.two()
        (code, out, err), calls, seen = self.run_diff("--worker", "second", targets=[("second", b)])
        self.assertEqual((calls, seen), (["second"], ["second"]))

    def test_one_worker_failing_or_being_declined_does_not_skip_the_others_and_is_reflected_in_the_exit_code(self):
        def inner(c, args):
            calls.append(c.name)
            if c.name == "first":
                raise ClientError("first is unreachable")
            return 0
        calls = []
        (code, out, err), called, _ = self.run_diff(inner=inner)
        self.assertEqual(called if called else calls, ["first", "second"])
        self.assertNotEqual(code, 0)
        self.assertIn("first is unreachable", out + err)

    def test_a_single_worker_is_run_exactly_as_before_with_no_heading(self):
        a, _ = self.two()
        (code, out, err), calls, _ = self.run_diff(targets=[("first", a)])
        self.assertEqual((code, calls), (0, ["first"]))
        self.assertNotIn("WORKER", out + err)


class ApplyOnAnAlreadyCurrentWorker(AdminBase):
    """Seen on a real `ct-admin apply`: an up-to-date worker got two 'Nothing to change' cards, and a hint (--force) that would have forced every worker."""

    def run_apply(self, *extra, targets=1):
        sha = self.rel[0]
        out = io.StringIO()
        with mock.patch.object(release, "installed_rev", return_value=sha), mock.patch.object(self.ctx.source, "resolve", return_value=sha):
            with redirect_stdout(out), redirect_stderr(out):
                code = cli.main(["apply", *extra], ctx=self.ctx)
        return code, out.getvalue()

    def test_it_says_nothing_to_change_once(self):
        code, text = self.run_apply()
        self.assertEqual(code, 0)
        self.assertEqual(text.count("Nothing to change"), 1, text)

    def test_it_refuses_a_worker_of_another_platform_and_changes_nothing(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"platform": "windows"}))
        code, text = self.run_apply("--force")
        self.assertEqual(code, 1, text)
        self.assertIn("This release is not for that worker", text)
        self.assertIn("windows", text)
        self.assertNotIn("Install ", text)

    def test_a_worker_that_publishes_no_platform_is_linux(self):
        (self.repos.results / "worker-state.json").write_text(json.dumps({"release": "0" * 40}))
        code, text = self.run_apply()
        self.assertNotIn("not for that worker", text)

    def test_the_force_hint_names_the_worker_so_it_does_not_force_them_all(self):
        code, text = self.run_apply()
        self.assertIn(f"ct-admin apply --force --worker {self.ctx.name}", text)


if __name__ == "__main__":
    unittest.main()
