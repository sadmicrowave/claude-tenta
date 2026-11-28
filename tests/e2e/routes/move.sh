#!/bin/bash
# Route: moving an install from GitHub to Direct with the move command keeps the history and everything still works.
. "$(dirname "$0")/../lib.sh"
step "Prepare the machine"; prepare_system; prepare_checkout
baseline_github

step "Moving the install from GitHub to Direct keeps the history and everything still works"
as_admin arm-setup transport direct >/tmp/e2e-transport-preview.out 2>&1
check "the preview says what it would do and changes nothing" bash -c "grep -q PREVIEW /tmp/e2e-transport-preview.out && ! test -d /var/lib/$NAME-mail"
as_admin arm-setup transport direct --apply --yes --no-sandbox-test --source $SRC >/tmp/e2e-transport.out 2>&1; rc=$?
tail -25 /tmp/e2e-transport.out
check "the move finished successfully" test $rc -eq 0
check "the mailbox holds the registry that was on GitHub (history copied)" rgit -C /var/lib/$NAME-mail/queue.git cat-file -e main:registry.yaml
check "both clones now point at the mailbox" bash -c "rgit -C /var/lib/$NAME/queue remote get-url origin | grep -q '$NAME-mail' && as_admin git -C $H/$ADMIN/.armada/queue remote get-url origin | grep -q 'ssh://$NAME-mail@'"
check "the GitHub repositories were not touched" rgit -C $H/git/$OWNER/$NAME-queue.git cat-file -e main:registry.yaml
check "the job loop is running again and nothing failed" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer | sort -u | grep -qx active && ! systemctl --failed --no-legend | grep -q $NAME"
check "a job goes through the whole loop over the mailbox" bash -c "as_admin arm-run submit sys-status --wait --timeout 240 >/tmp/e2e-job-direct.out 2>&1"
as_admin arm-setup transport direct >/tmp/e2e-again.out 2>&1; sed "s/\x1b\[[0-9;]*m//g" /tmp/e2e-again.out | head -12
check "running the move again says the install already uses that method, Direct" bash -c "sed 's/\x1b\[[0-9;]*m//g' /tmp/e2e-again.out | grep -qi 'already uses that method'"
finish
