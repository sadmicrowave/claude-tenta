#!/bin/bash
# Shared parts of the end-to-end routes (tests/e2e/routes/*.sh). Source it; run as root on a Debian or Ubuntu machine. It holds what every route needs: the steps and checks (with a clock
# on each, so the slow parts are known and not guessed), the helpers, the machine preparation (split so it can be baked into a cached base machine), the GitHub stand-in and the baseline installs.
# GitHub is simulated faithfully: github.com resolves to this machine, where an SSH user called git holds the two bare repositories under someone/. Every clone, fetch and push goes over
# SSH with real keys (the worker's own deploy keys included). Nothing in the project is fake. Exit status of a route is the number of failed checks.
set -uo pipefail
SRC=${SRC:-/opt/arm-src}
rgit() { git -c safe.directory="*" "$@"; }; export -f rgit      # root LOOKING at a repository that belongs to someone else, for that one command; the programs under test get no such trust
H=/home; NAME=armada; ADMIN=ctadmin; WORKER=armworker; OWNER=someone
export DEBIAN_FRONTEND=noninteractive
SSH_FROM=127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
PASS=0; FAIL=0
T0=$(date +%s); STEP_NAME=""; STEP_T=$T0; STEP_LOG=()

# ---- steps, checks and the clock --------------------------------------------------------------------------------------------------------------------
_close_step() { [ -n "$STEP_NAME" ] && STEP_LOG+=("$(( $(date +%s) - STEP_T ))s  $STEP_NAME"); }
step() { _close_step; STEP_NAME=$*; STEP_T=$(date +%s); printf '\n=== %s\n' "$*"; }
check() { local what=$1; shift; if "$@" >/tmp/e2e-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/e2e-check.out | tail -12; fi; }
finish() {
  _close_step
  printf '\n=== time per step (%ds in all), slowest first\n' "$(( $(date +%s) - T0 ))"
  printf '%s\n' "${STEP_LOG[@]}" | sort -rn | head -12 | sed 's/^/  /'
  printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
  exit "$FAIL"
}
as_admin() { runuser -u $ADMIN -- env "PATH=/opt/ctvenv/bin:/usr/local/bin:/usr/bin:/bin" HOME=$H/$ADMIN "$@"; }
wait_for() { local tries=$1; shift; for _ in $(seq "$tries"); do "$@" >/dev/null 2>&1 && return 0; sleep 3; done; return 1; }
export -f as_admin; export H ADMIN NAME WORKER OWNER SRC

# ---- the machine: what can be baked into a cached base machine (prepare_system) and what must be done per run (prepare_checkout) ----------------------
quiet_apt() {   # a freshly cloned machine starts apt's own background jobs (unattended upgrades) a few minutes after boot, and the installer's apt-get then fails on the package lock (found on the first full e2e run: five routes stopped there)
  systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1
  for _ in $(seq 60); do fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1 || return 0; sleep 3; done
  echo "apt is still locked after 3 minutes: $(fuser -v /var/lib/dpkg/lock-frontend 2>&1)"
}
prepare_system() {
  quiet_apt
  # NO_GITHUB=1: no GitHub stand-in at all (the Direct route proves nothing here is needed).  WARM=1: also the packages the installer itself installs, so a run does not wait on apt.
  apt-get update -qq && apt-get install -y -qq git python3 python3-yaml python3-venv python3-pip sudo openssh-server openssh-client iproute2 util-linux >/dev/null
  [ "${WARM:-0}" = 1 ] && apt-get install -y -qq bubblewrap ufw auditd unattended-upgrades >/dev/null 2>&1
  id $ADMIN >/dev/null 2>&1 || adduser --disabled-password --gecos "" $ADMIN >/dev/null
  usermod -aG sudo $ADMIN
  systemctl enable --now ssh >/dev/null 2>&1
  runuser -u $ADMIN -- bash -c '[ -f ~/.ssh/id_ed25519 ] || { mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keygen -q -t ed25519 -N "" -f ~/.ssh/id_ed25519; }; grep -qf ~/.ssh/id_ed25519.pub ~/.ssh/authorized_keys 2>/dev/null || cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
  usermod -p '*' $ADMIN                                           # no password, but not locked (sshd refuses locked accounts even for keys)
  printf 'Host *\n  StrictHostKeyChecking accept-new\n' > /etc/ssh/ssh_config.d/99-e2e.conf   # this machine's test setup: trust the stand-in hosts on first use
  git config --system user.name e2e; git config --system user.email e2e@example.com
  echo "$ADMIN ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/e2e-$ADMIN; chmod 440 /etc/sudoers.d/e2e-$ADMIN      # this test machine has no one to type a sudo password
  [ "${NO_GITHUB:-0}" = 1 ] || github_standin
}

