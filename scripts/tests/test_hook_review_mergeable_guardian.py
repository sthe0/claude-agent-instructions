"""review-mergeable Stop guardian: fires when a review this session AUTHORED has
no monitor armed in the registry; silent once armed, and silent for a review the
session merely read. Vendor-neutral fixtures — the detector matches by generic
review path segment, never by host."""
import importlib.util
import io
import json
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "hook_review_mergeable_guardian", SCRIPTS / "hook-review-mergeable-guardian.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

detect = mod.review_open_detect

_REVIEW = "https://reviews.example.com/review/4271"
_OTHER_REVIEW = "https://reviews.example.com/review/9008"
_CREATE_CMD = "my-review-cli pr create --title 'fix formatter'"


def _tool_result(text, use_id=None):
    block = {"type": "tool_result", "content": text}
    if use_id:
        block["tool_use_id"] = use_id
    return {"message": {"role": "user", "content": [block]}}


def _bash(command, use_id=None):
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    if use_id:
        block["id"] = use_id
    return {"message": {"role": "assistant", "content": [block]}}


def _assistant(text):
    return {"message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _write(tmp_path, entries):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def _run(monkeypatch, capsys, transcript_path, session="s1", stop_active=False):
    payload = {"transcript_path": str(transcript_path),
               "session_id": session,
               "stop_hook_active": stop_active}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = mod.main()
    return rc, capsys.readouterr().out


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point both the registry and the per-session nudge markers at tmp: the
    hook reads live runtime state otherwise, so a real armed review on this
    machine would silence the suite (and a test run would litter it)."""
    monkeypatch.setenv("CLAUDE_AGENT_HOME", str(tmp_path / "agent-home"))
    monkeypatch.setenv("CLAUDE_MONITORED_REVIEWS", str(tmp_path / "registry.json"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # Same reason for the verb list: a machine that configures its own
    # `review_open_verbs=` must not change what this suite asserts about the
    # Core defaults. An absent file resolves to DEFAULT_REVIEW_OPEN_VERBS.
    monkeypatch.setattr("difficulty_channel.authority.LOCAL_IDENTITY_PATH",
                        tmp_path / "absent-identity.local")
    detect._verb_pattern.cache_clear()


def _arm(tmp_path, identity, status="running"):
    (tmp_path / "registry.json").write_text(
        json.dumps({identity: {"out": "/tmp/x.log", "status": status, "pid": 1}}),
        encoding="utf-8",
    )


# --- detector unit ------------------------------------------------------------

def test_identity_collapses_segment_spellings():
    host = "https://code.example.com"
    idents = {detect.review_identity(f"{host}/{seg}/42")
              for seg in ("review", "pull", "pull-requests", "merge_requests",
                          "review-requests", "pr", "prs", "pulls",
                          "merge-requests")}
    assert idents == {"code.example.com/42"}


def test_identity_keeps_repo_prefix_distinct():
    a = detect.review_identity("https://code.example.com/team/alpha/pull/42")
    b = detect.review_identity("https://code.example.com/team/beta/pull/42")
    assert a and b and a != b


def test_identity_ignores_view_suffix_query_and_fragment():
    base = detect.review_identity("https://code.example.com/pull/42")
    assert detect.review_identity("https://code.example.com/pull/42/files") == base
    assert detect.review_identity("https://code.example.com/pull/42?tab=checks#c1") == base


def test_identity_rejects_non_review_url():
    assert detect.review_identity("https://docs.example.com/guide/intro") is None
    # a review-ish segment without a numeric id is not a review reference
    assert detect.review_identity("https://code.example.com/pulls/open") is None


def test_review_url_detected_through_trailing_punctuation():
    # Tool output routinely reads "created <url>." or "<url>: green". Without
    # trimming, the trailing character defeats the numeric-id match and the
    # review is missed — silently, which is the expensive direction here.
    for text in (f"created {_REVIEW}.", f"status of {_REVIEW}: green",
                 f"{_REVIEW}, {_REVIEW}?"):
        assert detect.review_urls(text) == {"reviews.example.com/4271": _REVIEW}, text


def test_verb_list_override_read_from_identity_file(tmp_path):
    identity = tmp_path / "agent-identity.local"
    identity.write_text("review_open_verbs=my-cli ship, review submit\n", encoding="utf-8")
    verbs = detect.review_open_verbs(identity_path=identity)
    assert verbs == ("my-cli ship", "review submit")
    assert detect.opens_review("my-cli ship --now", verbs)
    assert not detect.opens_review(_CREATE_CMD, verbs)


def test_verb_list_falls_back_to_default_when_unset(tmp_path):
    identity = tmp_path / "absent-identity.local"
    assert detect.review_open_verbs(identity_path=identity) == detect.DEFAULT_REVIEW_OPEN_VERBS


def test_authored_reviews_needs_both_signals():
    seen_only = [_tool_result(f"reviewing {_REVIEW}")]
    assert detect.authored_reviews(seen_only) == {}
    both = [_bash(_CREATE_CMD), _tool_result(f"created {_REVIEW}")]
    assert detect.authored_reviews(both) == {"reviews.example.com/4271": _REVIEW}


def test_authorship_is_correlated_per_review_not_per_session():
    # The session opens ONE review and later merely reads another's status.
    # A session-wide "did I create anything?" flag would claim both; only the
    # created one is authored.
    entries = [_bash(_CREATE_CMD),
               _tool_result(f"review created: {_REVIEW}"),
               _bash("my-review-cli show 9008"),
               _tool_result(f"status of {_OTHER_REVIEW}: green")]
    assert detect.review_urls(f"status of {_OTHER_REVIEW}: green")  # fixture is detectable
    assert detect.authored_reviews(entries) == {"reviews.example.com/4271": _REVIEW}


def test_authorship_pairs_output_by_tool_use_id():
    # Real transcripts carry ids, so the pairing is exact even when a create
    # call and an unrelated read call share one assistant turn.
    entries = [
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "id": "call_create",
             "input": {"command": _CREATE_CMD}},
            {"type": "tool_use", "name": "Bash", "id": "call_read",
             "input": {"command": "my-review-cli show 9008"}}]}},
        {"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_read",
             "content": f"status of {_OTHER_REVIEW}: green"},
            {"type": "tool_result", "tool_use_id": "call_create",
             "content": f"created {_REVIEW}"}]}},
    ]
    assert detect.authored_reviews(entries) == {"reviews.example.com/4271": _REVIEW}


def test_authorship_from_review_named_on_the_create_command_line():
    entries = [_bash(f"my-review-cli review publish {_REVIEW}"),
               _tool_result("published")]
    assert detect.authored_reviews(entries) == {"reviews.example.com/4271": _REVIEW}


def test_authorship_from_bare_id_named_on_the_create_command_line():
    # `... review publish 42` prints nothing useful; the URL arrives from a
    # later status call, and the numeric id is what ties the two together.
    entries = [_bash("my-review-cli review publish 4271 --retries 9008"),
               _tool_result("published"),
               _bash("my-review-cli status 4271"),
               _tool_result(f"status of {_REVIEW}: pending"),
               _bash("my-review-cli show 9008"),
               _tool_result(f"status of {_OTHER_REVIEW}: green")]
    # Only the argument right after the verb is the review: 9008 is a flag's
    # value here, and review 9008 was merely read.
    assert detect.authored_reviews(entries) == {"reviews.example.com/4271": _REVIEW}


def test_a_number_inside_a_create_commands_title_is_not_a_review_id():
    entries = [_bash("my-review-cli pr create --title 'fix 9008 in the parser'"),
               _tool_result("opened"),
               _bash("my-review-cli show 9008"),
               _tool_result(f"status of {_OTHER_REVIEW}: green")]
    assert detect.authored_reviews(entries) == {}


def test_bare_publish_is_not_a_create_verb():
    for cmd in ("npm publish --access public", "cargo publish",
                "docker publish myimage:latest",
                "git commit -m 'publish the new docs'"):
        assert not detect.opens_review(cmd), cmd


def test_verbs_match_whole_tokens_only():
    assert detect.opens_review("gh pr create --fill")
    # the same letters inside a longer word are not the verb
    assert not detect.opens_review("expr create-table")
    assert not detect.opens_review("run-pr-createish --now")


def test_armed_identities_fail_open_on_garbage(tmp_path):
    bad = tmp_path / "registry.json"
    bad.write_text("{not json", encoding="utf-8")
    assert detect.armed_identities(bad) == set()
    assert detect.armed_identities(tmp_path / "missing.json") == set()


# --- hook behaviour -----------------------------------------------------------

def test_fires_when_authored_and_unmonitored(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_bash(_CREATE_CMD),
                          _tool_result(f"review created: {_REVIEW}"),
                          _assistant("Ревью открыто, иду дальше.")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert "review-mergeable" in out
    assert _REVIEW in out


def test_silent_when_monitor_armed(monkeypatch, capsys, tmp_path):
    _arm(tmp_path, "reviews.example.com/4271")
    p = _write(tmp_path, [_bash(_CREATE_CMD),
                          _tool_result(f"review created: {_REVIEW}")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_silent_when_review_only_read(monkeypatch, capsys, tmp_path):
    text = f"status of {_REVIEW}: green"
    # Guard against passing for the wrong reason: silence must come from the
    # authorship filter, not from a fixture URL the detector never sees.
    assert detect.review_urls(text)
    p = _write(tmp_path, [_bash("my-review-cli show 4271"), _tool_result(text)])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_silent_for_a_review_only_read_in_an_authoring_session(monkeypatch, capsys, tmp_path):
    # The session DID open a review (4271, and it is armed, so no nudge for it)
    # and then read another review's status. The read-only one must not be
    # nudged about: the agent does not own a review it merely looked at.
    _arm(tmp_path, "reviews.example.com/4271")
    p = _write(tmp_path, [_bash(_CREATE_CMD),
                          _tool_result(f"review created: {_REVIEW}"),
                          _bash("my-review-cli show 9008"),
                          _tool_result(f"status of {_OTHER_REVIEW}: green")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_bare_publish_command_does_not_claim_authorship(monkeypatch, capsys, tmp_path):
    text = f"status of {_REVIEW}: green"
    assert detect.review_urls(text)  # the fixture URL is detectable
    p = _write(tmp_path, [_bash("npm publish --access public"), _tool_result(text)])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_silent_when_no_review_url(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_bash(_CREATE_CMD), _tool_result("nothing to see")])
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_nudges_once_per_session(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_bash(_CREATE_CMD),
                          _tool_result(f"review created: {_REVIEW}")])
    first_rc, first_out = _run(monkeypatch, capsys, p)
    second_rc, second_out = _run(monkeypatch, capsys, p)
    assert (first_rc, second_rc) == (0, 0)
    assert "review-mergeable" in first_out
    assert second_out == ""


def test_stop_hook_active_guard(monkeypatch, capsys, tmp_path):
    p = _write(tmp_path, [_bash(_CREATE_CMD),
                          _tool_result(f"review created: {_REVIEW}")])
    rc, out = _run(monkeypatch, capsys, p, stop_active=True)
    assert rc == 0
    assert out == ""


def test_malformed_transcript_is_silent(monkeypatch, capsys, tmp_path):
    p = tmp_path / "transcript.jsonl"
    p.write_text("{not json\n[]\nnull\n", encoding="utf-8")
    rc, out = _run(monkeypatch, capsys, p)
    assert rc == 0
    assert out == ""


def test_missing_transcript_is_silent(monkeypatch, capsys, tmp_path):
    rc, out = _run(monkeypatch, capsys, tmp_path / "does-not-exist.jsonl")
    assert rc == 0
    assert out == ""


def test_empty_payload_is_silent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert mod.main() == 0
    assert capsys.readouterr().out == ""


# --- poller (review-monitor.sh) ----------------------------------------------

def _monitor(tmp_path, probe, extra=()):
    out = tmp_path / "markers.log"
    registry = tmp_path / "registry.json"
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "review-monitor.sh"),
         "--review-id", _REVIEW, "--probe", probe, "--out", str(out),
         "--registry", str(registry), "--sleep", "0", *extra],
        capture_output=True, text=True, timeout=60,
    )
    markers = out.read_text(encoding="utf-8") if out.exists() else ""
    registry_data = (json.loads(registry.read_text(encoding="utf-8"))
                     if registry.exists() else {})
    return proc, markers, registry_data


def test_monitor_help_prints_contract_without_side_effects(tmp_path):
    proc = subprocess.run(["bash", str(SCRIPTS / "review-monitor.sh"), "--help"],
                          capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert proc.returncode == 0
    assert "PROBE CONTRACT" in proc.stdout
    assert "unresolved_comments" in proc.stdout
    assert list(tmp_path.iterdir()) == []


def test_monitor_requires_its_arguments(tmp_path):
    proc = subprocess.run(["bash", str(SCRIPTS / "review-monitor.sh")],
                          capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert proc.returncode == 2


def test_monitor_terminal_on_failed_check(tmp_path):
    proc, markers, registry = _monitor(
        tmp_path, "echo tests=failure approved=pending unresolved_comments=0 merged=false")
    assert proc.returncode == 0
    assert "CHECK_FAILED" in markers
    assert registry["reviews.example.com/4271"]["status"] == "done"


def test_monitor_terminal_on_merged(tmp_path):
    _, markers, _ = _monitor(
        tmp_path, "echo tests=success approved=success unresolved_comments=0 merged=true")
    assert "MERGED" in markers
    # merged wins over the approval marker: the review is done, not just landable
    assert "APPROVED" not in markers


def test_monitor_terminal_on_approval(tmp_path):
    _, markers, _ = _monitor(
        tmp_path, "echo tests=success approved=success unresolved_comments=0 merged=false")
    assert "APPROVED" in markers


def test_monitor_registers_itself_while_running(tmp_path):
    _, _, registry = _monitor(
        tmp_path, "echo tests=pending approved=pending unresolved_comments=0 merged=false",
        extra=["--max", "2"])
    entry = registry["reviews.example.com/4271"]
    assert entry["out"].endswith("markers.log")
    assert isinstance(entry["pid"], int)


def test_monitor_caps_out_without_terminal_state(tmp_path):
    _, markers, _ = _monitor(
        tmp_path, "echo tests=pending approved=pending unresolved_comments=0 merged=false",
        extra=["--max", "3"])
    assert "CAP_HIT" in markers


def test_monitor_logs_new_comments_and_keeps_polling(tmp_path):
    # A probe whose unresolved count climbs with each call: NEW_COMMENTS must be
    # logged without ending the run (the review is still moving).
    counter = tmp_path / "n"
    probe = (f"c=$(cat {counter} 2>/dev/null || echo 0); c=$((c+1)); echo $c > {counter}; "
             "echo tests=pending approved=pending unresolved_comments=$c merged=false")
    _, markers, _ = _monitor(tmp_path, probe, extra=["--max", "3"])
    assert "NEW_COMMENTS" in markers
    assert "CAP_HIT" in markers


def test_monitor_survives_a_failing_probe(tmp_path):
    _, markers, _ = _monitor(tmp_path, "exit 7", extra=["--max", "2"])
    assert "PROBE_UNREADABLE" in markers
    assert "CAP_HIT" in markers


def test_monitor_substitutes_the_numeric_id(tmp_path):
    # {num} lets a probe take `<cli> status 4271` while --review-id stays the
    # URL — the only form whose registry key the guardian can match.
    got = tmp_path / "probe-arg"
    _monitor(tmp_path,
             f"echo '{{num}}' > {got}; echo tests=pending approved=pending "
             "unresolved_comments=0 merged=false",
             extra=["--max", "1"])
    assert got.read_text(encoding="utf-8").strip() == "4271"


def test_monitor_warns_that_a_bare_id_cannot_silence_the_guardian(tmp_path):
    out = tmp_path / "markers.log"
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "review-monitor.sh"),
         "--review-id", "4271", "--probe", "echo merged=true",
         "--out", str(out), "--registry", str(tmp_path / "registry.json"),
         "--sleep", "0"],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "not a review URL" in proc.stderr


# --- poller <-> guardian key space -------------------------------------------

def test_arming_a_monitor_by_url_silences_the_guardian(monkeypatch, capsys, tmp_path):
    """The invariant that makes the mechanism whole: the registry key the poller
    writes for a review MUST equal the identity the guardian derives from that
    same review's URL. Two key spaces that never intersect leave the guardian
    nagging about a review that is demonstrably being driven."""
    p = _write(tmp_path, [_bash(_CREATE_CMD),
                          _tool_result(f"review created: {_REVIEW}")])
    before_rc, before_out = _run(monkeypatch, capsys, p, session="s-before")
    assert (before_rc, "review-mergeable" in before_out) == (0, True)

    _monitor(tmp_path, "echo tests=pending approved=pending unresolved_comments=0 merged=false",
             extra=["--max", "1"])  # writes tmp_path/registry.json = $CLAUDE_MONITORED_REVIEWS

    after_rc, after_out = _run(monkeypatch, capsys, p, session="s-after")
    assert after_rc == 0
    assert after_out == ""
