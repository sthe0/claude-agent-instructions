"""Runs the OpenCode binary under the isolated-run rule against the stub and reads back what happened.

Every run gets a fresh root R, a copy of the probe plugin, a stub provider and an
``opencode.json`` the harness writes (``OPENCODE_CONFIG`` points at it). A refused call is
judged by its effect (a marker file the call would have written), never by the event log alone.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import core, isolated, stub

PLUGIN_DIR = core.PROBE_DIR / "plugin"
STUB_MODELS = ("m1", "m2", "m3")
REFUSAL_TEXT = {"PreToolUse": "probe throw", "permission.evaluate": "probe deny"}
CLI_BASE = ("run", "--standalone", "--auto", "--format", "json")


class VersionMismatch(Exception):
    """The binary's version is not the pin; no run is launched."""


def binary_version(binary: str) -> str:
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    words = result.stdout.split()
    return words[-1].removeprefix("v") if words else ""


def ensure_pinned_version(binary: str, probe_dir: Path = core.PROBE_DIR) -> str:
    pinned = core.read_pin(probe_dir)["version"]
    seen = binary_version(binary)
    if seen != pinned:
        raise VersionMismatch(f"{binary} reports {seen!r}, the pin is {pinned!r}")
    return seen


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class Run:
    label: str
    root: Path
    argv: list[str]
    env_paths: dict[str, str]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    events: list[dict] = field(default_factory=list)
    probe: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    stand_ins_before: dict[str, str] = field(default_factory=dict)
    stand_ins_after: dict[str, str] = field(default_factory=dict)

    def stand_ins_changed(self) -> list[str]:
        return sorted(rel for rel, digest in self.stand_ins_before.items()
                      if self.stand_ins_after.get(rel) != digest)

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def read(self, rel: str) -> str | None:
        path = self.root / rel
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None

    def tool_parts(self) -> list[dict]:
        return [e["part"] for e in self.events if e.get("type") == "tool_use" and "part" in e]

    def tool_part(self, call_id: str) -> dict | None:
        for part in self.tool_parts():
            if part.get("id") == call_id:
                return part
        return None

    def probe_entries(self, at: str) -> list[dict]:
        return [e for e in self.probe if e.get("at") == at]

    def refusal_event(self, mechanism: str, call_id: str | None = None) -> str | None:
        """The positive event showing ``mechanism`` refused: for a call, the tool part whose error
        text is the plugin's refusal message (the runtime honoured the refusal); otherwise the
        plugin's own ``refusal`` log entry, which is only the plugin's decision. The log entry
        is the fallback for a call with no tool part in the output (the nested ``child-run``).
        None when neither is there."""
        part = self.tool_part(call_id) if call_id else None
        if part is not None:
            error = str((part.get("state") or {}).get("error") or "")
            if REFUSAL_TEXT.get(mechanism, "\0") in error:
                return "tool-part-error"
            return None
        for entry in self.probe_entries("refusal"):
            if entry.get("mechanism") == mechanism and (call_id is None or entry.get("id") == call_id):
                return "hook-log"
        return None

    def tool_schema(self, tool: str) -> dict:
        for request in self.requests:
            if tool in (request.get("tool_specs") or {}):
                return request["tool_specs"][tool]
        return {}

    def tool_description(self, tool: str) -> str:
        for request in self.requests:
            if tool in (request.get("tool_descriptions") or {}):
                return str(request["tool_descriptions"][tool])
        return ""

    def tool_list(self) -> list[str]:
        """The tools the model was offered: names from the stub's chat requests that carry tools."""
        names: set[str] = set()
        for request in self.requests:
            names.update(request.get("tools") or [])
        return sorted(names)

    def session_ids(self) -> list[str]:
        return sorted({e["sessionID"] for e in self.events if "sessionID" in e})


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"raw": line[:500]})
    return out


@dataclass
class World:
    """One isolated root with the stub running and the config, files and probe environment in place."""
    root: Path
    env: dict[str, str]
    binary: str
    port: int
    stub_dir: Path
    probe_log: Path
    fill: Callable[[str], str]
    stand_ins_before: dict[str, str]

    def env_paths(self) -> dict[str, str]:
        """The path-valued variables the launched process saw, for the record's isolation check."""
        return {key: self.env[key] for key in isolated.PATH_KEYS if key in self.env}

    def probe(self) -> list[dict]:
        return _read_jsonl(self.probe_log)

    def requests(self) -> list[dict]:
        return _read_jsonl(self.stub_dir / "requests.jsonl")


