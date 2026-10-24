"""Hook fragments: a machine-local seam for hooks Core does not own.

A fragment is a JSON file in ``<agent home>/hook-plugins/`` with the shape of
settings.json's ``hooks`` key. ``install-reminder-hooks.sh`` merges every healthy
fragment into ``<agent home>/settings.json`` after Core's own rows, so a layer above
Core can register hooks without Core naming it.

Contract:

* ``@FRAGMENT_DIR@`` in a command expands to the real directory of the fragment file
  (symlinks resolved), so a fragment can live in another checkout and still point at
  its own scripts. The path is substituted unquoted: write ``"@FRAGMENT_DIR@/x.py"``
  if the directory may contain whitespace.
* An entry is identified by its optional string ``id`` (fragment-only, never written
  into settings.json) within its (event, matcher) group, else by its exact expanded
  command. Nothing is parsed to decide identity. Adding or removing an ``id`` on an
  installed entry keeps the entry in place.
* The plugin entry name is the fragment's file name. Each installed entry is recorded
  in ``<agent home>/hook-plugins.installed.json`` as ``{name: [{event, matcher, id
  (only when given), command}]}``, so a removal touches exactly what the fragment
  installed and never a hand-added hook, even one with an identical command.
* Every run makes a healthy fragment's owned entries equal its current entries, in
  place: an edited command (with a kept ``id``) or a moved directory leaves no dead
  or doubled entry. The installer detaches owned entries before Core's rows are added
  and ``sync`` puts them back, so Core's basename matching never touches them and a
  hand-deleted Core row is restored.
* An entry whose command is already present in its group (a Core row, a hand entry,
  another fragment's entry, an earlier entry of the same fragment) is not installed,
  with a warning naming the holder; an installed entry that later meets an identical
  command is dropped with a warning. One command never fires twice from the seam.
* An entry whose script file name equals that of another entry of the group that is
  not the fragment's own gets an advisory warning naming both; it blocks and removes
  nothing. A differently spelled copy of one gate may fire twice.
* A missing ownership record reads as empty: entries already in settings.json look
  hand-added and are left alone (identical ones block with a warning). An unparsable
  record skips the whole fragment pass, with a warning: nothing is added, removed or
  recorded, and Core's own pass then sees fragment entries as ordinary hooks (it may
  retime one that runs a Core script name, or skip re-adding a hand-deleted Core row).
* A broken fragment (unparsable, malformed, a dangling symlink, or naming a script
  that is missing under its own directory) is skipped with a warning and keeps the
  entries it owns.
* A plugin entry whose file name is gone from the plugin directory loses its owned
  entries.
* When settings.json holds two groups with the same matcher under one event, only the
  first is seen, as in the installer's own ``group_for``.
"""
import json
import os
import shlex
import sys
from pathlib import Path

try:
    from lib.hook_script import hook_script_path
except ImportError:
    from hook_script import hook_script_path

PLACEHOLDER = "@FRAGMENT_DIR@"
FRAGMENT_DIR_NAME = "hook-plugins"
RECORD_NAME = "hook-plugins.installed.json"

_OWNER, _HELD, _ENTRY = "__fragment_owner__", "__fragment_held__", "__fragment_entry__"


def _ident(event, matcher, id_, command):
    if id_ is not None:
        return ("id", event, matcher or None, id_)
    return ("cmd", event, matcher or None, command)


def _entry(event, matcher, id_, command):
    entry = {"event": event, "matcher": matcher or None}
    if id_ is not None:
        entry["id"] = id_
    entry["command"] = command
    return entry


def _entry_ident(e):
    return _ident(e["event"], e["matcher"], e.get("id"), e["command"])


