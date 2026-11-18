#!/bin/bash
# Harness self-test, failing case: the machine must still be deleted and its logs saved.
echo "smoke failing" | tee /tmp/e2e-smoke.out
exit 3
