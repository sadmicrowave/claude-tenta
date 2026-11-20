#!/bin/bash
# Route: a computer other people use. Installing with isolation.purpose = shared must leave every machine-wide setting as it was (sshd, the firewall, sudoers, groups, IPv6, automatic updates), a second
# user must still be able to log in by password, the whole-machine protections must be recorded as offered-and-declined (--yes takes none), the worker must still run a job, and the worker account must be
# able to see who is logged in (what availability.pause_when_logged_in reads).
. "$(dirname "$0")/../lib.sh"

step "Prepare the machine, with a second user who logs in by password"
prepare_system; prepare_checkout
apt-get install -y -qq sshpass >/dev/null 2>&1
id guest >/dev/null 2>&1 || useradd -m -s /bin/bash guest
echo 'guest:guestpw' | chpasswd
printf 'PasswordAuthentication yes\n' > /etc/ssh/sshd_config.d/00-e2e-guest-password.conf
systemctl reload ssh >/dev/null 2>&1 || systemctl restart ssh >/dev/null 2>&1
guest_login() { sshpass -p guestpw ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new -o BatchMode=no guest@localhost "$@"; }
check "before: the second user logs in by password" guest_login true

fp() {
  echo "-- sshd effective"; sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|pubkeyauthentication|kbdinteractiveauthentication|allowusers|allowgroups) ' | sort
  echo "-- sshd files"; sha256sum /etc/ssh/sshd_config /etc/ssh/sshd_config.d/* 2>/dev/null | sort
  echo "-- firewall"; ufw status verbose 2>&1 | sed 's/^Status: .*/&/'; iptables -S 2>/dev/null | sort
  echo "-- sudoers"; sha256sum /etc/sudoers /etc/sudoers.d/* 2>/dev/null | grep -v "$NAME" | sort
  echo "-- groups"; getent group sudo lxd docker disk shadow | sort
  echo "-- the second user's groups"; id -nG guest
  echo "-- ipv6"; sysctl -n net.ipv6.conf.all.disable_ipv6
  echo "-- automatic updates"; cat /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null; systemctl is-enabled unattended-upgrades 2>&1
}
fp > /tmp/e2e-fp-before.out 2>&1

step "ct-setup install on a shared computer: only the protections that affect tenta's own jobs"
chmod -R a+rX $SRC
install_github --set isolation.purpose=shared >/tmp/e2e-install.out 2>&1; rc=$?
tail -30 /tmp/e2e-install.out
check "the install finished successfully" test $rc -eq 0
fp > /tmp/e2e-fp-after.out 2>&1
check "every machine-wide setting is exactly as it was (sshd, firewall, sudoers other than tenta's own file, groups, IPv6, automatic updates)" bash -c "diff /tmp/e2e-fp-before.out /tmp/e2e-fp-after.out"
check "the second user still logs in by password" guest_login true
check "password SSH is still on" bash -c "sshd -T | grep -qx 'passwordauthentication yes'"
check "the four whole-machine protections were offered and declined, none applied (--yes takes none)" bash -c "python3 - <<'PY'
import json
d = json.load(open('/etc/$NAME/hardening.json'))
for m in ('ssh-keys-only', 'ufw-lan-egress', 'no-root-equivalent-groups', 'unattended-upgrades'):
    assert d[m]['status'] == 'declined', (m, d.get(m))
assert d['job-isolation']['status'] in ('applied', 'satisfied'), d['job-isolation']
PY"
check "the configuration says what the computer is used for and offers, not applies, the whole-machine ones" bash -c "grep -q 'purpose = \"shared\"' /etc/$NAME/ct.toml && grep -q 'offered' /etc/$NAME/ct.toml"

step "The worker still works, and the audit does not call the declined protections drift"
as_admin ct-run setup >/dev/null 2>&1
as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "a job came back with a result" test $rc -eq 0
audit_run() { (cd $SRC && PYTHONPATH=$SRC/src CT_CONFIG=/etc/$NAME/ct.toml python3 -m ct.setup audit 2>&1); }
AUDIT_OK=1; case "$(systemctl is-enabled auditd 2>/dev/null)" in masked*) AUDIT_OK="allow-auditd";; esac
if [ "$AUDIT_OK" = "allow-auditd" ]; then
  check "the audit agrees (the missing kernel audit on this machine is the one allowed failure)" bash -c "$(declare -f audit_run); export SRC NAME; ! audit_run | grep -E '^ +FAIL' | grep -v auditd"
else
  check "the audit agrees" bash -c "$(declare -f audit_run); export SRC NAME; audit_run >/dev/null"
fi

step "The worker account can see who is logged in (what pause_when_logged_in reads)"
sshpass -p guestpw ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no guest@localhost 'sleep 40' &
sleep 6
check "the worker account sees the second user's session" bash -c "runuser -u $WORKER -- env PYTHONPATH=/opt/$NAME/lib python3 -c 'from ct.runner import LinuxSensors; import sys; sys.exit(0 if \"guest\" in LinuxSensors().logged_in() else 1)'"
wait
finish
