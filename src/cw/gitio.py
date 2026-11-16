"""Everything that touches the two git repos: fetching jobs, publishing results, and keeping the results history small.

  queue repo    the worker only READS it (a read-only deploy key): jobs live on the jobs branch, the registry proposal on main.
  results repo  the worker WRITES it (its own deploy key). A deploy key that can write can also read and rewrite history, so the results
                repo is never an integrity boundary: treat everything read from it as untrusted data.

Git is always run with a hard timeout that kills the whole process group (git -> ssh), and never prompts. One lock serialises the
worker's own git work, with a bounded wait so a hung holder cannot hang everything else.
"""
import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .runner import kill_group, run_group

SSH_COMMAND = "ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"


def harden_git_env(environ=None):
    """Never let git or ssh wait forever on a dead connection or a password prompt."""
    environ = os.environ if environ is None else environ
    environ.setdefault("GIT_SSH_COMMAND", SSH_COMMAND)
    environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    return environ


class FileLock:
    """Exclusive file lock with a bounded wait; check `.got` (a hung holder must never hang everyone else)."""

    def __init__(self, path, wait=300):
        self.path, self.wait, self.got, self.f = Path(path), wait, False, None

    def __enter__(self):
        self.f = open(self.path, "w")
        end = time.time() + self.wait
        while True:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.got = True
                break
            except BlockingIOError:
                if time.time() >= end:
                    break
                time.sleep(0.2)
        return self

    def __exit__(self, *a):
        self.f.close()


