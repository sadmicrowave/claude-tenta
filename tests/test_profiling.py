import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from cw import profiling, registry  # noqa: E402

RULES = registry.profiling_rules({})


class Verdict(unittest.TestCase):
    def v(self, status="ok", rc=0, note="", **m):
        base = {"mem_total_mb": 4000, "peak_rss_mb": 50, "wall_s": 5, "cores_busy": 0.5, "peak_temp_c": 55, "disk_mb": 0}
        return profiling.profile_verdict(status, rc, note, {**base, **m}, ["x"], {}, RULES)

    def test_costs(self):
        self.assertEqual(self.v()["cost"], "light")
        self.assertEqual(self.v(wall_s=45)["cost"], "medium")
        self.assertEqual(self.v(peak_rss_mb=500)["cost"], "medium")
        self.assertEqual(self.v(wall_s=120, cores_busy=1.9)["cost"], "heavy")
        self.assertEqual(self.v(peak_rss_mb=1200)["cost"], "heavy")
        self.assertEqual(self.v(peak_temp_c=80)["cost"], "heavy")

    def test_does_not_fit_and_why(self):
        r = self.v(peak_rss_mb=2500)
        self.assertEqual((r["verdict"], r["limiting"]), ("does-not-fit", ["memory"]))
        self.assertEqual(self.v(wall_s=2400)["limiting"], ["time"])
        self.assertEqual(self.v(peak_temp_c=84)["limiting"], ["heat"])
        both = self.v(peak_rss_mb=2500, wall_s=2400)
        self.assertEqual(both["limiting"], ["memory", "time"])
        self.assertEqual(len(both["reasons"]), 2)

    def test_more_ram_changes_the_verdict(self):
        self.assertEqual(self.v(peak_rss_mb=2500)["verdict"], "does-not-fit")
        self.assertEqual(self.v(peak_rss_mb=2500, mem_total_mb=16000)["verdict"], "fits")        # after the RAM upgrade

    def test_a_run_stopped_by_a_limit_or_a_timeout_does_not_fit(self):
        self.assertEqual(self.v("limit", note="memory 900 MB went over the 800 MB limit")["limiting"], ["memory"])
        self.assertEqual(self.v("limit", note="core temperature 90C went over the 85C limit")["limiting"], ["heat"])
        self.assertEqual(self.v("limit", note="disk use 5000 MB went over")["limiting"], ["disk"])
        self.assertEqual(self.v("timeout", rc=None)["limiting"], ["time"])

    def test_a_failed_run_proves_nothing(self):
        r = self.v("error", rc=2)
        self.assertEqual((r["verdict"], r["limiting"]), ("inconclusive", []))
        self.assertIn("profile it again", r["reasons"][0])
        self.assertNotIn("entry", r)

    def test_the_drafted_entry_is_the_measurement_times_a_margin_and_never_above_a_ceiling(self):
        r = self.v(wall_s=7, peak_rss_mb=120, peak_temp_c=60, disk_mb=3.2)
        e = r["entry"]
        self.assertEqual((e["tier"], e["cost"], e["run"]), ("auto", "light", ["x"]))
        self.assertEqual((e["timeout_s"], e["max_rss_mb"], e["max_temp_c"], e["max_disk_mb"]), (30, 250, 70, 100))
        self.assertLessEqual(self.v(peak_temp_c=81)["entry"]["max_temp_c"], RULES["temp_ceiling_c"])
        self.assertNotIn("max_disk_mb", self.v(disk_mb=0)["entry"])
        self.assertEqual(self.v(wall_s=5000, peak_rss_mb=10)["verdict"], "does-not-fit")
        v = profiling.profile_verdict("ok", 0, "", {"mem_total_mb": 4000, "peak_rss_mb": 10, "wall_s": 1000, "peak_temp_c": 40}, ["x"], {"a.txt": "x"}, RULES)
        self.assertEqual((v["entry"]["timeout_s"], v["entry"]["accepts_files"]), (3000, True))
        self.assertEqual(registry.validate({"classes": {"drafted": v["entry"]}}, "/opt/x/tools"), [])      # a draft is a valid registry entry

    def test_the_product_name_is_an_argument(self):
        r = profiling.profile_verdict("ok", 0, "", {"mem_total_mb": 4000, "peak_rss_mb": 3000, "wall_s": 1}, ["x"], {}, RULES, product="homecw")
        self.assertIn("homecw's 4000 MB", r["reasons"][0])


