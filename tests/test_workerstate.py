import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import workerstate  # noqa: E402


class Pieces(unittest.TestCase):
    def test_os_release_and_cpu_model(self):
        self.assertEqual(workerstate.parse_os_release('ID=ubuntu\nVERSION_ID="26.04"\n'), {"id": "ubuntu", "version": "26.04"})
        self.assertEqual(workerstate.cpu_model("processor : 0\nmodel name\t: Intel(R) Core(TM)2 Duo   CPU  P8800 @ 2.66GHz\n"), "Intel(R) Core(TM)2 Duo CPU P8800 @ 2.66GHz")
        self.assertEqual(workerstate.cpu_model(""), "")

    def test_packages_are_every_package_the_installed_classes_name_sorted_once(self):
        reg = {"classes": {"a": {"packages": ["apt:jq==1.7", "pip:rich==13"]}, "b": {"packages": ["apt:jq==1.7"]}, "c": {}}}
        self.assertEqual(workerstate.packages_of(reg), ["apt:jq==1.7", "pip:rich==13"])
        self.assertEqual(workerstate.packages_of({}), [])


class Jail:
    name = "bwrap"

    def ready(self):
        return True


class Build(unittest.TestCase):
    EXTRA = {"name": "w1", "release": "abc", "os": {"id": "ubuntu", "version": "26.04"}, "cpu": "X", "native_claude": False, "capacity": 4,
             "claude": {"auth": "account", "tool": {"kind": "legacy", "version": "2.1.112"}}, "approvers": ["SHA256:k"]}
    SNAP = {"at": "t", "cores": 2, "mem_total_mb": 15000, "mem_available_mb": 14000, "load_1m": 0.2, "temp_c": 50, "disk_free_gb": 400, "running": 0, "waiting": 1}

    def test_the_environment_and_the_observed_readings_are_kept_apart(self):
        env, doc = workerstate.build(snapshot=self.SNAP, registry={"classes": {"x": {}}}, extra=self.EXTRA, jail=Jail(), locked=False, now_iso="now")
        self.assertEqual(doc["claude"]["tool"]["version"], "2.1.112")
        self.assertEqual((doc["hardware"]["cores"], doc["hardware"]["native_claude"], doc["isolation"]), (2, False, {"ready": True, "method": "bwrap", "mode": "full", "purpose": "tenta-only", "tool_roots": []}))
        self.assertEqual(doc["classes"], ["x"])
        self.assertEqual(doc["observed"]["load_1m"], 0.2)
        self.assertNotIn("observed", env)
        self.assertNotIn("updated", env)

    def test_only_a_change_of_environment_changes_the_digest(self):
        a, _ = workerstate.build(snapshot=self.SNAP, registry={}, extra=self.EXTRA, jail=Jail(), locked=False, now_iso="1")
        b, _ = workerstate.build(snapshot={**self.SNAP, "load_1m": 9.9, "temp_c": 80}, registry={}, extra=self.EXTRA, jail=Jail(), locked=False, now_iso="2")
        c, _ = workerstate.build(snapshot=self.SNAP, registry={}, extra=self.EXTRA, jail=Jail(), locked=True, now_iso="3")
        d, _ = workerstate.build(snapshot=self.SNAP, registry={"classes": {"new": {"packages": ["apt:x==1"]}}}, extra=self.EXTRA, jail=Jail(), locked=False, now_iso="4")
        self.assertEqual(workerstate.digest(a), workerstate.digest(b))            # load and heat are readings, not state
        self.assertNotEqual(workerstate.digest(a), workerstate.digest(c))         # a lockdown is
        self.assertNotEqual(workerstate.digest(a), workerstate.digest(d))         # a new class and package are


