import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import calibration, calibration_reference  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "calibration").glob("*.json"))


class Factor(unittest.TestCase):
    REF = {"compile": 1.0, "import": 0.5, "hash": 0.5, "git": 0.2}

    def test_the_factor_is_the_median_of_the_step_ratios(self):
        self.assertEqual(calibration.speed_factor({"compile": 2.0, "import": 1.0, "hash": 1.0, "git": 0.4}, self.REF), (2.0, "install, 4 of 4 steps"))
        got = calibration.speed_factor({"compile": 2.0, "import": 1.0, "hash": 1.0, "git": 20.0}, self.REF)         # one wild step does not move the median
        self.assertEqual(got[0], 2.0)

    def test_a_thin_measurement_says_so_and_none_gives_one(self):
        self.assertEqual(calibration.speed_factor({"hash": 1.0}, self.REF), (2.0, "install, 1 of 4 steps"))
        self.assertEqual(calibration.speed_factor({}, self.REF), (1.0, "no reference"))
        self.assertEqual(calibration.speed_factor({"compile": 1.0}, {}), (1.0, "no reference"))

    def test_the_reference_is_the_median_over_recordings(self):
        self.assertEqual(calibration.reference_from([{"compile": 1.0}, {"compile": 3.0}, {"compile": 2.0, "git": 5.0}]), {"compile": 2.0, "git": 5.0})


class Measure(unittest.TestCase):
    def test_the_real_steps_run_here_and_each_takes_a_measurable_time(self):
        got = calibration.measure(ROOT / "src" / "ct")                  # the real thing once, so the fakes elsewhere are trusted
        self.assertEqual(sorted(got), sorted(calibration.PHASES), got)
        self.assertTrue(all(v > 0 for v in got.values()), got)

    def test_a_step_that_fails_is_left_out_never_guessed(self):
        with mock.patch.object(calibration, "_git", side_effect=OSError("no git")):
            got = calibration.measure(ROOT / "src" / "ct")
        self.assertNotIn("git", got)
        self.assertIn("hash", got)

    def test_store_and_load_round_trip_and_a_bad_file_is_empty(self):
        f = Path(tempfile.mkdtemp()) / "state" / "calibration.json"
        calibration.store(f, {"hash": 0.5})
        self.assertEqual(calibration.load(f)["phases"], {"hash": 0.5})
        f.write_text("not json")
        self.assertEqual(calibration.load(f), {})
        self.assertEqual(calibration.load(f.parent / "nothing"), {})


class Reference(unittest.TestCase):
    def test_the_shipped_reference_is_what_the_recordings_say(self):
        recs = [d["phases"] for d in (json.loads(p.read_text()) for p in FIXTURES) if d.get("reference")]
        self.assertTrue(recs, "no worker recording: run scripts/record-calibration LABEL --ssh USER@HOST")
        self.assertEqual(calibration_reference.REFERENCE, calibration.reference_from(recs))                  # scripts/record-calibration --rebuild

    def test_every_recording_has_all_the_steps_and_a_generic_label(self):
        for p in FIXTURES:
            d = json.loads(p.read_text())
            self.assertEqual(sorted(d["phases"]), sorted(calibration.PHASES), p.name)
            self.assertEqual(d["label"], p.stem)
            self.assertNotIn(".local", p.read_text())                                                        # a label, never a host name


if __name__ == "__main__":
    unittest.main()
