"""Hook fragments: a machine-local seam for hooks Core does not own.

A fragment is a JSON file in ``<agent home>/hook-plugins/`` with the shape of
settings.json's ``hooks`` key. ``install-reminder-hooks.sh`` merges every healthy
fragment into ``<agent home>/settings.json`` after Core's own rows, so a layer above
Core can register hooks without Core naming it.

Contract:

* ``@FRAGMENT_DIR@`` in a command expands to the real directory of the fragment file
  (symlinks resolved), so a fragment can live in another checkout and still point at
  its own scripts.
* The plugin entry name is the fragment's file name. Each installed entry is recorded
  in ``<agent home>/hook-plugins.installed.json`` as ``{name: [[event, matcher,
  command], ...]}``, so a removal touches exactly what the fragment installed and
  never a hand-added hook, even one with an identical command.
* Every run makes a healthy fragment's owned entries equal its current entries: an
  edited command or a moved directory leaves no dead or doubled entry.
* An entry is blocked, with a warning, when its script (``lib/hook_script.py``
  ``script_name``, the same identity Core's installer uses) is already used in its
  (event, matcher) group by an entry the same fragment does not own (a hand entry, a
  Core row, another fragment's entry), so one script never fires twice. The check also
  covers entries the fragment already owns: when Core or a hand edit later adds the
  same script, the fragment's entry is dropped with a warning on that run. Entries of
  one fragment never block each other. Only the first group with a given matcher under
  an event is looked at.
* A lost or corrupt ``hook-plugins.installed.json`` fails safe: it reads as empty, the
  entries already in settings.json look hand-added and are left alone (a warning each
  run), and nothing is removed later. To recover, delete the fragment's entries from
  settings.json by hand and rerun the installer, which installs and records them anew.
* A broken fragment (unparsable, malformed, or naming a script that is missing under
  its own directory) is skipped with a warning and keeps the entries it owns.
* A plugin entry whose fragment file is gone loses its owned entries.
"""
import json
import os
import shlex
import sys
from pathlib import Path

try:
    from lib.hook_script import script_name
except ImportError:
    from hook_script import script_name

PLACEHOLDER = "@FRAGMENT_DIR@"
FRAGMENT_DIR_NAME = "hook-plugins"
RECORD_NAME = "hook-plugins.installed.json"


def _key(event, matcher, command):
    return (event, matcher or None, command)


def load_record(path):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    record = {}
    for name, entries in raw.items():
        if isinstance(entries, list):
            record[name] = [
                _key(*e) for e in entries if isinstance(e, list) and len(e) == 3
            ]
    return record


def save_record(path, record):
    data = {n: [list(k) for k in keys] for n, keys in sorted(record.items())}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def _expand(value, real_dir):
    if isinstance(value, str):
        return value.replace(PLACEHOLDER, real_dir)
    if isinstance(value, list):
        return [_expand(v, real_dir) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, real_dir) for k, v in value.items()}
    return value


def load_fragment(path):
    """Return ``(entries, error)``: entries is ``[(event, matcher, hook_dict)]``."""
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
                hook = _expand(hook, real_dir)
                try:
                    tokens = shlex.split(hook["command"])
                except ValueError as exc:
                    return None, f"unparsable command {hook['command']!r} ({exc})"
                for tok in tokens:
                    if tok.startswith(real_dir + os.sep) and not os.path.exists(tok):
                        return None, f"names a missing script {tok}"
                entries.append((event, matcher, hook))
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


def owned_keys(record):
    """Every (event, matcher, command) key some fragment has installed."""
    return {k for keys in record.values() for k in keys}


def _remove(hooks, event, matcher, command):
    """Drop one instance of an exact entry; drop the group and event it empties.

    True if removed. An identical hand copy of the command keeps its other instance.
    """
    group = _find_group(hooks, event, matcher)
    if group is None:
        return False
    for i, h in enumerate(group["hooks"]):
        if h.get("command") == command:
            del group["hooks"][i]
            break
    else:
        return False
    if not group["hooks"]:
        hooks[event].remove(group)
        if not hooks[event]:
            del hooks[event]
    return True


