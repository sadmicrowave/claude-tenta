"""The client's two clones: the queue repo (jobs are pushed to its jobs branch) and the results repo (read-only to the client).

Jobs are pushed with git plumbing (a commit is built from a temporary index, never touching a working tree) so a half-edited folder can
never end up in a job, and a push is never forced: a rejected one is re-fetched and retried. The jobs branch is disposable data and may be
squashed; the results repo is mirrored (fetch and reset), never rebased onto, because the worker squashes it now and then.
"""
import json
import os
import random
import threading
import uuid
import subprocess
from urllib.parse import urlparse
import time
from datetime import datetime
from pathlib import Path
from ..words import plural

REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
CLIENT_IDENTITY = {"GIT_AUTHOR_NAME": "arm-client", "GIT_AUTHOR_EMAIL": "arm-client@localhost", "GIT_COMMITTER_NAME": "arm-client", "GIT_COMMITTER_EMAIL": "arm-client@localhost"}
PENDING = ("awaiting_approval", "running", "queued")        # a job in one of these states has no final result yet


class ClientError(Exception):
    """Something the person can act on; the message says what and how."""

    def __init__(self, title, why=None, steps=None, example=None, facts=None):
        """`title` is a fixed sentence; what a person named (a worker, a job, a class, a file, a host) goes in `facts`, a list of (label, value) rows, never in the title."""
        super().__init__(title)
        self.title, self.why, self.steps, self.example, self.facts = title, why, steps or [], example, list(facts or [])


