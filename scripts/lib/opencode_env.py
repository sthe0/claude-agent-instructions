"""The environment and inline config of an `opencode run` single call.

One module builds what `lib.host_llm` hands the child: the XDG_* and OPENCODE_* isolation that keeps
the user's own OpenCode config out of the call, the tool-less agent `agent-single-call` (every tool
denied), and the generated config file that carries the provider. The per-machine settings live in
the uncommitted `opencode.local.toml` under the agent home:

    opencode-provider = { id = "<provider id>", base_url = "<OpenAI-compatible base URL>" }
    opencode-model-low = "<model id>"       # also -medium, -high; "<provider id>/<model id>" is accepted
    opencode-credential-env = "<NAME>"      # the API key is read by OpenCode from this env var
    opencode-credential-file = "<path>"     # or from this file

Without `opencode-provider` the `provider` block of the user's `~/.config/opencode/opencode.json[c]`
is read, never written. A credential is only ever named here, never held in a file Core commits.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path

from .config_root import agent_home

AGENT_NAME = "agent-single-call"
SETTINGS_FILE = "opencode.local.toml"
TIERS = ("low", "medium", "high")
OPENAI_COMPATIBLE_PACKAGE = "aisdk:@ai-sdk/openai-compatible"
DISABLE_AUTOUPDATE = "OPENCODE_DISABLE_AUTOUPDATE"
ISOLATION_ENV_KEYS = (
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", DISABLE_AUTOUPDATE,
)
_COMMENTS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)


def settings() -> dict:
    path = agent_home() / SETTINGS_FILE
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def user_config_provider() -> dict | None:
    """The `provider` block of the user's own OpenCode config; the file is only read."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    for name in ("opencode.json", "opencode.jsonc"):
        try:
            text = base.joinpath(name).read_text(encoding="utf-8")
        except OSError:
            continue
        text = _COMMENTS.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
        try:
            provider = json.loads(text).get("provider")
        except (ValueError, AttributeError):
            continue
        if isinstance(provider, dict) and provider:
            return provider
    return None


def provider_setting() -> dict | None:
    value = settings().get("opencode-provider")
    if isinstance(value, dict) and value.get("id") and value.get("base_url"):
        return {"id": str(value["id"]), "base_url": str(value["base_url"])}
    return None


def model_setting(complexity: str) -> str | None:
    """The `<provider id>/<model id>` launch id of a tier, or None when none is configured."""
    if complexity not in TIERS:
        raise ValueError(f"unknown complexity {complexity!r}; must be one of {TIERS}")
    value = settings().get(f"opencode-model-{complexity}")
    if not isinstance(value, str) or not value:
        return None
    if "/" in value:
        return value
    provider = provider_setting()
    return f"{provider['id']}/{value}" if provider else value


def _bare_model(launch_id: str) -> str:
    return launch_id.split("/", 1)[-1]


def provider_config() -> dict | None:
    """The generated config body that carries the provider; None when no provider is available."""
    configured = provider_setting()
    if configured is None:
        user = user_config_provider()
        return {"provider": user} if user else None
    table = settings()
    models = {}
    for tier in TIERS:
        launch_id = model_setting(tier)
        if launch_id:
            models[_bare_model(launch_id)] = {"name": _bare_model(launch_id)}
    options = {"baseURL": configured["base_url"]}
    if table.get("opencode-credential-env"):
        options["apiKey"] = "{env:%s}" % table["opencode-credential-env"]
    elif table.get("opencode-credential-file"):
        options["apiKey"] = "{file:%s}" % table["opencode-credential-file"]
    body = {
        "providers": {configured["id"]: {
            "name": configured["id"], "package": OPENAI_COMPATIBLE_PACKAGE,
            "settings": options, "models": models,
        }},
        "enabled_providers": [configured["id"]],
    }
    small = next((model_setting(t) for t in TIERS if model_setting(t)), None)
    if small:
        body["small_model"] = small
    return body


def agent_definition() -> dict:
    return {"permissions": [{"action": "*", "resource": "*", "effect": "deny"}]}


def config_content() -> str:
    return json.dumps({"agents": {AGENT_NAME: agent_definition()}})


def not_ready() -> tuple[str, str] | None:
    """(code, detail) of what keeps a single call from launching under OpenCode, else None."""
    if shutil.which("opencode") is None:
        return "binary_missing", "`opencode` is not on PATH"
    if provider_config() is None:
        return "provider_unconfigured", f"no `opencode-provider` in {SETTINGS_FILE} and no provider in the user's OpenCode config"
    return None


def _root() -> Path:
    root = Path(tempfile.gettempdir()) / f"opencode-single-call-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    scratch.write_text(text, encoding="utf-8")
    os.replace(scratch, path)


def run_kwargs(base_env: dict | None = None) -> dict:
    """kwargs for `subprocess.run` of one `opencode run` call: the env keeps the real HOME and
    agent home and replaces the config, data, state and cache roots; cwd is an empty directory."""
    root = _root()
    dirs = {name: root / name for name in ("config", "data", "state", "cache", "config-dir", "cwd")}
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    config_file = root / "config.json"
    _write_if_changed(config_file, json.dumps(provider_config() or {}, indent=2, sort_keys=True))
    env = dict(os.environ if base_env is None else base_env)
    env.update({
        "XDG_CONFIG_HOME": str(dirs["config"]), "XDG_DATA_HOME": str(dirs["data"]),
        "XDG_STATE_HOME": str(dirs["state"]), "XDG_CACHE_HOME": str(dirs["cache"]),
        "OPENCODE_CONFIG": str(config_file), "OPENCODE_CONFIG_DIR": str(dirs["config-dir"]),
        "OPENCODE_CONFIG_CONTENT": config_content(), DISABLE_AUTOUPDATE: "1",
    })
    return {"cwd": str(dirs["cwd"]), "env": env}
