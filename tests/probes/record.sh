#!/bin/bash
# Recorder (not a probe): runs, on one real release inside a container, the commands the project reads the output of, and saves what they REALLY print to /out/<name>.txt.
# `scripts/record-fixtures` runs it on every supported release and puts the files in tests/fixtures/real/<release>/. Unit tests then parse these real outputs, so a parser is never tested only
# against what its author imagined a tool prints. Re-record when a release changes (scripts/record-fixtures) and read the diff.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq git python3 python3-yaml sudo openssh-client bubblewrap apparmor systemd util-linux >/dev/null 2>&1
mkdir -p /out
cp /etc/os-release /out/os-release.txt
{ sudo --version 2>&1 | head -2; } > /out/sudo-version.txt
git --version > /out/git-version.txt 2>&1
python3 --version > /out/python-version.txt 2>&1
ssh -V > /out/ssh-version.txt 2>&1
bwrap --version > /out/bwrap-version.txt 2>&1
apparmor_parser --version 2>&1 | head -1 > /out/apparmor-version.txt
systemctl --version 2>&1 | head -1 > /out/systemd-version.txt
# what the candidacy check reads from a machine, exactly as the project's own script prints it here
python3 - > /out/assess-script.sh <<'PY'
import sys
sys.path.insert(0, "/src/src")
from ct.setup import assess
print(assess.SCRIPT)
PY
sh /out/assess-script.sh 2>/dev/null | sed -E 's/^(KERNEL|HOSTNAME)=.*/\1=recorded-elsewhere/' > /out/assess-output.txt
rm /out/assess-script.sh
# git's real refusal of a repository that belongs to another account (the message the project recognises)
useradd -m worker >/dev/null 2>&1; useradd -m other >/dev/null 2>&1
runuser -u worker -- git init -q --bare /home/worker/r.git 2>/dev/null; chmod -R a+rX /home/worker
runuser -u other -- git upload-pack --advertise-refs /home/worker/r.git > /out/git-dubious-ownership.txt 2>&1
echo "recorded on $(. /etc/os-release; echo "$PRETTY_NAME")"
