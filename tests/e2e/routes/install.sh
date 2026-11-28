#!/bin/bash
# Route: a fresh GitHub install, and everything that must be true of it: the repositories and keys it made itself, running it again, the services and protections, a job through the whole loop,
# what must be refused, the tripwire (needs a kernel with audit), and installing the same release again.
. "$(dirname "$0")/../lib.sh"

step "Prepare the machine"
prepare_system
prepare_checkout

step "arm-setup install: the whole setup, from your computer, in one command"
chmod -R a+rX $SRC
install_github >/tmp/e2e-install.out 2>&1; rc=$?
tail -45 /tmp/e2e-install.out
check "the install command finished successfully" test $rc -eq 0
check "it created the two private repositories itself" bash -c "test -d $H/git/$OWNER/armada-queue.git && test -d $H/git/$OWNER/armada-results.git"
check "it added the server's two keys to GitHub, results with write access" bash -c "grep -q 'armada-queue (write=0)' /tmp/e2e-gh.log && grep -q 'armada-results (write=1)' /tmp/e2e-gh.log"
check "nothing was copied by hand: the worker's keys are in the git user's authorized keys" bash -c "grep -c 'armada-' $H/git/.ssh/authorized_keys | grep -qx 2"
check "the working folder it used on the server was removed" bash -c "! ls -d /tmp/arm-install.* 2>/dev/null"
check "the client configuration was written" test -f $H/$ADMIN/.config/armada/armada.toml
check "the registry was seeded on main of the queue repository" rgit -C $H/git/$OWNER/armada-queue.git cat-file -e main:registry.yaml

step "Running the install again changes nothing and still works"
install_github >/tmp/e2e-install2.out 2>&1; rc=$?
tail -8 /tmp/e2e-install2.out
check "the second run finished successfully" test $rc -eq 0
check "the worker account exists with no shell" bash -c "getent passwd $WORKER | grep -q nologin"
check "the installed commands exist" bash -c "test -x /usr/local/bin/arm-worker && test -x /usr/local/sbin/arm-update && test -f /opt/$NAME/lib/armada/cli.py"
check "the composed registry is installed and says it is generated" grep -q GENERATED /etc/$NAME/registry.yaml
check "the core classes are in it" grep -q "sys-status" /etc/$NAME/registry.yaml
check "the policy is in it" grep -q "nsenter" /etc/$NAME/registry.yaml
check "the timers are running" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer $NAME-canary.timer $NAME-registry-sync.timer | sort -u | grep -qx active"
check "no service has failed" bash -c "! systemctl --failed --no-legend | grep -q $NAME"
check "password SSH is off" bash -c "sshd -T | grep -qx 'passwordauthentication no'"
check "the firewall is on" bash -c "ufw status | grep -q 'Status: active'"
check "the worker has no root-equivalent groups" bash -c "! id -nG $WORKER | grep -Eqw 'sudo|lxd|docker|disk'"
check "the state folder is not world readable" bash -c "! stat -c %a /var/lib/$NAME/state | grep -q '[0-7][0-7][1-7]'"
AUDIT_OK=1; case "$(systemctl is-enabled auditd 2>/dev/null)" in masked*) AUDIT_OK="allow-auditd";; esac
audit_run() { (cd $SRC && PYTHONPATH=$SRC/src ARM_CONFIG=/etc/$NAME/armada.toml python3 -m armada.setup audit 2>&1); }
if [ "$AUDIT_OK" = "allow-auditd" ]; then
  check "the audit command agrees (the missing kernel audit on this machine is the one allowed failure)" bash -c "$(declare -f audit_run); export SRC NAME; ! audit_run | grep -E '^ +FAIL' | grep -v auditd-canaries | grep -q ."
else
  check "the audit command agrees" bash -c "$(declare -f audit_run); export SRC NAME; audit_run >/dev/null"
fi

step "A job through the whole loop"
as_admin arm-run setup >/dev/null 2>&1
as_admin arm-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "arm-run submit sys-status came back with a result" test $rc -eq 0
grep -i -E "load average|up " /tmp/e2e-job.out | head -2
check "arm-admin can reach the worker over SSH" as_admin arm-admin check
check "arm-admin status reads the worker without a password" as_admin arm-admin status
check "status shows the fetch and step passes healthy" bash -c "as_admin arm-admin status 2>&1 | grep -E 'Fetch +OK' && as_admin arm-admin status 2>&1 | grep -E 'Step +OK'"
check "no fetch or step pass has failed in the journal" bash -c "! journalctl -u $NAME-fetch.service -u $NAME-step.service --no-pager -o cat 2>&1 | grep -E 'Traceback|Failed to start'"
check "the worker trusts the administrator's approval key (approvals are signed on the administrator's computer)" bash -c "grep -q 'arm-approval,arm-registry,arm-control' /etc/$NAME/approvers && as_admin test -f /home/$ADMIN/.armada/approval/key"
check "the registry sync runs, and says why it did nothing when no registry has been signed" bash -c "env PYTHONPATH=/opt/$NAME/lib ARM_CONFIG=/etc/$NAME/armada.toml python3 -m armada.update --registry-sync 2>&1 | grep -qi 'registry sync'"
check "arm-admin workers compares workers from what each published, with no login" bash -c "as_admin arm-admin workers --json | grep -q classes"
check "the worker reports its isolation as ON" bash -c "as_admin arm-admin status 2>&1 | grep -E 'Isolation +ON'"
check "the job-isolation hardening module is satisfied" bash -c "grep -q 'job-isolation' /etc/$NAME/hardening.json && python3 -c \"import json; d=json.load(open('/etc/$NAME/hardening.json')); raise SystemExit(0 if d['job-isolation']['status'] in ('applied','satisfied') else 1)\""

step "Safety: what must be refused"
as_admin arm-run offload profile --what test --expect test --est-time 1 --est-mem 10 -- sudo id >/tmp/e2e-sudo.out 2>&1
check "a profile run of sudo is refused" grep -qi -E "cannot be profiled|never uses sudo" /tmp/e2e-sudo.out
check "a class that runs a denied program cannot be installed" bash -c "cd $SRC && PYTHONPATH=$SRC/src python3 - <<'PY'
from armada import packs
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
  printf '  SKIP  this machine has no kernel audit (a container-style machine, not a full VM); the tripwire needs one: scripts/probe --on <machine> audit, or run this route on a full VM\n'
else
  wait_for 20 test -f /etc/audit/rules.d/60-$NAME.rules
  sleep 5
  cat $H/$WORKER/.aws/credentials >/dev/null 2>&1
  wait_for 25 test -e /var/lib/$NAME/state/lockdown
  check "the worker locked itself down" test -e /var/lib/$NAME/state/lockdown
  check "arm-admin status reports it" bash -c "as_admin arm-admin status 2>&1 | grep -qi -E 'lock|breach'"
  runuser -u $WORKER -- /opt/$NAME/bin/$NAME unlock >/dev/null 2>&1
  check "it can be unlocked" bash -c "! test -e /var/lib/$NAME/state/lockdown"
fi

step "Installing a release again is safe"
rev=$(git -C $SRC rev-parse HEAD 2>/dev/null || true)
check "the update tool re-installs the same release" bash -c "/usr/local/sbin/arm-update --local $SRC $rev | tail -3"
check "and the worker is still healthy afterwards" bash -c "! systemctl --failed --no-legend | grep -q $NAME"
finish
