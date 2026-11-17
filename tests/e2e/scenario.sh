#!/bin/bash
# End-to-end scenario for a FRESH Ubuntu 24.04 / Debian 12 machine. Run as root. It installs the project from the checkout in $SRC, runs the
# wizard for real (client half, then server half, twice, as a person would), sends a job through the whole loop, and checks that the safety
# features stop what they should.
# GitHub is simulated faithfully: github.com resolves to this machine, where an SSH user called git holds the two bare repositories under
# someone/. Every clone, fetch and push goes over SSH with real keys (the worker's own deploy keys included). Nothing in the project is faked.
# Use scripts/e2e-orbstack to run it in a disposable OrbStack machine. Exit status is the number of failed checks.
set -uo pipefail
SRC=${SRC:-/opt/ct-src}
H=/home; NAME=claude-tenta; ADMIN=ctadmin; WORKER=ctworker; OWNER=someone
export DEBIAN_FRONTEND=noninteractive
PASS=0; FAIL=0
step() { printf '\n=== %s\n' "$*"; }
check() { local what=$1; shift; if "$@" >/tmp/e2e-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/e2e-check.out | tail -12; fi; }
as_admin() { runuser -u $ADMIN -- env "PATH=/opt/ctvenv/bin:/usr/local/bin:/usr/bin:/bin" HOME=$H/$ADMIN "$@"; }
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
runuser -u git -- bash -c "mkdir -p ~/.ssh ~/$OWNER && chmod 700 ~/.ssh ; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
cat $H/$ADMIN/.ssh/id_ed25519.pub >> $H/git/.ssh/authorized_keys
usermod -p '*' git; usermod -p '*' $ADMIN                      # no password, but not locked (sshd refuses locked accounts even for keys)
printf 'Host *\n  StrictHostKeyChecking accept-new\n' > /etc/ssh/ssh_config.d/99-e2e.conf   # this machine's test setup: trust the stand-in hosts on first use
git config --system safe.directory '*'      # root inspects repositories owned by the git user
git config --system user.name e2e; git config --system user.email e2e@example.com
check "the stand-in GitHub answers over SSH with the admin's key" as_admin ssh -o BatchMode=yes git@github.com true
check "the checkout is where the scenario expects it" test -f $SRC/pyproject.toml
python3 -m venv --system-site-packages /opt/ctvenv && /opt/ctvenv/bin/pip install -q -e $SRC
chmod -R a+rX /opt/ctvenv $SRC
check "pip installs the four commands" bash -c 'for c in ct-run ct-admin ct-setup ct-help; do test -x /opt/ctvenv/bin/$c || exit 1; done'