class GitIO:
    def __init__(self, queue_repo, results_repo, state, queue_branch="main", jobs_branch="jobs", results_branch="main",
                 git_timeout=60, lock_wait=300, committer=("cw-worker", "cw-worker@localhost"), log=None, run=run_group):
        self.queue, self.results, self.state = Path(queue_repo), Path(results_repo), Path(state)
        self.queue_branch, self.results_branch = queue_branch, results_branch
        # where jobs are read from, in order: the jobs branch (so main stays a clean code log), then main's jobs/ folder (older clients)
        self.job_sources = ((f"origin/{jobs_branch}", "jobs"), (f"origin/{queue_branch}", "jobs"))
        self.git_timeout, self.lock_wait, self.committer = git_timeout, lock_wait, committer
        self.log = log or (lambda msg: None)
        self._run = run

    def lock(self, wait=None):
        return FileLock(self.state / "git.lock", self.lock_wait if wait is None else wait)

    # ---- plain git ----
    def git(self, repo, *args, check=False):
        r = self._run(["git", "-C", str(repo), *args], self.git_timeout)
        if check and r.returncode:
            raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)
        return r

    def git_bytes(self, repo, *args):
        """stdout of a git command as raw bytes (exact digests need exact bytes), or None on failure or timeout."""
        p = subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=self.git_timeout)
        except subprocess.TimeoutExpired:
            kill_group(p.pid)
            p.communicate()
            return None
        return out if p.returncode == 0 else None

    def remote_has(self, repo, branch):
        return self.git(repo, "ls-remote", "--exit-code", "--heads", "origin", branch).returncode == 0

    # ---- reading jobs ----
    def sync(self):
        with self.lock() as lk:
            if not lk.got:
                self.log("git lock busy; skipping this sync")
                return False
            for repo, branch in ((self.queue, self.queue_branch), (self.results, self.results_branch)):
                if not self.remote_has(repo, branch):         # e.g. a brand-new empty results repo: nothing to pull yet
                    continue
                if repo == self.results:                      # we are the only writer here: keep our branch current
                    r = self.git(repo, "pull", "--rebase", "--quiet", "origin", branch)
                    if r.returncode != 0:
                        self.git(repo, "rebase", "--abort")
                        self.log(f"git pull failed in {repo}: " + r.stderr.strip()[:300])
                else:                                         # the queue repo is read-only to us: fetch every branch, drop deleted ones
                    r = self.git(repo, "fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")
                    if r.returncode != 0:
                        self.log(f"git fetch failed in {repo}: " + r.stderr.strip()[:300])
            return True

    def job_listing(self):
        """{job id: (ref, path)} for every job file visible in the queue repo. The first source wins when an id is in several."""
        found = {}
        for ref, folder in self.job_sources:
            out = self.git_bytes(self.queue, "ls-tree", "-z", "--name-only", ref, f"{folder}/")
            for name in (out or b"").decode("utf-8", "replace").split("\0"):
                if name.endswith(".json"):
                    found.setdefault(Path(name).stem, (ref, name))
        return found

    def job_bytes(self, jid):
        """The job exactly as the worker will run it: its inbox copy (first seen wins), else straight from git."""
        inbox = self.state / "inbox" / f"{jid}.json"
        if inbox.exists():
            return inbox.read_bytes()
        src = self.job_listing().get(jid)
        return self.git_bytes(self.queue, "show", f"{src[0]}:{src[1]}") if src else None

    def approval_bytes(self, jid):
        """The signed approval file for a job from the approvals branch of the queue repo, or None. Only read here; trusting it is approvals.check's job."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", jid or ""):
            return None
        return self.git_bytes(self.queue, "show", f"origin/approvals:approvals/{jid}.json")

    def published_result(self, jid):
        """The result file this worker last published for a job (from its own results clone), or None."""
        try:
            return json.loads((self.results / "results" / f"{jid}.json").read_text())
        except (OSError, ValueError):
            return None

    # ---- publishing ----
    def publish(self, res):
        """Write results/<id>.json, commit, push. Returns True if the push succeeded (False: retry later)."""
        return self.publish_files({f"results/{res['id']}.json": json.dumps(res, indent=2) + "\n"}, f"result {res['id']}: {res['status']}")

    def publish_file(self, rel, text, msg):
        return self.publish_files({rel: text}, msg)

    def publish_files(self, files, msg):
        with self.lock() as lk:
            if not lk.got:
                self.log(f"git lock busy; will retry publishing: {msg}")
                return False
            return self._publish_files(files, msg)

    def _publish_files(self, files, msg):
        for rel, text in files.items():
            f = self.results / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            self.git(self.results, "add", rel)
        name, email = self.committer
        self.git(self.results, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", msg)
        for _ in range(3):
            # HEAD:refs/heads/<branch> works whatever the local branch is called (master vs main)
            if self.git(self.results, "push", "origin", f"HEAD:refs/heads/{self.results_branch}").returncode == 0:
                return True
            if self.remote_has(self.results, self.results_branch):
                self.git(self.results, "pull", "--rebase", "--quiet", "origin", self.results_branch)
        self.log(f"push failed: {msg}")
        return False

    # ---- keeping the results history small ----
    def _identity_env(self, idx=None):
        name, email = self.committer
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
        if idx:
            env["GIT_INDEX_FILE"] = str(idx)
        return env

    def prune_results(self, days=14, threshold=200, force=False):
        """Squash the results repo to ONE commit holding only results younger than `days` (plus other files such as registry.json), and
        force-push it with a lease on the exact remote commit seen. Results are disposable output, so history is not worth keeping.
        Returns a message; nothing is changed if the remote moved underneath."""
        with self.lock() as lk:
            if not lk.got:
                return "git lock busy; try again later"
            r = self.results
            n = int((self.git(r, "rev-list", "--count", "HEAD").stdout.strip() or "0"))
            if not force and n <= threshold:
                return f"{n} commits; no prune needed (threshold {threshold})"
            tree = self.git(r, "ls-tree", "-r", "-z", "HEAD").stdout
            keep, dropped = [], 0
            cutoff = time.time() - days * 86400
            for ent in filter(None, tree.split("\0")):
                meta, _, path = ent.partition("\t")
                mode, typ, sha = meta.split()
                if path.startswith("results/") and path.endswith(".json"):
                    try:
                        res = json.loads(self.git(r, "cat-file", "-p", sha).stdout)
                        when = datetime.fromisoformat((res.get("finished") or res.get("started") or "").replace("Z", "+00:00"))
                        if when.timestamp() < cutoff:
                            dropped += 1
                            continue
                    except Exception:
                        pass                                   # unreadable or undated: keep it
                keep.append(f"{mode} {sha}\t{path}\n")
            idx = self.state / "prune.index"
            idx.unlink(missing_ok=True)
            env = self._identity_env(idx)
            t = self.git_timeout
            try:
                subprocess.run(["git", "-C", str(r), "read-tree", "--empty"], env=env, check=True, timeout=t)
                subprocess.run(["git", "-C", str(r), "update-index", "--add", "--index-info"], env=env, check=True, input="".join(keep).encode(), timeout=t)
                new_tree = subprocess.run(["git", "-C", str(r), "write-tree"], env=env, check=True, capture_output=True, timeout=t).stdout.decode().strip()
                commit = subprocess.run(["git", "-C", str(r), "commit-tree", new_tree, "-m", f"results: squashed history, keeping the last {days} days"],
                                        env=env, check=True, capture_output=True, timeout=t).stdout.decode().strip()
            except (subprocess.SubprocessError, OSError) as e:
                return f"prune failed while building the new commit: {e}"
            finally:
                idx.unlink(missing_ok=True)
            remote = self.git(r, "ls-remote", "origin", f"refs/heads/{self.results_branch}").stdout.split()
            lease = f"--force-with-lease=refs/heads/{self.results_branch}" + (f":{remote[0]}" if remote else ":")
            p = self.git(r, "push", lease, "origin", f"{commit}:refs/heads/{self.results_branch}")
            if p.returncode != 0:
                return "prune push refused (the remote changed underneath us); nothing was changed: " + p.stderr.strip()[:200]
            self.git(r, "reset", "-q", "--hard", commit)
            return f"results history squashed: {n} commits -> 1, dropped {dropped} results older than {days} days"
