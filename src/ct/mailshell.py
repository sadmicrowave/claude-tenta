"""The only thing the mailbox account can run (Direct transport): git, on the worker's two repositories, and nothing else.

Your computer logs in as `<name>-mail` with a key whose authorized_keys line is `restrict,command="ct-mail-shell"`, so every login runs this and nothing else: no terminal, no
forwarding, no other program. The command the client sent (SSH_ORIGINAL_COMMAND) is allowed only when it is git reading the queue or the results, or git writing the queue:
the results repository is read-only here, and a hostile client cannot change what the worker publishes. Standard library only."""
import os
import shlex
import shutil
import subprocess
import sys

READ, WRITE = "git-upload-pack", "git-receive-pack"
LIMIT_HIT = b"exceeds maximum allowed size"          # what git tells the pusher when receive.maxInputSize stops a pack


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
    if argv[0] == WRITE:
        return _receive(argv, env, mailbox)
    return _serve(argv, env)


def mem_available_mb(path="/proc/meminfo"):
    """Available memory in MB where /proc/meminfo exists (Linux), None where it cannot be read (Windows: no memory term)."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def owner_ceiling(env):
    """hardware.max_job_mb as the owner set it: the launcher's CT_MAX_JOB_MB (Windows, where this account cannot read the configuration), else the configuration; None when unset or unreadable (then automatic)."""
    try:
        if env.get("CT_MAX_JOB_MB"):
            return int(env["CT_MAX_JOB_MB"])
        if not env.get("CT_MAILBOX"):
            from . import config
            return config.load().hardware.max_job_mb
    except Exception:                           # a reading that cannot be made never blocks a push; the worker still checks the job file
        pass
    return None


def _receive(argv, env, mailbox, usage=shutil.disk_usage):
    """A push, held to what the worker accepts BEFORE it is written: nothing while the disk is below the reserve, and git stops reading a pack that grows past the ceiling (receive.maxInputSize), so the bytes never stay.
    Without this a client holding the key could put any amount on the disk, because the worker's ceiling is checked only after the job file is in the repository (finding F5)."""
    from . import intake, refusals
    try:
        u = usage(mailbox)
    except OSError:
        u = None                                  # the disk cannot be read: let git decide, the worker still checks the job file
    cap = None
    if u is not None:
        why, facts = intake.push_decision(u.free / 1e9, u.total / 1e9, owner_ceiling(env), mem_available_mb())
        if why:
            sys.stderr.write(str(refusals.refuse("job.push.disk_nearly_full", **facts)) + "\n")       # output: log (git's own client prints it)
            return 1
        cap = facts
    child = _git_env(argv, env)
    if cap:
        child.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "receive.maxInputSize", "GIT_CONFIG_VALUE_1": str(cap["cap_bytes"])})
    child_proc = subprocess.Popen(_git_argv(argv, env), env=child, stdout=subprocess.PIPE)            # git's answers pass through unchanged and at once; only a look at them for "too big"
    stopped, tail = False, b""
    try:
        while True:
            chunk = os.read(child_proc.stdout.fileno(), 65536)
            if not chunk:
                break
            stopped = stopped or LIMIT_HIT in tail + chunk                 # (the words may be split between two reads)
            tail = chunk[-len(LIMIT_HIT):]
            os.write(1, chunk)
    except OSError:                                                      # the pusher went away: git sees the same and ends
        pass
    rc = child_proc.wait()
    if cap and stopped:                                   # (git can exit 0 after telling the pusher about the unpack error, so the sign is what it said)
        sys.stderr.write(str(refusals.refuse("job.push.too_big", ceiling=cap["ceiling"], slack=intake.PUSH_SLACK_MB)) + "\n")       # output: log (git's own client prints it)
    return rc


def _git_env(argv, env):
    """The environment git runs in: nothing inherited, and this one repository trusted for this one command (git 2.35.2 and newer refuse another account's repository as "dubious ownership" otherwise; found on a real
    machine: the test machines had trusted every repository for another reason, which hid it)."""
    if os.name == "nt":
        from pathlib import Path
        git = Path(env.get("CT_GIT") or Path(sys.executable).resolve().parents[1] / "git" / "cmd" / "git.exe")
        return {"SystemRoot": env.get("SystemRoot", r"C:\Windows"), "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": argv[1], "PATH": str(git.parent)}
    return {"PATH": "/usr/bin:/bin", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": argv[1]}


def _git_argv(argv, env):
    if os.name == "nt":
        from pathlib import Path
        git = Path(env.get("CT_GIT") or Path(sys.executable).resolve().parents[1] / "git" / "cmd" / "git.exe")
        return [str(git), {"git-upload-pack": "upload-pack", "git-receive-pack": "receive-pack"}[argv[0]], argv[1]]
    return ["/usr/bin/" + argv[0] if os.path.exists("/usr/bin/" + argv[0]) else "/usr/lib/git-core/" + argv[0], argv[1]]


def _serve(argv, env):
    """A fetch: git runs in place of this program (no limit is needed on what leaves the worker)."""
    child = _git_env(argv, env)
    if os.name == "nt":
        return subprocess.run(_git_argv(argv, env), env=child).returncode
    os.execve(_git_argv(argv, env)[0], argv, child)


if __name__ == "__main__":
    sys.exit(main())