class ClientRepos:
    def __init__(self, queue, results, queue_url, results_url, *, results_branch="main", jobs_branch="jobs", keep_commits=150, sleep=time.sleep, ssh_command=None,
                 where="GitHub", unreachable_steps=None, admin_target=None, kind="github"):
        self.queue, self.results = Path(queue), Path(results)
        self.ssh_command, self.where = ssh_command, where                 # the transport's way of reaching the repositories (None: the person's own ssh) and what to call the other end
        self.unreachable_steps = unreachable_steps or ["Check that your GitHub SSH key is set up on this computer:  ssh -T git@github.com"]
        self.queue_url, self.results_url = queue_url, results_url
        self.admin_target, self.kind = admin_target or "you@the-worker", kind          # who you log in to the worker as, and which connection this is: for explaining a failure
        self.results_branch, self.jobs_branch, self.keep_commits, self._sleep = results_branch, jobs_branch, keep_commits, sleep
        self._seen = {}                                   # when this process last looked at each repository (see max_age)

    @classmethod
    def from_config(cls, cfg, **kw):
        """The two repositories of a configuration, reached the way its transport says."""
        from .. import transport
        t = transport.for_config(cfg)
        return cls(cfg.path("client_queue"), cfg.path("client_results"), cfg.queue_url, cfg.results_url, jobs_branch=cfg.github.jobs_branch,
                   ssh_command=t.client_ssh_command(cfg), where=t.where(cfg), unreachable_steps=t.unreachable_steps(cfg),
                   admin_target=f"{cfg.server.admin_user}@{cfg.server.host}", kind=t.kind, **kw)

    def _net_env(self, env=None):
        """The environment for a git command that talks to the other end: with the transport's own ssh command when it has one."""
        if not self.ssh_command:
            return env
        return {**(os.environ if env is None else env), "GIT_SSH_COMMAND": self.ssh_command}

    # ---- plain git ----
    def git(self, repo, *args, check=True, env=None, input=None):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=self._net_env(env), input=input)
        if check and r.returncode:
            from . import refuse                                            # (refuse imports this module, so it is imported where it is used)
            raise refuse.error("repo.git_failed", ("Command", "git " + " ".join(args)), ("Folder", str(repo)), reason=r.stderr.strip())
        return r

    def is_set_up(self):
        return (self.queue / ".git").exists() and (self.results / ".git").exists()

    def need_setup(self):
        if not self.is_set_up():
            from . import refuse
            raise refuse.error("repo.not_set_up", example="arm-run setup")

    def clone_all(self, home):
        """Clone whichever of the two repos is missing. Returns [(what, path)] for each that was cloned or already there."""
        Path(home).mkdir(parents=True, exist_ok=True)
        rows = []
        for path, url in ((self.queue, self.queue_url), (self.results, self.results_url)):
            if (path / ".git").exists() or (path / "HEAD").exists():
                have = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
                if have and have != url:                                    # an install set up again for another connection must send and read the new way, not the old
                    subprocess.run(["git", "-C", str(path), "remote", "set-url", "origin", url], capture_output=True)
                    rows.append(("repointed", str(path)))
                else:
                    rows.append(("exists", str(path)))
                continue
            r = subprocess.run(["git", "clone", "--quiet", url, str(path)], capture_output=True, text=True, env=self._net_env())
            if r.returncode:
                from .. import clonediag
                cause, meaning, steps = clonediag.classify(r.stderr, kind=self.kind, admin=self.admin_target, path=urlparse(url).path, host=urlparse(url).hostname or "")
                from . import refuse
                raise refuse.error("repo.clone_failed", ("From", self.where), steps=steps or self.unreachable_steps, url=url, detail=" ".join(r.stderr.split())[:400] + (f" ({meaning})" if meaning else ""))
            rows.append(("cloned", str(path)))
        return rows

    # ---- the queue repo ----
    def _recent(self, kind, max_age):
        return bool(max_age) and kind in self._seen and time.monotonic() - self._seen[kind] < max_age

    def fetch_queue(self, max_age=0):
        """Update every remote branch of the queue repo. Never touches the working tree. `max_age` (seconds): a look made that recently in this process is reused (for callers that only need a recent picture;
        the default is always a real one)."""
        if self._recent("queue", max_age):
            return
        self.git(self.queue, "fetch", "--quiet", "--prune", "origin", REFSPEC, check=False)
        self._seen["queue"] = time.monotonic()

    def show_file(self, ref, path):
        r = subprocess.run(["git", "-C", str(self.queue), "show", f"{ref}:{path}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def show_files(self, ref, paths):
        """{path: text or None} for many files of `ref` in ONE git process (`cat-file --batch`), not one `git show` each: reading every job file on the queue branch was most of the time of a send, and
        grew with every job sent. A path that is not there is None."""
        paths = list(paths)
        if not paths:
            return {}
        r = subprocess.run(["git", "-C", str(self.queue), "cat-file", "--batch"], input="".join(f"{ref}:{p}\n" for p in paths).encode(), capture_output=True)
        buf, i, out = r.stdout, 0, {}
        for p in paths:
            j = buf.find(b"\n", i)
            if j < 0:
                out[p] = None
                continue
            head = buf[i:j].split()
            if len(head) < 3 or head[1] != b"blob":                       # "<name> missing" (or anything that is not a file)
                out[p] = None
                i = j + 1
                continue
            size = int(head[2])
            out[p] = buf[j + 1:j + 1 + size].decode("utf-8", "replace")
            i = j + 1 + size + 1
        return out

    def remote_branch_exists(self, branch):
        return self.git(self.queue, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False).returncode == 0

    def _plumb(self, env, *a, **kw):
        r = subprocess.run(["git", "-C", str(self.queue), *a], env=env, capture_output=True, text=True, **kw)
        if r.returncode:
            from . import refuse
            raise refuse.error("repo.git_failed", ("Command", "git " + " ".join(a)), reason=r.stderr.strip())
        return r.stdout.strip()

    def push_files(self, branch, files, msg, base=None, retries=8, remove=()):
        """Commit {path: text} onto `branch` of the queue repo without touching the working tree, and push it. The new commit's parent is
        origin/<base or branch> (none for a brand-new orphan branch). A plain push, never forced: a rejected one is re-fetched and retried."""
        for _ in range(retries):
            self.fetch_queue()
            has_parent = self.remote_branch_exists(base or branch)
            parent = self.git(self.queue, "rev-parse", f"origin/{base or branch}").stdout.strip() if has_parent else None        # ONE commit id, used for the tree and for the parent: a name read twice can move in between
            idx = self.queue / ".git" / f"arm-client.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.index"        # its own file: other sends at the same moment (a script, a second session, a load test) never share it
            idx.unlink(missing_ok=True)
            env = {**os.environ, **CLIENT_IDENTITY, "GIT_INDEX_FILE": str(idx)}
            try:
                if has_parent:
                    self._plumb(env, "read-tree", parent)
                for path, text in files.items():
                    blob = self._plumb(env, "hash-object", "-w", "--stdin", input=text)
                    self._plumb(env, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
                for path in remove:                                 # files taken out of the new commit (everything else on the branch stays)
                    self._plumb(env, "update-index", "--force-remove", path)
                args = ["commit-tree", self._plumb(env, "write-tree"), "-m", msg + "\n"] + (["-p", parent] if has_parent else [])
                commit = self._plumb(env, *args)
            finally:
                idx.unlink(missing_ok=True)
            if self.git(self.queue, "push", "-q", "origin", f"{commit}:refs/heads/{branch}", check=False).returncode == 0:
                self.fetch_queue()
                missing = [p for p in files if self.git(self.queue, "cat-file", "-e", f"origin/{branch}:{p}", check=False).returncode]
                if missing:                                          # a push that succeeded is not proof it is there: say so loudly rather than report a job sent that the worker will never see
                    from . import refuse
                    raise refuse.error("repo.file_missing", ("Files", ", ".join(missing)), ("Branch", branch), ("On", self.where))
                return commit
            self._sleep(random.uniform(0.2, 1.5) if self._sleep is time.sleep else 2)          # a rejected push means someone else pushed first: a short random pause so a burst does not retry in step
        from . import refuse
        raise refuse.error("repo.push_failed", ("Branch", branch), ("On", self.where), steps=self.unreachable_steps)

    # ---- the results repo ----
    def pull_results(self, max_age=0):
        """Mirror the results repo (it may be empty or have no branch yet; that is not an error). Returns the security status dict
        if the worker has locked itself, else None. `max_age`: reuse a pull made that recently (never for the lock check before a send)."""
        if not self._recent("results", max_age):
            if self.git(self.results, "fetch", "--quiet", "origin", self.results_branch, check=False).returncode == 0:
                self.git(self.results, "checkout", "-q", "-f", "-B", self.results_branch, "FETCH_HEAD", check=False)
            self._seen["results"] = time.monotonic()
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

    def worker_state(self):
        """The state document the worker published (hardware, isolation, what it will do with a profile run ...), or None."""
        try:
            return json.loads((self.results / "worker-state.json").read_text())
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
        self.fetch_queue(max_age=30)                         # the send that called this has just looked at both
        self.pull_results(max_age=30)
        b = self.jobs_branch
        if not self.remote_branch_exists(b):
            return "no jobs branch yet"
        n = int(self.git(self.queue, "rev-list", "--count", f"origin/{b}").stdout.strip() or 0)
        if not force and n <= self.keep_commits:
            return f"{plural(n, 'commit')} on the jobs branch; no prune needed (threshold {self.keep_commits})"
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
        idx = self.queue / ".git" / f"arm-prune.{os.getpid()}.{uuid.uuid4().hex[:8]}.index"
        idx.unlink(missing_ok=True)
        env = {**os.environ, **CLIENT_IDENTITY, "GIT_INDEX_FILE": str(idx)}
        try:
            self._plumb(env, "read-tree", "--empty")
            for path, text in keep.items():
                self._plumb(env, "update-index", "--add", "--cacheinfo", f"100644,{self._plumb(env, 'hash-object', '-w', '--stdin', input=text)},{path}")
            commit = self._plumb(env, "commit-tree", self._plumb(env, "write-tree"), "-m", f"jobs: squashed history, keeping the last {plural(days, 'day')}")
        finally:
            idx.unlink(missing_ok=True)
        if self.git(self.queue, "push", f"--force-with-lease=refs/heads/{b}:{head}", "origin", f"{commit}:refs/heads/{b}", check=False).returncode:
            return "prune push refused (someone pushed a job meanwhile); nothing changed. Try again."
        self.fetch_queue()
        return f"jobs branch squashed: {plural(n, 'commit')} -> 1, dropped {plural(dropped, 'job')} older than {plural(days, 'day')}"
