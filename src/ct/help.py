"""ct-help: every command in one screen, grouped by who runs it and where."""
import sys

from . import cards, ui

SECTIONS = [
    ("Set up", "run once", [("ct-setup install", "everything, from your computer: both sides, the deploy keys, a first job"),
                            ("ct-setup add-worker [address]", "find another computer on your network, check it, and set it up as one more worker; your other installs are not touched"),
                            ("ct-setup client", "set up your computer: config, SSH key login, the two private repositories"),
                            ("ct-setup server", "turn a clean Ubuntu or Debian machine into a worker (run as root on it)"),
                            ("ct-setup audit", "check a server against its configuration, read-only"),
                            ("ct-setup uninstall", "remove the install from the server (preview first; the protections stay)"),
                            ("ct-setup demo", "try the question and protection screens on sample content; nothing is saved")]),
    ("Send work", "on your computer", [("ct-run submit <class>", "queue a job and wait for its result"),
                                       ("ct-run jobs", "your recent jobs, and the ones waiting for your approval"),
                                       ("ct-run classes", "what the worker can run today"),
                                       ("ct-run patch <job>", "show the patch a write-capable job returned, and apply it here after a look"),
                                       ("ct-run offload ...", "find out which of your commands are worth running on the worker")]),
    ("Run it", "from your computer; approvals and orders are signed here, no login", [("ct-admin status | check | workers | watch", "the worker's health, the connection to it, a comparison of your workers, and alerts when one locks, goes quiet or cannot be reached"),
                                              ("ct-admin wake | audit | tripwire-test", "wake a worker now, check it against its configuration, or prove its tripwire (every worker, or --worker NAME)"),
                                              ("ct-admin review | approve", "look at waiting jobs, then approve exactly what you saw"),
                                              ("ct-admin cancel | kill", "stop a job that has not started, or one that is running"),
                                              ("ct-admin quarantine | unlock | output", "look at a security hold, clear it, read a failed command's output"),
                                              ("ct-admin adopt | retire | proposals", "add or remove job classes, and merge the changes waiting on branches"),
                                              ("ct-admin action add | remove | list | action-hash", "register, remove or list commands that run as root; print a program's fingerprint"),
                                              ("ct-admin theme", "show the colour roles and where the theme came from"),
                                              ("ct-admin diff | apply | update | clean", "see and install a release, with a health check and a way back; update looks for a newer one; clean clears old signed approvals")]),
    ("On the worker", "after SSH, or through ct-admin", [("ct-worker status | show | review | approve ...", "the worker's own commands (always ct-worker, whatever the install is called)"),
                                                          ("sudo ct-update --rev <commit>", "install a release (ct-admin apply runs this for you)")]),
]


def main(argv=None):
    rows = []
    for title, where, items in SECTIONS:
        rows += [cards.Heading(f"{title} ({where})")] + [(cards.Cmd(cmd), what) for cmd, what in items]
    rows += [cards.Heading("More")] + [(cards.Cmd(c), "every command with its options and examples") for c in ("ct-run help", "ct-admin help", "ct-setup help")]
    ui.say("claude-tenta", "Offload slow jobs to a spare machine on your network.", "\n", *rows, tag="HELP", color=ui.PRIMARY, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
