#!/usr/bin/env bash
# Smoke-check that the difficulty channel this machine is CONFIGURED to use can
# actually be resolved.
#
# Why it exists: a channel whose adapter is not built in attaches through the
# machine-local plugin directory instead of shipping here (see
# scripts/lib/plugin_dir.py and scripts/difficulty_channel/adapters/__init__.py).
# Nothing inside the repo can see that directory, so on a machine where the
# adapter was never installed the configured channel fails only at the moment
# someone files a difficulty — which is exactly the moment nobody wants to
# debug it. This check turns that silence into a loud, early failure.
#
# What it decides, from the name in agent-identity.local alone:
#   * nothing configured, or a built-in channel  -> pass, nothing to resolve
#   * any other name                             -> its plugin adapter must load
#                                                   AND register that same name
#
# It names no channel of its own: it reports whatever name the machine gave it.
#
# How to run it:  bash scripts/verify-difficulty-channel-resolves.sh
# It also runs as part of scripts/verify-instructions-sync.sh, next to the
# extracted-skills check, so the installer path covers it too.
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHONPATH="$SCRIPTS_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import sys

from difficulty_channel import adapters, authority, port

channel = authority.read_configured_channel()

if channel in adapters.BUILTIN_NAMES:
    print(f"OK: configured difficulty channel {channel!r} is built in - no plugin needed.")
    sys.exit(0)

INSTALL_HINT = ("Install the adapter into the machine-local plugin dir; "
                "scripts/setup-symlinks.sh creates the dir and its README.")

try:
    adapters.load_adapter(channel)
# Every import-time failure means one thing to the caller - the channel will not
# resolve at submit time either - so they are reported alike rather than split.
except Exception as exc:  # noqa: BLE001
    print(f"FAIL: configured difficulty channel {channel!r} does not resolve: {exc}")
    print(INSTALL_HINT)
    sys.exit(1)

# Loading the module is only half of what the submit path does: it then calls
# get_channel(name), which raises KeyError unless the module registered under
# exactly this name. Asked of the registry rather than by building the channel,
# because constructing one may demand credentials this check has no business
# needing.
if not port.is_registered(channel):
    print(f"FAIL: the adapter for difficulty channel {channel!r} loaded but did not "
          f"register a channel named {channel!r}, so filing a difficulty would fail.")
    print("Its module must call port.register_channel(<name>, <factory>) at import "
          "time, under the same name the machine is configured with.")
    print(INSTALL_HINT)
    sys.exit(1)

print(f"OK: configured difficulty channel {channel!r} resolves from the machine-local plugin dir.")
PY
