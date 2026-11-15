"""Shared fixtures for the worker and CLI tests: a requester, a bare queue repo and results repo, and a worker wired to them with only the
command runner and the process spawner faked."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# Safety net: no test may ever touch the real home folder (~/.cw, ~/.config, ...). Importing this module points HOME at a throwaway folder.
os.environ["HOME"] = tempfile.mkdtemp(prefix="cw-test-home-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cw import alerts, gitio, guard, packages, results, util, worker  # noqa: E402
from cw.client import cli  # noqa: E402
from cw.client.repo import ClientRepos  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
IP = "192." + "168." + "7.77"
REGISTRY = """\
classes:
  echo:
    tier: auto
    cost: light
    run: [/bin/echo, "{word}"]
    params: {word: {regex: "[a-z]+"}}
    timeout_s: 30
  careful:
    tier: approve
    cost: medium
    run: [/bin/echo, careful]
  big:
    tier: auto
    cost: heavy
    run: [/bin/echo, big]
  keyed:
    tier: auto
    cost: light
    run: [/bin/echo, keyed]
    output_keys: [ok]
  files:
    tier: auto
    cost: light
    run: [/bin/cat, in.txt]
    accepts_files: true
  profile-run:
    kind: profile
    cost: heavy
    accepts_files: true
    max_timeout_s: 600
"""


def sh(*cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd}: {r.stderr}"
    return r.stdout.strip()


class FakeRunner:
    """Stands in for runner.run_command: answers from `reply`, records every call."""

    def __init__(self):
        self.calls = []
        self.reply = ("ok", 0, "fine\n", {"wall_s": 1.0, "peak_rss_mb": 40, "mem_total_mb": 8000, "cores_busy": 0.5, "peak_temp_c": 50, "disk_mb": 0}, "")

    def __call__(self, argv, timeout, wd, **kw):
        self.calls.append({"argv": argv, "timeout": timeout, "wd": Path(wd), "kw": kw, "files": sorted(p.name for p in Path(wd).iterdir())})
        return self.reply


class Sensors:
    def __init__(self):
        self.t = 40

    def temp(self):
        return self.t

    def meminfo(self):
        return 8000, 6000

    def group_rss_mb(self, pgid):
        return 0.0


class Base(unittest.TestCase):
    def setUp(self):
        t = self.tmp = Path(tempfile.mkdtemp())
        for name in ("queue", "results"):
            sh("git", "init", "-q", "--bare", "-b", "main", str(t / f"{name}.git"))
        sh("git", "clone", "-q", str(t / "queue.git"), str(t / "requester"))
        (t / "requester" / "README.md").write_text("queue\n")
        sh("git", "add", "-A", cwd=t / "requester")
        sh("git", "commit", "-q", "-m", "init", cwd=t / "requester")
        sh("git", "push", "-q", "origin", "main", cwd=t / "requester")
        sh("git", "clone", "-q", str(t / "queue.git"), str(t / "w-queue"))
        sh("git", "clone", "-q", str(t / "results.git"), str(t / "w-results"))
        self.state, self.home = t / "state", t / "home"
        self.home.mkdir()
        self.state.mkdir()
        self.registry_path = t / "registry.yaml"
        self.registry_path.write_text(REGISTRY)
        self.io = gitio.GitIO(t / "w-queue", t / "w-results", self.state, lock_wait=1, committer=("cw-worker", "cw-worker@localhost"))
        self.pushes, self.logged = [], []
        self.notifier = alerts.Notifier(url="https://ntfy.example/t", opener=self.pushes.append, syslog=False, product="testcw")
        self.seclog = guard.SecurityLog(self.state / "security.log")
        self.lockdown = alerts.Lockdown(self.state, self.notifier, self.seclog, product="testcw")
        self.keeper = results.OutputKeeper(self.state)
        self.pk = packages.Packages(t / "tools", self.state / "profile-env", run=lambda argv, timeout, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        self.sensors, self.runner, self.spawned, self.load = Sensors(), FakeRunner(), [], 0.2
        self.settings = worker.Settings(state=self.state, registry_path=self.registry_path, tools_root=t / "tools", product="testcw")
        self.make_worker()

    def make_worker(self, **over):
        kw = dict(seclog=self.seclog, sensors=self.sensors, runner=self.runner, log=self.logged.append, home=self.home, hostname="zzhost",
                  netns=False, is_job_process=lambda pid, jid: False, spawn=lambda jid: self.spawned.append(jid) or 4242, loadavg=lambda: self.load,
                  find_program=lambda n: "/usr/bin/" + n if n in ("ls", "jq") else None)
        kw.update(over)
        self.w = worker.Worker(self.settings, self.io, self.lockdown, self.keeper, self.pk, **kw)
        return self.w

    def job(self, jid, cls="echo", branch="jobs", **extra):
        body = {"id": jid, "class": cls, "params": {"word": "hi"} if cls == "echo" else {}, "created": util.iso(), **extra}
        req = self.tmp / "requester"
        remote = subprocess.run(["git", "-C", str(self.tmp / "queue.git"), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], capture_output=True).returncode == 0
        sh("git", "checkout", "-q", "-B", branch, f"origin/{branch}" if remote else "origin/main", cwd=req)
        (req / "jobs").mkdir(exist_ok=True)
        (req / "jobs" / f"{jid}.json").write_text(json.dumps(body))
        sh("git", "add", "-A", cwd=req)
        sh("git", "commit", "-q", "-m", f"job {jid}", cwd=req)
        sh("git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=req)
        return body

    def result(self, jid):
        r = subprocess.run(["git", "-C", str(self.tmp / "results.git"), "show", f"main:results/{jid}.json"], capture_output=True, text=True)
        return json.loads(r.stdout) if r.returncode == 0 else None

    def run_all(self):
        """fetch, then execute everything that was started."""
        self.w.fetch()
        while self.spawned:
            jid = self.spawned.pop(0)
            self.w.execute(jid)
            self.w._p("running", jid).unlink(missing_ok=True)
        return self


class RepoFixture(Base):
    """The real worker fixture plus a client with its own clones of the same two repos."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "clienthome"
        self.sleeps = []
        self.repos = ClientRepos(self.home / "queue", self.home / "results", str(self.tmp / "queue.git"), str(self.tmp / "results.git"), sleep=self.sleeps.append)
        self.repos.clone_all(self.home)
        self.said = mock.patch.dict(os.environ, {"CW_COLOR": "never"})
        self.said.start()
        self.addCleanup(self.said.stop)
        self.answers = []
        self.admin_calls = []
        self.ctx = cli.Context(self.repos, product="testcw", admin="tcw-admin", ask=lambda p: self.answers.pop(0), sleep=lambda s: None,
                               run_admin=lambda args: self.admin_calls.append(args) or 0, is_tty=lambda: False, home=self.home)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args), ctx=self.ctx)
        return code, out.getvalue(), err.getvalue()
