"""Every message drawn for a person fits the terminal it is drawn on, however narrow (scope: a pane of about 36 columns drew every card 50 wide, so lines wrapped and frames broke).
One guard for all the card kinds: each is drawn at several widths and no visible line may be wider than the terminal. A card kind added later is added to KINDS, and fails here until it fits."""
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401
from armada import cards, ui  # noqa: E402
from armada.client import view  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z~]|\x1b\][^\x07]*\x07")
WIDTHS = (20, 24, 30, 36, 40, 43, 44, 45, 50, 59, 60, 61, 80, 100, 140, 200)      # the edges of every switch (stack below 44, rail below 60) on both sides, and the extremes
LONG = "a-long-worker-name-that-is-longer-than-the-pane"
PATH = "/home/example/.local/share/armada/workers/win/mailbox/key"


class Tty(io.StringIO):
    def isatty(self):
        return True


def at(cols, fn):
    out = Tty()
    with mock.patch.dict(os.environ, {"ARM_COLOR": "always"}), mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((cols, 24))):
        fn(out)
    return ANSI.sub("", out.getvalue())


def say(*a, **k):
    return lambda out: ui.say(*a, stream=out, **k)


RES = {"id": "profile-bkhudd", "status": "rejected", "note": "job expired: it is more than 24 hours old when the worker looked at it, so it is not run; send it again", "output": ""}
OK = {"id": "sys-status-qpr7xp", "status": "ok", "output": " up 10:58,  0 users\n", "started": "2026-11-17T05:36:11Z", "finished": "2026-11-17T05:36:13Z",
      "metrics": {"wall_s": 0.02, "cpu_s": 0.01, "peak_rss_mb": 0, "mem_total_mb": 15219, "peak_temp_c": 50, "disk_mb": 0.0}}

def _setup_rows():
    from armada.setup import report
    r = report.Styler(styled=True, verbose=False)
    r.stream = Tty()
    return "\n".join([r.note("ready", "tools", "git version 2.56.1.windows.1; Python 3.13.17"), r.note("fetched", "release", "release-win-0.2.0 (a1db3df61)"),
                      r.note("registered", "worker task", "starts at boot as the worker account; started now")])


def _update_cards(out):
    """The two cards `arm-admin update` prints for several workers: the devices it would touch, and how each went."""
    from contextlib import redirect_stdout
    from armada.admin import cli as admin_cli
    ws = [(LONG, mock.Mock(ssh=mock.Mock(host="192.0.2.50"))), ("arm2", mock.Mock(ssh=mock.Mock(host="arm2.lan"))), ("win1", mock.Mock(ssh=mock.Mock(host="192.0.2.77")))]
    with redirect_stdout(out):
        admin_cli.update_roster(None, ws, {"commit": "a" * 40}, "release-0.2.0", [("update", None), ("current", None), ("left", "windows")])
        admin_cli.fleet_apply_summary([(LONG, "installed"), ("arm2", "current"), ("win1", "left"), ("arm3", "failed")], "a" * 40, verb="update")


def _status_panel(out):
    """The R6 panel with a long worker name, a silent worker, a full queue and results of every kind."""
    from armada.client import overview
    row = lambda **k: {"silent": False, "stale": False, "paused": False, "locked": False, "age": "5 min ago", "load_value": 1.5, "cores_value": 4, **k}
    data = overview.collect(Path(tempfile.mkdtemp()), [(LONG, row()), ("arm2", row(silent=True, age="30 h ago SILENT", load_value=None)), ("none", None)], now=2_000_000_000)
    data["queue"] = {"depth": {"high": 9, "normal": 3, "low": 0}, "total": 12, "oldest": "3 h"}
    data["running"] = [{"job": "sys-status-qpr7xp", "class": "sys-status", "worker": LONG, "for": "4 min"}]
    data["recent"] = [{"job": "profile-bkhudd", "class": "profile-run", "worker": LONG, "status": "error", "ago": "9 min"}, {"job": "x-1", "class": "x", "worker": "", "status": "ok", "ago": "now"}]
    overview.show(data, stream=out)


def _help_screen(out):
    from contextlib import redirect_stdout
    from armada import help as arm_help
    with redirect_stdout(out):
        arm_help.main()


def _worker_status_card(out):
    """The rows of `arm-worker status`: a value with an explanation after it (the first part must not push the line past the pane), a value ending in chips, a long path, a long explanation."""
    S = cards.Styled
    ui.say("Status, Thu 2026-11-26 20:07 UTC", ("Worker", "armada"), ("Needs attention", "Registry sync, Security"),
           ("Memory", S([("14.1 GB free of 14.9 GB", "ok"), ("enough for the next job", "muted")])),
           ("Disk", S([("458 GB free of 488 GB", "ok"), ("plenty of room", "muted")])),
           ("Registry sync", S([("REFUSED", "bad"), ("20:04 UTC: refused: the signed approval is for a different registry.yaml than the one on the queue repository", None)])),
           ("Runs on", S([("armada", None), ("DEDICATED", "warn_chip")])), tag="ATTENTION", color=ui.YELLOW, stream=out)


