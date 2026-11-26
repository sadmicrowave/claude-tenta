"""Shared by the test_admin_* modules: the fake ssh, the base fixture, and the throwaway HOME (support sets it)."""
import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout

from support import REGISTRY, RepoFixture, sh, ssh_key  # noqa: F401

from ct.admin import cli, release
from ct.admin.ssh import Ssh


INSTALLED = "cat /etc/testct/installed-rev 2>/dev/null"


class FakeSsh:
    """Records every remote command; `answers` maps a substring of a captured command to its stdout."""

    host, user, port = "worker.example", "admin", 22
    target = "admin" + "@" + "worker.example"

    def __init__(self):
        self.calls, self.captured, self.answers, self.rc = [], [], {}, {}
        self._run = lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", "")

    def argv(self, remote=None, **k):
        return ["ssh", "x", remote or ""]

    quote = staticmethod(Ssh.quote)

    def interactive(self, remote):
        self.calls.append(remote)
        return next((rc for key, rc in self.rc.items() if key in remote), 0)

    def capture(self, remote):
        self.captured.append(remote)
        hit = self.answers.get(remote) or next((self.answers[k] for k in sorted(self.answers, key=len) if k in remote and k != remote), None)
        if hit is not None:
            return subprocess.CompletedProcess([], 0, hit, "")
        return subprocess.CompletedProcess([], 0, "", "")


class AdminBase(RepoFixture):
    template_kind = "registry"             # the registry is already pushed in the queue (built once per process)

    def setUp(self):
        super().setUp()
        self.ssh = FakeSsh()
        src = self.tmp / "release-src"
        sh("git", "init", "-q", "-b", "main", str(src))
        self.src_dir = src
        self.rel = [self.release_commit({"src/ct/a.py": "a=1\n", "README.md": "x\n"}, "first")]
        self.source = release.Source(self.tmp / "source.git", str(src), "main")
        self.tty = False
        self.ctx = cli.Context(self.repos, self.ssh, self.source, name="testct", config_dir="/etc/testct", code_dir="/opt/testct", home=self.home,
                               ask=lambda p: self.answers.pop(0), is_tty=lambda: self.tty, run_result=lambda jid: self.results_followed.append(jid) or 0)
        self.results_followed = []
        self.repos.pull_results = lambda: None     # the results repo is filled by hand below

    def release_commit(self, files, msg):
        for path, text in files.items():
            p = self.src_dir / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        sh("git", "add", "-A", cwd=self.src_dir)
        sh("git", "commit", "-q", "-m", msg, cwd=self.src_dir)
        return sh("git", "rev-parse", "HEAD", cwd=self.src_dir)

    def put_result(self, jid, **body):
        d = self.repos.results / "results"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{jid}.json").write_text(json.dumps({"id": jid, **body}))

    def put_job(self, jid, **extra):
        self.repos.push_files("jobs", {f"jobs/{jid}.json": json.dumps({"id": jid, "class": "careful", **extra})}, f"job {jid}")

    def admin(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx)
        return code, out.getvalue(), err.getvalue()
