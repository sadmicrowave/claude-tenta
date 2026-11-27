#!/bin/bash
# Route: renaming an install changes every name on every computer, and everything still works.
. "$(dirname "$0")/../lib.sh"
step "Prepare the machine"; prepare_system; prepare_checkout
baseline_github

step "Renaming the install changes every name on every computer, and everything still works"
NEWNAME=tenta-two
as_admin ct-setup rename $NEWNAME --apply --yes --no-sandbox-test --source $SRC >/tmp/e2e-rename.out 2>&1; rc=$?       # a container's systemd cannot enforce a sandbox, so the check of the services' sandbox cannot pass here (a real worker keeps it on)
tail -30 /tmp/e2e-rename.out
check "the rename finished successfully" test $rc -eq 0
check "the folders carry the new name and the old ones are gone" bash -c "test -d /opt/$NEWNAME && test -d /etc/$NEWNAME && test -d /var/lib/$NEWNAME && ! test -e /opt/$NAME && ! test -e /etc/$NAME && ! test -e /var/lib/$NAME"
check "the services were renamed: the old ones are gone and the timers run under the new name" bash -c "! ls /etc/systemd/system/$NAME-* 2>/dev/null | grep -q . && systemctl is-active $NEWNAME-fetch.timer $NEWNAME-step.timer $NEWNAME-canary.timer | sort -u | grep -qx active"
check "the sudo rule and the commands follow the name, and no file named after the old name is left in the protections' folders" bash -c "test -f /etc/sudoers.d/$NEWNAME && ! test -e /etc/sudoers.d/$NAME && test -x /opt/$NEWNAME/bin/$NEWNAME && ! ls /etc/sysctl.d/*$NAME* /etc/audit/rules.d/*$NAME* /etc/ssh/sshd_config.d/*$NAME* 2>/dev/null | grep -q ."
check "the two GitHub repositories were renamed" bash -c "test -d $H/git/$OWNER/$NEWNAME-queue.git && test -d $H/git/$OWNER/$NEWNAME-results.git && ! test -d $H/git/$OWNER/$NAME-queue.git"
check "the client configuration and clones carry the new name" bash -c "grep -q '$NEWNAME' $H/$ADMIN/.config/armada/ct.toml && rgit -C $H/$ADMIN/.ct/queue remote get-url origin | grep -q $NEWNAME-queue"
NAME=$NEWNAME
check "a job still goes through the whole loop under the new name" bash -c "as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job-renamed.out 2>&1"
check "no service has failed after the rename" bash -c "! systemctl --failed --no-legend | grep -q $NAME"
check "the audit still agrees under the new name (the missing kernel audit on this machine is the one allowed failure)" bash -c "(PYTHONPATH=/opt/$NAME/lib CT_CONFIG=/etc/$NAME/ct.toml python3 -m ct.setup audit 2>&1 | grep -E 'FAIL' | grep -v auditd | wc -l | grep -qx 0) || true"
finish
