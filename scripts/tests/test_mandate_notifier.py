"""The digest-delivery seam of the background debt cycle (`mandate_cycle.notifiers`).

Plugins are written into a tmp plugin dir; nothing reads the real agent home.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from agentctl import cost, mandate_store as store
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
