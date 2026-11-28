#!/bin/bash
# Route: setting up again with the OTHER connection through the wizard (not the move command), in both directions, and jobs still go through.
. "$(dirname "$0")/../lib.sh"
step "Prepare the machine"; prepare_system; prepare_checkout
baseline_github

step "Setting up again with the other connection (the wizard): Direct, then GitHub, then Direct, and jobs still go through"
install_github --set transport.kind=direct >/tmp/e2e-switch-direct1.out 2>&1; rc=$?
tail -12 /tmp/e2e-switch-direct1.out
check "setting up again for Direct finished successfully" test $rc -eq 0
check "no error line was printed (seen on a real machine: deploy key errors against github owner None)" bash -c "! grep -E '^ *(ERROR|FAILED) ' /tmp/e2e-switch-direct1.out"
check "both of the worker's clones and both of this computer's clones now point at the mailbox" bash -c "rgit -C /var/lib/$NAME/queue remote get-url origin | grep -q '$NAME-mail' && as_admin git -C $H/$ADMIN/.armada/queue remote get-url origin | grep -q 'ssh://$NAME-mail@'"
check "the registry survived into the mailbox" rgit -C /var/lib/$NAME-mail/queue.git cat-file -e main:registry.yaml
check "a job goes through over the mailbox within 30 seconds (a Direct worker never slows down)" bash -c "as_admin arm-run submit sys-status --wait --timeout 30 >/tmp/e2e-job-switch1.out 2>&1"
install_github --set transport.kind=github >/tmp/e2e-switch-github.out 2>&1; rc=$?
tail -12 /tmp/e2e-switch-github.out
check "setting up again for GitHub finished successfully" test $rc -eq 0
check "no error line was printed (seen once: deploy key errors on a switch)" bash -c "! grep -E '^ *(ERROR|FAILED) ' /tmp/e2e-switch-github.out"
check "both of the worker's clones and both of this computer's clones now point at GitHub" bash -c "rgit -C /var/lib/$NAME/queue remote get-url origin | grep -q github && as_admin git -C $H/$ADMIN/.armada/queue remote get-url origin | grep -q github"
check "a job goes through over GitHub after the switch" bash -c "as_admin arm-run submit sys-status --wait --timeout 240 >/tmp/e2e-job-switch2.out 2>&1"
install_github --set transport.kind=direct >/tmp/e2e-switch-direct2.out 2>&1; rc=$?
check "and back to Direct finished successfully" test $rc -eq 0
check "a job goes through over the mailbox again" bash -c "as_admin arm-run submit sys-status --wait --timeout 30 >/tmp/e2e-job-switch3.out 2>&1"
check "the worker's pass is the quick one, not the idle one" bash -c "! journalctl -u $NAME-fetch.service --no-pager -o cat | tail -20 | grep -qi 'idle'"
finish
