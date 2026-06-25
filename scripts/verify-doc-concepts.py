#!/usr/bin/env python3
"""Verify foundational-concept doc-bindings: doc section present + anchors importable.

For each concept in scripts/doc-bindings.json:
  (a) assert the named markdown heading exists in doc.file
  (b) for each anchor, import the module and getattr each symbol

Use --staged is accepted but ignored (whole-repo check).
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
REGISTRY = SCRIPTS_DIR / "doc-bindings.json"

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _check_section(doc_file: str, section: str, repo_root: Path) -> str | None:
    """Return an error string if the heading is absent, else None."""
    path = repo_root / doc_file
    if not path.exists():
        return f"doc file not found: {doc_file}"
    text = path.read_text(encoding="utf-8")
    for m in _HEADING_RE.finditer(text):
        if m.group(1) == section:
            return None
    return f"heading not found: '{section}' in {doc_file}"


def _check_anchors(anchors: list[dict]) -> list[str]:
    """Return a list of error strings for each missing symbol."""
    errors: list[str] = []
    for anchor in anchors:
        module_name = anchor["module"]
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"cannot import {module_name}: {type(exc).__name__}: {exc}")
            continue
        for sym in anchor.get("symbols", []):
            if not hasattr(mod, sym):
                errors.append(f"{module_name}.{sym} not found")
    return errors


def check(registry_path: Path = REGISTRY, repo_root: Path = REPO_ROOT) -> list[str]:
    """Return a list of error strings (empty = all OK)."""
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    for concept in data.get("concepts", []):
        cid = concept.get("id", "<unknown>")
        doc = concept.get("doc", {})
        section_err = _check_section(doc.get("file", ""), doc.get("section", ""), repo_root)
        if section_err:
            errors.append(f"[{cid}] {section_err}")
        for anchor_err in _check_anchors(concept.get("anchors", [])):
            errors.append(f"[{cid}] {anchor_err}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--staged", action="store_true", help="accepted; ignored")
    parser.parse_args(argv)

    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    errors = check()
    if errors:
        print("verify-doc-concepts: FAIL", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("verify-doc-concepts: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
