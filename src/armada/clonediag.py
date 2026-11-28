"""Why a clone from the worker's mailbox (or from GitHub) failed, in plain words, and the one thing to do about THAT cause.

A list of steps that fits every failure ("is the worker on? run the installer again") is wrong for most of them: it sent someone to re-run an installer that could not fix what was wrong. Each
known cause here has its own meaning and its own fix; the machine's real error text is always kept and shown by the caller, and an error that is not recognised gets the general list.
"""
import re


def classify(text, *, kind="direct", admin="you@the-worker", path="", host=""):
    """(cause id or None, meaning, [steps]) for the text git and ssh printed."""
    t = text or ""
    low = t.lower()
    if "dubious ownership" in low and kind == "direct":
        repo = path or (re.search(r"repository at '([^']+)'", t) or [None, ""])[1]
        return ("dubious-ownership",
                "git refused to serve a repository that belongs to another account on the worker. The worker's own software normally tells git it is meant; this worker's release does not yet.",
                [f"Fix it on the worker with one command, then run the same command again: ssh -t {admin} 'sudo git config --system --add safe.directory {repo or 'THE-REPOSITORY-PATH-ABOVE'}'",
                 "Or update the worker's release (it is the release that tells git): arm-admin update."])
    if "permission denied (publickey" in low or "permission denied, please try again" in low:
        return ("key-refused", "The worker did not accept this computer's key for its mailbox account.",
                ["The key for it was made by the installer and sent to the worker with it: run arm-setup install again to send it, or check that it is still in the worker's mailbox account's authorized keys."] if kind == "direct" else
                ["Check that your GitHub SSH key is set up on this computer: ssh -T git@github.com"])
    if "host key verification failed" in low or "remote host identification has changed" in low:
        return ("host-key", "This computer remembers a different identity for that address than the one that answered (the worker was reinstalled, or it is another machine).",
                [f"If you reinstalled the worker, forget the old identity: ssh-keygen -R {host or 'the-worker-address'}", "If you did not, stop: something else is answering at that address."])
    if any(x in low for x in ("timed out", "no route to host", "connection refused", "could not resolve", "name or service not known", "network is unreachable")):
        return ("unreachable", "The worker did not answer at all.", [f"Is the worker on, and on this network? Try: ssh {admin} true"])
    missing = "does not appear to be a git repository" in low or "no such file or directory" in low or ("repository" in low and "does not exist" in low) or "repository not found" in low
    if missing and kind != "direct":
        return ("no-repository", "GitHub has no repository at that address, or this computer's key cannot see it.",
                ["Check that your GitHub SSH key is set up on this computer: ssh -T git@github.com", "Then check the repository names in your configuration, or run arm-setup install to create them."])
    if missing:
        return ("no-mailbox", "The worker answered, but the mailbox repository is not there.",
                ["The worker's setup did not get as far as making it. Run arm-setup install again (it continues where it stopped), and read what it says about the repositories."])
    if "this account can only fetch" in low and kind == "direct":
        return ("shell-refused", "The worker's mailbox account refused the command git sent it: its mailbox software is older or broken.", ["Update the worker: arm-admin update, or run arm-setup install again."])
    return None, "", []
