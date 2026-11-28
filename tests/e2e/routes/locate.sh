#!/bin/bash
# Route: a Direct worker whose address changed (docs/design/DISCOVERY.md). The one machine plays both: the worker's sshd answers on a NEW address that is NOT one of this computer's own addresses (it lives
# in a network namespace joined to this computer by a virtual cable, because a scan always leaves out this computer's own addresses, secondary ones included: found on the first real run), while this
# computer's configuration and known_hosts still say the OLD address, which nothing answers. `arm-admin locate` must find the worker by its SSH host key, say which
# rung found it, stop scanning at the first match, save the new address (keeping the old one as a comment), and a second run must find it at the address in the configuration.
export NO_GITHUB=1
. "$(dirname "$0")/../lib.sh"
OLD=198.51.100.9          # the address the worker used to have: nothing answers here
ME=192.0.2.2              # this computer's own address on its end of the virtual cable (never scanned: it is "this computer")
NEW=192.0.2.50            # the worker's address now: the other end of the cable, answered by a real sshd with the worker's own host keys
ARM_TOML=$H/$ADMIN/.config/armada/armada.toml

step "Prepare the machine and install a Direct worker"
prepare_system
prepare_checkout
as_admin arm-setup install --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set transport.kind=direct --set network.ssh_from=$SSH_FROM >/tmp/e2e-install.out 2>&1; rc=$?
tail -8 /tmp/e2e-install.out
check "the install command finished successfully" test $rc -eq 0

step "Two addresses on one machine: the worker moves from the old address to the new one"
check "a virtual cable joins this computer ($ME) to a second network space that holds the worker's new address ($NEW)" bash -c "ip netns add wk && ip link add veth-ct0 type veth peer name veth-ct1 && ip link set veth-ct1 netns wk && ip addr add $ME/24 dev veth-ct0 && ip link set veth-ct0 up && ip netns exec wk ip addr add $NEW/24 dev veth-ct1 && ip netns exec wk ip link set veth-ct1 up && ip netns exec wk ip link set lo up"
check "a real sshd with the worker's own host keys runs in that space" bash -c "ip netns exec wk /usr/sbin/sshd -o PidFile=/run/sshd-wk.pid"
check "the real sshd answers on the new address with the worker's key" bash -c "ssh-keyscan -T 3 -t ed25519 $NEW 2>/dev/null | grep -q ssh-ed25519"
# this computer recorded the worker under its OLD address, hashed (the way ssh does by default), and its configuration still names the old address
install -d -m 700 -o $ADMIN $H/$ADMIN/.ssh
ssh-keyscan -T 3 -t ed25519 $NEW 2>/dev/null | grep -v '^#' | sed "s/^[^ ]*/$OLD/" > $H/$ADMIN/.ssh/known_hosts
as_admin ssh-keygen -H -f $H/$ADMIN/.ssh/known_hosts >/dev/null 2>&1; rm -f $H/$ADMIN/.ssh/known_hosts.old; chown $ADMIN $H/$ADMIN/.ssh/known_hosts
sed -i "s/^host = \"localhost\".*/host = \"$OLD\"/" $ARM_TOML
check "the configuration names the old address and known_hosts has no readable address" bash -c "grep -q 'host = \"$OLD\"' $ARM_TOML && ! grep -q $OLD $H/$ADMIN/.ssh/known_hosts"

step "arm-admin locate finds it by its key, stops at the first match and saves the new address"
S=$(date +%s)
as_admin script -qec "stty cols 100 rows 40; arm-admin locate --yes" /tmp/e2e-locate.out >/dev/null 2>&1; rc=$?
T=$(( $(date +%s) - S ))
plain() { sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' "$1" | tr '\r' '\n' | grep -av '^ *$'; }; export -f plain
plain /tmp/e2e-locate.out | sed 's/^/  | /'; echo "  locate took ${T}s"
check "it finished successfully" test $rc -eq 0
check "the card says a worker was found at the new address" bash -c "plain /tmp/e2e-locate.out | grep -ai 'address' | grep -q $NEW"
check "it says it was found by a scan, by the key" bash -c "plain /tmp/e2e-locate.out | grep -ai 'found by' | grep -qi scan"
check "the new address was saved and the old one is kept as a comment" bash -c "grep -q 'host = \"$NEW\"  # was: $OLD' $ARM_TOML"
check "the scan did not wait for the whole network (the real network is scanned first and then the virtual one, both within this limit)" test $T -lt 60

step "A second run finds the worker where the configuration now says"
# locate saved the address but the key is recorded only under the old one: the person connects once to the new address (the command the card gives), as ssh does for any new address
check "connecting once to the new address records its key" bash -c "as_admin ssh -o BatchMode=yes $ADMIN@$NEW true && as_admin ssh-keygen -F $NEW >/dev/null"
as_admin script -qec "stty cols 100 rows 40; arm-admin locate --yes" /tmp/e2e-locate2.out >/dev/null 2>&1; rc=$?
plain /tmp/e2e-locate2.out | sed 's/^/  | /'
check "it finished successfully" test $rc -eq 0
check "it says the worker is where its configuration says" bash -c "plain /tmp/e2e-locate2.out | grep -qi 'where its configuration says'"

step "Another machine at the new address is refused (a different key)"
ssh-keygen -t ed25519 -N "" -f /tmp/other -q
awk -v d=$OLD '{print d" "$1" "$2}' /tmp/other.pub > $H/$ADMIN/.ssh/known_hosts; chown $ADMIN $H/$ADMIN/.ssh/known_hosts
sed -i "s/^host = \"$NEW\".*/host = \"$OLD\"/" $ARM_TOML
as_admin script -qec "stty cols 100 rows 40; arm-admin locate --yes" /tmp/e2e-locate3.out >/dev/null 2>&1; rc=$?
plain /tmp/e2e-locate3.out | sed 's/^/  | /'
check "it was not found, and the configuration still names the old address" bash -c "test $rc -ne 0 && grep -q 'host = \"$OLD\"' $ARM_TOML && plain /tmp/e2e-locate3.out | grep -qi 'not found'"
kill $(cat /run/sshd-wk.pid) 2>/dev/null; ip netns del wk 2>/dev/null; ip link del veth-ct0 2>/dev/null
finish
