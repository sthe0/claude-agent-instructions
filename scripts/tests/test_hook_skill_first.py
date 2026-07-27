"""Stage 5: skill-first advisory hook — nudges on hand-rolled domain ops,
silent on plain shell, never blocks, once per operation-class per session.

Core ships exactly one operation class (`tracker`, a host-agnostic REST shape);
every other class is machine-local data, so the tests below exercise the loading
contract with SYNTHETIC class definitions rather than any deployment's real
command verbs."""
import importlib.util
import io
import json
import uuid
from pathlib import Path

# A synthetic operation class: the shape a deployment writes into
# config_root.skill_first_classes_file(), with a verb no real tool owns.
SYNTHETIC = [
    {"name": "widgetry", "pattern": r"\bwidgetctl\s+(apply|destroy)\b", "skill": "widget-ops"},
    {"name": "ledgering", "pattern": r"\bledgerctl\s+post\b", "skill": "ledger-client"},
]

_SPEC = importlib.util.spec_from_file_location(
    "hook_skill_first",
    Path(__file__).resolve().parents[1] / "hook-skill-first.py",
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _run(monkeypatch, capsys, command, session=None, tool="Bash"):
    sid = session or f"s-{uuid.uuid4().hex[:8]}"
    payload = {"tool_name": tool, "tool_input": {"command": command}, "session_id": sid}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out, sid


def _classes_file(tmp_path, entries) -> Path:
    p = tmp_path / "skill-first-classes.local"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


# --- machine-local operation classes ------------------------------------------

def test_core_ships_only_the_tracker_class():
    """The org-neutrality invariant: with no machine-local file, Core's own class
    list is exactly {tracker}. A future builtin naming a specific tool's verbs
    fails here rather than shipping to every deployment."""
    assert [n for n, _, _ in mod.build_classes(classes_path="/nonexistent/classes.local")] \
        == ["tracker"]


def test_local_classes_extend_detection(tmp_path):
    classes = mod.build_classes(classes_path=_classes_file(tmp_path, SYNTHETIC))
    assert [n for n, _, _ in classes] == ["tracker", "widgetry", "ledgering"]
    assert [n for n, _ in mod.detect("widgetctl apply -f x.yaml", classes)] == ["widgetry"]
    assert mod.detect("widgetctl status", classes) == []


def test_local_classes_are_matched_case_insensitively(tmp_path):
    classes = mod.build_classes(classes_path=_classes_file(tmp_path, SYNTHETIC))
    assert [n for n, _ in mod.detect("WidgetCtl DESTROY prod", classes)] == ["widgetry"]


def test_local_classes_fail_open_per_entry(tmp_path):
    """A malformed entry costs its own nudge, not the whole file — and never the
    Bash call the hook advises on."""
    path = _classes_file(tmp_path, [
        {"name": "broken", "pattern": r"[unclosed", "skill": "x"},
        {"no_name_key": True},
        SYNTHETIC[0],
    ])
    assert [n for n, _, _ in mod.build_classes(classes_path=path)] == ["tracker", "widgetry"]


def test_local_classes_malformed_file_yields_core_builtin_only(tmp_path):
    path = tmp_path / "classes.local"
    path.write_text("not json", encoding="utf-8")
    assert [n for n, _, _ in mod.build_classes(classes_path=path)] == ["tracker"]


# --- detector unit ------------------------------------------------------------

def test_detect_tracker_rest():
    names = [n for n, _ in mod.detect(
        "curl -X PATCH https://tracker.example.com/v2/issues/ABC-1")]
    assert "tracker" in names


def test_tracker_generic_shape_needs_no_configured_host():
    """The built-in pattern is host-agnostic: a REST issue-API shape matches
    without any operator-supplied host in agent-identity.local."""
    generic_re = mod._build_tracker_re(extra_hosts=())
    assert generic_re.search("curl -X GET https://tracker.example.com/rest/api/2/issue/ABC-1")
    assert generic_re.search("curl -X GET https://any-host.example/v2/issues/ABC-1")
    assert generic_re.search("curl -X GET https://plain-host.example/widgets") is None


def test_tracker_extra_hosts_are_operator_configurable():
    """A machine-local host fragment (agent-identity.local's
    skill_first_tracker_hosts=) extends detection beyond the generic shape."""
    custom_re = mod._build_tracker_re(extra_hosts=("issues.internal-example.org",))
    assert custom_re.search("curl -X GET https://issues.internal-example.org/get/1")
    assert mod._tracker_hosts(Path("/nonexistent/agent-identity.local")) == ()


def test_silent_on_plain_shell():
    assert mod.detect("git status") == []
    assert mod.detect("python3 build.py") == []


# --- hook behaviour -----------------------------------------------------------

def test_fires_once_per_class(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        mod, "CLASSES", mod.build_classes(classes_path=_classes_file(tmp_path, SYNTHETIC))
    )
    rc, out, sid = _run(monkeypatch, capsys, "widgetctl apply -f x.yaml")
    assert rc == 0 and "skill-first" in out and "widgetry" in out
    # same class again -> silent
    rc2, out2, _ = _run(monkeypatch, capsys, "widgetctl destroy prod", session=sid)
    assert rc2 == 0 and out2 == ""
    # a different class in the same session still fires
    rc3, out3, _ = _run(monkeypatch, capsys, "ledgerctl post 42", session=sid)
    assert rc3 == 0 and "ledgering" in out3


def test_silent_on_negative(monkeypatch, capsys):
    rc, out, _ = _run(monkeypatch, capsys, "make all")
    assert rc == 0 and out == ""


def test_ignores_non_bash(monkeypatch, capsys):
    rc, out, _ = _run(
        monkeypatch, capsys,
        "curl -X PATCH https://tracker.example.com/v2/issues/ABC-1", tool="Write",
    )
    assert rc == 0 and out == ""
