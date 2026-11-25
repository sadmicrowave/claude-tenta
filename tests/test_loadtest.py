"""scripts/loadtest: the mixed burst (scheduled jobs and jobs from a folder that dedicates the class to one worker) is planned and judged correctly."""
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

import support  # noqa: F401

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loadtest"


def load():
    loader = importlib.machinery.SourceFileLoader("loadtest_script", str(SCRIPT))
    spec = importlib.util.spec_from_loader("loadtest_script", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class MixedBurst(unittest.TestCase):
    def test_the_plan_sends_scheduled_jobs_from_one_folder_and_pinned_jobs_from_the_other(self):
        lt = load()
        plan = lt.plan_for(3, 2, "/free", "/pin")
        self.assertEqual(plan, [("scheduled", "/free")] * 3 + [("pinned", "/pin")] * 2)
        self.assertEqual(lt.plan_for(4, 0, "/free", "/pin"), [("scheduled", "/free")] * 4)

    def test_the_verdict_counts_pinned_jobs_that_landed_on_the_worker_and_names_each_stray(self):
        lt = load()
        rows = [{"group": "pinned", "id": "a", "worker": "tenta3"}, {"group": "pinned", "id": "b", "worker": "tenta2"},
                {"group": "pinned", "id": None, "worker": "-"}, {"group": "scheduled", "id": "c", "worker": "tenta2"}]
        self.assertEqual(lt.pin_verdict(rows, "tenta3"), (1, 2, [("b", "tenta2")]))               # a refused send is not counted; a scheduled job is not judged
        self.assertEqual(lt.pin_verdict(rows[:1], "tenta3"), (1, 1, []))


    def test_a_global_dedication_of_the_class_is_noticed_because_it_hides_the_orchestrator(self):
        lt = load()
        self.assertIn("tenta3", lt.global_note("sys-status", {"sys-status": "tenta3"}))
        self.assertIsNone(lt.global_note("sys-status", {"nmap": "tenta3"}))
        self.assertIsNone(lt.global_note("sys-status", {}))


if __name__ == "__main__":
    unittest.main()
