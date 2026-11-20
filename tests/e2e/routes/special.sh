# Route: a Special Use computer (a tools box). The wizard answered with isolation.purpose = special-use and a tool folder must record relaxed isolation and that folder, offer (not apply) the whole-machine
# protections, leave the machine-wide settings as they were, and still run a job and pass the audit.
. "$(dirname "$0")/../lib.sh"

step "Prepare the machine, with a folder of tools that already exists"
prepare_system; prepare_checkout
mkdir -p /opt/e2e-tools && printf '#!/bin/sh\necho tool\n' > /opt/e2e-tools/tool && chmod 755 /opt/e2e-tools/tool

fp() { echo "-- sshd"; sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|pubkeyauthentication) ' | sort; echo "-- firewall"; ufw status verbose 2>&1; echo "-- ipv6"; sysctl -n net.ipv6.conf.all.disable_ipv6; echo "-- updates"; cat /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null; }
fp > /tmp/e2e-fp-before.out 2>&1

step "ct-setup install as a Special Use computer"
chmod -R a+rX $SRC
install_github --set isolation.purpose=special-use --set isolation.tool_roots=/opt/e2e-tools >/tmp/e2e-install.out 2>&1; rc=$?
tail -30 /tmp/e2e-install.out
check "the install finished successfully" test $rc -eq 0
fp > /tmp/e2e-fp-after.out 2>&1
check "the machine-wide settings are as they were" bash -c "diff /tmp/e2e-fp-before.out /tmp/e2e-fp-after.out"
check "the configuration records Special Use, relaxed isolation and the tool folder" bash -c "grep -q 'purpose = \"special-use\"' /etc/$NAME/ct.toml && grep -q 'mode = \"relaxed\"' /etc/$NAME/ct.toml && grep -q '/opt/e2e-tools' /etc/$NAME/ct.toml"
check "the whole-machine protections were offered and declined, none applied" bash -c "python3 - <<'PY'
import json
d = json.load(open('/etc/$NAME/hardening.json'))
for m in ('ssh-keys-only', 'ufw-lan-egress', 'no-root-equivalent-groups', 'unattended-upgrades'):
    assert d[m]['status'] == 'declined', (m, d.get(m))
PY"

step "The worker still works and the audit agrees"
as_admin ct-run setup >/dev/null 2>&1
as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "a job came back with a result" test $rc -eq 0
audit_run() { (cd $SRC && PYTHONPATH=$SRC/src CT_CONFIG=/etc/$NAME/ct.toml python3 -m ct.setup audit 2>&1); }
case "$(systemctl is-enabled auditd 2>/dev/null)" in
  masked*) check "the audit agrees (the missing kernel audit is the one allowed failure)" bash -c "$(declare -f audit_run); export SRC NAME; ! audit_run | grep -E '^ +FAIL' | grep -v auditd";;
  *) check "the audit agrees" bash -c "$(declare -f audit_run); export SRC NAME; audit_run >/dev/null";;
esac
finish
