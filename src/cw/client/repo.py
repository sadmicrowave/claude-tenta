"""The client's two clones: the queue repo (jobs are pushed to its jobs branch) and the results repo (read-only to the client).

Jobs are pushed with git plumbing (a commit is built from a temporary index, never touching a working tree) so a half-edited folder can
never end up in a job, and a push is never forced: a rejected one is re-fetched and retried. The jobs branch is disposable data and may be
squashed; the results repo is mirrored (fetch and reset), never rebased onto, because the worker squashes it now and then.
"""
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
CLIENT_IDENTITY = {"GIT_AUTHOR_NAME": "cw-client", "GIT_AUTHOR_EMAIL": "cw-client@localhost", "GIT_COMMITTER_NAME": "cw-client", "GIT_COMMITTER_EMAIL": "cw-client@localhost"}
PENDING = ("awaiting_approval", "running", "queued")        # a job in one of these states has no final result yet


class ClientError(Exception):
    """Something the person can act on; the message says what and how."""

    def __init__(self, title, why=None, steps=None, example=None):
        super().__init__(title)
        self.title, self.why, self.steps, self.example = title, why, steps or [], example


class ClientRepos:
    def __init__(self, queue, results, queue_url, results_url, *, results_branch="main", jobs_branch="jobs", keep_commits=150, sleep=time.sleep):
        self.queue, self.results = Path(queue), Path(results)
        self.queue_url, self.results_url = queue_url, results_url
        self.results_branch, self.jobs_branch, self.keep_commits, self._sleep = results_branch, jobs_branch, keep_commits, sleep

    # ---- plain git ----
    def git(self, repo, *args, check=True, env=None, input=None):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, input=input)
        if check and r.returncode:
            raise ClientError(f"git {' '.join(args)} failed in {repo}", r.stderr.strip())
        return r

    def is_set_up(self):
        return (self.queue / ".git").exists() and (self.results / ".git").exists()

    def need_setup(self):
        if not self.is_set_up():
            raise ClientError("This computer is not set up yet", example="cw-run setup")

    def clone_all(self, home):
        """Clone whichever of the two repos is missing. Returns [(what, path)] for each that was cloned or already there."""
        Path(home).mkdir(parents=True, exist_ok=True)
        rows = []
        for path, url in ((self.queue, self.queue_url), (self.results, self.results_url)):
            if (path / ".git").exists():
                rows.append(("already cloned", str(path)))
                continue
            r = subprocess.run(["git", "clone", "--quiet", url, str(path)], capture_output=True, text=True)
            if r.returncode:
                raise ClientError("Could not clone a repo from GitHub", f"{url}: {r.stderr.strip()[:200]}",
                                  ["Check that your GitHub SSH key is set up on this computer:  ssh -T git@github.com"])
            rows.append(("cloned", str(path)))
        return rows

    # ---- the queue repo ----
    def fetch_queue(self):
        """Update every remote branch of the queue repo. Never touches the working tree."""
        self.git(self.queue, "fetch", "--quiet", "--prune", "origin", REFSPEC, check=False)

    def show_file(self, ref, path):
        r = subprocess.run(["git", "-C", str(self.queue), "show", f"{ref}:{path}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def remote_branch_exists(self, branch):
        return self.git(self.queue, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False).returncode == 0

    def _plumb(self, env, *a, **kw):
        r = subprocess.run(["git", "-C", str(self.queue), *a], env=env, capture_output=True, text=True, **kw)
        if r.returncode:
            raise ClientError(f"git {' '.join(a)} failed", r.stderr.strip())
        return r.stdout.strip()

    def push_files(self, branch, files, msg, base=None, retries=3, remove=()):
        """Commit {path: text} onto `branch` of the queue repo without touching the working tree, and push it. The new commit's parent is
        origin/<base or branch> (none for a brand-new orphan branch). A plain push, never forced: a rejected one is re-fetched and retried."""
        for _ in range(retries):
            self.fetch_queue()
            parent_ref, has_parent = f"origin/{base or branch}", self.remote_branch_exists(base or branch)
            idx = self.queue / ".git" / "cw-client.index"
            idx.unlink(missing_ok=True)
            env = {**os.environ, **CLIENT_IDENTITY, "GIT_INDEX_FILE": str(idx)}
            try:
                if has_parent:
                    self._plumb(env, "read-tree", parent_ref)
                for path, text in files.items():
                    blob = self._plumb(env, "hash-object", "-w", "--stdin", input=text)
                    self._plumb(env, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
                for path in remove:                                 # files taken out of the new commit (everything else on the branch stays)
                    self._plumb(env, "update-index", "--force-remove", path)
                args = ["commit-tree", self._plumb(env, "write-tree"), "-m", msg + "\n"] + (["-p", parent_ref] if has_parent else [])
                commit = self._plumb(env, *args)
            finally:
                idx.unlink(missing_ok=True)
            if self.git(self.queue, "push", "-q", "origin", f"{commit}:refs/heads/{branch}", check=False).returncode == 0:
                self.fetch_queue()
                return commit
            self._sleep(2)
        raise ClientError(f"Could not push to branch {branch} on GitHub", "Check your connection and your GitHub access, then try again.")

    # ---- the results repo ----
    def pull_results(self):
        """Mirror the results repo (it may be empty or have no branch yet; that is not an error). Returns the security status dict
        if the worker has locked itself, else None."""
        if self.git(self.results, "fetch", "--quiet", "origin", self.results_branch, check=False).returncode == 0:
            self.git(self.results, "checkout", "-q", "-f", "-B", self.results_branch, "FETCH_HEAD", check=False)
        sec = self.security()
        return sec if sec.get("locked") else None

    def security(self):
        try:
            return json.loads((self.results / "security.json").read_text())
        except (OSError, ValueError):
            return {}

    def read_result(self, jid):
        f = self.results / "results" / f"{jid}.json"
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None

    def result_ids(self):
        d = self.results / "results"
        return {f.stem for f in d.glob("*.json")} if d.is_dir() else set()

    def registry_info(self):
        """What the worker published about its installed registry (classes, limits, capacity), or None."""
        try:
            return json.loads((self.results / "registry.json").read_text())
        except (OSError, ValueError):
            return None

    def finished_ids(self):
        """Ids of jobs with a final result. A job without one (waiting for approval, queued, running, not yet seen) must never be pruned."""
        done = set()
        for jid in self.result_ids():
            res = self.read_result(jid)
            if res and res.get("status") not in PENDING:
                done.add(jid)
        return done

    def job_known(self, jid):
        """True if the job has a result, or its file is on the jobs branch. Anything else is a typo or a job long since pruned."""
        if self.read_result(jid):
            return True
        self.fetch_queue()
        return self.show_file(f"origin/{self.jobs_branch}", f"jobs/{jid}.json") is not None

    # ---- tidying the jobs branch ----
    def prune_jobs(self, days=14, force=False, now=time.time):
        """Squash the jobs branch to one commit holding only jobs newer than `days` (at least 1) and every job that has no final result yet.
        Force-push with a lease on the commit seen, so a job pushed meanwhile is never lost. Returns a message."""
        days = max(1, int(days))
        self.fetch_queue()
        self.pull_results()
        b = self.jobs_branch
        if not self.remote_branch_exists(b):
            return "no jobs branch yet"
        n = int(self.git(self.queue, "rev-list", "--count", f"origin/{b}").stdout.strip() or 0)
        if not force and n <= self.keep_commits:
            return f"{n} commits on the jobs branch; no prune needed (threshold {self.keep_commits})"
        cutoff, finished, keep, dropped = now() - days * 86400, self.finished_ids(), {}, 0
        for path in filter(None, self.git(self.queue, "ls-tree", "-r", "--name-only", f"origin/{b}").stdout.split("\n")):
            txt = self.show_file(f"origin/{b}", path) or ""
            try:
                created = datetime.fromisoformat(json.loads(txt)["created"].replace("Z", "+00:00")).timestamp()
            except Exception:
                created = now()
            if created >= cutoff or Path(path).stem not in finished:
                keep[path] = txt
            else:
                dropped += 1
        head = self.git(self.queue, "rev-parse", f"origin/{b}").stdout.strip()
        idx = self.queue / ".git" / "cw-prune.index"
        idx.unlink(missing_ok=True)
        env = {**os.environ, **CLIENT_IDENTITY, "GIT_INDEX_FILE": str(idx)}
        try:
            self._plumb(env, "read-tree", "--empty")
            for path, text in keep.items():
                self._plumb(env, "update-index", "--add", "--cacheinfo", f"100644,{self._plumb(env, 'hash-object', '-w', '--stdin', input=text)},{path}")
            commit = self._plumb(env, "commit-tree", self._plumb(env, "write-tree"), "-m", f"jobs: squashed history, keeping the last {days} days")
        finally:
            idx.unlink(missing_ok=True)
        if self.git(self.queue, "push", f"--force-with-lease=refs/heads/{b}:{head}", "origin", f"{commit}:refs/heads/{b}", check=False).returncode:
            return "prune push refused (someone pushed a job meanwhile); nothing changed. Try again."
        self.fetch_queue()
        return f"jobs branch squashed: {n} commits -> 1, dropped {dropped} jobs older than {days} days"
