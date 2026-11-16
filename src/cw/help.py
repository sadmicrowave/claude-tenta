"""cw-help: every command in one screen, grouped by who runs it and where."""
import sys

from . import ui
from .ui import BOLD, CYAN, GREEN, paint

SECTIONS = [
    ("Set up", "run once", [("cw-setup client", "set up your computer: config, SSH key login, the two private repositories"),
                            ("cw-setup server", "turn a clean Ubuntu or Debian machine into a worker (run as root on it)"),
                            ("cw-setup audit", "check a server against its configuration, read-only")]),
    ("Send work", "on your computer", [("cw-run submit <class>", "queue a job and wait for its result"),
                                       ("cw-run jobs", "your recent jobs, and the ones waiting for your approval"),
                                       ("cw-run classes", "what the worker can run today"),
                                       ("cw-run offload ...", "find out which of your commands are worth running on the worker")]),
    ("Run it", "on your computer, over SSH", [("cw-admin status | check", "the worker's health and the connection to it"),
                                              ("cw-admin review | approve", "look at waiting jobs, then approve exactly what you saw"),
                                              ("cw-admin cancel | kill", "stop a job that has not started, or one that is running"),
                                              ("cw-admin quarantine | unlock | output", "look at a security hold, clear it, read a failed command's output"),
                                              ("cw-admin adopt | retire | proposals", "add or remove job classes"),
                                              ("cw-admin diff | apply", "see and install a release, with a health check and a way back")]),
    ("On the worker", "after SSH, or through cw-admin", [("<name> status | show | review | approve ...", "the worker's own commands (the name is your install's name)"),
                                                          ("sudo <name>-update --rev <commit>", "install a release (cw-admin apply runs this for you)")]),
]


def main(argv=None):
    out = sys.stdout
    print(f"\n{paint('claude-worker', BOLD, GREEN, stream=out)}  {paint('offload slow jobs to a spare machine on your network', BOLD, stream=out)}")
    for title, where, rows in SECTIONS:
        print(f"\n{paint(title.upper(), BOLD, CYAN, stream=out)}  ({where})")
        width = max(len(r[0]) for r in rows)
        for cmd, what in rows:
            print(f"  {paint(cmd.ljust(width), BOLD, stream=out)}  {what}")
    print(f"\nEvery command has its own help:  {paint('cw-run help', BOLD, GREEN, stream=out)}   {paint('cw-admin help', BOLD, GREEN, stream=out)}   "
          f"{paint('cw-setup help', BOLD, GREEN, stream=out)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