def wait_until(predicate: Callable[[], object], timeout: float, interval: float = 0.3) -> object:
    """The predicate's first truthy value, or its last value once ``timeout`` seconds have passed."""
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() >= deadline:
            return value
        time.sleep(interval)


class Service:
    """A running ``opencode serve`` and a minimal JSON client for its HTTP API."""

    def __init__(self, world: World, base: str, out_path: Path):
        self.world = world
        self.base = base
        self.out_path = out_path
        self.headers: dict[str, str] = {}

    def password(self) -> str:
        for line in self.out_path.read_text(errors="replace").splitlines():
            if line.startswith("server password "):
                return line.split(" ", 2)[2].strip()
        return ""

    def call(self, method: str, path: str, body: object = None, timeout: float = 20.0,
             headers: dict[str, str] | None = None) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"content-type": "application/json", **self.headers,
                                                  **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, text = response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            status, text = error.code, error.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as error:
            return 0, {"transport_error": str(error)}
        try:
            return status, json.loads(text) if text else None
        except ValueError:
            return status, {"raw": text[:500]}

    def wait_until(self, predicate: Callable[[], object], timeout: float, interval: float = 0.3) -> object:
        return wait_until(predicate, timeout, interval)


class Harness:
    def __init__(self, case_id: str, events_dir: Path, *, inert: bool = False,
                 state: Path | None = None, binary: str | None = None):
        self.case_id = case_id
        self.events_path = Path(events_dir) / f"{case_id}.jsonl"
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.inert = inert
        self.state = state
        self.binary_source = binary or core.opencode_bin()
        self.count = 0

    def _log(self, entry: dict) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def base_config(self, *, plugin: bool) -> dict:
        config: dict = {
            "enabled_providers": ["stub"],
            "small_model": f"stub/{STUB_MODELS[0]}",
            "providers": {"stub": {
                "name": "stub", "package": "aisdk:@ai-sdk/openai-compatible",
                "settings": {"apiKey": "stub-key", "baseURL": "http://127.0.0.1:{port}/v1"},
                "models": {m: {"name": m} for m in STUB_MODELS}}},
        }
        if plugin:
            config["plugins"] = ["file://{root}/pdir"]
        return config

    @contextmanager
    def world(self, scenario: dict, *, rules: dict | None = None, config: dict | None = None,
              files: dict[str, str] | None = None, env: dict[str, str] | None = None,
              plugin: bool = True, stand_ins: bool = False,
              tool_marker: str = "m-plugin-tool") -> Iterator[World]:
        ensure_pinned_version(self.binary_source)
        root = isolated.make_root(self.state)
        envmap = isolated.isolated_env(root)
        isolated.assert_scratch_in_effect(root, envmap)
        before = isolated.seed_home_stand_ins(root) if stand_ins else {}
        pdir = root / "pdir"
        pdir.mkdir()
        for source in PLUGIN_DIR.iterdir():
            (pdir / source.name).write_bytes(source.read_bytes())
        stub_dir = root / "stub"
        stub_dir.mkdir()
        server, port = None, 0
        try:
            def fill(text: str) -> str:
                return text.replace("{root}", str(root)).replace("{port}", str(port))

            scenario_path = stub_dir / "scenario.json"
            scenario_path.write_text("{}", encoding="utf-8")
            server, port = stub.start_stub(scenario_path)
            scenario_path.write_text(fill(json.dumps(scenario)), encoding="utf-8")
            server.stub_state.reload()
            merged = deep_merge(self.base_config(plugin=plugin), config or {})
            (root / "opencode.json").write_text(fill(json.dumps(merged)), encoding="utf-8")
            for rel, text in (files or {}).items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(fill(text), encoding="utf-8")
            probe_log = root / "probe.log"
            envmap["PROBE_LOG"] = str(probe_log)
            envmap["PROBE_RULES"] = fill(json.dumps(rules or {}))
            envmap["PROBE_TOOL_MARKER"] = str(root / tool_marker)
            if self.inert:
                envmap["PROBE_INERT"] = "1"
            for key, value in (env or {}).items():
                envmap[key] = fill(value)
            binary = isolated.place_binary(root, self.binary_source)
            yield World(root, envmap, binary, port, stub_dir, probe_log, fill, before)
        finally:
            if server is not None:
                server.shutdown()
            isolated.drop_binary(root)

    def run(self, scenario: dict, *, label: str, rules: dict | None = None,
            config: dict | None = None, files: dict[str, str] | None = None,
            prompt: str = "go", cli: tuple[str, ...] = (), env: dict[str, str] | None = None,
            cwd: str = "", timeout: int = 120, model: str = "stub/m1", wait_for: tuple[str, ...] = (),
            settle: float = 6.0, plugin: bool = True, stand_ins: bool = False,
            tool_marker: str = "m-plugin-tool", signal_after: tuple[float, int] | None = None) -> Run:
        """One ``opencode run`` in a fresh root. ``{root}`` and ``{port}`` expand in the scenario,
        config, files, rules and env values. ``wait_for`` paths (relative to R) are awaited for up to
        ``settle`` seconds after the process exits (a background subagent may still be running).
        ``signal_after`` is (seconds, signal number): the signal goes to the process itself once the
        seconds pass with the process still running."""
        with self.world(scenario, rules=rules, config=config, files=files, env=env, plugin=plugin,
                        stand_ins=stand_ins, tool_marker=tool_marker) as w:
            argv = [w.binary, *CLI_BASE, "--model", model, *[w.fill(a) for a in cli], w.fill(prompt)]
            workdir = w.root / cwd if cwd else w.root
            workdir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(argv, env=w.env, cwd=workdir, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            timed_out = False
            try:
                if signal_after:
                    try:
                        proc.communicate(timeout=signal_after[0])
                    except subprocess.TimeoutExpired:
                        proc.send_signal(signal_after[1])
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                timed_out = True
            deadline = time.time() + settle
            while wait_for and time.time() < deadline and not all((w.root / p).exists() for p in wait_for):
                time.sleep(0.2)
            events = []
            for line in out.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    events.append({"raw": line[:500]})
            run = Run(label, w.root, argv, w.env_paths(), proc.returncode, timed_out, out, err,
                      events, w.probe(), w.requests(), w.stand_ins_before,
                      isolated.home_stand_in_digests(w.root) if stand_ins else {})
        self._record(run)
        return run

    @contextmanager
    def serve(self, scenario: dict, *, label: str, rules: dict | None = None, config: dict | None = None,
              files: dict[str, str] | None = None, env: dict[str, str] | None = None,
              plugin: bool = True, ready_timeout: float = 40.0) -> Iterator[Service]:
        """An ``opencode serve`` on a free loopback port in a fresh root; the process group is killed
        on exit and the probe log, stub requests and server output are logged like a run's."""
        with self.world(scenario, rules=rules, config=config, files=files, env=env, plugin=plugin) as w:
            port = free_port()
            argv = [w.binary, "serve", "--print-logs", "--log-level", "info",
                    "--hostname", "127.0.0.1", "--port", str(port)]
            out_path, err_path = w.root / "serve.out", w.root / "serve.err"
            with out_path.open("w") as out_handle, err_path.open("w") as err_handle:
                proc = subprocess.Popen(argv, env=w.env, cwd=w.root, stdout=out_handle, stderr=err_handle,
                                        start_new_session=True)
                service = Service(w, f"http://127.0.0.1:{port}", out_path)
                try:
                    ready = service.wait_until(service.password, ready_timeout)
                    token = base64.b64encode(f"opencode:{service.password()}".encode()).decode()
                    service.headers["authorization"] = f"Basic {token}"
                    if not ready:
                        raise RuntimeError(
                            f"opencode serve not ready on port {port} (exit {proc.poll()}): "
                            f"stdout {out_path.read_text(errors='replace')[-300:]!r} "
                            f"stderr {err_path.read_text(errors='replace')[-300:]!r}")
                    yield service
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            run = Run(label, w.root, argv, w.env_paths(), proc.returncode, False,
                      out_path.read_text(errors="replace"), err_path.read_text(errors="replace"),
                      [], w.probe(), w.requests())
        self._record(run)

    def _record(self, run: Run) -> None:
        self.count += 1
        self._log({"run": self.count, "label": run.label, "src": "meta", "argv": run.argv,
                   "exit_code": run.exit_code, "timed_out": run.timed_out, "root": str(run.root),
                   "env_paths": run.env_paths, "stderr": run.stderr[-600:]})
        for source, entries in (("stdout", run.events), ("probe", run.probe), ("stub", run.requests)):
            for entry in entries:
                self._log({"run": self.count, "label": run.label, "src": source, "entry": entry})
