"""agentctl.text_shape: the two normalizers, and the line between them.

normalize_for_match exists for one comparison — bytes we REGISTERED against
bytes a client RENDERED — where an invisible Cf character present on one side
alone would otherwise fail a delivery that happened. The tests below pin both
directions of that: what it must look through (Cf), and what it must still
refuse to look through (missing content), because a normalizer that equates
everything would pass the first half and silently destroy the gate.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentctl import text_shape  # noqa: E402

# Spelled as code points rather than string literals: a literal Cf character is
# invisible in this source too, so a reader could not tell which one each case
# pins, and an editor could silently drop it.
SOFT_HYPHEN = chr(0x00AD)
ZERO_WIDTH_SPACE = chr(0x200B)
ZERO_WIDTH_JOINER = chr(0x200D)
BYTE_ORDER_MARK = chr(0xFEFF)


def test_normalize_string_casefolds_and_collapses_whitespace():
    assert text_shape.normalize_string("  ## Stage   1\n Done  ") == "## stage 1 done"
    assert text_shape.normalize_string(None) == ""


def test_normalize_for_match_drops_format_characters():
    # The live case: a soft hyphen the client had inserted mid-word (registered
    # rendering 7629 chars vs delivered 7627, differing at exactly one U+00AD
    # plus a trailing newline) -- and the other Cf characters a rendering
    # pipeline introduces or drops the same way.
    for invisible in (SOFT_HYPHEN, ZERO_WIDTH_SPACE, ZERO_WIDTH_JOINER, BYTE_ORDER_MARK):
        padded = f"deli{invisible}very gate"
        assert text_shape.normalize_for_match(padded) == text_shape.normalize_for_match("delivery gate")


def test_normalize_for_match_still_casefolds_and_collapses_whitespace():
    assert text_shape.normalize_for_match("  ## Stage   1\n Done  ") == "## stage 1 done"
    assert text_shape.normalize_for_match(None) == ""


def test_normalize_for_match_does_not_equate_missing_content():
    # The property that makes dropping Cf safe: a genuinely absent word or line
    # still fails the comparison. Without it the delivery gate would certify a
    # truncated rendering.
    full = "## Stage 1\nWrite the normalizer\n## Stage 2\nDecouple the certificate"
    missing_word = "## Stage 1\nWrite the\n## Stage 2\nDecouple the certificate"
    missing_line = "## Stage 1\nWrite the normalizer\n## Stage 2"
    assert text_shape.normalize_for_match(full) != text_shape.normalize_for_match(missing_word)
    assert text_shape.normalize_for_match(full) != text_shape.normalize_for_match(missing_line)
    assert text_shape.normalize_for_match(full) not in text_shape.normalize_for_match(missing_line)


def test_normalize_string_still_notices_invisible_padding():
    # Why the two are separate functions: normalize_string also guards the
    # plan-field placeholder checks, where a field padded with invisible
    # characters is something a validator should NOTICE rather than look
    # through.
    assert text_shape.normalize_string(f"t{ZERO_WIDTH_SPACE}odo") not in text_shape.PLACEHOLDER_SET
    assert text_shape.normalize_string("todo") in text_shape.PLACEHOLDER_SET
    assert text_shape.normalize_for_match(f"t{ZERO_WIDTH_SPACE}odo") in text_shape.PLACEHOLDER_SET
