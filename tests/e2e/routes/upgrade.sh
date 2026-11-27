#!/bin/bash
# Route: an UPGRADE. A worker installed by an OLDER release is brought to the current one with the update tool it already has (the path `ct-admin apply` takes: sudo ct-update --rev). The old tool renders
# the new release's templates, so anything new in them that the old tool cannot fill refuses the install: seen on the first `ct-admin apply` of the shared-computer release, on all three workers
# ("the template uses {{resources}} but no value is defined for it"), which no fresh-install route could show.
# Then the shared-computer limits are switched on and off in ct.toml and the drop-in is checked against real systemd.
# The older release is E2E_FROM_REV (default: the release the author's workers ran); scripts/e2e-routes unpacks it into /opt/ct-old.
export SRC=/opt/ct-rel
. "$(dirname "$0")/../lib.sh"
NEW=/opt/ct-src; OLD=/opt/ct-old
UPD=/usr/local/sbin/ct-update
git config --system url."$SRC".insteadOf https://releases.example.test/ct.git
git_rel() { git -C $SRC -c user.name=e2e -c user.email=e2e@example.com "$@"; }

step "Prepare the machine with the OLD release as the release source"
prepare_system
mkdir -p $SRC && cp -a $OLD/. $SRC/ && git -C $SRC init -q -b main && git_rel add -A && git_rel commit -qm "old release"
OLD_REV=$(git_rel rev-parse HEAD)
prepare_checkout
check "the old release is what is checked out" test -f $SRC/src/ct/update.py

step "ct-setup install with the OLD release"
chmod -R a+rX $SRC
install_github >/tmp/e2e-install.out 2>&1; rc=$?
tail -12 /tmp/e2e-install.out
check "the old release installed" test $rc -eq 0
check "the worker records the old revision" bash -c "grep -q ${OLD_REV:0:9} /etc/$NAME/installed-rev"
check "the old update tool knows nothing of drop-in limits (so this really is the old tool)" bash -c "! grep -q step_dropin /opt/$NAME/lib/ct/render.py"

step "The current release becomes the release source, and the OLD tool installs it"
git_rel rm -rqf . >/dev/null; find $SRC -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a $NEW/. $SRC/ && git_rel add -A && git_rel commit -qm "current release"
NEW_REV=$(git_rel rev-parse HEAD)
chmod -R a+rX $SRC
python3 - <<PY                                                    # the release source must be an https address; git is told that address is this checkout (the install recorded the author's GitHub address, which this machine cannot reach)
import re
p = "/etc/armada/ct.toml"
t = open(p).read()
t = re.sub(r'(?m)^source = .*\n', '', t.split("[updates]")[0]) + "[updates]" + t.split("[updates]")[1] if "[updates]" in t else t + "\n[updates]\n"
t = t.replace("[updates]", '[updates]\nsource = "https://releases.example.test/ct.git"', 1) if 'source = "https://releases.example.test/ct.git"' not in t else t
open(p, "w").write(t)
PY
grep -n -A3 '^\[updates\]' /etc/$NAME/ct.toml
$UPD --rev $NEW_REV >/tmp/e2e-upgrade.out 2>&1; rc=$?
tail -15 /tmp/e2e-upgrade.out
check "the old update tool installed the current release (rc $rc)" test $rc -eq 0
check "the worker records the current revision" bash -c "grep -q ${NEW_REV:0:9} /etc/$NAME/installed-rev"
check "the installed code is the current release's" bash -c "grep -q step_dropin /opt/$NAME/lib/ct/render.py"
check "the step unit has no placeholder left and no limits (none were asked for)" bash -c "! grep -q '{{' /etc/systemd/system/$NAME-step.service && ! test -e /etc/systemd/system/$NAME-step.service.d/resources.conf"
check "nothing failed and the loop is still running" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer | sort -u | grep -qx active && ! systemctl --failed --no-legend | grep -q $NAME"
as_admin ct-run setup >/dev/null 2>&1
check "a job goes through the loop after the upgrade" bash -c "as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1"
$UPD --rev $NEW_REV >/tmp/e2e-again.out 2>&1
check "installing the same release again changes nothing" bash -c "grep -qi 'Installed' /tmp/e2e-again.out && ! grep -q '^ *Updated' /tmp/e2e-again.out"

step "Resource limits for a shared computer: written as a drop-in, honoured by systemd, removed again"
CONF=/etc/$NAME/ct.toml
printf '\n[availability]\ncpu_weight = 20\nmemory_max_percent = 50\nnice = 10\n' >> $CONF
$UPD --no-sandbox-test --rev $NEW_REV >/tmp/e2e-limits.out 2>&1; rc=$?
tail -6 /tmp/e2e-limits.out
check "the update with limits set succeeded" test $rc -eq 0
check "the drop-in exists with exactly what was set" bash -c "grep -qx 'CPUWeight=20' /etc/systemd/system/$NAME-step.service.d/resources.conf && grep -qx 'Nice=10' /etc/systemd/system/$NAME-step.service.d/resources.conf"
check "systemd reports the limits on the step service" bash -c "systemctl show $NAME-step.service -p CPUWeight -p Nice | sort | tr '\n' ' ' | grep -q 'CPUWeight=20 Nice=10'"
python3 - <<'PY'
import re
p = "/etc/armada/ct.toml"
t = open(p).read()
t = re.sub(r"\n\[availability\].*?(?=\n\[|\Z)", "\n", t, flags=re.S)
open(p, "w").write(t)
PY
$UPD --no-sandbox-test --rev $NEW_REV >/tmp/e2e-nolimits.out 2>&1; rc=$?
check "the update with the limits removed succeeded" test $rc -eq 0
check "the drop-in is gone and systemd was told" bash -c "! test -e /etc/systemd/system/$NAME-step.service.d/resources.conf && ! systemctl show $NAME-step.service -p CPUWeight | grep -qx 'CPUWeight=20'"
finish