def _classes_card(out):
    ui.say("What your workers can run for you today", *[(cards.Chip("Class", "primary"), cards.Styled([("summarise-read", "strong")])), ("Runs", "read-only summaries of files you attach"),
           ("Runs on", cards.Styled([("armada", None), ("DEDICATED", "warn_chip")])), ("Cost", "medium, waits for your approval")], "\n",
           "A dedicated class goes only to its worker (the command below).", tag="CLASSES", color=ui.PRIMARY, run=["arm-admin workers use WORKER --command CLASS", "arm-run classes --worker NAME"], stream=out)


def _fleet_rule(out):
    out.write(cards.rule("status: a-long-worker-name-that-is-longer-than-the-pane", "WORKER", ui.PRIMARY) + "\n")


KINDS = {
    "the arm-help screen": _help_screen,
    "the rows of arm-worker status": _worker_status_card,
    "a class list card with two commands": _classes_card,
    "a fleet heading with a long name": _fleet_rule,
    "a card with a long steps heading": say("The workers do not all run the same release", ("Worker", "armada"), tag="RELEASES", color=ui.YELLOW, steps=["`arm-admin apply` (every worker in turn)"], steps_label="How to bring them together"),
    "the status panel": _status_panel,
    "the update cards for several workers": _update_cards,
    "a short card": say("Ready", "It is done.", tag="DONE", color=ui.GREEN),
    "facts, why, steps and a command": say("A step of the Windows setup failed", ("Step", "tools"), ("Computer", "192.0.2.50"), cards.Why("The Python installer finished, but python.exe is not where it was expected"),
                                           tag="STOPPED", color=ui.RED, steps=["Run the same command again: it continues from where it stopped."], run="arm-setup add-worker 192.0.2.50"),
    "a long title": say("A step of the Windows setup failed and it is a long title", ("Worker", LONG), tag="STOPPED", color=ui.RED),
    "a long path value": say("Configuration", ("Mailbox key", PATH), tag="NOTE", color=ui.YELLOW),
    "a command": say("Run it", tag="NEXT", color=ui.CYAN, run="arm-admin approve job-1"),            # (a command longer than the pane is drawn alone and left to the terminal to wrap: never cut, so it can be copied whole)
    "a document card": lambda out: ui.document("Job file", '{\n  "id": "profile-bkhudd",\n  "class": "profile-run",\n  "created": "2026-11-21T14:02:11Z"\n}', stream=out),
    "a table": lambda out: ui.table("Workers", ["NAME", "STATE", "LOAD"], [[LONG, "awake", "0.3"], ["arm2", "asleep", "0.0"]], stream=out),
    "a result that ran": lambda out: view.print_result(OK, out=out, err=io.StringIO()),
    "a result that never ran": lambda out: view.print_result(RES, out=out, err=io.StringIO()),
    "a quiet facts block": lambda out: out.write("\n".join(cards.meta([("Measured", [("Time", "0.02 s wall, 0.01 s cpu"), ("Memory", "peak 0 MB of 15219 MB")])])) + "\n"),
    "named blocks": say("Waiting for you", *cards.job_blocks([("profile-bkhudd", [("CLASS", "profile-run"), ("WAITING", "3 hours")])]), tag="APPROVE", color=ui.YELLOW, run=["arm-admin approve profile-bkhudd"]),
    "the workers list": lambda out: out.write("\n".join(__import__("armada.admin.workers", fromlist=["x"]).cards(
        [{"name": "arm2", "os": "ubuntu 24.04", "cpu": "2 cores", "memory": "2 of 3 GB free", "spend": "0/20 jobs", "state": "jail ON", "release": "f4c9ae706", "load": "0.00",
          "age": "5 min ago", "locked": False, "stale": False, "packages": 0, "classes": 3}], width=__import__("shutil").get_terminal_size().columns, color=True)) + "\n"),
    "a setup progress row": lambda out: out.write(_setup_rows() + "\n"),
    "the approval screen": lambda out: out.write(__import__("armada.review", fromlist=["x"]).review_text({"argv": ["python3", "-m", "unittest", "discover", "-s", "tests"], "what": "Runs the unit tests of the project against the attached source.",
                                                                                                  "expect": "about 15 seconds", "files": {"src/armada/jobs.py": "x"}}) + "\n"),
    "a yes/no question": lambda out: out.write(ui.confirm_prompt("Remove the armada worker from 192.0.2.50?", stream=out) + "\n"),
}


FLOOR = {"a setup progress row": 30}                      # the installer progress rows have an 11-column status label: below 30 columns there is no layout for them, and no real pane is that narrow
COMMANDS = ("arm-admin workers use WORKER --command CLASS", "arm-run classes --worker NAME", "arm-setup add-worker 192.0.2.50", "arm-admin approve job-1", "arm-admin approve profile-bkhudd", "python3 -m unittest discover -s tests")      # a command longer than the pane is drawn alone and never cut, so it can be copied whole


class Fits(unittest.TestCase):
    def test_no_line_is_wider_than_the_terminal(self):
        bad = []
        for kind, fn in KINDS.items():
            for cols in [c for c in WIDTHS if c >= FLOOR.get(kind, 0)]:
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