def load_record(path):
    """Return ``(record, error)``: a missing file is an empty record, an unreadable or
    malformed one is ``(None, reason)``."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path} ({exc})"
    if not isinstance(raw, dict):
        return None, f"{path} is not a JSON object"
    record = {}
    for name, entries in raw.items():
        if not isinstance(entries, list):
            return None, f"{path}: {name!r} is not a list"
        record[name] = []
        for e in entries:
            ok = (
                isinstance(e, dict)
                and isinstance(e.get("event"), str)
                and isinstance(e.get("command"), str)
                and (e.get("matcher") is None or isinstance(e["matcher"], str))
                and (e.get("id") is None or isinstance(e["id"], str))
            )
            if not ok:
                return None, f"{path}: a malformed entry under {name!r}"
            record[name].append(_entry(e["event"], e.get("matcher"), e.get("id"), e["command"]))
    return record, None


def save_record(path, record):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(sorted(record.items())), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _expand(value, real_dir):
    if isinstance(value, str):
        return value.replace(PLACEHOLDER, real_dir)
    if isinstance(value, list):
        return [_expand(v, real_dir) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, real_dir) for k, v in value.items()}
    return value


def load_fragment(path):
    """Return ``(entries, error)``: entries is ``[(event, matcher, id, hook_dict)]``."""
    real = os.path.realpath(path)
    real_dir = os.path.dirname(real)
    try:
        with open(real, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"cannot read fragment ({exc})"
    hooks = raw.get("hooks") if isinstance(raw, dict) else None
    if not isinstance(hooks, dict):
        return None, "no top-level 'hooks' object"
    entries = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            return None, f"event {event!r} is not a list of groups"
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                return None, f"a group under {event!r} has no 'hooks' list"
            matcher = group.get("matcher") or None
            for hook in group["hooks"]:
                if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
                    return None, f"a hook under {event!r} has no string 'command'"
                if "id" in hook and not isinstance(hook["id"], str):
                    return None, f"a hook under {event!r} has a non-string 'id'"
                if hook.get("type", "command") != "command":
                    return None, f"a hook under {event!r} is not of type 'command'"
                hook = _expand(hook, real_dir)
                hook.setdefault("type", "command")
                id_ = hook.pop("id", None)
                try:
                    tokens = shlex.split(hook["command"])
                except ValueError as exc:
                    return None, f"unparsable command {hook['command']!r} ({exc})"
                for tok in tokens:
                    if tok.startswith(real_dir + os.sep) and not os.path.exists(tok):
                        return None, f"names a missing script {tok}"
                entries.append((event, matcher, id_, hook))
    return entries, None


def _group_for(event_groups, matcher):
    for g in event_groups:
        if (g.get("matcher") or None) == matcher:
            return g
    g = {} if matcher is None else {"matcher": matcher}
    g["hooks"] = []
    event_groups.append(g)
    return g


def _find_group(hooks, event, matcher):
    """The first group under ``event`` with this matcher (a second one is not seen)."""
    for g in hooks.get(event, []):
        if (g.get("matcher") or None) == matcher:
            return g
    return None


def _is_sentinel(hook):
    return _OWNER in hook


def _command(hook):
    return (hook[_HELD] if _is_sentinel(hook) else hook).get("command")


def detach(hooks, record):
    """Replace each hook a fragment owns by a placeholder that keeps its place.

    The installer calls this before Core's rows are added; ``sync`` must follow and
    resolves every placeholder, so none reaches settings.json.
    """
    for name, entries in record.items():
        for e in entries:
            group = _find_group(hooks, e["event"], e["matcher"])
            if group is None:
                continue
            for i, h in enumerate(group["hooks"]):
                if not _is_sentinel(h) and h.get("command") == e["command"]:
                    group["hooks"][i] = {"command": "", _OWNER: name, _HELD: h, _ENTRY: e}
                    break


def _sentinels(hooks):
    return [
        (event, g, h)
        for event, groups in hooks.items()
        for g in groups
        for h in g.get("hooks", [])
        if _is_sentinel(h)
    ]


def _swap(group, sentinel, replacement):
    i = next(i for i, h in enumerate(group["hooks"]) if h is sentinel)
    if replacement is None:
        del group["hooks"][i]
    else:
        group["hooks"][i] = replacement


def _tidy(hooks, touched):
    for event, matcher in touched:
        group = _find_group(hooks, event, matcher)
        if group is not None and not group["hooks"]:
            hooks[event].remove(group)
        if event in hooks and not hooks[event]:
            del hooks[event]


def _where(event, matcher):
    return f"{event}/{matcher or '*'}"


def _present_fragments(fragment_dir):
    """``{file name: path}`` of the fragment entries. A dangling symlink still counts:
    it is a broken fragment, which keeps its entries, not a removed one."""
    present = {}
    if os.path.isdir(fragment_dir):
        for fname in sorted(os.listdir(fragment_dir)):
            path = os.path.join(fragment_dir, fname)
            if fname.endswith(".json"):
                present[fname] = path
    return present


class _Merge:
    """One ``sync`` run: the hooks being edited and what the run reports."""

    def __init__(self, hooks):
        self.hooks = hooks
        self.changes, self.warnings = [], []
        self.touched = set()
        self.installed_by = {}

    def drop(self, event, group, sentinel, why):
        _swap(group, sentinel, None)
        self.touched.add((event, group.get("matcher") or None))
        self.changes.append(f"- {_where(event, group.get('matcher'))}: {_command(sentinel)} ({why})")

    def owner_of(self, hook):
        if _is_sentinel(hook):
            return f"fragment {hook[_OWNER]}"
        if id(hook) in self.installed_by:
            return f"fragment {self.installed_by[id(hook)]}"
        return "an entry no fragment owns, a Core row or a hand-added hook"

    @staticmethod
    def claim(pool, ident, event, matcher, cmd):
        """Take the removed owned entry this entry continues: the one with its identity,
        else the one holding the same command (an ``id`` added or removed in place)."""
        if pool.get(ident):
            return pool[ident].pop(0)
        for removed in pool.values():
            for r in removed:
                e = r["entry"]
                if (e["event"], e["matcher"], e["command"]) == (event, matcher, cmd):
                    removed.remove(r)
                    return r
        return None

    def remove_owned(self, own):
        """Phase 1: take every owned entry out of its group, remembering where it stood
        (the count of foreign hooks before it) and what it was."""
        owned_ids = {id(s) for _, _, s in own}
        removed = []
        for event, group, s in own:
            before = group["hooks"][: group["hooks"].index(s)]
            slot = sum(1 for h in before if id(h) not in owned_ids)
            _swap(group, s, None)
            self.touched.add((event, group.get("matcher") or None))
            removed.append({"slot": slot, "held": s[_HELD], "entry": s[_ENTRY]})
        return removed

    def warn_same_script(self, name, group, hook, where, own_hooks):
        script = hook_script_path(hook["command"])
        if script is None:
            return
        for h in group["hooks"]:
            other = hook_script_path(_command(h))
            if id(h) not in own_hooks and other is not None and other.name == script.name:
                self.warnings.append(
                    f"{name}: {hook['command']} and {_command(h)} in {where} both run "
                    f"{script.name}; it may fire twice"
                )
                return

    def sync_fragment(self, name, entries, own):
        """Make the fragment's owned entries equal ``entries``; returns its record rows.

        Two phases: every owned entry leaves its group, then the current entries go in
        one by one, each checked only against what its group holds at that moment, so
        no pending entry of this fragment is ever seen as a holder.
        """
        hooks = self.hooks
        pool = {}
        for r in self.remove_owned(own):
            pool.setdefault(_entry_ident(r["entry"]), []).append(r)
        own_hooks, seen, owned, placed = set(), set(), [], {}
        for event, matcher, id_, hook in entries:
            cmd = hook["command"]
            where = _where(event, matcher)
            ident = _ident(event, matcher, id_, cmd)
            if ident in seen:
                self.warnings.append(f"{name}: {cmd} is listed twice in {where}; installed once")
                continue
            seen.add(ident)
            old = self.claim(pool, ident, event, matcher, cmd)
            group = _find_group(hooks, event, matcher)
            holder = next((h for h in (group["hooks"] if group else []) if _command(h) == cmd), None)
            if holder is not None:
                by = self.owner_of(holder)
                if old is None:
                    self.warnings.append(f"{name}: {cmd} not installed, already present in {where} ({by})")
                else:
                    self.changes.append(f"- {where}: {cmd} ({name} displaced)")
                    self.warnings.append(f"{name}: {cmd} dropped, now also present in {where} ({by})")
                continue
            if group is None:
                group = _group_for(hooks.setdefault(event, []), matcher)
            if old is not None and old["held"] == hook:
                hook = old["held"]
            else:
                self.changes.append(f"~ {where}: {cmd}" if old is not None else f"+ {where}: {cmd} ({name})")
            slot = None if old is None else old["slot"]
            earlier = sum(1 for sl in placed.get(id(group), []) if slot is not None and sl <= slot)
            at = len(group["hooks"]) if slot is None else min(slot + earlier, len(group["hooks"]))
            group["hooks"].insert(at, hook)
            placed.setdefault(id(group), []).append(float("inf") if slot is None else slot)
            own_hooks.add(id(hook))
            self.installed_by[id(hook)] = name
            owned.append(_entry(event, matcher, id_, cmd))
            self.warn_same_script(name, group, hook, where, own_hooks)
        for left in (r for removed in pool.values() for r in removed):
            e = left["entry"]
            self.changes.append(f"- {_where(e['event'], e['matcher'])}: {e['command']} ({name} edited)")
        return owned


def sync(hooks, fragment_dir, record):
    """Merge the fragments in ``fragment_dir`` into ``hooks`` (edited in place).

    ``hooks`` must have been through ``detach(hooks, record)``. Returns ``(changes,
    warnings, new_record)``; ``changes`` describes what moved in ``hooks`` and is empty
    when the call left it unchanged.
    """
    run = _Merge(hooks)
    new_record = {}
    present = _present_fragments(fragment_dir)

    for event, group, s in _sentinels(hooks):
        if s[_OWNER] not in present:
            run.drop(event, group, s, f"{s[_OWNER]} gone")

    for name, path in present.items():
        own = [(e, g, s) for e, g, s in _sentinels(hooks) if s[_OWNER] == name]
        entries, error = load_fragment(path)
        if error:
            run.warnings.append(f"{name}: {error}; skipped, owned entries kept")
            for _, group, s in own:
                _swap(group, s, s[_HELD])
            if own:
                new_record[name] = [s[_ENTRY] for _, _, s in own]
            continue
        owned = run.sync_fragment(name, entries, own)
        if owned:
            new_record[name] = owned

    for _, group, s in _sentinels(hooks):
        _swap(group, s, s[_HELD])
    _tidy(hooks, run.touched)
    return run.changes, run.warnings, new_record


def missing_entries(settings_path, fragment_dir):
    """Fragment entries absent from settings.json, as ``[(name, command)]`` (for doctor)."""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            hooks = json.load(fh).get("hooks", {})
    except (OSError, ValueError):
        hooks = {}
    installed = {
        (e, g.get("matcher") or None, h.get("command"))
        for e, gs in hooks.items()
        for g in gs
        for h in g.get("hooks", [])
    }
    missing = []
    for fname, path in _present_fragments(fragment_dir).items():
        entries, error = load_fragment(path)
        if error:
            missing.append((fname, error))
            continue
        missing += [
            (fname, h["command"])
            for e, m, _, h in entries
            if (e, m, h["command"]) not in installed
        ]
    return missing


def main(argv):
    if len(argv) != 3 or argv[1] != "--check":
        print("usage: hook_fragments.py --check <agent home>", file=sys.stderr)
        return 2
    home = Path(argv[2])
    for name, what in missing_entries(home / "settings.json", home / FRAGMENT_DIR_NAME):
        print(f"{name}: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
