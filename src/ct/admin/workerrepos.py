"""A GitHub worker's own two private repositories, from the administering computer: make them (with the fleet's topic), clone them, and when the worker is taken off the list, offer to archive or delete them.

Nothing here can share a repository between workers: `workers.repo_conflict` is checked first by every caller, and a repository another worker lists is never archived or deleted by this one."""
import subprocess
from pathlib import Path

from .. import config
from . import workers as workers_mod

TOPIC = "claude-tenta"                                   # every repository the tool creates carries it, so one GitHub search groups the whole fleet


def gh_run(argv):
    try:
        r = subprocess.run(["gh", *argv], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def slugs_of(cfg):
    """[(role, 'owner/repo')] with the case the configuration wrote."""
    g = cfg.github
    return [] if not g.owner else [("queue", f"{g.owner}/{g.queue_repo}"), ("results", f"{g.owner}/{g.results_repo}")]


def prepare(cfg, own_path, wfile, run=gh_run, clone=None):
    """Create what is missing (private, with the topic), then clone. Returns [(what, slug-or-path, status)]; raises the catalog refusal when a repository is another worker's."""
    workers_mod.require_own_repos(cfg, own_path, wfile)
    rows = []
    for role, slug in slugs_of(cfg):
        if run(["repo", "view", slug])[0] == 0:
            rows.append((role, slug, "exists"))
        else:
            code, said = run(["repo", "create", slug, "--private", "--description", f"claude-tenta {role} (private)"])
            if code:
                raise RuntimeError(f"gh could not create {slug}: {said[:300]}")
            rows.append((role, slug, "created, private"))
        run(["repo", "edit", slug, "--add-topic", TOPIC])                       # best effort: the topic only groups, nothing depends on it
    if clone is None:
        from ..client.repo import ClientRepos
        clone = lambda: ClientRepos.from_config(cfg).clone_all(cfg.paths.client_home)
    rows += [("clone", path, what) for what, path in clone()]
    return rows


def removable(name, path, wfile):
    """The worker's repositories that no other worker lists, as [(role, slug)]; [] when its configuration cannot be read or has no account."""
    try:
        cfg = config.load(path, env={"CT_CONFIG": ""})
    except Exception:
        return []
    used = {s for _, o in workers_mod.others(path, wfile) for s in workers_mod.repo_slugs(o).values()}
    return [(r, s) for r, s in slugs_of(cfg) if s.lower() not in used]


def retire(slugs, how, run=gh_run):
    """Archive or delete each repository: [(slug, ok, said)]. The deploy keys go with the repository either way. `how` is 'archive' or 'delete'."""
    out = []
    for slug in slugs:
        code, said = run(["repo", how, slug, "--yes"])
        out.append((slug, code == 0, said))
    return out
