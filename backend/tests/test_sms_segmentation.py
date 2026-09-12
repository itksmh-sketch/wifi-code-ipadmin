"""Unit tests for src.modules.sms.segmentation — no server, no DB, safe anywhere.

Covers the boundary cases that matter for billing accuracy: the exact
160/70-char single-segment limits, a single non-GSM-7 character forcing UCS-2
for the whole message (not just that character), the reduced 153/67
per-segment limits once concatenation is needed, GSM-7 extended characters
costing 2 septets without forcing UCS-2, and UTF-16 code-unit counting for
astral characters (surrogate pairs) rather than Python codepoint counting.
"""
import pytest

from src.modules.sms.segmentation import count_sms_segments

# Every character here is confirmed in the GSM-7 basic table (1 septet each).
_GSM7_FILLER = "A"
# Confirmed in neither the GSM-7 basic nor extension table -> forces UCS-2.
_NON_GSM7_CHAR = "ç"


def test_gsm7_just_under_single_segment_limit():
    info = count_sms_segments(_GSM7_FILLER * 159)
    assert info.encoding == "gsm7"
    assert info.unit_count == 159
    assert info.segment_count == 1


def test_gsm7_exactly_at_single_segment_limit():
    info = count_sms_segments(_GSM7_FILLER * 160)
    assert info.encoding == "gsm7"
    assert info.unit_count == 160
    assert info.segment_count == 1


def test_gsm7_one_over_single_segment_limit_uses_concat_limit():
    info = count_sms_segments(_GSM7_FILLER * 161)
    assert info.encoding == "gsm7"
    assert info.unit_count == 161
    # Not "1 full segment + 1 char" -- concatenation drops the per-segment
    # limit to 153, so 161 septets needs ceil(161/153) = 2 segments.
    assert info.segment_count == 2


def test_gsm7_exactly_two_concat_segments():
    info = count_sms_segments(_GSM7_FILLER * 306)  # exactly 2 x 153
    assert info.segment_count == 2


def test_gsm7_one_over_two_concat_segments():
    info = count_sms_segments(_GSM7_FILLER * 307)
    assert info.segment_count == 3


def test_ucs2_just_under_single_segment_limit():
    info = count_sms_segments(_NON_GSM7_CHAR * 69)
    assert info.encoding == "ucs2"
    assert info.unit_count == 69
    assert info.segment_count == 1


def test_ucs2_exactly_at_single_segment_limit():
    info = count_sms_segments(_NON_GSM7_CHAR * 70)
    assert info.encoding == "ucs2"
    assert info.unit_count == 70
    assert info.segment_count == 1


def test_ucs2_one_over_single_segment_limit_uses_concat_limit():
    info = count_sms_segments(_NON_GSM7_CHAR * 71)
    assert info.encoding == "ucs2"
    assert info.unit_count == 71
    assert info.segment_count == 2  # ceil(71/67), not "1 full + 1 char"


def test_single_non_gsm7_character_forces_ucs2_for_whole_message():
    # 159 plain GSM-7 chars would be 1 gsm7 segment on their own; adding one
    # character outside both GSM-7 tables must switch the WHOLE message to
    # UCS-2, not just cost extra for that one character.
    text = _GSM7_FILLER * 159 + _NON_GSM7_CHAR
    info = count_sms_segments(text)
    assert info.encoding == "ucs2"
    assert info.unit_count == 160  # 160 BMP characters -> 160 UTF-16 units
    assert info.segment_count == 3  # ceil(160/67), computed on the UCS-2 scale


@pytest.mark.parametrize("ch", list("\f^{}\\[~]|€"))
def test_gsm7_extension_table_chars_cost_two_septets_without_forcing_ucs2(ch):
    info = count_sms_segments(ch)
    assert info.encoding == "gsm7"  # extension-table chars are still GSM-7
    assert info.unit_count == 2
    assert info.segment_count == 1


def test_mixed_basic_and_extension_chars_sum_septets_not_characters():
    # 80 basic-table chars (1 septet each) + 40 extension-table chars (2
    # septets each) = 160 septets from 120 characters -- exercises septet
    # summing, not character counting.
    text = ("A" * 80) + ("^" * 40)
    info = count_sms_segments(text)
    assert info.encoding == "gsm7"
    assert info.unit_count == 160
    assert info.segment_count == 1


def test_astral_character_counts_as_two_ucs2_units_not_one_codepoint():
    emoji = "\U0001F600"  # a single Python codepoint, but a UTF-16 surrogate pair
    assert len(emoji) == 1
    info = count_sms_segments(emoji)
    assert info.encoding == "ucs2"
    assert info.unit_count == 2
    assert info.segment_count == 1


def test_typical_short_voucher_message_is_one_gsm7_segment():
    text = "Your voucher code is ABCD-1234. Valid for 7 days. Enjoy your internet!"
    info = count_sms_segments(text)
    assert info.encoding == "gsm7"
    assert info.segment_count == 1
