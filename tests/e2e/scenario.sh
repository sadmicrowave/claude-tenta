#!/bin/bash
# End-to-end scenario for a FRESH Ubuntu 24.04 / Debian 12 machine. Run as root. It installs the project from the checkout in $SRC, runs the
# wizard for real (client half, then server half, twice, as a person would), sends a job through the whole loop, and checks that the safety
# features stop what they should.
# GitHub is simulated faithfully: github.com resolves to this machine, where an SSH user called git holds the two bare repositories under
# someone/. Every clone, fetch and push goes over SSH with real keys (the worker's own deploy keys included). Nothing in the project is faked.
# Use scripts/e2e-orbstack to run it in a disposable OrbStack machine. Exit status is the number of failed checks.
set -uo pipefail
SRC=${SRC:-/opt/cw-src}
H=/home; NAME=claude-worker; ADMIN=cwadmin; WORKER=cwworker; OWNER=someone
export DEBIAN_FRONTEND=noninteractive
PASS=0; FAIL=0
step() { printf '\n=== %s\n' "$*"; }
check() { local what=$1; shift; if "$@" >/tmp/e2e-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/e2e-check.out | tail -12; fi; }
as_admin() { runuser -u $ADMIN -- env "PATH=/opt/cwvenv/bin:/usr/local/bin:/usr/bin:/bin" HOME=$H/$ADMIN "$@"; }
export -f as_admin; export H ADMIN NAME WORKER OWNER SRC
wait_for() { local tries=$1; shift; for _ in $(seq "$tries"); do "$@" >/dev/null 2>&1 && return 0; sleep 3; done; return 1; }
SERVER_SET=(--set server.admin_user=$ADMIN --set github.owner=$OWNER --set network.ssh_from=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16)

step "Prepare the machine"
apt-get update -qq && apt-get install -y -qq git python3 python3-yaml python3-venv python3-pip sudo openssh-server openssh-client iproute2 util-linux >/dev/null
id $ADMIN >/dev/null 2>&1 || adduser --disabled-password --gecos "" $ADMIN >/dev/null
usermod -aG sudo $ADMIN
# the release comes from a git checkout (the update tool records which commit is installed)
[ -d $SRC/.git ] || { git -C $SRC init -q && git -C $SRC add -A && git -C $SRC -c user.name=e2e -c user.email=e2e@example.com commit -qm e2e; }
systemctl enable --now ssh >/dev/null 2>&1
runuser -u $ADMIN -- bash -c '[ -f ~/.ssh/id_ed25519 ] || { mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519; }; cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
# a stand-in for GitHub: github.com is this machine; user "git" owns the bare repositories
grep -q 'github.com' /etc/hosts || echo '127.0.0.1 github.com' >> /etc/hosts
id git >/dev/null 2>&1 || adduser --disabled-password --gecos "" git >/dev/null
runuser -u git -- bash -c "mkdir -p ~/.ssh ~/$OWNER && chmod 700 ~/.ssh && for r in cw-queue cw-results; do git init -q --bare -b main ~/$OWNER/\$r.git; done; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
cat $H/$ADMIN/.ssh/id_ed25519.pub >> $H/git/.ssh/authorized_keys
usermod -p '*' git; usermod -p '*' $ADMIN                      # no password, but not locked (sshd refuses locked accounts even for keys)
printf 'Host *\n  StrictHostKeyChecking accept-new\n' > /etc/ssh/ssh_config.d/99-e2e.conf   # this machine's test setup: trust the stand-in hosts on first use
git config --system safe.directory '*'      # root inspects repositories owned by the git user
git config --system user.name e2e; git config --system user.email e2e@example.com
check "the stand-in GitHub answers over SSH with the admin's key" as_admin ssh -o BatchMode=yes git@github.com true
check "the checkout is where the scenario expects it" test -f $SRC/pyproject.toml
python3 -m venv --system-site-packages /opt/cwvenv && /opt/cwvenv/bin/pip install -q -e $SRC
chmod -R a+rX /opt/cwvenv $SRC
check "pip installs the four commands" bash -c 'for c in cw-run cw-admin cw-setup cw-help; do test -x /opt/cwvenv/bin/$c || exit 1; done'

step "cw-setup client (your computer)"
as_admin cw-setup client --yes --set server.host=localhost --set server.admin_user=$ADMIN --set github.owner=$OWNER >/tmp/e2e-client.out 2>&1; echo "client wizard exit $?"
check "the client configuration was written" test -f $H/$ADMIN/.config/claude-worker/cw.toml
check "the registry was seeded on main of the queue repository" git -C $H/git/$OWNER/cw-queue.git cat-file -e main:registry.yaml

