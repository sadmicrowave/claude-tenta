"""Every message drawn for a person fits the terminal it is drawn on, however narrow (scope: a pane of about 36 columns drew every card 50 wide, so lines wrapped and frames broke).
One guard for all the card kinds: each is drawn at several widths and no visible line may be wider than the terminal. A card kind added later is added to KINDS, and fails here until it fits."""
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from ct import cards, ui  # noqa: E402
from ct.client import view  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1b\][^\x07]*\x07")
WIDTHS = (30, 36, 40, 50, 59, 60, 80, 100)
LONG = "a-long-worker-name-that-is-longer-than-the-pane"
PATH = "/home/example/.local/share/claude-tenta/workers/win/mailbox/key"


class Tty(io.StringIO):
    def isatty(self):
        return True


def at(cols, fn):
    out = Tty()
    with mock.patch.dict(os.environ, {"CT_COLOR": "always"}), mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((cols, 24))):
        fn(out)
    return ANSI.sub("", out.getvalue())


def say(*a, **k):
    return lambda out: ui.say(*a, stream=out, **k)


RES = {"id": "profile-bkhudd", "status": "rejected", "note": "job expired: it is more than 24 hours old when the worker looked at it, so it is not run; send it again", "output": ""}
OK = {"id": "sys-status-qpr7xp", "status": "ok", "output": " up 10:58,  0 users\n", "started": "2026-11-17T05:36:11Z", "finished": "2026-11-17T05:36:13Z",
      "metrics": {"wall_s": 0.02, "cpu_s": 0.01, "peak_rss_mb": 0, "mem_total_mb": 15219, "peak_temp_c": 50, "disk_mb": 0.0}}

def _setup_rows():
    from ct.setup import report
    r = report.Styler(styled=True, verbose=False)
    r.stream = Tty()
    return "\n".join([r.note("ready", "tools", "git version 2.56.1.windows.1; Python 3.13.17"), r.note("fetched", "release", "release-win-0.2.0 (a1db3df61)"),
                      r.note("registered", "worker task", "starts at boot as the worker account; started now")])


KINDS = {
    "a short card": say("Ready", "It is done.", tag="DONE", color=ui.GREEN),
    "facts, why, steps and a command": say("A step of the Windows setup failed", ("Step", "tools"), ("Computer", "192.0.2.50"), cards.Why("The Python installer finished, but python.exe is not where it was expected"),
                                           tag="STOPPED", color=ui.RED, steps=["Run the same command again: it continues from where it stopped."], run="ct-setup add-worker 192.0.2.50"),
    "a long title": say("A step of the Windows setup failed and it is a long title", ("Worker", LONG), tag="STOPPED", color=ui.RED),
    "a long path value": say("Configuration", ("Mailbox key", PATH), tag="NOTE", color=ui.YELLOW),
    "a command": say("Run it", tag="NEXT", color=ui.CYAN, run="ct-admin approve job-1"),            # (a command longer than the pane is drawn alone and left to the terminal to wrap: never cut, so it can be copied whole)
    "a document card": lambda out: ui.document("Job file", '{\n  "id": "profile-bkhudd",\n  "class": "profile-run",\n  "created": "2026-11-21T14:02:11Z"\n}', stream=out),
    "a table": lambda out: ui.table("Workers", ["NAME", "STATE", "LOAD"], [[LONG, "awake", "0.3"], ["tenta2", "asleep", "0.0"]], stream=out),
    "a result that ran": lambda out: view.print_result(OK, out=out, err=io.StringIO()),
    "a result that never ran": lambda out: view.print_result(RES, out=out, err=io.StringIO()),
    "a quiet facts block": lambda out: out.write("\n".join(cards.meta([("Measured", [("Time", "0.02 s wall, 0.01 s cpu"), ("Memory", "peak 0 MB of 15219 MB")])])) + "\n"),
    "named blocks": say("Waiting for you", *cards.job_blocks([("profile-bkhudd", [("CLASS", "profile-run"), ("WAITING", "3 hours")])]), tag="APPROVE", color=ui.YELLOW, run=["ct-admin approve profile-bkhudd"]),
    "the workers list": lambda out: out.write("\n".join(__import__("ct.admin.workers", fromlist=["x"]).cards(
        [{"name": "tenta2", "os": "ubuntu 24.04", "cpu": "2 cores", "memory": "2 of 3 GB free", "claude": "none", "spend": "0/20 jobs", "state": "jail ON", "release": "f4c9ae706", "load": "0.00",
          "age": "5 min ago", "has_claude": False, "locked": False, "stale": False, "packages": 0, "classes": 3}], width=__import__("shutil").get_terminal_size().columns, color=True)) + "\n"),
    "a setup progress row": lambda out: out.write(_setup_rows() + "\n"),
    "the approval screen": lambda out: out.write(__import__("ct.review", fromlist=["x"]).review_text({"argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "what": "Runs the unit tests of the project against the attached source.",
                                                                                                  "expect": "about 15 seconds", "files": {"src/ct/jobs.py": "x"}}) + "\n"),
    "a yes/no question": lambda out: out.write(ui.confirm_prompt("Remove the claude-tenta worker from 192.0.2.50?", stream=out) + "\n"),
}


COMMANDS = ("ct-setup add-worker 192.0.2.50", "ct-admin approve job-1", "ct-admin approve profile-bkhudd", "python3 -m unittest discover -s tests")      # a command longer than the pane is drawn alone and never cut, so it can be copied whole


class Fits(unittest.TestCase):
    def test_no_line_is_wider_than_the_terminal(self):
        bad = []
        for kind, fn in KINDS.items():
            for cols in WIDTHS:
                for line in at(cols, fn).split("\n"):
                    if len(line) > cols and line.strip() not in COMMANDS:
                        bad.append(f"{kind} at {cols}: {len(line)} wide: {line[:50]!r}")
        self.assertEqual(bad, [], "\n".join(bad[:25]) + f"\n({len(bad)} too wide)")

    def test_a_frame_is_never_broken_and_a_narrow_terminal_gets_the_rail_not_a_frame(self):
        bad = []
        for kind, fn in KINDS.items():
            for cols in WIDTHS:
                text = at(cols, fn)
                tops, bottoms = text.count("╭"), text.count("╰")
                if tops != bottoms:
                    bad.append(f"{kind} at {cols}: {tops} tops but {bottoms} bottoms")
                if cols < cards.FRAME_MIN and tops:
                    bad.append(f"{kind} at {cols}: a frame below {cards.FRAME_MIN} columns")
                if cols >= cards.FRAME_MIN and kind in ("a short card", "facts, why, steps and a command") and not tops:
                    bad.append(f"{kind} at {cols}: no frame at {cols} columns")
        self.assertEqual(bad, [], "\n".join(bad[:25]))

    def test_the_card_uses_the_width_it_has_and_never_a_fixed_minimum(self):
        self.assertEqual(cards.width(cols=36), 36)
        self.assertEqual(cards.width(cols=120), 100)
        self.assertEqual(cards.width(cols=10), 20)                                # a floor so a card is still readable, far below any real pane


if __name__ == "__main__":
    unittest.main()
