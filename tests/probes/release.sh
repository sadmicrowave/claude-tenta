#!/bin/bash
# Probe: does the project work on THIS release of Debian or Ubuntu? Runs in a throwaway container (scripts/probe release, or scripts/probe --releases release for all four at once, in parallel).
# It replaces running a whole end-to-end scenario per release just to learn "does that package or tool exist there": about 30 seconds per release. With the project's own code (mounted at /src):
#   1. every package the installer installs can be installed here (simulated: dependencies resolve);
#   2. Python is 3.11 or newer and the project imports (tomllib, PyYAML);
#   3. git has what the project relies on (GIT_CONFIG_GLOBAL, 2.32 or newer);
#   4. the sudoers file the project writes passes `visudo -c` (a bad one locks an administrator out);
#   5. every systemd unit the project writes parses (`systemd-analyze verify`, ignoring only the accounts and programs a container does not have);
#   6. the SSH drop-in the project writes is accepted by `sshd -t`;
#   7. the AppArmor profile it writes compiles (`apparmor_parser -Q`), where this release uses that profile at all.
set -uo pipefail
PASS=0; FAIL=0
check() { local what=$1; shift; if "$@" >/tmp/probe-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/probe-check.out | tail -8; fi; }
note() { printf '  NOTE  %s\n' "$1"; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq git python3 python3-yaml sudo openssh-server systemd apparmor util-linux >/dev/null 2>&1
echo "$(. /etc/os-release; echo "$PRETTY_NAME"), git $(git --version | cut -d' ' -f3), python $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
export PYTHONPATH=/src/src PYTHONDONTWRITEBYTECODE=1

echo "1. packages"
PKGS=$(python3 - <<'PY'
import re
from armada.setup import server
names = [p for p, _ in server.PREREQUISITES]
names += re.findall(r'"apt-get", "install", "-y", "([a-z0-9+.-]+)"', open("/src/src/armada/setup/hardening.py").read())
print(" ".join(dict.fromkeys(names)))
PY
)
note "the installer installs: $PKGS"
check "every one of them can be installed here (simulated; dependencies resolve)" apt-get install -s -y --no-install-recommends $PKGS

echo "2. python"
check "Python is 3.11 or newer" python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
check "the project's modules import (tomllib, yaml, every command)" python3 -c 'import tomllib, yaml, armada.cli, armada.worker, armada.setup.cli, armada.admin.cli, armada.client.cli'

echo "3. git"
check "git is 2.32 or newer (GIT_CONFIG_GLOBAL, which the project's history copy relies on)" bash -c 'v=$(git --version | cut -d" " -f3); [ "$(printf "%s\n2.32\n" "$v" | sort -V | head -1)" = "2.32" ]'
check "git honours GIT_CONFIG_GLOBAL as the project uses it" bash -c 'f=$(mktemp); printf "[safe]\n\tdirectory = *\n" > $f; GIT_CONFIG_GLOBAL=$f git config --global --get safe.directory | grep -qx "\*"'

CFG=$(cat <<'PY'
from armada import config
cfg = config.Config(config.validate({"server": {"admin_user": "me", "host": "h.example"}, "github": {"owner": "o"}, "access": {"sudo": "readonly-nopasswd"}, "transport": {"kind": "direct"}})[0])
PY
)
echo "4. sudoers"
check "the sudoers file the project writes passes visudo -c" bash -c "python3 - <<'PY'
$CFG
from armada import render
text = render.sudoers_text(cfg, '/src/templates')
assert text, 'no sudoers text'
open('/tmp/sudoers.test', 'w').write(text)
PY
chmod 440 /tmp/sudoers.test && visudo -c -f /tmp/sudoers.test"

echo "5. systemd units"
python3 - <<PY
$CFG
from armada import render
import pathlib
d = pathlib.Path("/tmp/units"); d.mkdir(exist_ok=True)
for name, text in render.unit_files(cfg, "/src/templates").items():
    (d / name).write_text(text)
PY
useradd -r -M -s /usr/sbin/nologin armworker 2>/dev/null; mkdir -p /opt/armada/lib /etc/armada /var/lib/armada
check "every unit the project writes parses (only accounts and programs a container lacks are ignored)" bash -c '
  cd /tmp/units; out=$(systemd-analyze verify --man=no ./*.service ./*.timer ./*.socket 2>&1 | grep -viE "is not executable|No such file or directory|Failed to (create|connect|open)|Cannot find unit|org.freedesktop|bus|/proc|cgroup|Unit .* not found|not found|Failed to determine user|transient|Unknown user|Failed to look up|aliased" | grep -E "[A-Za-z]" || true)
  [ -z "$out" ] || { echo "$out"; exit 1; }'

echo "6. sshd"
ssh-keygen -A >/dev/null 2>&1; mkdir -p /run/sshd
python3 - <<PY
$CFG
from armada.setup import hardening
cfg_ = cfg
print(hardening.MODULES["ssh-keys-only"].text(cfg_), end="")
PY
python3 - > /tmp/sshd_dropin.conf <<PY
$CFG
from armada.setup import hardening
print(hardening.MODULES["ssh-keys-only"].text(cfg), end="")
PY
check "the SSH drop-in the project writes is accepted by sshd -t" bash -c 'cp /etc/ssh/sshd_config /tmp/sshd_probe && printf "Include /tmp/sshd_dropin.conf\n" | cat - /tmp/sshd_probe > /tmp/sshd_probe2 && sshd -t -f /tmp/sshd_probe2'

echo "7. apparmor"
if [ -f /etc/apparmor.d/abi/4.0 ] && command -v apparmor_parser >/dev/null; then
  python3 - > /tmp/arm-bwrap.profile <<PY
from armada.setup import hardening
print(hardening.JobIsolation.PROFILE_TEXT.format(bwrap="/usr/bin/bwrap"), end="")
PY
  check "the AppArmor profile the project writes for bubblewrap compiles" bash -c 'apparmor_parser -Q -K /tmp/arm-bwrap.profile'
else
  note "this release has no AppArmor 4 ABI file, so the project writes no profile here (the job-isolation module only plans one where /etc/apparmor.d/abi/4.0 exists)"
fi

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
