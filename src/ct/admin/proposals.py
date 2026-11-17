"""Proposals: registry changes (and anything else) waiting on branches of the queue repo, reviewed and squash-merged into main.

Nothing is merged without showing it first. A merge is ONE commit on main authored as the branch's own author, with a Merged-Branch trailer
so a branch is recognised as done for good. A registry change is validated before it is merged, never after.
"""
import re
import subprocess

from ..client.repo import ClientError

RESERVED = ("main", "jobs", "data", "HEAD")
BRANCH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")


class Proposals:
    def __init__(self, repos, main="main"):
        self.repos, self.main = repos, main

    def git(self, *args, check=True, env=None):
        return self.repos.git(self.repos.queue, *args, check=check, env=env)

    @property
    def base(self):
        return f"origin/{self.main}"

    def fetch(self):
        self.repos.fetch_queue()

    def branches(self):
        out = self.git("for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin").stdout.split()
        return sorted(b for b in out if b not in RESERVED and b != self.main)

    def contained(self, branch):
        """True if the branch is already in main: named in a squash commit's Merged-Branch trailer, or merging it would change nothing."""
        ref = f"origin/{branch}"
        tip = self.git("rev-parse", ref).stdout.strip()
        if self.git("log", self.base, "-1", "--format=%H", "--fixed-strings", f"--grep=@{tip}").stdout.strip():
            return True
        r = self.git("merge-tree", "--write-tree", self.base, ref, check=False)
        return r.returncode == 0 and r.stdout.split()[0] == self.git("rev-parse", f"{self.base}^{{tree}}").stdout.strip()

    def pending(self):
        names = [b for b in self.branches() if not self.contained(b)]
        return sorted(names, key=lambda b: int(self.git("log", "-1", "--format=%ct", f"origin/{b}").stdout.strip() or 0))

    def listing(self):
        """[(branch, ahead, subject, merged)]"""
        rows = []
        for b in self.branches():
            ref = f"origin/{b}"
            merged = self.contained(b)
            rows.append((b, self.git("rev-list", "--count", f"{self.base}..{ref}").stdout.strip(), self.git("log", "-1", "--format=%s (%cr)", ref).stdout.strip(), merged))
        return rows

    def check_name(self, branch):
        if branch in RESERVED or not BRANCH_OK.match(branch):
            raise ClientError(f"{branch!r} is not a branch you can merge", "main, jobs and data are managed by the system.", example="ct-admin proposals")
        if not self.repos.remote_branch_exists(branch):
            raise ClientError(f"No branch called {branch} on GitHub", example="ct-admin proposals")

    def files(self, branch):
        return self.git("diff", "--name-only", f"{self.base}...origin/{branch}").stdout.split()

    def risks(self, branch):
        return ["REGISTRY"] if "registry.yaml" in self.files(branch) or branch.startswith("registry/") else []

    def log(self, branch):
        return self.git("log", "--stat", "--format=%n%h %s%n  %an, %cr", f"{self.base}..origin/{branch}").stdout

    def commits(self, branch):
        """[(short sha, subject, author, how long ago)] of what the branch adds to main, oldest first."""
        out = self.git("log", "--reverse", "--format=%h%x09%s%x09%an%x09%cr", f"{self.base}..origin/{branch}").stdout
        return [tuple(line.split("\t", 3)) for line in out.splitlines() if line.count("\t") == 3]

    def files(self, branch):
        """[(path, lines added, lines removed)] for the whole branch."""
        out = self.git("diff", "--numstat", f"{self.base}...origin/{branch}").stdout
        return [(p, a, d) for a, d, p in (line.split("\t", 2) for line in out.splitlines() if line.count("\t") == 2)]

    def squash(self, branch, subject=None):
        """Build (not push) the squash commit of origin/<branch> on origin/main. Returns (sha, message, tree)."""
        ref = f"origin/{branch}"
        t = self.git("merge-tree", "--write-tree", self.base, ref, check=False)
        if t.returncode:
            raise ClientError(f"{branch} does not merge cleanly into main", t.stdout.strip() or None, ["Rebase the branch, or resolve the conflict yourself."])
        tree = t.stdout.split()[0]
        subjects = self.git("log", "--reverse", "--format=%s", f"{self.base}..{ref}").stdout.strip().splitlines()
        if not subjects:
            raise ClientError(f"{branch} has nothing that is not already in main")
        body = self.git("log", "-1", "--format=%b", ref).stdout.strip() if len(subjects) == 1 else "\n".join(f"- {x}" for x in subjects)
        body = "\n".join(line for line in body.splitlines() if not line.lower().startswith("co-authored-by:")).strip()
        trailers = sorted({line for line in self.git("log", f"{self.base}..{ref}", "--format=%(trailers:key=Co-Authored-By,valueonly,separator=%x0a)").stdout.splitlines() if line.strip()})
        tip = self.git("rev-parse", ref).stdout.strip()
        msg = (subject or subjects[0]) + ("\n\n" + body if body else "") + "\n\n" + "\n".join([f"Co-Authored-By: {x}" for x in trailers] + [f"Merged-Branch: {branch}@{tip}"])
        who = self.git("log", "-1", "--format=%an%x00%ae", ref).stdout.strip().split("\x00")
        env = None
        if len(who) == 2:       # authored as the branch's author, so a merge never depends on this clone's git identity
            import os
            env = {**os.environ, "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1]}
        sha = self.git("commit-tree", tree, "-p", self.base, "-m", msg + "\n", env=env).stdout.strip()
        return sha, msg, tree

    def tree_file(self, tree, path):
        r = subprocess.run(["git", "-C", str(self.repos.queue), "show", f"{tree}:{path}"], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def push_main(self, sha):
        r = self.git("push", "-q", "origin", f"{sha}:refs/heads/{self.main}", check=False)
        if r.returncode:
            raise ClientError(*explain_refused_push(r.stderr))
        return sha

    def delete(self, branch):
        """Delete a remote branch. Returns True only if it really went."""
        return self.git("push", "-q", "origin", "--delete", branch, check=False).returncode == 0


def explain_refused_push(stderr):
    """(title, why, steps) for a push GitHub refused, in plain words (it is not always because main moved)."""
    text = stderr or ""
    if "GH007" in text or "email privacy" in text:
        return ("GitHub refused: the commit would publish a private email address", "Your GitHub account blocks pushes that expose your email.",
                ["Use your GitHub noreply address in this clone: git config user.email <id>+<handle>@users.noreply.github.com"])
    if re.search(r"non-fast-forward|fetch first|stale info|\[rejected\]", text):
        return ("main moved while you were reviewing", "GitHub refused the push because main has new commits.", ["Run the same command again."])
    lines = [line.replace("remote: ", "").strip() for line in text.splitlines() if line.strip()]
    return ("GitHub refused the push", "\n".join(lines[-4:]) or "No reason was given.", ["Fix the cause above, then run the same command again."])
