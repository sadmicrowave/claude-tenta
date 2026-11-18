#!/bin/bash
# Not a route: builds the shared starting point (a checkout installed and a GitHub install that works, with a first job) once per run, on a machine scripts/e2e-routes then clones for every route
# that is about something else (move, rename, switch, uninstall). The marker is written only when every check here passed, so a broken baseline can never be mistaken for a good one.
. "$(dirname "$0")/lib.sh"
step "Prepare the machine"; prepare_system; prepare_checkout
baseline_github
[ "$FAIL" = 0 ] && touch "$BASELINE_MARK"
finish
