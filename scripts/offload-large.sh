#!/usr/bin/env bash
# Pipe-through helper that protects the model's context window from a single
# huge tool result.
#
# Reads stdin. If the total size is below the threshold — passes through
# unchanged. If above — writes the full content to a scratch file and prints
# a digest: head, a truncation marker, tail, and a `Full output at: <path>`
# line. The model can then Read the file with offset/limit if it needs more.
#
# Usage:
#     <command> | ~/claude-agent-instructions/scripts/offload-large.sh
#     <command> 2>&1 | ~/claude-agent-instructions/scripts/offload-large.sh
#
# Flags (env, override defaults):
#     OFFLOAD_THRESHOLD_BYTES  default 4096   — above this, offload
#     OFFLOAD_HEAD_LINES       default 40     — lines kept at the top
#     OFFLOAD_TAIL_LINES       default 20     — lines kept at the bottom
#     OFFLOAD_SCRATCH_DIR      default /tmp/cc-scratch
#
# Exit code: pass through the upstream pipe's status when available; on dry
# pass-through this is `0` from `cat`. The threshold check itself never
# fails — disk full just yields the original output unmodified.

set -euo pipefail

THRESHOLD="${OFFLOAD_THRESHOLD_BYTES:-4096}"
HEAD_LINES="${OFFLOAD_HEAD_LINES:-40}"
TAIL_LINES="${OFFLOAD_TAIL_LINES:-20}"
SCRATCH_DIR="${OFFLOAD_SCRATCH_DIR:-/tmp/cc-scratch}"

mkdir -p "$SCRATCH_DIR"
tmp="$(mktemp "$SCRATCH_DIR/out-XXXXXXXX")"
trap 'rm -f "$tmp"' EXIT

# Stream stdin to a temp file; we need the byte count before deciding.
cat > "$tmp"
size_bytes=$(wc -c < "$tmp")

if [ "$size_bytes" -le "$THRESHOLD" ]; then
    cat "$tmp"
    exit 0
fi

# Above threshold — keep the file, print digest.
permanent="$SCRATCH_DIR/out-$(date +%Y%m%d-%H%M%S)-$$.txt"
mv "$tmp" "$permanent"
trap - EXIT

total_lines=$(wc -l < "$permanent")
head -n "$HEAD_LINES" "$permanent"
hidden_lines=$(( total_lines - HEAD_LINES - TAIL_LINES ))
if [ "$hidden_lines" -lt 1 ]; then
    # Tail would overlap head; just emit a note and stop.
    echo
    echo "[offload-large] output was ${size_bytes} bytes / ${total_lines} lines; full content kept at: ${permanent}"
    exit 0
fi
echo
echo "[offload-large] --- ${hidden_lines} lines omitted (${size_bytes} bytes total); full at ${permanent} ---"
echo
tail -n "$TAIL_LINES" "$permanent"
echo
echo "[offload-large] Full output at: ${permanent}"
