import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (points HOME at a throwaway folder)
from cw import guard, util  # noqa: E402

# Fixtures are assembled from pieces so this file itself never contains a real-looking address, key or token.
IP = "192." + "168." + "7.77"
MAC = ":".join(["aa", "bb", "cc", "dd", "ee", "ff"])
V6 = "fe80" + "::1ff:fe23:4567:890a"
KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
TOKEN = "ghp" + "_" + "a1" * 16
PASSWD = "root" + ":x:0:0:root:/root:/bin/bash"


class Rules(unittest.TestCase):
    def test_each_rule_fires_and_the_clean_cases_do_not(self):
        cases = {"private_ip": f"gw is {IP}", "mac": MAC, "ipv6_local": V6, "private_key": KEY, "token": TOKEN, "passwd_line": PASSWD,
                 "mdns_name": "printer" + ".local", "env_dump": "\n".join(f"VAR_{i}=value" for i in range(6)) + "\n"}
        for rule, text in cases.items():
            self.assertIn(rule, guard.scan(text, hostname="zz"), rule)
        self.assertEqual(guard.scan("load average: 0.00, 0.01, 0.05 and 10 users on 8.8.8.8", hostname="zz"), [])
        self.assertEqual(guard.scan("172.32.0.1 is public, 172." + "20.0.1 is not", hostname="zz"), ["private_ip"])

    def test_the_matched_text_is_never_in_the_result(self):
        self.assertEqual(guard.scan(f"gw is {IP}", hostname="zz"), ["private_ip"])

    def test_a_class_may_allow_what_it_legitimately_prints(self):
        self.assertEqual(guard.scan("gw 10.0.0." + "1", allow={"private_ip"}, hostname="zz"), [])
        self.assertEqual(guard.scan(f"gw is {IP}", allow={"mac"}, hostname="zz"), ["private_ip"])

    def test_encoded_blobs_need_high_entropy(self):
        blob = "".join("AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/"[(i * 7 + i // 3) % 38] for i in range(160))
        self.assertIn("encoded_blob", guard.scan(blob, hostname="zz"))
        self.assertEqual(guard.scan("a" * 200, hostname="zz"), [])            # long but low entropy
        self.assertEqual(guard.scan(blob, allow={"encoded_blob"}, hostname="zz"), [])

    def test_the_machines_own_hostname_is_caught_as_a_whole_word_only(self):
        self.assertEqual(guard.scan("running on mymachine now", hostname="mymachine"), ["hostname"])
        self.assertEqual(guard.scan("MYMACHINE", hostname="mymachine"), ["hostname"])
        self.assertEqual(guard.scan("mymachine-two and notmymachine", hostname="mymachine"), [])
        self.assertEqual(guard.scan("abc", hostname="abc"), [])                  # names under 4 letters are too common to match on
        self.assertEqual(guard.scan("mymachine", allow={"hostname"}, hostname="mymachine"), [])

    def test_a_canary_can_never_be_allowed(self):
        self.assertIn("canary", guard.scan("found SECRETVALUE123", allow=set(guard.GUARD_NAMES), hostname="zz", canary_tokens=["SECRETVALUE123"]))
        self.assertEqual(guard.scan("nothing", canary_tokens=["", None]), [])

    def test_every_rule_has_a_reason_and_the_text_reads_well(self):
        for rule in guard.GUARD_NAMES | {"canary_access"}:
            self.assertIn(rule, guard.RULE_WHY, rule)
        self.assertEqual(guard.why_text(["mac"]), "Withheld because it contained a hardware (MAC) address.")
        self.assertIn("; and ", guard.why_text(["mac", "token"]))
        self.assertIn("matched rule odd", guard.why_text(["odd"]))


class Canaries(unittest.TestCase):
    def test_planting_is_private_recorded_and_only_happens_once(self):
        state, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.assertTrue(guard.plant_canaries(state, home, identity="myinstall"))
        creds, gitcreds = home / ".aws" / "credentials", home / ".git-credentials"
        for f in (creds, gitcreds, state / "canary.json"):
            self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600, f.name)
        self.assertEqual(stat.S_IMODE((home / ".aws").stat().st_mode), 0o700)
        tokens = guard.canary_tokens(state)
        self.assertEqual(len(tokens), 3)
        self.assertTrue(creds.read_text().find(tokens[0]) >= 0 and gitcreds.read_text().startswith("https://myinstall:"))
        self.assertEqual(guard.canary_files(state), [str(creds), str(gitcreds)])
        self.assertFalse(guard.plant_canaries(state, home))                     # already there: untouched
        self.assertEqual(guard.canary_tokens(state), tokens)

    def test_a_planted_token_is_caught_in_output(self):
        state, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        guard.plant_canaries(state, home)
        leak = f"cat said: {guard.canary_tokens(state)[1]}"
        self.assertIn("canary", guard.scan(leak, hostname="zz", canary_tokens=guard.canary_tokens(state)))

    def test_missing_or_broken_state_gives_empty_lists(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual((guard.canary_tokens(d), guard.canary_files(d)), ([], []))
        (d / "canary.json").write_text("not json")
        self.assertEqual((guard.canary_tokens(d), guard.canary_files(d)), ([], []))


class Log(unittest.TestCase):
    def test_chain_is_intact_and_any_edit_is_detected(self):
        log = guard.SecurityLog(Path(tempfile.mkdtemp()) / "security.log")
        self.assertEqual(log.verify(), (True, 0))
        for i in range(4):
            log.append({"kind": "quarantine", "job": f"job-{i}"})
        self.assertEqual(log.verify(), (True, 4))
        first = json.loads(log.path.read_text().splitlines()[0])
        self.assertEqual(first["prev"], "0" * 64)
        lines = log.path.read_text().splitlines()
        log.path.write_text("\n".join(lines[:1] + [lines[1].replace("job-1", "job-X")] + lines[2:]) + "\n")      # edit line 2
        self.assertEqual(log.verify(), (False, 3))                              # line 3 no longer follows its predecessor
        log.path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")            # delete line 2
        self.assertEqual(log.verify(), (False, 2))
        log.path.write_text("garbage\n")
        self.assertEqual(log.verify(), (False, 1))


class Time(unittest.TestCase):
    def test_iso_round_trip_and_humans(self):
        t = util.now()
        self.assertTrue(util.iso(t).endswith("Z"))
        self.assertEqual(util.parse_iso(util.iso(t)), t.replace(microsecond=0))
        self.assertIsNone(util.parse_iso("yesterday"))
        self.assertIsNone(util.parse_iso("2026-01-01T00:00:00"))                 # no timezone: refused
        self.assertEqual([util.human_hours(h) for h in (0.5, 7, 72)], ["30 min", "7 h", "3 days"])


if __name__ == "__main__":
    unittest.main()
