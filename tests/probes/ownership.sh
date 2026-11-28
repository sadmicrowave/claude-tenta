#!/bin/bash
# Probe: git and the worker's repositories that belong to ANOTHER account. Runs inside a throwaway container as root (scripts/probe ownership); the checkout is mounted read-only at /src.
# Two real accounts exist here, which a unit test cannot have. It proves, with the project's own code:
#   1. the PREMISE: on this OS and git, an account asked to serve a repository owned by another account is refused ("dubious ownership"), so the problem is real;
#   2. the mailbox shell (armada.mailshell) serves such a repository anyway, for that one repository and no other;
#   3. root copying history between repositories owned by other accounts works with the project's helper (transport.copy_history_commands) and does not without it.
# Found by full end-to-end runs; this keeps them found in seconds.
set -uo pipefail
PASS=0; FAIL=0
check() { local what=$1; shift; if "$@" >/tmp/probe-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/probe-check.out | tail -6; fi; }
refused() { ! "$@" >/tmp/probe-refused.out 2>&1 && grep -q "dubious ownership" /tmp/probe-refused.out; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git python3 util-linux >/dev/null 2>&1
echo "git $(git --version | cut -d' ' -f3) on $(. /etc/os-release; echo "$PRETTY_NAME")"
useradd -m worker; useradd -r -M -d /var/lib/armada-mail -s /bin/sh armada-mail
M=/var/lib/armada-mail; mkdir -p $M && chown armada-mail:armada-mail $M && chmod 750 $M
usermod -aG armada-mail worker
RES=$M/results.git
git init -q --bare -b main $RES                                                          # made by root and handed to the worker, exactly as the installer does
chown -R worker:armada-mail $RES && chmod -R u+rwX,g+rX,o-rwx $RES
EMPTY=$(runuser -u worker -- git -C $RES hash-object -w -t tree /dev/null)
COMMIT=$(runuser -u worker -- git -c user.name=w -c user.email=w@w -C $RES commit-tree -m results $EMPTY)
runuser -u worker -- git -C $RES update-ref refs/heads/main $COMMIT                    # the worker's own data in it
cat > /tmp/armada.toml <<'TOML'
[server]
host = "h.example"
admin_user = "me"
[github]
owner = "o"
[transport]
kind = "direct"
TOML

echo "1. the premise"
check "an account that does not own a repository is refused by git (dubious ownership)" refused runuser -u armada-mail -- git upload-pack --advertise-refs $RES

echo "2. the mailbox shell"
serve() { runuser -u armada-mail -- env -i PATH=/usr/bin:/bin SSH_ORIGINAL_COMMAND="$1" PYTHONPATH=/src/src ARM_CONFIG=/tmp/armada.toml python3 -m armada.mailshell; }
check "it serves the results repository to the mailbox account" bash -c "$(declare -f serve); serve \"git-upload-pack '$RES'\" | head -c 4 | grep -qE '^[0-9a-f]{4}$'"
check "and refuses anything that is not git on those repositories" bash -c "$(declare -f serve); ! serve 'id' 2>&1 | grep -q uid="
check "the trust is for that one repository: another repository of the worker is still refused" bash -c "
  runuser -u worker -- git init -q --bare $M/other.git && chown -R worker:armada-mail $M/other.git
  $(declare -f serve); ! serve \"git-upload-pack '$M/other.git'\" 2>&1 | head -c 4 | grep -qE '^[0-9a-f]{4}$'"

echo "3. root copying history between repositories that belong to other accounts"
runuser -u worker -- bash -c "mkdir -p ~/w && cd ~/w && git init -q -b main . && git -c user.name=w -c user.email=w@w commit -q --allow-empty -m one && git -c user.name=w -c user.email=w@w checkout -q -b jobs && git -c user.name=w -c user.email=w@w commit -q --allow-empty -m job && git checkout -q main"
runuser -u worker -- git clone -q /home/worker/w /home/worker/clone
BARE=$M/queue.git; git init -q --bare -b main $BARE && chown -R armada-mail:armada-mail $BARE
helper() { PYTHONPATH=/src/src python3 - "$@" <<'PY'
import subprocess, sys
from armada import transport
clone, bare, use_helper = sys.argv[1], sys.argv[2], sys.argv[3] == "yes"
for argv in transport.copy_history_commands(clone, bare):
    if not use_helper:                       # the same push without the trust wrapper
        argv = [a for a in argv[4:]] if argv[0] == "sh" else argv
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        print(r.stderr.strip()[:200]); sys.exit(1)
PY
}
refused_copy() { local out; out=$(helper /home/worker/clone $BARE no 2>&1); [ $? -ne 0 ] && echo "$out" | grep -q 'dubious ownership'; }
check "without the helper's trust the copy is refused (dubious ownership)" bash -c "$(declare -f helper refused_copy); BARE=$BARE; refused_copy"
check "with the project's helper the copy works, as root" bash -c "$(declare -f helper); helper /home/worker/clone $BARE yes"
check "and the jobs branch arrived as a branch, not as a stray remote ref, and no branch called HEAD was made" bash -c "git -c safe.directory='*' -C $BARE for-each-ref --format='%(refname)' | sort | tr '\n' ' ' | grep -qx 'refs/heads/jobs refs/heads/main '"

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
