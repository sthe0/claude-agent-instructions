"""The digest-delivery seam of the background debt cycle (`mandate_cycle.notifiers`).

Plugins are written into a tmp plugin dir; nothing reads the real agent home.
"""
from __future__ import annotations

import json
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from agentctl import cost, mandate as rules, mandate_store as store
from mandate_cycle import driver, notifiers


def write_plugin(root, name, body):
    directory = root / notifiers.NOTIFIER_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def plugins(tmp_path):
    root = tmp_path / "mandate-plugins"
    root.mkdir()
    return root


def deliver(tmp_path, root, text="digest text\n"):
    return notifiers.deliver(text, digests_dir=tmp_path / "digests", cycle_id="c1", root=root)


def test_with_no_plugin_the_file_notifier_carries_the_digest_and_leaves_a_copy(tmp_path, plugins):
    delivery = deliver(tmp_path, plugins)
    assert (delivery.notifier, delivery.ok) == (notifiers.FILE_NOTIFIER, True)
    assert (tmp_path / "digests" / "c1.md").read_text(encoding="utf-8") == "digest text\n"


def test_a_missing_plugin_dir_is_the_file_notifier(tmp_path):
    assert deliver(tmp_path, tmp_path / "absent").notifier == notifiers.FILE_NOTIFIER


def test_a_loadable_plugin_is_preferred_over_the_file_notifier(tmp_path, plugins):
    write_plugin(plugins, "chat", """
        NAME = "chat"
        sent = []
        def send(text):
            sent.append(text)
            return True
    """)
    delivery = deliver(tmp_path, plugins)
    assert (delivery.notifier, delivery.ok) == ("chat", True)
    assert (tmp_path / "digests" / "c1.md").is_file()


def test_the_notifier_name_defaults_to_the_file_stem(tmp_path, plugins):
    write_plugin(plugins, "pager", "def send(text):\n    return True\n")
    assert deliver(tmp_path, plugins).notifier == "pager"


def test_a_plugin_that_fails_to_import_is_skipped_for_the_next_one(tmp_path, plugins):
    write_plugin(plugins, "a_broken", "raise RuntimeError('boom')\n")
    write_plugin(plugins, "b_nosend", "NAME = 'nosend'\n")
    write_plugin(plugins, "c_good", "NAME = 'good'\ndef send(text):\n    return True\n")
    assert [n.name for n in notifiers.discover_plugins(plugins)] == ["good"]
    assert deliver(tmp_path, plugins).notifier == "good"


@pytest.mark.parametrize("by", ["name", "stem"])
def test_a_plugin_named_file_cannot_take_the_built_in_notifiers_name(tmp_path, plugins, by):
    if by == "name":
        write_plugin(plugins, "chat", "NAME = 'file'\ndef send(text):\n    return True\n")
    else:
        write_plugin(plugins, "file", "def send(text):\n    return True\n")
    assert notifiers.discover_plugins(plugins) == []
    assert deliver(tmp_path, plugins).notifier == notifiers.FILE_NOTIFIER


def test_a_plugin_that_raises_is_a_failed_delivery_not_a_crash(tmp_path, plugins):
    write_plugin(plugins, "chat", "NAME = 'chat'\ndef send(text):\n    raise OSError('down')\n")
    delivery = deliver(tmp_path, plugins)
    assert (delivery.notifier, delivery.ok) == ("chat", False)
    assert "down" in delivery.detail


def test_a_plugin_that_returns_false_is_a_failed_delivery(tmp_path, plugins):
    write_plugin(plugins, "chat", "NAME = 'chat'\ndef send(text):\n    return False\n")
    assert deliver(tmp_path, plugins).ok is False


def test_the_plugin_dir_env_override_is_honoured(tmp_path, monkeypatch, plugins):
    monkeypatch.setenv(notifiers.PLUGIN_ENV, str(plugins))
    assert notifiers.plugin_root() == plugins


@pytest.fixture
def mandate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCTL_MANDATE_DIR", str(tmp_path / "mandates"))
    monkeypatch.setattr(cost, "COST_LOG", tmp_path / "costs.jsonl")


def test_notify_test_reports_the_file_notifier_and_that_it_does_not_count(mandate_env, plugins, capsys):
    rc = driver.main(["notify-test", "--json"], cfg=driver.Config(plugin_root=plugins))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"ok": True, "notifier": "file", "counts_as_delivered": False}
    assert (store.mandate_dir("core-debt") / "digests" / "notify-test.md").is_file()


def test_notify_test_reports_a_plugin_and_that_it_counts(mandate_env, plugins, capsys):
    write_plugin(plugins, "chat", "NAME = 'chat'\ndef send(text):\n    return True\n")
    rc = driver.main(["notify-test", "--json"], cfg=driver.Config(plugin_root=plugins))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"ok": True, "notifier": "chat", "counts_as_delivered": True}


def test_notify_test_fails_when_the_plugin_does_not_deliver(mandate_env, plugins, capsys):
    write_plugin(plugins, "chat", "NAME = 'chat'\ndef send(text):\n    return False\n")
    rc = driver.main(["notify-test", "--json"], cfg=driver.Config(plugin_root=plugins))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["counts_as_delivered"] is False


# --- redeliver-digest --------------------------------------------------------------------

REDELIVER_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
CYCLE = "c1"
DIGEST_TEXT = "# Background debt cycle c1\nthe stored digest\n"


def redeliver(plugins, cycle=CYCLE):
    seams = driver.Seams(clock=lambda: REDELIVER_NOW)
    return driver.main(["redeliver-digest", "--cycle", cycle, "--json"], seams=seams,
                       cfg=driver.Config(plugin_root=plugins))