github_standin() {
  # a stand-in for GitHub: github.com is this machine; user "git" owns the bare repositories
  grep -q 'github.com' /etc/hosts || echo '127.0.0.1 github.com' >> /etc/hosts
  id git >/dev/null 2>&1 || adduser --disabled-password --gecos "" git >/dev/null
  runuser -u git -- bash -c "mkdir -p ~/.ssh ~/$OWNER && chmod 700 ~/.ssh ; touch ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
  grep -qf $H/$ADMIN/.ssh/id_ed25519.pub $H/git/.ssh/authorized_keys || cat $H/$ADMIN/.ssh/id_ed25519.pub >> $H/git/.ssh/authorized_keys
  usermod -p '*' git
  # What the person would do with GitHub's `gh`: here a stand-in that works on the bare repositories above (view, create, rename, list and add deploy keys).
  cat > /usr/local/bin/gh <<'GH'
#!/bin/bash
H=/home; slug=""; file=""; write=0
args=("$@"); i=0
while [ $i -lt ${#args[@]} ]; do a=${args[$i]}; case "$a" in -R) i=$((i+1)); slug=${args[$i]};; -t|--json|--description) i=$((i+1));; --allow-write) write=1;; --private) ;; -*) ;; *) if [ -z "$sub" ]; then :; fi; last=$a;; esac; i=$((i+1)); done
case "$1 $2 $3" in
  "auth status"*) echo "Logged in to github.com (stand-in)" ;;
  "repo view"*) [ -d $H/git/$3.git ] ;;
  "repo create"*) sudo -n -u git git init -q --bare -b main $H/git/$3.git && echo "https://github.com/$3" ;;
  "repo rename"*) newn=$3; oldslug=""; for ((j=1;j<=$#;j++)); do if [ "${!j}" = "--repo" ]; then k=$((j+1)); oldslug=${!k}; fi; done
                  sudo -n -u git mv $H/git/$oldslug.git $H/git/${oldslug%/*}/$newn.git ;;
  "repo deploy-key list") echo '[]' ;;
  "repo deploy-key add") cat "$4" | sudo -n -u git tee -a $H/git/.ssh/authorized_keys >/dev/null; echo "key added to $slug (write=$write)" >> /tmp/e2e-gh.log ;;
  *) echo "gh stand-in: unsupported: $*" >&2; exit 1 ;;
esac
GH
  chmod +x /usr/local/bin/gh
}

# A machine cloned from the shared baseline (scripts/e2e-routes builds it once per run: tests/e2e/baseline.sh) already has the checkout installed and a GitHub baseline that works; the routes that are
# about something else start from it instead of each repeating a minute of install, measured as 6x slower when six ran at once.
BASELINE_MARK=${BASELINE_MARK:-/etc/arm-e2e-baseline}
from_baseline() { [ -f "$BASELINE_MARK" ]; }

prepare_checkout() {
  from_baseline && { printf '  (the checkout and the baseline install come from the shared baseline machine)\n'; return 0; }
  # the release comes from a git checkout (the update tool records which commit is installed)
  [ -d $SRC/.git ] || { git -C $SRC init -q && git -C $SRC add -A && git -C $SRC -c user.name=e2e -c user.email=e2e@example.com commit -qm e2e; }
  git config --system --add safe.directory $SRC      # only the release checkout (copied in by root, packaged by the admin). NOT every repository: trusting '*' once hid a real bug (the mailbox account was refused the results repository)
  [ "${NO_GITHUB:-0}" = 1 ] || check "the stand-in GitHub answers over SSH with the admin's key" as_admin ssh -o BatchMode=yes git@github.com true
  check "the checkout is where the route expects it" test -f $SRC/pyproject.toml
  [ -d /opt/ctvenv ] || python3 -m venv --system-site-packages /opt/ctvenv
  /opt/ctvenv/bin/pip install -q -e $SRC
  chmod -R a+rX /opt/ctvenv $SRC
  check "pip installs the four commands" bash -c 'for c in arm-run arm-admin arm-setup arm-help; do test -x /opt/ctvenv/bin/$c || exit 1; done'
}

# ---- installs ------------------------------------------------------------------------------------------------------------------------------------------
install_flags() { echo --yes --source $SRC --set server.host=localhost --set server.admin_user=$ADMIN --set github.owner=$OWNER --set network.ssh_from=$SSH_FROM; }
install_github() { as_admin arm-setup install $(install_flags) "$@"; }                       # the whole wizard, answered on the command line (GitHub is the default connection)

baseline_github() {
  from_baseline && { printf '  (from the shared baseline machine)\n'; return 0; }
  # the starting point of every route that is about something else: a GitHub install that works. Its own detail is checked by routes/install.sh.
  step "Baseline: arm-setup install (GitHub) and a first job"
  install_github >/tmp/e2e-install.out 2>&1; local rc=$?
  tail -8 /tmp/e2e-install.out
  check "the baseline install finished successfully" test $rc -eq 0
  check "the baseline worker runs a job" bash -c "as_admin arm-run submit sys-status --wait --timeout 240 >/tmp/e2e-job.out 2>&1"
}
