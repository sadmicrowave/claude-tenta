#!/bin/bash
# Route: uninstalling removes the install, keeps the protections, and installing again works.
. "$(dirname "$0")/../lib.sh"
step "Prepare the machine"; prepare_system; prepare_checkout
baseline_github

step "Uninstalling removes the install, keeps the protections, and installing again works"
as_admin arm-setup uninstall >/tmp/e2e-uninstall-preview.out 2>&1
check "the uninstall preview lists what it would remove and changes nothing" bash -c "grep -q 'What stays' /tmp/e2e-uninstall-preview.out && test -d /opt/$NAME"
as_admin arm-setup uninstall --apply --yes >/tmp/e2e-uninstall.out 2>&1; rc=$?
tail -6 /tmp/e2e-uninstall.out
check "the uninstall finished successfully" test $rc -eq 0
check "the code, configuration and state folders are gone" bash -c "! test -e /opt/$NAME && ! test -e /etc/$NAME && ! test -e /var/lib/$NAME"
check "the units, commands and sudoers rule are gone" bash -c "! ls /etc/systemd/system/$NAME-* 2>/dev/null | grep -q . && ! test -e /usr/local/bin/arm-worker && ! test -e /usr/local/sbin/arm-update && ! test -e /etc/sudoers.d/$NAME"
check "no timer is left" bash -c "! systemctl list-timers --all --no-legend | grep -q $NAME"
check "the protections stay: password SSH is still off and the firewall is still on" bash -c "sshd -T | grep -qx 'passwordauthentication no' && ufw status | grep -q 'Status: active'"
check "the worker account stays (reinstalling reuses it)" bash -c "getent passwd $WORKER"
check "the repositories on GitHub were not touched" rgit -C $H/git/$OWNER/$NAME-queue.git cat-file -e main:registry.yaml
install_github >/tmp/e2e-reinstall.out 2>&1; rc=$?
tail -6 /tmp/e2e-reinstall.out
check "installing again after an uninstall finished successfully" test $rc -eq 0
check "the timers are running again" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer $NAME-canary.timer $NAME-registry-sync.timer | sort -u | grep -qx active"
finish