class ToolCheck(unittest.TestCase):
    """Which tool a class with tool folders would really run, looked at on the worker (published in worker-state.json)."""

    def setUp(self):
        import os
        import tempfile
        self.base = os.path.realpath(tempfile.mkdtemp())
        for d in ("a/bin", "b"):
            os.makedirs(f"{self.base}/tools/{d}")
        for path in (f"{self.base}/tools/a/bin/scan", f"{self.base}/tools/b/scan", f"{self.base}/tools/b/only-b"):
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
        self.root = f"{self.base}/tools"

    def check(self, run, tools, roots=None, mode="relaxed"):
        reg = {"classes": {"scan": {"kind": "command", "run": run, "tools": tools}, "plain": {"kind": "command", "run": ["/bin/echo"]}}}
        return workerstate.tool_check(reg, [self.root] if roots is None else roots, mode)

    def test_the_first_folder_that_holds_the_program_wins_in_the_order_given(self):
        out = self.check(["scan"], [f"{self.root}/a/bin", f"{self.root}/b"])
        self.assertEqual((out["scan"]["found"], out["scan"]["where"]), (f"{self.root}/a/bin/scan", "tool folder"))
        out = self.check(["scan"], [f"{self.root}/b", f"{self.root}/a/bin"])
        self.assertEqual(out["scan"]["found"], f"{self.root}/b/scan")
        self.assertNotIn("plain", out)                                                         # a class without tool folders is not listed

    def test_a_program_in_no_tool_folder_falls_back_to_the_system_or_is_not_found(self):
        self.assertEqual(self.check(["ls"], [f"{self.root}/b"])["scan"]["where"], "system")
        gone = self.check(["no-such-tool-here"], [f"{self.root}/b"])["scan"]
        self.assertEqual((gone["found"], gone["where"]), (None, None))

    def test_a_folder_the_worker_does_not_allow_or_in_full_isolation_is_not_searched_and_says_so(self):
        out = self.check(["only-b"], [f"{self.root}/b", f"{self.base}/missing"], roots=[f"{self.root}/a"])["scan"]
        self.assertIsNone(out["found"])
        self.assertEqual([(f["exists"], f["allowed"]) for f in out["folders"]], [(True, False), (False, False)])
        self.assertFalse(self.check(["only-b"], [f"{self.root}/b"], mode="full")["scan"]["folders"][0]["allowed"])

    def test_the_state_carries_it_only_when_a_class_has_tool_folders(self):
        reg = {"classes": {"scan": {"kind": "command", "run": ["scan"], "tools": [f"{self.root}/b"]}}}
        env, _ = workerstate.build(snapshot={}, registry=reg, extra={"isolation_mode": "relaxed", "tool_roots": [self.root]}, jail=type("J", (), {"ready": lambda s: True, "name": "bwrap"})(), locked=False, now_iso="x")
        self.assertEqual(env["tool_check"]["scan"]["found"], f"{self.root}/b/scan")
        env2, _ = workerstate.build(snapshot={}, registry={"classes": {}}, extra={}, jail=type("J", (), {"ready": lambda s: True, "name": "bwrap"})(), locked=False, now_iso="x")
        self.assertNotIn("tool_check", env2)


class LargestJob(unittest.TestCase):
    def test_the_workers_list_shows_the_largest_job_a_worker_accepts_when_it_says_so(self):
        from ct.admin import workers
        doc = {"v": 1, "updated": "2999-01-01T00:00:00Z", "name": "x", "hardware": {"cores": 4, "mem_total_mb": 8000}, "observed": {"mem_available_mb": 4000, "job_ceiling_mb": 256}, "isolation": {"ready": True}, "classes": []}
        row = workers.row_for("tenta2", doc)
        self.assertEqual(row["ceiling"], "256 MB")
        self.assertIn(("Largest job", "256 MB"), workers.fact_rows(row))
        doc["observed"].pop("job_ceiling_mb")
        self.assertNotIn("Largest job", [k for k, _ in workers.fact_rows(workers.row_for("tenta2", doc))])        # an older worker does not say: no row


