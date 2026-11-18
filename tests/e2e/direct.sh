#!/bin/bash
# End-to-end scenario for the Direct connection on a FRESH Ubuntu machine. Run as root (scripts/e2e-orbstack with CT_E2E_SCENARIO=direct).
# There is no GitHub here at all: the worker keeps a mailbox, your computer talks to it over SSH with one key. Exit status is the number of failed checks.
set -uo pipefail
SRC=${SRC:-/opt/ct-src}
H=/home; NAME=claude-tenta; ADMIN=ctadmin; WORKER=ctworker; MAIL=$NAME-mail
export DEBIAN_FRONTEND=noninteractive
PASS=0; FAIL=0
step() { printf '\n=== %s\n' "$*"; }
check() { local what=$1; shift; if "$@" >/tmp/e2e-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/e2e-check.out | tail -12; fi; }
as_admin() { runuser -u $ADMIN -- env "PATH=/opt/ctvenv/bin:/usr/local/bin:/usr/bin:/bin" HOME=$H/$ADMIN "$@"; }
export -f as_admin; export H ADMIN NAME WORKER SRC MAIL
SSH_FROM=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16

step "Prepare the machine"
apt-get update -qq && apt-get install -y -qq git python3 python3-yaml python3-venv python3-pip sudo openssh-server openssh-client iproute2 util-linux >/dev/null
id $ADMIN >/dev/null 2>&1 || adduser --disabled-password --gecos "" $ADMIN >/dev/null
usermod -aG sudo $ADMIN; usermod -p '*' $ADMIN
[ -d $SRC/.git ] || { git -C $SRC init -q && git -C $SRC add -A && git -C $SRC -c user.name=e2e -c user.email=e2e@example.com commit -qm e2e; }
systemctl enable --now ssh >/dev/null 2>&1
runuser -u $ADMIN -- bash -c '[ -f ~/.ssh/id_ed25519 ] || { mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519; }; cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
printf 'Host *\n  StrictHostKeyChecking accept-new\n' > /etc/ssh/ssh_config.d/99-e2e.conf
git config --system safe.directory '*'; git config --system user.name e2e; git config --system user.email e2e@example.com
echo "$ADMIN ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/e2e-$ADMIN; chmod 440 /etc/sudoers.d/e2e-$ADMIN
python3 -m venv --system-site-packages /opt/ctvenv && /opt/ctvenv/bin/pip install -q -e $SRC
chmod -R a+rX /opt/ctvenv $SRC

step "ct-setup install with the Direct connection"
as_admin ct-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set transport.kind=direct --set network.ssh_from=$SSH_FROM >/tmp/e2e-install.out 2>&1; rc=$?
tail -45 /tmp/e2e-install.out
check "the install command finished successfully" test $rc -eq 0
check "no GitHub was needed: no gh, no deploy keys, no github clone urls" bash -c "! grep -qi 'deploy key' /tmp/e2e-install.out && as_admin git -C $H/$ADMIN/.ct/queue remote get-url origin | grep -q 'ssh://$MAIL@'"
check "the mailbox exists with the queue and results repositories" bash -c "test -d /var/lib/$NAME-mail/queue.git && test -d /var/lib/$NAME-mail/results.git"
check "the registry was seeded in the queue" git -C /var/lib/$NAME-mail/queue.git cat-file -e main:registry.yaml
check "the timers are running and nothing failed" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer | sort -u | grep -qx active && ! systemctl --failed --no-legend | grep -q $NAME"

step "A job through the whole loop, over SSH"
as_admin ct-run setup >/dev/null 2>&1
as_admin ct-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "ct-run submit sys-status came back with a result" test $rc -eq 0
check "ct-admin status reads the worker and fetch and step are healthy" bash -c "as_admin ct-admin status 2>&1 | grep -E 'Fetch +OK' && as_admin ct-admin status 2>&1 | grep -E 'Step +OK'"

step "What the mailbox key must NOT be able to do"
KEY=$H/$ADMIN/.ct/mailbox/key; export KEY
MSSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $MAIL@localhost"
check "it cannot get a shell or run an arbitrary command" bash -c "! as_admin $MSSH id 2>&1 | grep -q uid="
push_results() { local d; d=$(mktemp -d); chmod 777 $d; as_admin env GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" bash -c "cd $d && git clone -q ssh://$MAIL@localhost/var/lib/$NAME-mail/results.git r && cd r && git -c user.name=x -c user.email=x@x commit --allow-empty -qm x && git push origin HEAD:refs/heads/probe"; }
check "it can read the results" bash -c "d=\$(mktemp -d); chmod 777 \$d; as_admin env GIT_SSH_COMMAND='ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes' git clone -q ssh://$MAIL@localhost/var/lib/$NAME-mail/results.git \$d/r"
check "it cannot push to the results repository" bash -c "$(declare -f push_results); export KEY MAIL NAME; ! push_results"
check "it cannot use scp or sftp" bash -c "! as_admin scp -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $MAIL@localhost:/etc/passwd /tmp/x 2>&1 | grep -q root"
check "the mailbox account has no password and its key file is the only way in" bash -c "passwd -S $MAIL | awk '{print \$2}' | grep -qx -E 'L|NP' && grep -c ssh- /var/lib/$NAME-mail/.ssh/authorized_keys | grep -qx 1"
check "the worker's own account never connects out" bash -c "! ls $H/$WORKER/.ssh/id_* 2>/dev/null | grep -q ."

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
