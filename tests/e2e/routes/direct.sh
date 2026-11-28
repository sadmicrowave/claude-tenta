#!/bin/bash
# Route: a fresh Direct install, with no GitHub anywhere (nothing of it is even set up on this machine): the worker keeps a mailbox, this computer reaches it over SSH with one key made for
# that. It checks the install, a job through the loop, and what the mailbox key must NOT be able to do.
export NO_GITHUB=1
. "$(dirname "$0")/../lib.sh"
MAIL=$NAME-mail; export MAIL

step "Prepare the machine"
prepare_system
prepare_checkout

step "arm-setup install with the Direct connection"
as_admin arm-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set transport.kind=direct --set network.ssh_from=$SSH_FROM >/tmp/e2e-install.out 2>&1; rc=$?
tail -45 /tmp/e2e-install.out
check "the install command finished successfully" test $rc -eq 0
check "no error line was printed" bash -c "! grep -E '^ *(ERROR|FAILED) ' /tmp/e2e-install.out"
check "no GitHub was needed: no gh, no deploy keys, no github clone urls" bash -c "! grep -qi 'deploy key' /tmp/e2e-install.out && as_admin git -C $H/$ADMIN/.armada/queue remote get-url origin | grep -q 'ssh://$MAIL@'"
check "the mailbox exists with the queue and results repositories" bash -c "test -d /var/lib/$NAME-mail/queue.git && test -d /var/lib/$NAME-mail/results.git"
check "the registry was seeded in the queue" rgit -C /var/lib/$NAME-mail/queue.git cat-file -e main:registry.yaml
check "the timers are running and nothing failed" bash -c "systemctl is-active $NAME-fetch.timer $NAME-step.timer | sort -u | grep -qx active && ! systemctl --failed --no-legend | grep -q $NAME"

step "A job through the whole loop, over SSH"
as_admin arm-run setup >/dev/null 2>&1
as_admin arm-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1; rc=$?
check "arm-run submit sys-status came back with a result" test $rc -eq 0
# the step timer's first pass comes within its own interval of the job finishing: a faster machine (this one, now) reaches this line before it, so wait for it rather than assume it has run
check "arm-admin status reads the worker and fetch and step are healthy" bash -c 'for _ in $(seq 30); do out=$(runuser -u '$ADMIN' -- env PATH=/opt/ctvenv/bin:/usr/local/bin:/usr/bin:/bin HOME='$H/$ADMIN' arm-admin status 2>&1); echo "$out" | grep -qE "Fetch +OK" && echo "$out" | grep -qE "Step +OK" && exit 0; sleep 3; done; echo "$out"; exit 1'

