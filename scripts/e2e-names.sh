# Sourced by every end-to-end harness script (e2e-routes, e2e-orbstack, e2e-matrix, e2e-base, e2e-clean, probe, real-github-check). The ONE place that decides what a test machine, an output
# folder, a log folder, a lock and the machine caps are called, so two runs (two sessions, or a session and you) never create, count or delete each other's machines.
# A RUN ID names who made a machine: "r" + ARM_E2E_RUN when you set it, else "r" + the first 6 characters of a hash of this checkout's absolute path (one worktree = one id, the same on every run).
# Machines: arm-run-<id>-<image>-<route>, arm-e2e-<id>, arm-mx-<id>-...; output /tmp/arm-routes/<id>/<image>/; logs /tmp/arm-e2e-logs/<id>/<machine>/. The cached base machine arm-cache-<image> is shared
# (a rebuild per run would cost minutes and gigabytes): its build and its clones go through one lock. A machine with no id (the older naming) is "unowned": only e2e-clean --all-runs removes it.
# No script builds such a name by hand: tests/test_e2e_names.py fails if one does.
E2E_NAMES_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
E2E_PREFIX_RE='^(arm-e2e|arm-mx|arm-u|arm-run|cw-e2e)'
E2E_OWNED_RE='^(arm-run|arm-e2e|arm-mx|arm-u)-(r[a-z0-9]+)(-|$)'
E2E_GLOBAL_CAP=${ARM_E2E_GLOBAL_CAP:-12}

e2e_id() {
  if [ -z "${E2E_RUN_ID:-}" ]; then
    local raw=${ARM_E2E_RUN:-}
    [ -n "$raw" ] || raw=$(printf %s "$(cd "$E2E_NAMES_DIR/.." && pwd -P)" | shasum | cut -c1-6)
    raw=$(printf %s "$raw" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9' | cut -c1-10)
    [ -n "$raw" ] || { echo "ARM_E2E_RUN must contain letters or digits" >&2; return 1; }
    E2E_RUN_ID=r$raw
  fi
  printf %s "$E2E_RUN_ID"
}
e2e_banner() { echo "run id $(e2e_id) (this run's machines, output and logs carry it; set ARM_E2E_RUN to choose another)" >&2; }
e2e_machine() { local k=$1 s=${2:-}; printf 'arm-%s-%s%s' "$k" "$(e2e_id)" "${s:+-$s}"; }         # e2e_machine run ubuntu-26-04-install
e2e_out_dir() { printf '/tmp/arm-routes/%s/%s' "$(e2e_id)" "$1"; }
e2e_logs_dir() { printf '/tmp/arm-e2e-logs/%s/%s' "$(e2e_id)" "$1"; }
e2e_cache_name() { printf 'arm-cache-%s' "$1"; }
e2e_cache_hashfile() { printf "/tmp/arm-e2e-locks/%s.hash" "$(e2e_cache_name "$1")"; }       # what the shared cache was built from, kept on the host so asking never starts the machine
e2e_owner() { if [[ $1 =~ $E2E_OWNED_RE ]]; then printf %s "${BASH_REMATCH[2]}"; else printf unowned; fi; }
e2e_all_machines() { orb list 2>/dev/null | awk '{print $1}' | grep -E "$E2E_PREFIX_RE" || true; }     # every test machine, whoever made it (never the caches)
e2e_my_machines() { local m id; id=$(e2e_id) || return 1; for m in $(e2e_all_machines); do [ "$(e2e_owner "$m")" = "$id" ] && echo "$m"; done; return 0; }
e2e_cap_check() {  # $1 = most machines this run id may hold; the global cap protects the Mac. Prints the reason and the way out; status 1 when over.
  local mine_cap=$1 id mine all holders
  id=$(e2e_id) || return 1
  mine=$(e2e_my_machines | grep -c . || true); all=$(e2e_all_machines | grep -c . || true)
  if [ "$mine" -ge "$mine_cap" ]; then echo "Run $id already has $mine test machines (limit $mine_cap): scripts/e2e-clean (it removes only this run's)." >&2; return 1; fi
  if [ "$all" -ge "$E2E_GLOBAL_CAP" ]; then
    holders=$(for m in $(e2e_all_machines); do echo "$(e2e_owner "$m")"; done | sort | uniq -c | awk '{printf "%s%s (%s)", sep, $2, $1; sep=", "}')
    echo "$all test machines exist in all (global limit $E2E_GLOBAL_CAP), held by: $holders. Ask the owner to run scripts/e2e-clean; scripts/e2e-clean --list shows them." >&2; return 1
  fi
  return 0
}
e2e_locked() {  # e2e_locked <lock name> <command...>: one holder at a time across every run; a lock whose holder is gone, or older than 40 minutes, is taken over; waiting is said once.
  local name=$1 dir=/tmp/arm-e2e-locks/$1 told=0 rc; shift
  mkdir -p /tmp/arm-e2e-locks
  until mkdir "$dir" 2>/dev/null; do
    local pid owner; pid=$(cut -d' ' -f1 "$dir/holder" 2>/dev/null || true); owner=$(cut -d' ' -f2 "$dir/holder" 2>/dev/null || true)
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then rm -rf "$dir"; continue; fi
    if [ -n "$(find "$dir" -maxdepth 0 -mmin +40 2>/dev/null)" ]; then rm -rf "$dir"; continue; fi
    [ "$told" = 0 ] && { echo "waiting: run ${owner:-another} is using the shared $name (building it or cloning from it)" >&2; told=1; }
    sleep 3
  done
  echo "${BASHPID:-$$} $(e2e_id)" > "$dir/holder"
  "$@"; rc=$?
  rm -rf "$dir"
  return $rc
}
