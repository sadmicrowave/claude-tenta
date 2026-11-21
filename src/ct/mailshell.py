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
    path = _same(path)
    allowed = {_same(queue_path): queue_path, _same(results_path): results_path}          # compared in one form, but git is given the folder as configured
    if program == READ and path in allowed:
        return [program, allowed[path]]
    if program == WRITE and path == _same(queue_path):
        return [program, queue_path]
    return None


def _same(path):
    """A path in the one form the comparison uses. On Windows a client's URL path arrives as `/C:/x/y.git` and the configuration says `C:/x/y.git`: the slash before the drive letter is dropped, and the
    drive letter and the rest are compared without regard to case (the file system does the same). On POSIX it is just normpath."""
    if len(path) > 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if len(path) > 2 and path[1] == ":":
        return os.path.normpath(path.replace("\\", "/")).replace("\\", "/").lower()
    return os.path.normpath(path)


def _run_on_windows(argv, env):
    """Windows has no exec: run the same git as a child with this process's own input and output, trusting exactly this one repository for this one command. The git is the tools folder's (python.exe sits in
    <tools>\\python, git.exe in <tools>\\git\\cmd); `git receive-pack` and `git upload-pack` are the same programs as `git-receive-pack` and `git-upload-pack`."""
    import subprocess
    from pathlib import Path
    git = Path(env.get("CT_GIT") or Path(sys.executable).resolve().parents[1] / "git" / "cmd" / "git.exe")
    sub = {"git-upload-pack": "upload-pack", "git-receive-pack": "receive-pack"}[argv[0]]
    child = {"SystemRoot": env.get("SystemRoot", r"C:\Windows"), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": argv[1], "PATH": str(git.parent)}
    return subprocess.run([str(git), sub, argv[1]], env=child).returncode


def main(argv=None, environ=None):
    env = os.environ if environ is None else environ
    mailbox = env.get("CT_MAILBOX")                          # Windows: the launcher says where the mailbox is, so this account needs no read access to the worker's configuration
    if not mailbox:
        from . import config
        mailbox = config.load().path("mailbox")
    queue, results = mailbox + "/queue.git", mailbox + "/results.git"
    argv = decide(env.get("SSH_ORIGINAL_COMMAND", ""), queue, results)
    if argv is None:
        sys.stderr.write("This account can only fetch from the queue and results repositories and push to the queue.\n")       # output: log (git's own client prints it)
        return 1
    # The results repository belongs to the worker account, so git (2.35.2 and newer) refuses to serve it to this one ("detected dubious ownership") unless told it is meant. Said for exactly this
    # repository, for this one command, and nowhere else (found on a real machine: the test machines had trusted every repository for another reason, which hid it).
    if os.name == "nt":
        return _run_on_windows(argv, env)
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": argv[1]}
    os.execve("/usr/bin/" + argv[0] if os.path.exists("/usr/bin/" + argv[0]) else "/usr/lib/git-core/" + argv[0], argv, env)


if __name__ == "__main__":
    sys.exit(main())
