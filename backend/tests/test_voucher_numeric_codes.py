"""Unit tests for the numeric reseller codes and code_key (migrations 061/062).

Covers: the Damm check digit (a published vector, and every single-digit
mistake and neighbouring swap caught), the length rule (12 digits up to 10,000
live codes, one more per tenfold, capped at 16), the code shape, code_key, and
that the alphanumeric generator never makes an all-digit code (those are kept
for numeric codes, whose check digit the portal reads before any lookup).

No server and no database. Safe to run anywhere.
"""
from __future__ import annotations

import re

import pytest

from src.modules.vouchers import engine
from src.modules.vouchers.engine import (
    code_key, damm_check_digit, damm_valid, generate_numeric_code, is_numeric_code_key, numeric_code_length,
)


def test_damm_published_vector():
    # The worked example in Damm's paper and on every reference page.
    assert damm_check_digit("572") == "4"
    assert damm_valid("5724") and not damm_valid("5734") and not damm_valid("5742")


def test_damm_refuses_non_digits():
    assert not damm_valid("12a4") and not damm_valid("")
    assert not damm_valid("１２３")  # full-width digits are not ASCII
    with pytest.raises(ValueError):
        damm_check_digit("12-3")


def test_every_single_digit_mistake_and_neighbour_swap_is_caught():
    for _ in range(300):
        digits = code_key(generate_numeric_code(12))
        assert damm_valid(digits)
        for i in range(12):
            for d in "0123456789":
                if d != digits[i]:
                    assert not damm_valid(digits[:i] + d + digits[i + 1:]), (digits, i, d)
        for j in range(11):
            if digits[j] != digits[j + 1]:
                assert not damm_valid(digits[:j] + digits[j + 1] + digits[j] + digits[j + 2:]), (digits, j)


@pytest.mark.parametrize("live, length", [
    (1, 12), (10_000, 12), (10_001, 13), (100_000, 13), (100_001, 14),
    (1_000_000, 14), (1_000_001, 15), (10_000_001, 16), (100_000_000, 16),
])
def test_length_grows_a_digit_per_tenfold(live, length):
    assert numeric_code_length(live) == length


def test_length_is_capped_at_16():
    with pytest.raises(ValueError):
        numeric_code_length(100_000_001)


def test_guess_odds_stay_at_one_in_ten_million_at_every_step():
    # A guesser who computes the check digit picks from 10^(N-1) codes.
    for live in (10_000, 100_000, 1_000_000, 10_000_000, 100_000_000):
        assert live / 10 ** (numeric_code_length(live) - 1) <= 1e-7


@pytest.mark.parametrize("length, shape", [
    (12, r"\d{4}-\d{4}-\d{4}"), (13, r"\d{4}-\d{4}-\d{4}-\d"), (16, r"\d{4}-\d{4}-\d{4}-\d{4}"),
])
def test_code_shape(length, shape):
    for _ in range(50):
        code = generate_numeric_code(length)
        assert re.fullmatch(shape, code), code
        assert is_numeric_code_key(code_key(code)) and damm_valid(code_key(code))


def test_numeric_code_length_bounds():
    for bad in (11, 17):
        with pytest.raises(ValueError):
            generate_numeric_code(bad)


@pytest.mark.parametrize("raw, key", [
    ("ABCD-EFGH-JKLM-NPQR", "ABCDEFGHJKLMNPQR"),
    ("  abcd efgh-jklm_npqr ", "ABCDEFGHJKLMNPQR"),
    ("1234-5678-9012", "123456789012"),
    ("1234 5678 9012", "123456789012"),
    ("ÄBC-déf", "BCDF"),          # non-ASCII letters are dropped, as the SQL expression drops them
    ("１２３-45", "45"),            # so are full-width digits
    ("----", ""), ("", ""), (None, ""),
])
def test_code_key(raw, key):
    assert code_key(raw) == key


@pytest.mark.parametrize("key, numeric", [
    ("123456789012", True), ("1234567890123456", True), ("12345678901", False),
    ("12345678901234567", False), ("12345678901A", False), ("ABCDEFGHJKLM", False),
])
def test_numeric_code_key_shape(key, numeric):
    assert is_numeric_code_key(key) is numeric


@pytest.mark.asyncio
async def test_alphanumeric_generator_never_makes_an_all_digit_code(monkeypatch):
    drawn = iter(["1111-2222-3333-4444", "AAAA-1111-2222-3333", "5555-6666-7777-8888", "BBBB-2222-3333-4444"])
    monkeypatch.setattr(engine, "generate_voucher_code", lambda length=16: next(drawn))

    async def nothing_taken(db, candidates):
        return set()

    monkeypatch.setattr(engine, "_taken_strings", nothing_taken)
    codes = await engine.generate_unique_codes(None, 2)
    assert sorted(codes) == ["AAAA-1111-2222-3333", "BBBB-2222-3333-4444"]


def test_random_numeric_codes_are_spread_out():
    # Not a randomness test: just that the body isn't constant or sequential.
    codes = {generate_numeric_code(12) for _ in range(200)}
    assert len(codes) == 200
    assert len({c[0] for c in codes}) > 5
