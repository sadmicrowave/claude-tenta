#!/bin/bash
# Probe: the fixes the candidacy check tells a person to type on a machine (ct.setup.assess.fix_*) really work on THIS release, run exactly as printed, as the account that would run them.
# Found by a person: on Debian 13 `su -c 'usermod ...'` said "usermod: command not found" (su without the dash keeps the account's own PATH, which has no /usr/sbin). Runs in a throwaway container as root
# (scripts/probe fixes; scripts/probe --releases fixes for every supported release); the checkout is mounted read-only at /src.
set -uo pipefail
PASS=0; FAIL=0
check() { local what=$1; shift; if "$@" >/tmp/probe-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/probe-check.out | tail -8; fi; }
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq python3 util-linux passwd >/dev/null 2>&1
. /etc/os-release; echo "$PRETTY_NAME"
FAMILY=ubuntu; [ "$ID" = debian ] && FAMILY=debian
fix() { PYTHONPATH=/src/src python3 -c "from ct.setup import assess; print(assess.$1)" | sed 's/^[^:]*: //'; }    # the command part of the text the person is shown (after the last colon)
CMD_SUDO_MISSING=$(fix "fix_no_sudo('$FAMILY','agent')"); CMD_GROUP=$(fix "fix_not_in_sudo_group('$FAMILY','agent')"); CMD_PY=$(fix "fix_python('$FAMILY', False)")
echo "  no sudo:        $CMD_SUDO_MISSING"; echo "  not in group:   $CMD_GROUP"; echo "  no python:      $CMD_PY"
useradd -m -s /bin/bash agent; passwd -d root >/dev/null 2>&1                                       # root's password is empty here so `su` can be driven without a person
P=/usr/local/bin:/usr/bin:/bin:/usr/games                                                           # an ordinary account's PATH: no /usr/sbin
as_agent() { runuser -u agent -- env PATH=$P script -qec "$1" /dev/null; }                           # on a terminal, as the account itself
in_group() { id -nG agent | tr ' ' '\n' | grep -qx sudo; }
python_ok() { python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; }

if [ "$FAMILY" = debian ]; then
  check "the account has no sudo: the printed command installs it and adds the account to its group" bash -c "$(declare -f as_agent); P=$P; as_agent \"$CMD_SUDO_MISSING\" && command -v sudo && id -nG agent | tr ' ' '\n' | grep -qx sudo"
  gpasswd -d agent sudo >/dev/null 2>&1
  check "the account is not in the group: the printed command adds it" bash -c "$(declare -f as_agent); P=$P; as_agent \"$CMD_GROUP\" && id -nG agent | tr ' ' '\n' | grep -qx sudo"
  check "the OLD form (su -c without the dash) really fails here, which is why the dash is needed" bash -c "gpasswd -d agent sudo >/dev/null 2>&1; $(declare -f as_agent); P=$P; as_agent \"su -c 'usermod -aG sudo agent'\" | grep -q 'command not found' && ! id -nG agent | tr ' ' '\n' | grep -qx sudo"
  apt-get remove -y -qq python3 python3-minimal >/dev/null 2>&1; apt-get autoremove -y -qq >/dev/null 2>&1
  check "no python: the printed command installs one that is 3.11 or newer" bash -c "$(declare -f as_agent); P=$P; as_agent \"$CMD_PY\" && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'"
else
  check "the account has no sudo: the printed command (as root, at the console) installs it and adds the account" bash -c "$CMD_SUDO_MISSING && id -nG agent | tr ' ' '\n' | grep -qx sudo && command -v sudo"
  useradd -m -G sudo admin; echo 'admin ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/probe-admin; gpasswd -d agent sudo >/dev/null 2>&1
  check "the account is not in the group: the printed command, run by an account that has sudo, adds it" runuser -u admin -- bash -c "$CMD_GROUP && id -nG agent | tr ' ' '\n' | grep -qx sudo"
  check "no python: the printed command, run by an account that has sudo, installs one that is 3.11 or newer" runuser -u admin -- bash -c "sudo apt-get remove -y -qq python3 python3-minimal >/dev/null 2>&1; $CMD_PY && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'"
fi
echo "=== $PASS passed, $FAIL failed"; exit $FAIL
