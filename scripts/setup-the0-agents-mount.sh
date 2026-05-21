#!/usr/bin/env bash
# Create ~/arcadia_the0-agents and branch the0-agents if missing. Run from cd ~ only.
set -euo pipefail

MOUNT="${THE0_AGENTS_MOUNT:-$HOME/arcadia_the0-agents}"
BRANCH="${THE0_AGENTS_BRANCH:-the0-agents}"
OBJECT_STORE="${ARC_OBJECT_STORE:-$HOME/.arc/store/.arc/objects}"
LOG="/tmp/arc-mount-the0-agents.log"

cd ~ || exit 1

if arc mount --list 2>/dev/null | grep -qF "$MOUNT"; then
  echo "Already mounted: $MOUNT"
else
  mkdir -p "$MOUNT"
  arc mount -m "$MOUNT" \
    --object-store "$OBJECT_STORE" \
    --override-object-store \
    --allow-other \
    >"$LOG" 2>&1 &
  for _ in $(seq 1 60); do
    grep -q '\[mounted' "$LOG" 2>/dev/null && break
    arc mount --list 2>/dev/null | grep -qF "$MOUNT" && break
    sleep 1
  done
  grep '\[mounted' "$LOG" || { tail "$LOG"; exit 1; }
fi

cd "$MOUNT"
if ! arc branch 2>/dev/null | grep -qE "^\* $BRANCH$| $BRANCH$"; then
  arc checkout trunk
  arc checkout -b "$BRANCH" 2>/dev/null || arc checkout "$BRANCH"
fi
echo "Ready: $MOUNT on branch $BRANCH"
echo "Junk path: $MOUNT/junk/the0/agents"
