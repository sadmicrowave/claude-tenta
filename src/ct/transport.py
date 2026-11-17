"""How a job reaches a worker and its result comes back (design: docs/design/TRANSPORTS.md).

Jobs and results are git repositories. A transport says where those repositories live and who connects to whom, and nothing else: the signed approvals, the registry, the
output guard and the lockdown sit above it and do not change. Everything that used to build a repository address by hand asks the transport of the configuration.

A transport answers, for each of the two repositories (`queue`, `results`):
  client_url(cfg, which)       the address your computer clones, pulls and pushes
  worker_url(cfg, which)       the address the worker's own clone uses
  worker_ssh_config(cfg)       the text of the worker account's ~/.ssh/config for it ('' when it needs none)
and says `uses_deploy_keys` (whether setup must put the worker's public keys on a server, as it does on GitHub).

`github` and `direct` exist; `lan` comes later in 0.2."""
import shlex
from pathlib import Path


class GitHub:
    """A private repository on GitHub each way. Nothing listens anywhere: the worker and your computer only connect out to github.com."""
    kind = "github"
    label = "GitHub"
    uses_deploy_keys = True
    gain = "Most isolated: nothing on your network listens, and the worker only connects out to github.com. Needs no always-on machine of yours."
    cost = "Slower: a job waits for the worker's next look (every 5 s while busy, up to 90 s idle and 5 min asleep), and jobs and results pass through GitHub, a third party."

    def client_ssh_command(self, cfg):
        return None                                           # your own GitHub key, as for any git command

    def where(self, cfg):
        return "GitHub"

    def unreachable_steps(self, cfg):
        return ["Check that your GitHub SSH key is set up on this computer:  ssh -T git@github.com"]

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


class Direct:
    """Each worker keeps its own two repositories (the mailbox) and your computer connects to the worker over SSH, as a restricted account that can only run git on them: it may
    push jobs to the queue and read results, nothing else. The worker makes no connection to anything for the mailbox, so its block on the LAN stays on."""
    kind = "direct"
    label = "Direct"
    uses_deploy_keys = False
    gain = "Fastest: a job is picked up within seconds and nothing leaves your network. The worker reaches nothing on your network."
    cost = "The worker's SSH port is open to your computer's address, and the worker must be reachable when you send a job."

    def client_key(self, cfg):
        """The key your computer uses for the mailbox: made by ct-setup, with no other use, and the only key the mailbox account accepts."""
        return Path(cfg.paths.client_home).expanduser() / "mailbox" / "key"

    def client_ssh_command(self, cfg):
        key = shlex.quote(str(self.client_key(cfg)))
        return f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

    def where(self, cfg):
        return "the worker"

    def unreachable_steps(self, cfg):
        s = cfg.server
        return [f"Is the worker on, and on this network? Try: ssh {s.admin_user}@{s.host} true", "If that works, the mailbox key may be missing: run ct-setup install again."]

    def mail_user(self, cfg):
        """The account your computer logs in as; it has no other use and runs only the mailbox shell."""
        return f"{cfg.identity.name}-mail"

    def repo_path(self, cfg, which):
        return f"{cfg.path('mailbox')}/{which}.git"

    def client_url(self, cfg, which):
        cfg.require("server.host")
        port = f":{cfg.server.ssh_port}" if int(cfg.server.ssh_port) != 22 else ""
        return f"ssh://{self.mail_user(cfg)}@{cfg.server.host}{port}{self.repo_path(cfg, which)}"

    def worker_url(self, cfg, which):
        return self.repo_path(cfg, which)                     # a path on the worker itself: reading its own queue and writing its own results touches no network

    def worker_ssh_config(self, cfg):
        return ""


TRANSPORTS = {"github": GitHub(), "direct": Direct()}


def for_config(cfg):
    """The transport the configuration chose (`[transport] kind`)."""
    return TRANSPORTS[cfg.transport.kind]
