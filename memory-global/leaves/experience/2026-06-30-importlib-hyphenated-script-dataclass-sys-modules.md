---
name: 2026-06-30-importlib-hyphenated-script-dataclass-sys-modules
description: Difficulty — reusing a Python script whose filename has a hyphen (e.g. core-difficulty-digest.py) by loading it via importlib.util.spec_from_file_location: a @dataclass defined inside it raises AttributeError: 'NoneType' has no '__dict__' at import, because the dataclass machinery resolves cls.__module__ through sys.modules, which the spec loader has not populated yet at exec_module time. Fix: register sys.modules[mod_name] = mod BEFORE spec.loader.exec_module(mod). Corollary: importlib-load only single hyphenated MODULES this way; import normal subpackages by name (put scripts/ on sys.path via conftest) rather than loading a package by file path.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (inherited from parent 2026-06-26-critique-primitive-unifies; extracted 2026-06-30 from its ## Contexts implementation-gotcha)"
refs: [2026-06-26-critique-primitive-unifies-conflict-and-principle]
created: 2026-06-30
last_verified: 2026-06-30
---

# Loading a hyphenated-name script via importlib needs sys.modules pre-registration before exec_module, or a @dataclass in it fails

## Difficulty
A hyphenated-filename Python script cannot be imported by module name, so it is loaded via importlib.util.spec_from_file_location + exec_module; a @dataclass defined in it then fails with AttributeError: 'NoneType' has no '__dict__' because @dataclass resolves cls.__module__ through sys.modules, which the spec loader has not populated by exec_module time.

## Order & criterion
Create the spec and module object -> register sys.modules[mod_name] = mod -> THEN call spec.loader.exec_module(mod). For multi-file packages, do not importlib-load by file path at all: put scripts/ on sys.path (conftest) and import the subpackage by name.

**Acceptance check:** The script imports cleanly and the @dataclass instantiates; the AttributeError at import is gone. Verified by the consuming test/script importing the module and constructing the dataclass.

## Contexts

### 2026-06-30 — initial
- Where it arose: Reusing scripts/core-difficulty-digest.py (hyphenated) from tests and other scripts during ADR-0001 S3 implementation; the @dataclass inside it failed at importlib load until sys.modules was pre-registered.
- Working plan: ADR-0001 S3 (core-difficulty-digest clustering + authority routing); reuse the hyphenated digest module from tests.

## Cost
Real debug time during S3 (the AttributeError is opaque — points at NoneType, not at the missing sys.modules entry). One-line fix once localized.

## Self-critique of the agent system
The error message blames the dataclass, not the import mechanism; the localized lesson (pre-register in sys.modules) is the reusable artifact and was previously buried in a parent leaf's Contexts, unsearchable.
