#!/bin/bash
# Route: a fresh Direct install, with no GitHub anywhere (nothing of it is even set up on this machine): the worker keeps a mailbox, this computer reaches it over SSH with one key made for
# that. It checks the install, a job through the loop, and what the mailbox key must NOT be able to do.
export NO_GITHUB=1
. "$(dirname "$0")/../lib.sh"
MAIL=$NAME-mail; export MAIL

step "Prepare the machine"
prepare_system
prepare_checkout

step "ct-setup install with the Direct connection"
as_admin ct-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set transport.kind=direct --set network.ssh_from=$SSH_FROM >/tmp/e2e-install.out 2>&1; rc=$?
tail -45 /tmp/e2e-install.out
check "the install command finished successfully" test $rc -eq 0
check "no error line was printed" bash -c "! grep -E '^ *(ERROR|FAILED) ' /tmp/e2e-install.out"
check "no GitHub was needed: no gh, no deploy keys, no github clone urls" bash -c "! grep -qi 'deploy key' /tmp/e2e-install.out && as_admin git -C $H/$ADMIN/.ct/queue remote get-url origin | grep -q 'ssh://$MAIL@'"
check "the mailbox exists with the queue and results repositories" bash -c "test -d /var/lib/$NAME-mail/queue.git && test -d /var/lib/$NAME-mail/results.git"
check "the registry was seeded in the queue" rgit -C /var/lib/$NAME-mail/queue.git cat-file -e main:registry.yaml
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

finish
