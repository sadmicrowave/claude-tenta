"""Two runs of the end-to-end harness (two sessions, or a session and the user) never touch each other's machines, files or caps.

The naming lives in scripts/e2e-names.sh; these tests run its real shell functions and scripts/e2e-clean against a fake `orb`."""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
LISTING = """arm-cache-ubuntu-26-04       stopped  ubuntu  resolute  arm64  1.3 GB
arm-run-rmine-ubuntu-26-04-install  running  ubuntu  resolute  arm64  1.3 GB
arm-run-rmine-ubuntu-26-04-move  running  ubuntu  resolute  arm64  1.3 GB
arm-run-rother-ubuntu-26-04-install  running  ubuntu  resolute  arm64  1.3 GB
arm-e2e-rother  running  ubuntu  resolute  arm64  1.3 GB
arm-run-ubuntu-26-04-install  running  ubuntu  resolute  arm64  1.3 GB
arm-e2e-awmain  running  ubuntu  resolute  arm64  1.3 GB
unrelated  running  ubuntu  resolute  arm64  1.3 GB
"""


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.listing = self.tmp / "listing"
        self.listing.write_text(LISTING)
        self.deleted = self.tmp / "deleted"
        fake = self.tmp / "bin" / "orb"
        fake.parent.mkdir()
        fake.write_text(f'#!/bin/bash\nif [ "$1" = list ]; then cat {self.listing}; exit 0; fi\n'
                        f'if [ "$1" = delete ]; then echo "$3" >> {self.deleted}; grep -v "^$3 " {self.listing} > {self.listing}.n; mv {self.listing}.n {self.listing}; exit 0; fi\nexit 1\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "ARM_E2E_RUN": "mine"}
        self.env.pop("E2E_RUN_ID", None)

    def sh(self, code, **env):
        r = subprocess.run(["bash", "-c", f". {ROOT}/scripts/e2e-names.sh; {code}"], capture_output=True, text=True, env={**self.env, **env})
        return r.returncode, (r.stdout + r.stderr).strip()

    def clean(self, *args, **env):
        r = subprocess.run([str(ROOT / "scripts" / "e2e-clean"), *args], capture_output=True, text=True, env={**self.env, **env})
        return r.returncode, r.stdout + r.stderr

    def gone(self):
        return sorted(self.deleted.read_text().split()) if self.deleted.exists() else []


class TheRunId(Harness):
    def test_it_comes_from_arm_e2e_run_with_a_marker_letter(self):
        self.assertEqual(self.sh("e2e_id"), (0, "rmine"))
        self.assertEqual(self.sh("e2e_id", ARM_E2E_RUN="Feat_X-1")[1], "rfeatx1")

    def test_without_it_it_comes_from_the_checkout_path_and_is_stable(self):
        env = {k: v for k, v in self.env.items() if k != "ARM_E2E_RUN"}
        a = subprocess.run(["bash", "-c", f". {ROOT}/scripts/e2e-names.sh; e2e_id"], capture_output=True, text=True, env=env).stdout
        b = subprocess.run(["bash", "-c", f". {ROOT}/scripts/e2e-names.sh; e2e_id"], capture_output=True, text=True, env=env).stdout
        self.assertEqual(a, b)
        self.assertRegex(a, r"^r[0-9a-f]{6}$")

    def test_every_name_carries_it(self):
        self.assertEqual(self.sh("e2e_machine run ubuntu-26-04-install")[1], "arm-run-rmine-ubuntu-26-04-install")
        self.assertEqual(self.sh("e2e_machine e2e")[1], "arm-e2e-rmine")
        self.assertEqual(self.sh("e2e_out_dir ubuntu-26-04")[1], "/tmp/arm-routes/rmine/ubuntu-26-04")
        self.assertEqual(self.sh("e2e_logs_dir x")[1], "/tmp/arm-e2e-logs/rmine/x")

    def test_the_owner_is_read_from_the_name_and_the_older_names_are_unowned(self):
        for name, owner in (("arm-run-rmine-ubuntu-26-04-install", "rmine"), ("arm-e2e-rother", "rother"), ("arm-run-ubuntu-26-04-install", "unowned"),
                            ("arm-e2e-awmain", "unowned"), ("arm-e2e", "unowned"), ("cw-e2e-x", "unowned")):
            self.assertEqual(self.sh(f"e2e_owner {name}")[1], owner, name)


class TheCaps(Harness):
    def test_only_this_runs_machines_count_against_its_cap(self):
        self.assertEqual(self.sh("e2e_my_machines")[1].split(), ["arm-run-rmine-ubuntu-26-04-install", "arm-run-rmine-ubuntu-26-04-move"])
        self.assertEqual(self.sh("e2e_cap_check 3")[0], 0)           # 2 of mine, 4 of others: not over 3
        rc, text = self.sh("e2e_cap_check 2")
        self.assertEqual(rc, 1)
        self.assertIn("rmine already has 2", text)

    def test_the_global_cap_names_who_holds_the_machines(self):
        rc, text = self.sh("e2e_cap_check 9", ARM_E2E_GLOBAL_CAP="6")
        self.assertEqual(rc, 1)
        self.assertIn("6 test machines exist in all", text)
        self.assertIn("rother (2)", text)
        self.assertIn("unowned (2)", text)


class TheClean(Harness):
    def test_by_default_it_deletes_only_this_runs_machines_and_says_the_rest_stay(self):
        rc, out = self.clean()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.gone(), ["arm-run-rmine-ubuntu-26-04-install", "arm-run-rmine-ubuntu-26-04-move"])
        self.assertIn("4 machine(s) of other runs were left alone", out)

    def test_list_shows_owners_and_deletes_nothing(self):
        rc, out = self.clean("--list")
        self.assertEqual(rc, 0)
        self.assertIn("owner rother", out)
        self.assertIn("owner unowned", out)
        self.assertIn("(this run)", out)
        self.assertIn("shared cache", out)
        self.assertEqual(self.gone(), [])

    def test_all_adds_the_shared_cache_but_never_other_runs(self):
        self.clean("--all")
        self.assertEqual(self.gone(), ["arm-cache-ubuntu-26-04", "arm-run-rmine-ubuntu-26-04-install", "arm-run-rmine-ubuntu-26-04-move"])

    def test_all_runs_removes_everyones_and_the_unowned_and_says_so(self):
        rc, out = self.clean("--all-runs")
        self.assertIn("EVERY run id's", out)
        self.assertEqual(len(self.gone()), 6)
        self.assertNotIn("arm-cache-ubuntu-26-04", self.gone())
        self.assertNotIn("unrelated", self.gone())

    def test_an_unknown_flag_is_refused(self):
        self.assertEqual(self.clean("--everything")[0], 2)