def seed_cycle(mandate_env, *, delivery=("file", True)):
    store.save_mandate(rules.new_mandate("core-debt", "alice", REDELIVER_NOW))
    store.append_cycle_row("core-debt", {"cycle_id": CYCLE, "status": "ok"}, now=REDELIVER_NOW)
    directory = store.mandate_dir("core-debt") / "digests"
    directory.mkdir(parents=True)
    (directory / f"{CYCLE}.md").write_text(DIGEST_TEXT, encoding="utf-8")
    if delivery is not None:
        notifier, ok = delivery
        store.record_digest("core-debt", CYCLE, ok=ok, notifier=notifier, delivered_at=REDELIVER_NOW - timedelta(days=1))


def recording_plugin(plugins, tmp_path, working=True):
    """A notifier that appends what it is sent to sent.txt and reports ok while tmp_path/up exists."""
    sent, up = tmp_path / "sent.txt", tmp_path / "up"
    body = (
        "import os\nNAME = 'chat'\ndef send(text):\n"
        f"    open({str(sent)!r}, 'a').write(text + '=====\\n')\n    return os.path.exists({str(up)!r})\n"
    )
    write_plugin(plugins, "chat", body)
    if working:
        up.write_text("", encoding="utf-8")
    else:
        up.unlink(missing_ok=True)
    return sent


def redelivered_events():
    return [e for e in store.read_events("core-debt") if e["event"] == "digest-redelivered"]


def test_redeliver_digest_sends_the_stored_digest_through_the_notifier(mandate_env, plugins, tmp_path, capsys):
    seed_cycle(mandate_env)
    sent = recording_plugin(plugins, tmp_path)
    assert redeliver(plugins) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["notifier"] == "chat" and out["counts_as_delivered"] is True
    text = sent.read_text(encoding="utf-8")
    assert text.count("=====") == 1 and DIGEST_TEXT in text
    assert "the veto window counts from this message" in text.splitlines()[0]
    [event] = redelivered_events()
    assert event["detail"] == {"cycle_id": CYCLE, "ok": True, "notifier": "chat"}


def test_redeliver_digest_records_the_delivery_so_the_veto_window_starts_from_it(mandate_env, plugins, tmp_path):
    seed_cycle(mandate_env)
    recording_plugin(plugins, tmp_path)
    labelled = {"number": 7, "labels": ["backlog", "auto-ok"], "author": "owner", "created_at": "2026-10-01T00:00:00Z"}
    label_rows = [{"ts": "2026-10-08T00:00:00Z", "issue": 7, "label": "auto-ok", "by": "cycle", "cycle_id": CYCLE}]

    def verdict(now):
        return rules.evaluate_candidate(
            labelled, owner="owner", labels_cfg=dict(rules.DEFAULT_LABELS), label_rows=label_rows,
            digests=store.read_digests("core-debt"), pr_refs=set(), veto_window_hours=24.0, now=now,
        )

    assert verdict(REDELIVER_NOW).reason == "digest-not-delivered"
    assert redeliver(plugins) == 0
    assert store.read_digests("core-debt")[CYCLE] == {
        "ok": True, "notifier": "chat", "delivered_at": rules.format_ts(REDELIVER_NOW),
    }
    assert verdict(REDELIVER_NOW + timedelta(hours=1)).reason == "veto-window"
    assert verdict(REDELIVER_NOW + timedelta(hours=25)).eligible


def test_redeliver_digest_is_idempotent_for_a_delivered_digest(mandate_env, plugins, tmp_path, capsys):
    seed_cycle(mandate_env, delivery=("chat", True))
    sent = recording_plugin(plugins, tmp_path)
    rows_before = store.read_cycle_rows("core-debt")
    assert redeliver(plugins) == 0
    assert json.loads(capsys.readouterr().out)["sent"] is False
    assert not sent.exists()
    assert store.read_cycle_rows("core-debt") == rows_before
    assert redelivered_events() == []


@pytest.mark.parametrize("first", [("chat", False), ("file", True), None])
def test_redeliver_digest_retries_after_a_failed_or_file_only_digest(mandate_env, plugins, tmp_path, first):
    seed_cycle(mandate_env, delivery=first)
    recording_plugin(plugins, tmp_path, working=False)
    assert redeliver(plugins) == 1
    assert store.read_digests("core-debt")[CYCLE]["ok"] is False
    recording_plugin(plugins, tmp_path)
    assert redeliver(plugins) == 0
    assert rules.digest_counts_as_delivered(store.read_digests("core-debt")[CYCLE])
    assert redeliver(plugins) == 0
    assert len(redelivered_events()) == 2


def test_redeliver_digest_refuses_an_unknown_cycle_or_a_missing_file(mandate_env, plugins, tmp_path, capsys):
    seed_cycle(mandate_env)
    sent = recording_plugin(plugins, tmp_path)
    assert redeliver(plugins, cycle="nope") == 1
    assert "unknown cycle" in capsys.readouterr().out
    (store.mandate_dir("core-debt") / "digests" / f"{CYCLE}.md").unlink()
    assert redeliver(plugins) == 1
    assert "no stored digest" in capsys.readouterr().out
    assert not sent.exists()
    assert redelivered_events() == []


def test_redeliver_digest_keeps_the_original_digest_file(mandate_env, plugins, tmp_path):
    seed_cycle(mandate_env)
    recording_plugin(plugins, tmp_path)
    assert redeliver(plugins) == 0
    root = store.mandate_dir("core-debt")
    assert (root / "digests" / f"{CYCLE}.md").read_text(encoding="utf-8") == DIGEST_TEXT
    assert DIGEST_TEXT in (root / "redelivered" / f"{CYCLE}.md").read_text(encoding="utf-8")
