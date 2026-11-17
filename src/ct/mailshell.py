"""The only thing the mailbox account can run (Direct transport): git, on the worker's two repositories, and nothing else.

Your computer logs in as `<name>-mail` with a key whose authorized_keys line is `restrict,command="ct-mail-shell"`, so every login runs this and nothing else: no terminal, no
forwarding, no other program. The command the client sent (SSH_ORIGINAL_COMMAND) is allowed only when it is git reading the queue or the results, or git writing the queue:
the results repository is read-only here, and a hostile client cannot change what the worker publishes. Standard library only."""
import os
import shlex
import sys

READ, WRITE = "git-upload-pack", "git-receive-pack"


def decide(command, queue_path, results_path):
    """The argv to run for the command a client sent, or None when it is not allowed."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return None
    if len(parts) != 2:
        return None
    program, path = parts
    path = os.path.normpath(path)
    if program == READ and path in (os.path.normpath(queue_path), os.path.normpath(results_path)):
        return [program, path]
    if program == WRITE and path == os.path.normpath(queue_path):
        return [program, path]
    return None


def main(argv=None, environ=None):
    from . import config
    env = os.environ if environ is None else environ
    cfg = config.load()
    queue, results = cfg.path("mailbox") + "/queue.git", cfg.path("mailbox") + "/results.git"
    argv = decide(env.get("SSH_ORIGINAL_COMMAND", ""), queue, results)
    if argv is None:
        sys.stderr.write("This account can only fetch from the queue and results repositories and push to the queue.\n")       # output: log (git's own client prints it)
        return 1
    os.execv("/usr/bin/" + argv[0] if os.path.exists("/usr/bin/" + argv[0]) else "/usr/lib/git-core/" + argv[0], argv)


if __name__ == "__main__":
    sys.exit(main())
