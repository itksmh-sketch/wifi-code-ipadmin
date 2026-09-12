"""SMS segment counting — GSM 03.38 7-bit default alphabet vs. UCS-2, with the
reduced per-segment limits that apply once a message needs concatenation.

Computed entirely locally: never trusts a provider's reported segment/credit
count (Arkesel's synchronous send response carries none — see
ArkeselSMSProvider's class docstring), so this is the sole source of
segment_count for platform-gateway billing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

# GSM 03.38 default alphabet, basic table — 1 septet each. 0x1B (ESC, the
# escape into the extension table below) is not itself a character.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"                     # 0x00–0x0F
    "Δ_ΦΓΛΩΠΨΣΘΞ"                             # 0x10–0x1A
    "ÆæßÉ"                                    # 0x1C–0x1F
    " !\"#¤%&'()*+,-./0123456789:;<=>?"       # 0x20–0x3F
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"        # 0x40–0x5F
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"        # 0x60–0x7F
)
# GSM 03.38 extension table, reached via an ESC septet — 2 septets each. A
# character in neither table (not here, and not in the basic table above)
# forces the ENTIRE message to UCS-2 — GSM-7 and UCS-2 can't be mixed within
# one message.
_GSM7_EXTENDED = "\f^{}\\[~]|€"

_GSM7_BASIC_SET = set(_GSM7_BASIC)
_GSM7_EXTENDED_SET = set(_GSM7_EXTENDED)

_GSM7_SINGLE_LIMIT = 160
_UCS2_SINGLE_LIMIT = 70
# Per-segment limits once concatenation is needed: the 6-byte User Data Header
# costs 7 septets (GSM-7) / 3 code units (UCS-2) out of every segment,
# including the first, the moment a message needs more than one.
_GSM7_CONCAT_LIMIT = 153
_UCS2_CONCAT_LIMIT = 67

__all__ = ["SMSSegmentInfo", "count_sms_segments"]


@dataclass(frozen=True, slots=True)
class SMSSegmentInfo:
    encoding: Literal["gsm7", "ucs2"]
    unit_count: int
    segment_count: int


def _gsm7_septet_count(text: str) -> int | None:
    """Total septets if every character fits GSM-7 (basic or extension table),
    else None."""
    total = 0
    for ch in text:
        if ch in _GSM7_BASIC_SET:
            total += 1
        elif ch in _GSM7_EXTENDED_SET:
            total += 2
        else:
            return None
    return total


def count_sms_segments(text: str) -> SMSSegmentInfo:
    septets = _gsm7_septet_count(text)
    if septets is not None:
        segment_count = (
            1 if septets <= _GSM7_SINGLE_LIMIT
            else math.ceil(septets / _GSM7_CONCAT_LIMIT)
        )
        return SMSSegmentInfo(encoding="gsm7", unit_count=septets, segment_count=segment_count)

    # UCS-2: count UTF-16 code units, not Python codepoints. An astral
    # character (most emoji) is one Python codepoint but a surrogate pair —
    # two code units — on the wire, and it's the wire cost that's billed.
    units = len(text.encode("utf-16-le")) // 2
    segment_count = (
        1 if units <= _UCS2_SINGLE_LIMIT
        else math.ceil(units / _UCS2_CONCAT_LIMIT)
    )
    return SMSSegmentInfo(encoding="ucs2", unit_count=units, segment_count=segment_count)
