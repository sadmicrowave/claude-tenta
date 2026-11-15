"""The one place cw-admin talks to the server: SSH with keys only, never a password typed through us.

`capture` runs a command with no terminal and no prompts (fails fast if key login is not set up); `interactive` gives the command your
terminal so sudo can ask you for its password itself. Nothing here remembers a password or keeps a long-lived connection: a connection is
shared for at most a minute so that one command that makes several calls asks for the sudo password once, then lets go.
"""
import os
import shlex
import subprocess
import tempfile

from ..client.repo import ClientError

CONNECT_TIMEOUT_S = 10
SHARE_FOR_S = 60


class Ssh:
    def __init__(self, host, user=None, port=22, *, run=subprocess.run, control_dir=None):
        if not host or host.startswith("-"):
            raise ClientError("No server address is configured", "server.host is empty or not an address.", example="cw-setup client")
        self.host, self.user, self.port, self._run = host, user, int(port), run
        self.control_dir = control_dir or os.path.join(tempfile.gettempdir(), f"cw-ssh-{os.getuid()}")

    @property
    def target(self):
        return f"{self.user}@{self.host}" if self.user else self.host

    def argv(self, remote=None, *, tty=False, batch=False):
        a = ["ssh"] + (["-t"] if tty else [])
        if batch:
            a += ["-o", "BatchMode=yes"]
        os.makedirs(self.control_dir, mode=0o700, exist_ok=True)
        a += ["-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", "-p", str(self.port), "-o", "ControlMaster=auto", "-o", f"ControlPersist={SHARE_FOR_S}",
              "-o", f"ControlPath={self.control_dir}/%C", self.target]
        return a + ([remote] if remote else [])

    @staticmethod
    def quote(args):
        return " ".join(shlex.quote(str(x)) for x in args)

    def capture(self, remote):
        """Run `remote` (one command line) with no terminal. Returns the CompletedProcess; raises ClientError if SSH itself failed."""
        r = self._run(self.argv(remote, batch=True), capture_output=True, text=True)
        if r.returncode == 255:
            raise ClientError(f"Could not connect to {self.host}", (r.stderr or "").strip().splitlines()[-1:] and (r.stderr or "").strip().splitlines()[-1] or None,
                              ["Check that SSH key login works from this computer."], example=f"ssh {self.target} true")
        return r

    def interactive(self, remote):
        """Run `remote` on your terminal (sudo may ask for your password). Returns its exit code."""
        return self._run(self.argv(remote, tty=True)).returncode
