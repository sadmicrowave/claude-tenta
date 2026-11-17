"""How a job reaches a worker and its result comes back (design: docs/design/TRANSPORTS.md).

Jobs and results are git repositories. A transport says where those repositories live and who connects to whom, and nothing else: the signed approvals, the registry, the
output guard and the lockdown sit above it and do not change. Everything that used to build a repository address by hand asks the transport of the configuration.

A transport answers, for each of the two repositories (`queue`, `results`):
  client_url(cfg, which)       the address your computer clones, pulls and pushes
  worker_url(cfg, which)       the address the worker's own clone uses
  worker_ssh_config(cfg)       the text of the worker account's ~/.ssh/config for it ('' when it needs none)
and says `uses_deploy_keys` (whether setup must put the worker's public keys on a server, as it does on GitHub).

Only `github` exists so far; `direct` and `lan` are added by the 0.2 work."""


class GitHub:
    """A private repository on GitHub each way. Nothing listens anywhere: the worker and your computer only connect out to github.com."""
    kind = "github"
    label = "GitHub"
    uses_deploy_keys = True

    def repo(self, cfg, which):
        return cfg.github.queue_repo if which == "queue" else cfg.github.results_repo

    def client_url(self, cfg, which):
        cfg.require("github.owner")
        return f"git@github.com:{cfg.github.owner}/{self.repo(cfg, which)}.git"

    def alias(self, cfg, which):
        """The ssh host alias a worker clone uses, so each repository has its own deploy key (GitHub allows a key on one repository only)."""
        return f"github-{cfg.identity.name}-{which}"

    def worker_url(self, cfg, which):
        return f"git@{self.alias(cfg, which)}:{cfg.github.owner}/{self.repo(cfg, which)}.git"

    def worker_ssh_config(self, cfg):
        keydir = f"{cfg.paths.worker_home}/.ssh"
        out = ["# Written by ct-setup: one deploy key per repository (GitHub allows a key on one repository only).\n"]
        for which in ("queue", "results"):
            out.append(f"Host {self.alias(cfg, which)}\n  HostName github.com\n  User git\n  IdentityFile {keydir}/id_{which}\n  IdentitiesOnly yes\n"
                       "  StrictHostKeyChecking accept-new\n")      # no ControlMaster here: its background process keeps the pass's output pipes open, so every pass hung until it exited
        return "\n".join(out)


TRANSPORTS = {"github": GitHub()}


def for_config(cfg):
    """The transport the configuration chose (`[transport] kind`)."""
    return TRANSPORTS[cfg.transport.kind]
