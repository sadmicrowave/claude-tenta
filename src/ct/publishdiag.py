"""Why a result could not be published, in plain words, and the one command that usually fixes it.

The worker keeps a finished job's result until its push succeeds. When pushes keep failing, the person waiting sees "not picked up" while the job in fact ran,
so the worker records the git error here, and `status` names the cause and the fix instead of leaving it to the journal."""
import json
import time
from pathlib import Path

FILE = "publish-failing.json"

# (text to find in git's error, kind, what it means, the fix: a command, or None when waiting is the fix)
KNOWN = (
    ("repository not found", "repo-missing", "GitHub says the results repository does not exist under that name. After a rename the worker's remote can still point at the old name.", "ct-setup rename"),
    ("permission denied (publickey)", "key-rejected", "GitHub did not accept the worker's key for the results repository. A renamed or recreated repository needs its deploy key registered again.", "ct-setup install"),
    ("could not read from remote repository", "key-rejected", "The worker could not reach the results repository with its key (it is missing, renamed, or not registered on that repository).", "ct-setup install"),
    ("read-only", "key-readonly", "The worker's deploy key on the results repository is read-only; it must be allowed to write.", "ct-setup install"),
    ("write access to repository not granted", "key-readonly", "The worker's deploy key on the results repository is read-only; it must be allowed to write.", "ct-setup install"),
    ("could not resolve hostname", "offline", "The worker cannot reach GitHub (no network or DNS). It publishes by itself when the connection returns.", None),
    ("connection timed out", "offline", "The worker cannot reach GitHub (timed out). It publishes by itself when the connection returns.", None),
    ("non-fast-forward", "diverged", "The results repository changed under the worker and it could not be merged automatically.", "ct-setup audit"),
    ("rejected", "rejected", "GitHub refused the push.", "ct-setup audit"),
)


def classify(error):
    """(kind, meaning, fix command or None) for git's error text; unknown errors still say what git said."""
    low = (error or "").lower()
    for needle, kind, meaning, fix in KNOWN:
        if needle in low:
            return kind, meaning, fix
    return "unknown", "git could not push the result and gave no known reason.", "ct-setup audit"


def record(state, ok, error=""):
    """Remember the failure (and when it began), or forget it once a push works."""
    f = Path(state) / FILE
    if ok:
        f.unlink(missing_ok=True)
        return
    try:
        since = json.loads(f.read_text()).get("since")
    except (OSError, ValueError):
        since = None
    try:
        f.write_text(json.dumps({"since": since or int(time.time()), "last": int(time.time()), "error": (error or "").strip()[-400:]}))
    except OSError:
        pass


def load(state):
    try:
        return json.loads((Path(state) / FILE).read_text())
    except (OSError, ValueError):
        return None
