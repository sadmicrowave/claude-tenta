"""A read-only view of the server over SSH, enough for `detect` to learn about the machine before anything is asked or changed."""
from ..admin.ssh import Ssh
from .host import Result


class RemoteHost:
    def __init__(self, ssh):
        self.ssh, self.env = ssh, {}

    def read(self, path):
        r = self.ssh.capture(f"cat {Ssh.quote([path])} 2>/dev/null")
        return r.stdout if r.returncode == 0 else None

    def exists(self, path):
        return self.ssh.capture(f"test -e {Ssh.quote([path])}").returncode == 0

    def run(self, argv):
        r = self.ssh.capture(Ssh.quote(argv))
        return Result(r.returncode, (r.stdout or "") + (r.stderr or ""))