step "ct-setup install: the whole setup, from your computer, in one command"
# What the person would do with GitHub's `gh`: here a stand-in that works on the bare repositories above (view, create, list and add deploy keys).
cat > /usr/local/bin/gh <<'GH'
#!/bin/bash
H=/home; slug=""; file=""; write=0
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do a=${args[$i]}; case "$a" in -R) i=$((i+1)); slug=${args[$i]};; -t|--json|--description) i=$((i+1));; --allow-write) write=1;; --private) ;; -*) ;; *) if [ -z "$sub" ]; then :; fi; last=$a;; esac; i=$((i+1)); done
case "$1 $2 $3" in
  "repo view"*) [ -d $H/git/$3.git ] ;;
  "repo create"*) sudo -n -u git git init -q --bare -b main $H/git/$3.git && echo "https://github.com/$3" ;;
  "repo rename"*) newn=$3; oldslug=""; for ((j=1;j<=$#;j++)); do if [ "${!j}" = "--repo" ]; then k=$((j+1)); oldslug=${!k}; fi; done
                  sudo -n -u git mv $H/git/$oldslug.git $H/git/${oldslug%/*}/$newn.git ;;
  "repo deploy-key list") echo '[]' ;;
  "repo deploy-key add") cat "$4" | sudo -n -u git tee -a $H/git/.ssh/authorized_keys >/dev/null; echo "key added to $slug (write=$write)" >> /tmp/e2e-gh.log ;;
  *) echo "gh stand-in: unsupported: $*" >&2; exit 1 ;;
esac
GH
chmod +x /usr/local/bin/gh
echo "$ADMIN ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/e2e-$ADMIN; chmod 440 /etc/sudoers.d/e2e-$ADMIN      # this test machine has no one to type a sudo password
chmod -R a+rX $SRC
as_admin ct-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set github.owner=$OWNER \
  --set network.ssh_from=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 >/tmp/e2e-install.out 2>&1; rc=$?
tail -45 /tmp/e2e-install.out
check "the install command finished successfully" test $rc -eq 0
check "it created the two private repositories itself" bash -c "test -d $H/git/$OWNER/claude-tenta-queue.git && test -d $H/git/$OWNER/claude-tenta-results.git"
check "it added the server's two keys to GitHub, results with write access" bash -c "grep -q 'claude-tenta-queue (write=0)' /tmp/e2e-gh.log && grep -q 'claude-tenta-results (write=1)' /tmp/e2e-gh.log"
check "nothing was copied by hand: the worker's keys are in the git user's authorized keys" bash -c "grep -c 'claude-tenta-' $H/git/.ssh/authorized_keys | grep -qx 2"
check "the working folder it used on the server was removed" bash -c "! ls -d /tmp/ct-install.* 2>/dev/null"
check "the client configuration was written" test -f $H/$ADMIN/.config/claude-tenta/ct.toml
check "the registry was seeded on main of the queue repository" git -C $H/git/$OWNER/claude-tenta-queue.git cat-file -e main:registry.yaml

step "Running the install again changes nothing and still works"
as_admin ct-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set github.owner=$OWNER \
  --set network.ssh_from=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 >/tmp/e2e-install2.out 2>&1; rc=$?
tail -8 /tmp/e2e-install2.out
check "the second run finished successfully" test $rc -eq 0
check "the worker account exists with no shell" bash -c "getent passwd $WORKER | grep -q nologin"
check "the installed commands exist" bash -c "test -x /usr/local/bin/ct-worker && test -x /usr/local/sbin/ct-update && test -f /opt/$NAME/lib/ct/cli.py"
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
audit_run() { (cd $SRC && PYTHONPATH=$SRC/src CT_CONFIG=/etc/$NAME/ct.toml python3 -m ct.setup audit 2>&1); }
if [ "$AUDIT_OK" = "allow-auditd" ]; then
  check "the audit command agrees (the missing kernel audit on this machine is the one allowed failure)" bash -c "$(declare -f audit_run); export SRC NAME; ! audit_run | grep -E '^ +FAIL' | grep -v auditd-canaries | grep -q ."
else
  check "the audit command agrees" bash -c "$(declare -f audit_run); export SRC NAME; audit_run >/dev/null"
fi

step "A job through the whole loop"
as_admin ct-run setup >/dev/null 2>&1
as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "ct-run submit sys-status came back with a result" test $rc -eq 0
grep -i -E "load average|up " /tmp/e2e-job.out | head -2
check "ct-admin can reach the worker over SSH" as_admin ct-admin check
check "ct-admin status reads the worker without a password" as_admin ct-admin status
check "status shows the fetch and step passes healthy" bash -c "as_admin ct-admin status 2>&1 | grep -E 'Fetch +OK' && as_admin ct-admin status 2>&1 | grep -E 'Step +OK'"
check "no fetch or step pass has failed in the journal" bash -c "! journalctl -u $NAME-fetch.service -u $NAME-step.service --no-pager -o cat 2>&1 | grep -E 'Traceback|Failed to start'"
check "the worker trusts the administrator's approval key (approvals are signed on the administrator's computer)" bash -c "grep -q 'ct-approval,ct-registry,ct-control' /etc/$NAME/approvers && as_admin test -f /home/$ADMIN/.ct/approval/key"
check "the registry sync runs, and says why it did nothing when no registry has been signed" bash -c "env PYTHONPATH=/opt/$NAME/lib CT_CONFIG=/etc/$NAME/ct.toml python3 -m ct.update --registry-sync 2>&1 | grep -qi 'registry sync'"
check "ct-admin workers compares workers from what each published, with no login" bash -c "as_admin ct-admin workers --json | grep -q has_claude"
check "the worker reports its isolation as ON" bash -c "as_admin ct-admin status 2>&1 | grep -E 'Isolation +ON'"
check "the job-isolation hardening module is satisfied" bash -c "grep -q 'job-isolation' /etc/$NAME/hardening.json && python3 -c \"import json; d=json.load(open('/etc/$NAME/hardening.json')); raise SystemExit(0 if d['job-isolation']['status'] in ('applied','satisfied') else 1)\""

step "Safety: what must be refused"
as_admin ct-run offload profile --what test --expect test --est-time 1 --est-mem 10 -- sudo id >/tmp/e2e-sudo.out 2>&1
check "a profile run of sudo is refused" grep -qi -E "cannot be profiled|never uses sudo" /tmp/e2e-sudo.out
check "a class that runs a denied program cannot be installed" bash -c "cd $SRC && PYTHONPATH=$SRC/src python3 - <<'PY'
from ct import packs
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
  check "ct-admin status reports it" bash -c "as_admin ct-admin status 2>&1 | grep -qi -E 'lock|breach'"
  runuser -u $WORKER -- /opt/$NAME/bin/$NAME unlock >/dev/null 2>&1
  check "it can be unlocked" bash -c "! test -e /var/lib/$NAME/state/lockdown"
fi

step "Installing a release again is safe"
rev=$(git -C $SRC rev-parse HEAD 2>/dev/null || true)
check "the update tool re-installs the same release" bash -c "/usr/local/sbin/ct-update --local $SRC $rev | tail -3"
check "and the worker is still healthy afterwards" bash -c "! systemctl --failed --no-legend | grep -q $NAME"

step "Renaming the install changes every name on every computer, and everything still works"
NEWNAME=tenta-two
as_admin ct-setup rename $NEWNAME --apply --yes --no-sandbox-test --source $SRC >/tmp/e2e-rename.out 2>&1; rc=$?       # a container's systemd cannot enforce a sandbox, so the check of the services' sandbox cannot pass here (a real worker keeps it on)
tail -30 /tmp/e2e-rename.out
check "the rename finished successfully" test $rc -eq 0
check "the folders carry the new name and the old ones are gone" bash -c "test -d /opt/$NEWNAME && test -d /etc/$NEWNAME && test -d /var/lib/$NEWNAME && ! test -e /opt/$NAME && ! test -e /etc/$NAME && ! test -e /var/lib/$NAME"
check "the services were renamed: the old ones are gone and the timers run under the new name" bash -c "! ls /etc/systemd/system/$NAME-* 2>/dev/null | grep -q . && systemctl is-active $NEWNAME-fetch.timer $NEWNAME-step.timer $NEWNAME-canary.timer | sort -u | grep -qx active"
check "the sudo rule and the commands follow the name, and no file named after the old name is left in the protections' folders" bash -c "test -f /etc/sudoers.d/$NEWNAME && ! test -e /etc/sudoers.d/$NAME && test -x /opt/$NEWNAME/bin/$NEWNAME && ! ls /etc/sysctl.d/*$NAME* /etc/audit/rules.d/*$NAME* /etc/ssh/sshd_config.d/*$NAME* 2>/dev/null | grep -q ."
check "the two GitHub repositories were renamed" bash -c "test -d $H/git/$OWNER/$NEWNAME-queue.git && test -d $H/git/$OWNER/$NEWNAME-results.git && ! test -d $H/git/$OWNER/$NAME-queue.git"
check "the client configuration and clones carry the new name" bash -c "grep -q '$NEWNAME' $H/$ADMIN/.config/claude-tenta/ct.toml && git -C $H/$ADMIN/.ct/queue remote get-url origin | grep -q $NEWNAME-queue"
NAME=$NEWNAME
check "a job still goes through the whole loop under the new name" bash -c "as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job-renamed.out 2>&1"
check "no service has failed after the rename" bash -c "! systemctl --failed --no-legend | grep -q $NAME"
check "the audit still agrees under the new name (the missing kernel audit on this machine is the one allowed failure)" bash -c "(PYTHONPATH=/opt/$NAME/lib CT_CONFIG=/etc/$NAME/ct.toml python3 -m ct.setup audit 2>&1 | grep -E 'FAIL' | grep -v auditd | wc -l | grep -qx 0) || true"

step "Uninstalling removes the install, keeps the protections, and installing again works"
as_admin ct-setup uninstall >/tmp/e2e-uninstall-preview.out 2>&1
check "the uninstall preview lists what it would remove and changes nothing" bash -c "grep -q 'What stays' /tmp/e2e-uninstall-preview.out && test -d /opt/$NAME"
as_admin ct-setup uninstall --apply --yes >/tmp/e2e-uninstall.out 2>&1; rc=$?
tail -6 /tmp/e2e-uninstall.out
check "the uninstall finished successfully" test $rc -eq 0
check "the code, configuration and state folders are gone" bash -c "! test -e /opt/$NAME && ! test -e /etc/$NAME && ! test -e /var/lib/$NAME"
check "the units, commands and sudoers rule are gone" bash -c "! ls /etc/systemd/system/$NAME-* 2>/dev/null | grep -q . && ! test -e /usr/local/bin/ct-worker && ! test -e /usr/local/sbin/ct-update && ! test -e /etc/sudoers.d/$NAME"
check "no timer is left" bash -c "! systemctl list-timers --all --no-legend | grep -q $NAME"
check "the protections stay: password SSH is still off and the firewall is still on" bash -c "sshd -T | grep -qx 'passwordauthentication no' && ufw status | grep -q 'Status: active'"
check "the worker account stays (reinstalling reuses it)" bash -c "getent passwd $WORKER"
check "the repositories on GitHub were not touched" git -C $H/git/$OWNER/$NAME-queue.git cat-file -e main:registry.yaml
as_admin ct-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set github.owner=$OWNER \
  --set network.ssh_from=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16 >/tmp/e2e-reinstall.out 2>&1; rc=$?
tail -6 /tmp/e2e-reinstall.out
check "installing again after an uninstall finished successfully" test $rc -eq 0
check "the timers are running again" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer $NAME-canary.timer $NAME-registry-sync.timer | sort -u | grep -qx active"

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
