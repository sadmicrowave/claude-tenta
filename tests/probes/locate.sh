#!/bin/bash
# Probe: does finding a moved worker by its SSH host key work with the REAL tools of this release (scripts/probe locate, or --releases locate for all four)? A real sshd shows its keys, the real
# ssh-keyscan reads them, a HASHED known_hosts entry (the default on many systems) is looked up with the real ssh-keygen -F, and the project's own ladder (/src) matches them:
#   1. a worker still at its configured address is proved by its key alone (no scan);
#   2. a worker whose recorded address is dead (a documentation address nothing answers) is found at its new address by a scan, by key;
#   3. a different machine (another key) at the new address is refused, never accepted on its name or address alone;
#   4. a worker this computer never connected to cannot be found (nothing is guessed).
set -uo pipefail
PASS=0; FAIL=0
check() { local what=$1; shift; if "$@" >/tmp/probe-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/probe-check.out | tail -8; fi; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq python3 python3-yaml openssh-server openssh-client >/dev/null 2>&1
echo "$(. /etc/os-release; echo "$PRETTY_NAME"), $(ssh -V 2>&1)"
export PYTHONPATH=/src/src PYTHONDONTWRITEBYTECODE=1
mkdir -p /run/sshd && ssh-keygen -A >/dev/null 2>&1 && /usr/sbin/sshd && sleep 1
DEAD=192.0.2.77
# this computer recorded the worker under its OLD address, hashed, the way ssh does by default
mkdir -p /root/.ssh && chmod 700 /root/.ssh
ssh-keyscan -T 3 -t ed25519 127.0.0.1 2>/dev/null | grep -v "^#" | sed "s/^[^ ]*/$DEAD/" > /root/.ssh/known_hosts
ssh-keygen -H -f /root/.ssh/known_hosts >/dev/null 2>&1; rm -f /root/.ssh/known_hosts.old
check "the recorded entry is hashed (the old address is not readable in the file)" bash -c "! grep -q $DEAD /root/.ssh/known_hosts || { cut -c1-70 /root/.ssh/known_hosts; false; }"
ladder() {   # $1 configured host, $2 name of the case; prints "<kind> <rung> <addresses>"
python3 - "$1" <<'PY'
import sys
from ct import rediscover
from ct.setup import discover
host = sys.argv[1]
m, rung, _ = rediscover.ladder(host, 22, "w", "/tmp/home", scan_real=lambda until=None: ([discover.Device("127.0.0.1")], []), scan_virtual=lambda until=None: ([], []), mdns=lambda h: None, arp=lambda: {})
print(m.kind, "|", rung, "|", " ".join(m.addresses))
PY
}
ssh-keyscan -T 3 -t ed25519 127.0.0.1 2>/dev/null | grep -v '^#' > /root/.ssh/known_hosts; ssh-keygen -H -f /root/.ssh/known_hosts >/dev/null 2>&1; rm -f /root/.ssh/known_hosts.old
check "a worker still at its address is proved by its key, with no scan" bash -c "$(declare -f ladder); out=\$(ladder 127.0.0.1); echo \"\$out\"; echo \"\$out\" | grep -q '^one | the configured address | 127.0.0.1'"
ssh-keyscan -T 3 -t ed25519 127.0.0.1 2>/dev/null | grep -v "^#" | sed "s/^[^ ]*/$DEAD/" > /root/.ssh/known_hosts; ssh-keygen -H -f /root/.ssh/known_hosts >/dev/null 2>&1; rm -f /root/.ssh/known_hosts.old
check "a moved worker is found by a scan, by its key, through a hashed known_hosts entry" bash -c "$(declare -f ladder); out=\$(ladder $DEAD); echo \"\$out\"; echo \"\$out\" | grep -q '^one | a scan of your real networks | 127.0.0.1'"
# another machine: the recorded key is not the one sshd shows
ssh-keygen -t ed25519 -N "" -f /tmp/other -q
awk -v d=$DEAD '{print d" "$1" "$2}' /tmp/other.pub > /root/.ssh/known_hosts
check "a machine showing another key is refused, whatever its name or address" bash -c "$(declare -f ladder); out=\$(ladder $DEAD); echo \"\$out\"; echo \"\$out\" | grep -q '^none'"
rm -f /root/.ssh/known_hosts
check "a worker this computer never connected to is not guessed at" bash -c "$(declare -f ladder); out=\$(ladder $DEAD); echo \"\$out\"; echo \"\$out\" | grep -q '^unknown'"
printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
