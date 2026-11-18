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
        self.assertEqual((doc["hardware"]["cores"], doc["hardware"]["native_claude"], doc["isolation"]), (2, False, {"ready": True, "method": "bwrap"}))
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