step "A set over 1 GB goes compressed over SSH, and the real worker unpacks it (what unit tests cannot show: the real size, memory, disk and terminal)"
# no shipped class takes files, so add one to the installed registry for this machine only; the worker republishes registry.json when it changes
/opt/ctvenv/bin/python - "$NAME" <<'PY'
import sys, yaml
p = f"/etc/{sys.argv[1]}/registry.yaml"
reg = yaml.safe_load(open(p))
reg["classes"]["bigfiles"] = {"tier": "auto", "cost": "light", "accepts_files": True, "timeout_s": 120, "run": ["/usr/bin/wc", "-c"] + [f"f{i:02d}.txt" for i in range(11)]}
open(p, "w").write(yaml.safe_dump(reg, sort_keys=False))
PY
check "the worker published that it can unpack compressed files" wait_for 40 bash -c 'as_admin arm-run classes 2>&1 | grep -q bigfiles && as_admin python3 -c "import json,sys; sys.exit(0 if json.load(open(\"$H/$ADMIN/.armada/results/registry.json\")).get(\"compress\") else 1)"'
install -d -o $ADMIN /tmp/big
for i in $(seq 0 10); do head -c 100000000 /dev/zero | tr '\0' 'a' > /tmp/big/$(printf 'f%02d.txt' $i); done
chown -R $ADMIN /tmp/big; free -m | sed 's/^/  /'; df -h /var/lib | sed 's/^/  /'
RSS="python3 -c 'import resource,subprocess,sys; r=subprocess.call(sys.argv[1:]); print(\"CLIENT PEAK MEMORY MB\", resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss//1024); sys.exit(r)'"
as_admin script -qec "stty cols 100 rows 40; $RSS arm-run submit bigfiles --dir /tmp/big --wait --timeout 600" /tmp/e2e-big.out >/dev/null 2>&1; rc=$?
plain() { sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' "$1" | tr '\r' '\n' | grep -av 'CHECKING\|SENDING\|COMPRESSING\|waiting for\|^ *$'; }; export -f plain      # what the person reads, without the colours and the progress line
plain /tmp/e2e-big.out | sed 's/^/  | /'
check "the run finished" test $rc -eq 0
check "the card says compressed, with the sizes before and after" bash -c "plain /tmp/e2e-big.out | grep -aiE 'compressed +1\.[0-9] GB to [0-9.]+ (MB|KB)'"
check "the old over-1 GB notice is gone" bash -c "! plain /tmp/e2e-big.out | grep -aqi 'very large set'"
check "the worker unpacked every file whole and ran the job on them" bash -c "grep -a -c '100000000' /tmp/e2e-big.out | grep -qx -E '[1-9][0-9]*' && grep -aq 1100000000 /tmp/e2e-big.out"
check "the job file in the mailbox is small, not a gigabyte" bash -c "test \$(rgit -C /var/lib/$NAME-mail/queue.git ls-tree -r -l jobs | awk '{s+=\$4} END {print s}') -lt 50000000"
check "the unpacked files were cleaned up or kept within the disk reserve" bash -c "df --output=avail -B1G /var/lib | tail -1 | awk '{exit \$1 < 2}'"
as_admin script -qec "stty cols 100 rows 40; arm-run submit bigfiles --dir /tmp/big --no-compress --no-wait" /tmp/e2e-big2.out >/dev/null 2>&1
plain /tmp/e2e-big2.out | sed 's/^/  | /'
check "--no-compress says so on the card" bash -c "plain /tmp/e2e-big2.out | grep -aiq 'not compressed'"

step "What the mailbox key must NOT be able to do"
KEY=$H/$ADMIN/.armada/mailbox/key; export KEY
MSSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $MAIL@localhost"
check "it cannot get a shell or run an arbitrary command" bash -c "! as_admin $MSSH id 2>&1 | grep -q uid="
push_results() { local d; d=$(mktemp -d); chmod 777 $d; as_admin env GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes" bash -c "cd $d && git clone -q ssh://$MAIL@localhost/var/lib/$NAME-mail/results.git r && cd r && git -c user.name=x -c user.email=x@x commit --allow-empty -qm x && git push origin HEAD:refs/heads/probe"; }
check "it can read the results" bash -c "d=\$(mktemp -d); chmod 777 \$d; as_admin env GIT_SSH_COMMAND='ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes' git clone -q ssh://$MAIL@localhost/var/lib/$NAME-mail/results.git \$d/r"
check "it cannot push to the results repository" bash -c "$(declare -f push_results); export KEY MAIL NAME; ! push_results"
check "it cannot use scp or sftp" bash -c "! as_admin scp -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes $MAIL@localhost:/etc/passwd /tmp/x 2>&1 | grep -q root"
check "the mailbox account has no password and its key file is the only way in" bash -c "passwd -S $MAIL | awk '{print \$2}' | grep -qx -E 'L|NP' && grep -c ssh- /var/lib/$NAME-mail/.ssh/authorized_keys | grep -qx 1"
check "the worker's own account never connects out" bash -c "! ls $H/$WORKER/.ssh/id_* 2>/dev/null | grep -q ."

finish