class TheCacheLock(Harness):
    def test_a_lock_whose_holder_is_gone_is_taken_over(self):
        lock = Path("/tmp/arm-e2e-locks/test-stale-lock")
        subprocess.run(["rm", "-rf", str(lock)])
        lock.mkdir(parents=True)
        (lock / "holder").write_text("999999 rgone\n")                 # no such process
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(lock)]))
        rc, out = self.sh("e2e_locked test-stale-lock echo ran")
        self.assertEqual((rc, out), (0, "ran"))
        self.assertFalse(lock.exists())

    def test_a_busy_lock_makes_the_second_run_wait_and_say_so(self):
        lock = Path("/tmp/arm-e2e-locks/test-busy-lock")
        subprocess.run(["rm", "-rf", str(lock)])
        lock.mkdir(parents=True)
        holder = subprocess.Popen(["sleep", "30"])
        (lock / "holder").write_text(f"{holder.pid} rother\n")
        self.addCleanup(lambda: (holder.kill(), subprocess.run(["rm", "-rf", str(lock)])))
        p = subprocess.Popen(["bash", "-c", f". {ROOT}/scripts/e2e-names.sh; e2e_locked test-busy-lock echo ran"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        try:
            p.communicate(timeout=5)
            self.fail("it did not wait")
        except subprocess.TimeoutExpired:
            holder.kill(); holder.wait()                               # the holder ends: the waiter takes the lock
            out, err = p.communicate(timeout=20)
        self.assertEqual(out.strip(), "ran")
        self.assertIn("run rother is using the shared test-busy-lock", err)


class NoScriptBuildsNamesByHand(unittest.TestCase):
    """Every machine, output folder and log folder name comes from scripts/e2e-names.sh."""
    FILES = [p for d in ("scripts", "tests/e2e") for p in sorted((ROOT / d).rglob("*")) if p.is_file() and p.suffix in ("", ".sh") and "__pycache__" not in p.parts and p.name != "e2e-names.sh"]
    HAND = re.compile(r"(?<![/\w])arm-(run|e2e|mx|u)-[\w$]|/tmp/arm-routes|/tmp/arm-e2e-logs|/tmp/arm-e2e-locks|(?<![/\w])arm-cache-[\w$]")

    def test_no_hand_built_names(self):
        bad = []
        for p in self.FILES:
            try:
                lines = p.read_text().splitlines()
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(lines, 1):
                code = line.split(" # ")[0] if not line.lstrip().startswith("#") else ""
                if self.HAND.search(code) and "arm-run submit" not in code and "arm-run setup" not in code and "bin/arm-run" not in code:
                    bad.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:90]}")
        self.assertEqual(bad, [], "build the name with e2e_machine / e2e_out_dir / e2e_logs_dir / e2e_cache_name (scripts/e2e-names.sh)")

    def test_every_script_that_makes_or_counts_machines_uses_the_shared_names_and_caps(self):
        for name in ("e2e-routes", "e2e-orbstack", "e2e-base", "e2e-clean", "probe", "real-github-check", "real-github-addworker"):
            self.assertIn("e2e-names.sh", (ROOT / "scripts" / name).read_text(), name)
        for name in ("e2e-routes", "e2e-orbstack"):
            self.assertIn("e2e_cap_check", (ROOT / "scripts" / name).read_text(), name)
        self.assertIn('e2e_locked "cache-$TAG" build', (ROOT / "scripts" / "e2e-base").read_text())


if __name__ == "__main__":
    unittest.main()