def sync(hooks, fragment_dir, record):
    """Merge the fragments in ``fragment_dir`` into ``hooks`` (edited in place).

    Returns ``(changes, warnings, new_record)``; ``changes`` describes what moved in
    ``hooks`` and is empty when the call left it unchanged.
    """
    changes, warnings = [], []
    new_record = {n: list(k) for n, k in record.items()}
    present = {}
    if os.path.isdir(fragment_dir):
        for fname in sorted(os.listdir(fragment_dir)):
            path = os.path.join(fragment_dir, fname)
            if fname.endswith(".json") and os.path.isfile(path):
                present[fname] = path

    for name in sorted(set(record) - set(present)):
        for event, matcher, command in record[name]:
            if _remove(hooks, event, matcher, command):
                changes.append(f"- {event}/{matcher or '*'}: {command} ({name} gone)")
        del new_record[name]

    owner = {k: n for n, keys in new_record.items() for k in keys}

    for name, path in present.items():
        entries, error = load_fragment(path)
        if error:
            warnings.append(f"{name}: {error}; skipped, owned entries kept")
            continue
        current = {_key(e, m, h["command"]) for e, m, h in entries}
        for key in new_record.get(name, []):
            if key in current:
                continue
            if _remove(hooks, *key):
                changes.append(f"- {key[0]}/{key[1] or '*'}: {key[2]} ({name} edited)")
            owner.pop(key, None)
        owned = []
        for event, matcher, hook in entries:
            key = _key(event, matcher, hook["command"])
            group = _find_group(hooks, event, matcher)
            siblings = group["hooks"] if group else []
            same = [h for h in siblings if h.get("command") == key[2]]
            sname = script_name(key[2])
            foreign = [
                h["command"]
                for h in siblings
                if script_name(h.get("command")) == sname
                and owner.get(_key(event, matcher, h.get("command"))) != name
            ]
            if owner.get(key) == name and len(same) > 1:
                foreign.append(key[2])
            if owner.get(key) == name and foreign:
                _remove(hooks, event, matcher, key[2])
                owner.pop(key, None)
                changes.append(f"- {event}/{matcher or '*'}: {key[2]} ({name} displaced)")
                warnings.append(
                    f"{name}: {key[2]} dropped, {foreign[0]} now runs {sname} "
                    f"in {event}/{matcher or '*'}"
                )
                continue
            if same:
                existing = same[0]
                if owner.get(key) not in (None, name):
                    warnings.append(f"{name}: {key[2]} is already installed by {owner[key]}")
                elif owner.get(key) == name:
                    owned.append(key)
                    if existing != hook:
                        existing.clear()
                        existing.update(hook)
                        changes.append(f"~ {event}/{matcher or '*'}: {key[2]}")
                else:
                    warnings.append(f"{name}: {key[2]} is already present as a hand entry")
                continue
            if foreign:
                warnings.append(
                    f"{name}: {key[2]} blocked, {foreign[0]} already runs "
                    f"{sname} in {event}/{matcher or '*'}"
                )
                continue
            if group is None:
                group = _group_for(hooks.setdefault(event, []), matcher)
            group["hooks"].append(hook)
            owned.append(key)
            owner[key] = name
            changes.append(f"+ {event}/{matcher or '*'}: {key[2]} ({name})")
        if owned:
            new_record[name] = owned
        else:
            new_record.pop(name, None)
    return changes, warnings, new_record


def missing_entries(settings_path, fragment_dir):
    """Fragment entries absent from settings.json, as ``[(name, command)]`` (for doctor)."""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            hooks = json.load(fh).get("hooks", {})
    except (OSError, ValueError):
        hooks = {}
    installed = {
        _key(e, g.get("matcher"), h.get("command"))
        for e, gs in hooks.items()
        for g in gs
        for h in g.get("hooks", [])
    }
    missing = []
    if os.path.isdir(fragment_dir):
        for fname in sorted(os.listdir(fragment_dir)):
            path = os.path.join(fragment_dir, fname)
            if not (fname.endswith(".json") and os.path.isfile(path)):
                continue
            entries, error = load_fragment(path)
            if error:
                missing.append((fname, error))
                continue
            missing += [
                (fname, h["command"])
                for e, m, h in entries
                if _key(e, m, h["command"]) not in installed
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