class Facts(unittest.TestCase):
    def test_facts_about_a_real_program(self):
        tool = Path(tempfile.mkdtemp()) / "mytool"
        tool.write_bytes(b"x" * 5000)
        tool.chmod(0o755)
        answers = {"dpkg": "mypkg: /usr/bin/mytool", "whatis": "mytool (1) - does a thing"}
        facts = profiling.command_facts([str(tool), "--go"], lookup=lambda *c: answers[c[0]])
        self.assertEqual(facts, {"binary": str(tool), "size_kb": 4, "package": "mypkg", "summary": "mytool (1) - does a thing"})

    def test_interpreters_are_flagged_because_the_arguments_are_the_program(self):
        for name in ("python3", "bash", "sh", "node", "env", "xargs", "awk"):
            self.assertTrue(profiling.command_facts([name, "x"], find_program=lambda n: None, lookup=lambda *c: "")["interpreter"], name)
        self.assertNotIn("interpreter", profiling.command_facts(["jq", "."], find_program=lambda n: None, lookup=lambda *c: ""))

    def test_a_missing_program_and_a_broken_lookup_never_break_the_job(self):
        self.assertEqual(profiling.command_facts(["nope"], find_program=lambda n: None), {"binary": None})
        facts = profiling.command_facts(["jq"], find_program=lambda n: "/nonexistent/jq", lookup=lambda *c: "nothing appropriate" if c[0] == "whatis" else "")
        self.assertEqual(facts, {"binary": "/nonexistent/jq"})

    def test_the_real_lookups_tolerate_missing_tools(self):
        facts = profiling.command_facts([sys.executable])                                       # runs the real dpkg/whatis lookups, if any
        self.assertEqual(facts["binary"], sys.executable)


class Glance(unittest.TestCase):
    snap = {"at": "2026-01-02T03:04:05Z", "cores": 4, "mem_total_mb": 8000, "mem_available_mb": 6100, "load_1m": 0.2, "temp_c": 51,
            "disk_free_gb": 120.5, "running": 1, "waiting": 2}

    def test_the_screen_shows_estimate_hard_stop_and_headroom(self):
        job = {"est_time_s": 30, "est_mem_mb": 200, "est_cpu_cores": 1, "est_disk_mb": 5}
        lim = profiling.profile_limits(RULES, 8000)
        text = profiling.glance(job, RULES, lim, self.snap, 600, facts={"binary": "/usr/bin/jq", "size_kb": 300, "package": "jq"}, product="homecw")
        for needle in ("AT A GLANCE", "homecw now (03:04:05 UTC)", "~30 s", "600 s", "4,800 MB (60% of RAM)", "6,100 MB free of 8,000 MB",
                       "1 running, 2 waiting", "stops at 85C", "core now 51C", "120.5 GB free", "Program: /usr/bin/jq (300 KB), package jq"):
            self.assertIn(needle, text)

    def test_unknowns_are_shown_as_such(self):
        snap = {**self.snap, "mem_available_mb": None, "temp_c": None, "disk_free_gb": None}
        text = profiling.glance({}, RULES, profiling.profile_limits(RULES, 0), snap, 60)
        self.assertEqual(text.count("not estimated"), 5)
        self.assertIn("sensor unreadable", text)
        self.assertEqual(profiling.profile_limits(RULES, 0)["max_rss_mb"], None)

    def test_number_formatting(self):
        self.assertEqual([profiling.number(x) for x in (0.5, 5, 10, 1234.5, 1_000_000)], ["0.5", "5", "10", "1,234", "1,000,000"])


if __name__ == "__main__":
    unittest.main()
