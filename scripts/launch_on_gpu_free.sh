#!/bin/bash
# Poll ROCm VRAM usage and launch a command once the GPU(s) are FREE
# (VRAM% == 0 on every card). Use to chain a run behind a previous one:
# wait for the prior process to release VRAM (clean exit OR crash), then
# fire the next launch automatically without babysitting the terminal.
#
# "Free" = the MAX "GPU Memory Allocated (VRAM%)" across all cards is 0.
# Requiring the max (not card0) covers multi-GPU / -tp N runs where any
# single card still holding VRAM means the GPU set is not yet free.
#
# Usage: bash scripts/launch_on_gpu_free.sh [-i POLL_SEC] [-n MAX_MIN] [--] CMD [ARGS...]
#   -i POLL_SEC   poll interval, default 15
#   -n MAX_MIN    give up after this many minutes (0 = wait forever), default 0
#   CMD [ARGS...] the command to launch when the GPU is free (required)
#
# Examples:
#   bash scripts/launch_on_gpu_free.sh -- bash scripts/start_atom_server.sh
#   bash scripts/launch_on_gpu_free.sh -i 30 -n 20 -- python -m atom.examples.simple_inference --model X
#
# Exit codes:
#   (replaced by CMD via exec — its exit code is yours)
#   1 — bad usage / rocm-smi missing
#   4 — MAX_MIN elapsed before the GPU went free

set -uo pipefail

POLL=15
MAX_MIN=0

while [ $# -gt 0 ]; do
    case "$1" in
        -i) POLL="$2"; shift 2 ;;
        -n) MAX_MIN="$2"; shift 2 ;;
        --) shift; break ;;
        -*) echo "unknown option: $1" >&2; exit 1 ;;
        *)  break ;;
    esac
done

if [ $# -eq 0 ]; then
    echo "error: no command given to launch" >&2
    echo "usage: bash scripts/launch_on_gpu_free.sh [-i POLL_SEC] [-n MAX_MIN] [--] CMD [ARGS...]" >&2
    exit 1
fi

if ! command -v rocm-smi >/dev/null 2>&1; then
    echo "error: rocm-smi not found on PATH" >&2
    exit 1
fi

# Return the max VRAM% across all cards (empty/garbage -> treat as busy=100
# so we never launch on a parse failure).
max_vram_pct() {
    rocm-smi --showmemuse --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(100); sys.exit()
vals = []
for card in d.values():
    v = card.get("GPU Memory Allocated (VRAM%)")
    try:
        vals.append(int(float(v)))
    except (TypeError, ValueError):
        pass
print(max(vals) if vals else 100)
'
}

START=$SECONDS
i=0
while true; do
    i=$(( i + 1 ))
    VRAM=$(max_vram_pct)
    ELAPSED=$(( SECONDS - START ))
    echo "[t=${ELAPSED}s] max VRAM%=${VRAM}"

    if [ "$VRAM" -eq 0 ]; then
        echo "GPU FREE (VRAM 0%) — launching: $*"
        exec "$@"
    fi

    if [ "$MAX_MIN" -gt 0 ] && [ "$ELAPSED" -ge $(( MAX_MIN * 60 )) ]; then
        echo "GPU still busy after ${MAX_MIN} min — giving up (exit 4)"
        exit 4
    fi

    sleep "$POLL"
done
