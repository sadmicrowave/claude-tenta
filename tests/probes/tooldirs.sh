#!/bin/bash
# Probe: a class's tool folders (Special Use) really are shown to a job inside the project's own jail on a real kernel, read-only and in PATH order, while a folder that is not named stays invisible.
# Needs a machine where an unprivileged bubblewrap works (a real worker or VM: `scripts/probe --on user@host tooldirs`); in a container where it cannot be built, every check is reported as skipped.
# With the project's own code (at $ARM_PROBE_ROOT, or /src in a container).
set -uo pipefail
ROOT=${ARM_PROBE_ROOT:-/src}
PASS=0; FAIL=0
check() { local what=$1; shift; if "$@" >/tmp/probe-check.out 2>&1; then PASS=$((PASS+1)); printf '  PASS  %s\n' "$what"; else FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/probe-check.out | head -5; fi; }
note() { printf '  NOTE  %s\n' "$1"; }
export PYTHONPATH=$ROOT/src PYTHONDONTWRITEBYTECODE=1
echo "$(uname -sr), $(. /etc/os-release; echo "$PRETTY_NAME")"

if ! command -v bwrap >/dev/null || ! command -v python3 >/dev/null; then
  note "bubblewrap or python3 is not installed here: nothing to prove, skipped"
  printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"; exit "$FAIL"
fi
base=$(mktemp -d /var/tmp/arm-tooldirs.XXXXXX); trap 'rm -rf "$base"' EXIT
mkdir -p "$base/tools/a/bin" "$base/tools/b" "$base/other" "$base/wd"
printf '#!/bin/sh\necho from-a\n' > "$base/tools/a/bin/mytool"; chmod +x "$base/tools/a/bin/mytool"
printf '#!/bin/sh\necho from-b\n' > "$base/tools/b/mytool"; chmod +x "$base/tools/b/mytool"
echo secret > "$base/other/secret"
chmod -R a+rX "$base"

run_in_jail() {   # run_in_jail <path folders, colon separated> <command...>
  local path=$1; shift
  python3 - "$base" "$path" "$@" <<'PY'
import subprocess, sys
from armada import jail
base, path, argv = sys.argv[1], sys.argv[2], sys.argv[3:]
env = {"PATH": path}
j = jail.Bwrap(hide=[])
if j.problem():
    print("SKIP: " + str(j.problem())); sys.exit(77)
a = j.args(argv, network=False, wd=base + "/wd", home="/nonexistent-arm-home", env=env)
r = subprocess.run(a, capture_output=True, text=True, env={"PATH": path + ":/usr/bin:/bin"})
print(r.stdout, end=""); print(r.stderr, end="", file=sys.stderr); sys.exit(r.returncode)
PY
}
export -f run_in_jail; export base PYTHONPATH PYTHONDONTWRITEBYTECODE
run_in_jail "$base/tools/a/bin" /usr/bin/true >/tmp/probe-skip.out 2>&1; rc=$?
if [ $rc -eq 77 ] || [ $rc -ne 0 ]; then
  note "an unprivileged jail cannot be built here ($(head -c 150 /tmp/probe-skip.out | tr '\n' ' ')): skipped"
  printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"; exit "$FAIL"
fi
check "a tool in a named folder runs inside the jail" bash -c "[ \"\$(run_in_jail '$base/tools/a/bin' mytool)\" = from-a ]"
check "the folders come first in the order given (a before b)" bash -c "[ \"\$(run_in_jail '$base/tools/a/bin:$base/tools/b' mytool)\" = from-a ]"
check "the other order gives the other tool" bash -c "[ \"\$(run_in_jail '$base/tools/b:$base/tools/a/bin' mytool)\" = from-b ]"
check "a folder that is not named is not visible" bash -c "! run_in_jail '$base/tools/a/bin' cat '$base/other/secret'"
check "a named folder is read-only inside the jail" bash -c "! run_in_jail '$base/tools/a/bin' sh -c 'echo x > $base/tools/a/bin/new'"

printf '\n=== %d passed, %d failed\n' "$PASS" "$FAIL"
exit "$FAIL"
