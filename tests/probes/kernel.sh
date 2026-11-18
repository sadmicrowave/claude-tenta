#!/bin/bash
# Probe: the facts only a REAL kernel and a REAL installed worker can show. Read-only: it changes nothing, needs no password, and is safe on a worker that is running. Run it on a real machine with
#   scripts/probe --on user@host kernel
# (or in a container, where the kernel facts are simply reported as not available). With the project's own code (at $CT_PROBE_ROOT, or /src in a container):
#   1. the jail the worker runs every job in can be built by an unprivileged account (the check that failed with `Failed RTM_NEWADDR` on Ubuntu 24.04 until the AppArmor profile existed);
#   2. the kernel and AppArmor settings that decide that, reported;
#   3. an installed worker's timers got the accuracy the project asks for (systemd ignores a setting it cannot parse, silently);
#   4. nothing but SSH listens for TCP connections from other machines.
set -uo pipefail
ROOT=${CT_PROBE_ROOT:-/src}
PASS=0; FAIL=0
check() { local what=$1; shift; if "$@" >/tmp/probe-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/probe-check.out | tail -8; fi; }
note() { printf '  NOTE  %s\n' "$1"; }
export PYTHONPATH=$ROOT/src PYTHONDONTWRITEBYTECODE=1
echo "$(uname -sr), $(. /etc/os-release; echo "$PRETTY_NAME")"

echo "1. the jail"
if command -v bwrap >/dev/null && command -v python3 >/dev/null; then
  note "bubblewrap $(bwrap --version | cut -d' ' -f2), mode $(stat -c %a "$(command -v bwrap)")"
  check "an unprivileged account can build the project's jail (own network, loopback only)" bash -c "python3 - <<'PY'
import subprocess, sys
from ct import jail
argv = jail.Bwrap(path='$(command -v bwrap)', hide=[]).probe_argv('/tmp')
r = subprocess.run(argv, capture_output=True, text=True)
if r.returncode:
    print(r.stdout.strip()[-200:], r.stderr.strip()[-200:]); sys.exit(1)
names = [l.split(':')[0].strip() for l in r.stdout.splitlines()[2:] if ':' in l]
sys.exit(0 if names == ['lo'] else 1)
PY"
else
  note "bubblewrap or python3 is not installed here: the jail check is skipped"
fi

echo "2. kernel and AppArmor"
note "kernel.apparmor_restrict_unprivileged_userns = $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 'not present (this kernel has no such restriction)')"
if [ -f /etc/apparmor.d/ct-bwrap ]; then note "the project's AppArmor profile for bubblewrap is installed (/etc/apparmor.d/ct-bwrap)"; else note "no ct-bwrap AppArmor profile on this machine"; fi
note "auditd: $(systemctl is-active auditd 2>/dev/null || echo 'not running')"

echo "3. an installed worker's timers"
timers=$(systemctl list-units --all --no-legend --plain 'claude-tenta-*.timer' '*-fetch.timer' 2>/dev/null | awk '{print $1}' | grep -E -- '-(fetch|step)\.timer$' | sort -u)
if [ -z "$timers" ]; then
  note "no worker is installed here: the timer check is skipped"
else
  for t in $timers; do
    check "$t has the one-second accuracy the project asks for (systemd's default is a minute)" bash -c "v=\$(systemctl show -p AccuracyUSec --value $t); echo \"AccuracyUSec is \$v\"; [ \"\$v\" = '1s' ]"
  done
fi

echo "4. what listens"
if command -v ss >/dev/null; then
  check "nothing but SSH listens for TCP connections from other machines" bash -c "! ss -Hltn 2>/dev/null | awk '{print \$4}' | grep -vE '^(127\\.|\\[::1\\]|::1)' | grep -vE ':22\$' | grep -q ."
fi

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