step "cw-setup server (the worker), first run: it must stop and ask for the deploy keys"
cd $SRC && PYTHONPATH=$SRC/src python3 -m cw.setup server --yes --apply "${SERVER_SET[@]}" >/tmp/e2e-server1.out 2>&1; echo "server wizard exit $?"
check "it waits for the deploy keys instead of going on" grep -q "add the deploy keys" /tmp/e2e-server1.out
check "it printed a read-only command for the queue key and a write command for the results key" bash -c 'grep -q "deploy-key add claude-worker-queue.pub" /tmp/e2e-server1.out && grep -q "allow-write" /tmp/e2e-server1.out'
cat $H/$WORKER/.ssh/id_queue.pub $H/$WORKER/.ssh/id_results.pub >> $H/git/.ssh/authorized_keys     # what the person does with gh on their own computer
step "cw-setup server, second run: it must carry on from there"
cd $SRC && PYTHONPATH=$SRC/src python3 -m cw.setup server --yes --apply "${SERVER_SET[@]}" >/tmp/e2e-server2.out 2>&1; echo "server wizard exit $?"; tail -25 /tmp/e2e-server2.out
check "the worker account exists with no shell" bash -c "getent passwd $WORKER | grep -q nologin"
check "the installed commands exist" bash -c "test -x /usr/local/bin/$NAME && test -x /usr/local/sbin/$NAME-update && test -f /opt/$NAME/lib/cw/cli.py"
check "the composed registry is installed and says it is generated" grep -q GENERATED /etc/$NAME/registry.yaml
check "the core classes are in it" grep -q "sys-status" /etc/$NAME/registry.yaml
check "the policy is in it" grep -q "nsenter" /etc/$NAME/registry.yaml
check "the timers are running" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer $NAME-canary.timer | sort -u | grep -qx active"
check "no service has failed" bash -c "! systemctl --failed --no-legend | grep -q $NAME"
check "password SSH is off" bash -c "sshd -T | grep -qx 'passwordauthentication no'"
check "the firewall is on" bash -c "ufw status | grep -q 'Status: active'"
check "the worker has no root-equivalent groups" bash -c "! id -nG $WORKER | grep -Eqw 'sudo|lxd|docker|disk'"
check "the state folder is not world readable" bash -c "! stat -c %a /var/lib/$NAME/state | grep -q '[0-7][0-7][1-7]'"
AUDIT_OK=1; case "$(systemctl is-enabled auditd 2>/dev/null)" in masked*) AUDIT_OK="allow-auditd";; esac
audit_run() { (cd $SRC && PYTHONPATH=$SRC/src CW_CONFIG=/etc/$NAME/cw.toml python3 -m cw.setup audit 2>&1); }
if [ "$AUDIT_OK" = "allow-auditd" ]; then
  check "the audit command agrees (the missing kernel audit on this machine is the one allowed failure)" bash -c "$(declare -f audit_run); export SRC NAME; ! audit_run | grep -E '^ +FAIL' | grep -v auditd-canaries | grep -q ."
else
  check "the audit command agrees" bash -c "$(declare -f audit_run); export SRC NAME; audit_run >/dev/null"
fi

step "A job through the whole loop"
as_admin cw-run setup >/dev/null 2>&1
as_admin cw-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "cw-run submit sys-status came back with a result" test $rc -eq 0
grep -i -E "load average|up " /tmp/e2e-job.out | head -2
check "cw-admin can reach the worker over SSH" as_admin cw-admin check
check "cw-admin status reads the worker without a password" as_admin cw-admin status
check "the worker reports its isolation as ON" bash -c "as_admin cw-admin status 2>&1 | grep -E 'Isolation +ON'"
check "the job-isolation hardening module is satisfied" bash -c "grep -q 'job-isolation' /etc/$NAME/hardening.json && python3 -c \"import json; d=json.load(open('/etc/$NAME/hardening.json')); raise SystemExit(0 if d['job-isolation']['status'] in ('applied','satisfied') else 1)\""

step "Safety: what must be refused"
as_admin cw-run offload profile --what test --expect test --est-time 1 --est-mem 10 -- sudo id >/tmp/e2e-sudo.out 2>&1
check "a profile run of sudo is refused" grep -qi -E "cannot be profiled|never uses sudo" /tmp/e2e-sudo.out
check "a class that runs a denied program cannot be installed" bash -c "cd $SRC && PYTHONPATH=$SRC/src python3 - <<'PY'
from cw import packs
pack = packs.load_pack('packs/core')
try:
    packs.compose({'core': pack}, ['core'], ['core'], {'classes': {'bad': {'tier': 'auto', 'cost': 'light', 'run': ['/usr/bin/nsenter', '-t', '1']}}})
except packs.PackError:
    raise SystemExit(0)
raise SystemExit(1)
PY"

step "Reading a decoy credential must lock the worker down"
case "$(systemctl is-enabled auditd 2>/dev/null)" in masked*) NOAUDIT=1;; esac
if [ -n "${NOAUDIT:-}" ] || ! auditctl -s >/dev/null 2>&1; then
  printf '  SKIP  this machine has no kernel audit (a container-style machine, not a full VM); the tripwire needs one, run this scenario on a full VM to test it\n'
else
  wait_for 20 test -f /etc/audit/rules.d/60-$NAME.rules
  sleep 5
  cat $H/$WORKER/.aws/credentials >/dev/null 2>&1
  wait_for 25 test -e /var/lib/$NAME/state/lockdown
  check "the worker locked itself down" test -e /var/lib/$NAME/state/lockdown
  check "cw-admin status reports it" bash -c "as_admin cw-admin status 2>&1 | grep -qi -E 'lock|breach'"
  runuser -u $WORKER -- /opt/$NAME/bin/$NAME unlock >/dev/null 2>&1
  check "it can be unlocked" bash -c "! test -e /var/lib/$NAME/state/lockdown"
fi

step "Installing a release again is safe"
rev=$(git -C $SRC rev-parse HEAD 2>/dev/null || true)
check "the update tool re-installs the same release" bash -c "/usr/local/sbin/$NAME-update --local $SRC $rev | tail -3"
check "and the worker is still healthy afterwards" bash -c "! systemctl --failed --no-legend | grep -q $NAME"

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