class ProfileTimeRow(unittest.TestCase):
    PT = {"factor": 1.59, "basis": "install, 4 of 4 steps + 2 profile runs", "margin": 3, "ceiling_s": None, "shared": False}

    def doc(self, pt):
        env, doc = workerstate.build(snapshot={}, registry={"classes": {}}, extra={"profile_time": pt}, jail=type("J", (), {"ready": lambda s: True, "name": "bwrap"})(), locked=False, now_iso="2999-01-01T00:00:00Z")
        return doc

    def test_the_state_carries_what_a_profile_runs_limit_is_built_from(self):
        self.assertEqual(self.doc(self.PT)["profile_time"], self.PT)
        self.assertNotIn("profile_time", self.doc(None))                        # an older worker does not say

    def test_the_workers_list_shows_the_limit_its_basis_and_the_ceiling(self):
        from ct.admin import workers
        rows = dict(workers.fact_rows(workers.row_for("x", self.doc(self.PT))))
        self.assertEqual(rows["Profile limit"], "estimate x 1.59 x 3; no ceiling; from install, 4 of 4 steps + 2 profile runs")
        shared = dict(workers.fact_rows(workers.row_for("x", self.doc({**self.PT, "ceiling_s": 600, "shared": True, "factor": 1.0, "basis": "none yet"}))))
        self.assertEqual(shared["Profile limit"], "estimate x 1 x 3; ceiling 600 s (shared computer); from none yet")
        self.assertNotIn("Profile limit", [k for k, _ in workers.fact_rows(workers.row_for("x", self.doc(None)))])


class ListLabelsFit(unittest.TestCase):
    def test_every_label_of_the_workers_list_leaves_a_gap_before_its_value(self):
        """A label as wide as the label column ran into its value (LARGEST JOB1024 MB). Every row a worker can have is built here, so a new longer label fails this."""
        from ct.admin import workers
        row = {"name": "x", "os": "o", "cpu": "c", "memory": "m", "claude": "c", "spend": "s", "state": "st", "release": "r", "load": "l", "age": "a", "ceiling": "1 MB",
               "purpose": "p", "profile_time": "estimate x 1 x 3", "platform": "windows", "held": "busy", "stale": False, "locked": False, "packages": 0}
        labels = [label for label, _ in workers.fact_rows(row, upper=True)]
        self.assertGreaterEqual(len(labels), 13, labels)
        for label in labels:
            self.assertLess(len(label), workers.HANG, label)


if __name__ == "__main__":
    unittest.main()


class TheConnectionAWorkerReallyUses(unittest.TestCase):
    """Found on the real Mac mini: its configuration said direct while its queue copy still pointed at GitHub, so it announced the wrong connection. What it fetches over is where its copy points."""

    def test_the_copy_decides_and_a_disagreeing_configuration_is_published_separately(self):
        facts = workerstate.transport_facts("direct", "git@github.com:someone/queue.git")
        self.assertEqual(facts, {"transport": "github", "transport_configured": "direct"})
        self.assertEqual(workerstate.transport_facts("github", "git@github.com:someone/queue.git"), {"transport": "github"})
        self.assertEqual(workerstate.transport_facts("direct", "/var/lib/x-mail/queue.git"), {"transport": "direct"})

    def test_with_no_copy_to_look_at_the_configuration_is_used(self):
        self.assertEqual(workerstate.transport_facts("direct", ""), {"transport": "direct"})

    def test_build_carries_both(self):
        class Jail:
            name = "bwrap"

            def ready(self):
                return True
        _e, doc = workerstate.build(snapshot={"cores": 2, "at": "x"}, registry={"classes": {}}, extra={"name": "w", "transport": "github", "transport_configured": "direct", "claude": {"auth": "none"}}, jail=Jail(), locked=False, now_iso="x")
        self.assertEqual((doc["transport"], doc["transport_configured"]), ("github", "direct"))
