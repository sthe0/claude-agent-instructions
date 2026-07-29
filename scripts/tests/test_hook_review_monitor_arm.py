"""review-monitor auto-arm PostToolUse hook: launches the detached poller when
ONE Bash call both opened a review and printed that review's URL — and does
nothing at all without a configured probe, for a review merely read, or for a
review already armed. Vendor-neutral fixtures; the probe is a plain `echo`."""
import importlib.util
import io
import json
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "hook_review_monitor_arm", SCRIPTS / "hook-review-monitor-arm.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

detect = mod.review_open_detect

_REVIEW = "https://reviews.example.com/review/4271"
_IDENTITY = "reviews.example.com/4271"
_OTHER = "https://reviews.example.com/review/9002"
_CREATE_CMD = "my-review-cli pr create --title 'fix formatter'"
_READ_CMD = "my-review-cli review status 4271"
_PROBE = "echo tests=failure approved=pending unresolved_comments=0 merged=false"


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point the claim markers, the poller logs and the registry at tmp, and
    supply the probe from a fake identity file: the hook reads live runtime
    state otherwise, so this machine's own config would decide the assertions
    (and a test run would litter it — and could start a real poller)."""
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
    monkeypatch.setenv("CLAUDE_MONITORED_REVIEWS", str(tmp_path / "registry.json"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    identity = tmp_path / "agent-identity.local"
    identity.write_text(f"review_probe={_PROBE}\n", encoding="utf-8")
    monkeypatch.setattr("difficulty_channel.authority.LOCAL_IDENTITY_PATH", identity)
    detect._verb_pattern.cache_clear()


@pytest.fixture
def launches(monkeypatch):
    """Record what would have been launched instead of spawning a real poller."""
    calls = []
    monkeypatch.setattr(mod, "launch", lambda url, out, probe: (
        calls.append({"url": url, "out": Path(out), "probe": probe}) or True
    ))
    return calls


def _payload(command, response):
    return {"tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": response}


def _run(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out


# --- the authorship gate ------------------------------------------------------

def test_arms_when_one_call_creates_and_prints_the_review(launches):
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == [_REVIEW]
    assert launches[0]["url"] == _REVIEW
    assert launches[0]["probe"] == _PROBE


def test_a_read_only_call_never_arms_a_monitor(launches):
    # The mutation proof of the authorship gate: the SAME review URL in the
    # output, only without a create verb, must launch nothing. Arming on any
    # review URL seen would start a poller for every review merely looked at.
    assert mod.arm(_payload(_READ_CMD, f"status of {_REVIEW}: green")) == []
    assert launches == []


def test_a_create_call_printing_no_review_url_arms_nothing(launches):
    assert mod.arm(_payload(_CREATE_CMD, "error: no upstream configured")) == []
    assert launches == []


def test_a_review_url_from_an_unrelated_earlier_call_is_not_correlated(launches):
    # Per-call correlation: the create command's own output is the evidence, so
    # a URL that arrived through a different call cannot be paired with it.
    assert mod.arm(_payload(_CREATE_CMD, "")) == []
    assert launches == []


def test_non_bash_tool_is_ignored(launches):
    payload = _payload(_CREATE_CMD, f"created {_REVIEW}")
    payload["tool_name"] = "Write"
    assert mod.arm(payload) == []
    assert launches == []


# --- authorship, not co-occurrence --------------------------------------------
# `a create verb somewhere` AND `a review URL somewhere` is NOT authorship of
# that URL. Both tests below arm a poller for a review the agent never opened
# under a gate built from those two independent existence checks.

def test_a_second_review_url_in_the_output_arms_neither(launches):
    # A create call whose stdout also quotes an unrelated review (a "depends on"
    # line, a bot comment, a "see also"). Nothing in the call says WHICH of the
    # two it produced, so the honest answer is to arm none and let the Stop
    # guardian advise a manual arm — over-firing here would start a poller for a
    # review the agent only read about.
    out = f"created {_REVIEW}\nnote: depends on {_OTHER}"
    assert mod.arm(_payload(_CREATE_CMD, out)) == []
    assert launches == []


@pytest.mark.parametrize("sep", [" && ", " ; ", " | "])
@pytest.mark.parametrize("order", ["create-first", "view-first"])
def test_a_compound_create_plus_view_never_arms_the_viewed_review(
        launches, sep, order):
    # `opens_review` is a whole-command boolean, so ANY create verb anywhere
    # makes it true — including in a chain whose other half only READS a
    # different review. That read review's URL is in the same call's output.
    view = f"my-review-cli pr view {_OTHER}"
    parts = [_CREATE_CMD, view] if order == "create-first" else [view, _CREATE_CMD]
    assert mod.arm(_payload(sep.join(parts), f"viewing {_OTHER}: checks green")) == []
    assert launches == []


def test_publish_by_id_arms_exactly_the_review_the_command_named(launches):
    # The one shape where a second URL in the output IS disambiguable: the
    # command names its review's id at the create verb, so authorship is tied to
    # the create ACTION rather than inferred from co-occurrence.
    out = f"published {_REVIEW}\nsee also {_OTHER}"
    assert mod.arm(_payload("my-review-cli review publish 4271", out)) == [_REVIEW]
    assert [c["url"] for c in launches] == [_REVIEW]


def test_a_named_id_that_the_output_does_not_show_arms_nothing(launches):
    # The command said which review it acted on and the output shows a
    # different one — a mismatch, not a licence to arm whatever URL is present.
    assert mod.arm(_payload("my-review-cli review publish 4271",
                            f"published {_OTHER}")) == []
    assert launches == []


def test_a_named_id_matching_two_repos_reviews_arms_nothing(launches):
    # Same numeric id, two different repos on one host: `review publish 42`
    # cannot say which, so the id tie is ambiguous and arms neither.
    a = "https://reviews.example.com/team/alpha/review/42"
    b = "https://reviews.example.com/team/beta/review/42"
    assert mod.arm(_payload("my-review-cli review publish 42",
                            f"{a} and {b}")) == []
    assert launches == []


# --- the probe gate -----------------------------------------------------------

def test_no_probe_configured_means_no_launch(tmp_path, monkeypatch, launches):
    # Core ships no probe. A poller without one could only append
    # PROBE_UNREADABLE until its cap — and would register itself, silencing the
    # guardian's honest "arm one by hand" nudge.
    monkeypatch.setattr("difficulty_channel.authority.LOCAL_IDENTITY_PATH",
                        tmp_path / "absent-identity.local")
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == []
    assert launches == []


def test_probe_resolves_from_the_identity_file(tmp_path, monkeypatch):
    assert detect.review_probe() == _PROBE
    monkeypatch.setattr("difficulty_channel.authority.LOCAL_IDENTITY_PATH",
                        tmp_path / "absent-identity.local")
    assert detect.review_probe() == ""


# --- dedup --------------------------------------------------------------------

def test_a_review_already_in_the_registry_is_skipped(tmp_path, launches):
    (tmp_path / "registry.json").write_text(
        json.dumps({_IDENTITY: {"out": "/tmp/x.log", "status": "running", "pid": 1}}),
        encoding="utf-8",
    )
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == []
    assert launches == []


def test_a_second_fire_does_not_launch_a_second_poller(launches):
    # The registry entry appears only once the poller starts, so back-to-back
    # calls would both see it empty; the atomic claim marker is what dedups.
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == [_REVIEW]
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == []
    assert len(launches) == 1


# --- payload robustness (fail-open) -------------------------------------------

@pytest.mark.parametrize("payload", [
    {},
    {"tool_name": "Bash"},
    {"tool_name": "Bash", "tool_input": "not-a-dict", "tool_response": "x"},
    {"tool_name": "Bash", "tool_input": {"command": 7}, "tool_response": None},
    {"tool_name": "Bash", "tool_input": {"command": _CREATE_CMD},
     "tool_response": {"unexpected": ["shape"]}},
])
def test_malformed_payload_is_a_silent_exit_zero(monkeypatch, capsys, payload, launches):
    rc, out = _run(monkeypatch, capsys, payload)
    assert rc == 0
    assert out == ""
    assert launches == []


def test_unparseable_stdin_is_a_silent_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert mod.main() == 0
    assert capsys.readouterr().out == ""


def test_a_failing_launch_is_not_reported_as_armed(monkeypatch, capsys):
    monkeypatch.setattr(mod, "launch", lambda url, out, probe: False)
    rc, out = _run(monkeypatch, capsys, _payload(_CREATE_CMD, f"created {_REVIEW}"))
    assert rc == 0
    assert out == ""


def test_a_failing_launch_releases_its_claim_so_a_retry_can_arm(monkeypatch):
    # A claim held past a spawn that never happened would disable autopilot for
    # that review forever: the registry entry the dedup falls back on is written
    # by the poller, which is exactly what failed to start.
    attempts = []

    def _flaky(url, out, probe):
        attempts.append(url)
        return len(attempts) > 1

    monkeypatch.setattr(mod, "launch", _flaky)
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == []
    assert mod.arm(_payload(_CREATE_CMD, f"created {_REVIEW}")) == [_REVIEW]
    assert attempts == [_REVIEW, _REVIEW]


@pytest.mark.parametrize("response,expected", [
    ("plain string", "plain string"),
    ({"stdout": "out", "stderr": "err"}, "out\nerr"),
    ([{"type": "text", "text": "block"}], "block"),
    (None, ""),
    (17, ""),
])
def test_result_text_flattens_every_tool_response_shape(response, expected):
    assert mod.result_text(response) == expected


# --- advisory + end-to-end launch ---------------------------------------------

def test_advisory_names_the_review_it_armed(monkeypatch, capsys, launches):
    rc, out = _run(monkeypatch, capsys, _payload(_CREATE_CMD, f"created {_REVIEW}"))
    assert rc == 0
    assert "review-monitor" in out
    assert _REVIEW in out


def test_end_to_end_launch_writes_markers_and_registers(monkeypatch, capsys, tmp_path):
    """The one test that really spawns: proves the detached launch reaches
    review-monitor.sh with a usable probe and that the hook returns without
    waiting on it."""
    rc, out = _run(monkeypatch, capsys, _payload(_CREATE_CMD, f"created {_REVIEW}"))
    assert rc == 0
    assert _REVIEW in out

    safe = _IDENTITY.replace("/", "-").replace(".", "-")
    log = tmp_path / "agent-home" / "state" / "review-monitor" / f"{safe}.out"
    registry_file = tmp_path / "registry.json"

    def _registry():
        try:
            return json.loads(registry_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    # Wait on the LAST thing the poller does (its `done` registry write), not on
    # the CHECK_FAILED marker: the marker is emitted first, so waiting on it
    # leaves a window in which the registry still reads `running`.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _registry().get(_IDENTITY, {}).get("status") == "done":
            break
        time.sleep(0.05)

    assert log.exists(), f"poller wrote no log at {log}"
    markers = log.read_text(encoding="utf-8")
    assert "MONITOR_STARTED" in markers
    assert "CHECK_FAILED" in markers
    assert _registry()[_IDENTITY]["status"] == "done"
