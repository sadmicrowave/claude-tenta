"""The machine a step acts on. Everything the wizard does to a computer goes through one of these, so every step can be shown first
(dry run), and tested with a fake that records instead of acting."""
import os
import subprocess
from pathlib import Path


class Result:
    def __init__(self, rc, out=""):
        self.rc, self.out = rc, out

    @property
    def ok(self):
        return self.rc == 0


class Host:
    """The real machine, or a prefix of it (`root` lets tests and chroots work on a folder)."""

    def __init__(self, root="/", env=None):
        self.root, self.env = Path(root), dict(os.environ if env is None else env)

    def path(self, p):
        return self.root / str(p).lstrip("/")

    def run(self, argv, *, input=None, timeout=120):
        try:
            r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, input=input, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            return Result(127, str(e))
        return Result(r.returncode, (r.stdout or "") + (r.stderr or ""))

    def read(self, p):
        try:
            return self.path(p).read_text(encoding="utf-8")
        except OSError:
            return None

    def exists(self, p):
        return self.path(p).exists()

    def write(self, p, text, mode=0o644):
        dest = self.path(p)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, dest)

    def remove(self, p):
        self.path(p).unlink(missing_ok=True)

    def which(self, name):
        for d in self.env.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
            if (Path(d) / name).exists():
                return str(Path(d) / name)
        return None
