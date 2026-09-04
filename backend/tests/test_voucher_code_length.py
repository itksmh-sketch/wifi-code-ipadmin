"""Voucher code length + collision handling.

Pure unit tests -- no server, no DB. The collision path is exercised with a stub
session so the regeneration loop, its bounds and its uniqueness *scope* can be
asserted directly; a live-DB test would only tell us the UNIQUE constraint works.
"""
import asyncio
import string

import pytest

from src.modules.vouchers.engine import (
    CODE_ALPHABET,
    DEFAULT_CODE_LENGTH,
    MAX_CODE_LENGTH,
    MAX_COLLISION_ROUNDS,
    MIN_CODE_LENGTH,
    generate_unique_codes,
    generate_voucher_code,
    max_codes_per_batch,
    validate_code_length,
)


def _symbols(code: str) -> str:
    """The code without the printed grouping dashes."""
    return code.replace("-", "")


# ---------------------------------------------------------------------------
# Shape: length is honoured, format and character set are not touched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [8, 9, 12, 16, 17, 24])
def test_generated_codes_have_the_requested_length(length):
    for _ in range(50):
        code = generate_voucher_code(length)
        assert len(_symbols(code)) == length


@pytest.mark.parametrize("length", [8, 9, 12, 16, 17, 24])
def test_format_stays_groups_of_four(length):
    groups = generate_voucher_code(length).split("-")
    assert all(len(g) == 4 for g in groups[:-1])
    assert 1 <= len(groups[-1]) <= 4


def test_character_set_is_unchanged():
    # A-Z and 0-9 only: the set FreeRADIUS' default safe_characters already covers,
    # so a length change cannot surface a code that breaks the RADIUS match.
    assert CODE_ALPHABET == string.ascii_uppercase + string.digits
    seen = set()
    for _ in range(200):
        seen.update(_symbols(generate_voucher_code(MIN_CODE_LENGTH)))
    assert seen <= set(CODE_ALPHABET)


def test_default_length_is_unchanged_at_16():
    assert DEFAULT_CODE_LENGTH == 16
    code = generate_voucher_code()
    assert len(_symbols(code)) == 16
    assert [len(g) for g in code.split("-")] == [4, 4, 4, 4]


# ---------------------------------------------------------------------------
# Bounds, enforced server-side
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [0, 1, 7, 25, 64, -8])
def test_out_of_bounds_length_is_rejected(length):
    with pytest.raises(ValueError, match="between"):
        validate_code_length(length)


@pytest.mark.parametrize("length", [MIN_CODE_LENGTH, DEFAULT_CODE_LENGTH, MAX_CODE_LENGTH])
def test_in_bounds_length_is_accepted(length):
    validate_code_length(length)


def test_out_of_bounds_length_is_rejected_by_the_generator_entry_point(stub_db):
    with pytest.raises(ValueError, match="between"):
        asyncio.run(generate_unique_codes(stub_db, 1, MIN_CODE_LENGTH - 1))


# ---------------------------------------------------------------------------
# Batch vs. code space
# ---------------------------------------------------------------------------

def test_batch_too_large_for_length_is_rejected(stub_db):
    # 36^8 * 1% still leaves room for any real batch, so squeeze the space instead
    # of asking for an impossible quantity: request more than the length allows.
    limit = max_codes_per_batch(MIN_CODE_LENGTH)
    with pytest.raises(ValueError, match="do not fit safely"):
        asyncio.run(generate_unique_codes(stub_db, limit + 1, MIN_CODE_LENGTH))


def test_batch_limit_grows_with_length():
    assert max_codes_per_batch(MIN_CODE_LENGTH) < max_codes_per_batch(MAX_CODE_LENGTH)
    # A full 500-voucher batch (1000 strings) fits at every offered length.
    assert max_codes_per_batch(MIN_CODE_LENGTH) > 500 * 2


def test_saturated_space_errors_instead_of_looping(stub_db_all_taken):
    with pytest.raises(ValueError, match="Could not find"):
        asyncio.run(generate_unique_codes(stub_db_all_taken, 5, MIN_CODE_LENGTH))
    # Bounded: it gave up after the round cap rather than spinning.
    assert stub_db_all_taken.queries == MAX_COLLISION_ROUNDS


# ---------------------------------------------------------------------------
# Collision regeneration and its uniqueness scope
# ---------------------------------------------------------------------------

def test_collisions_are_regenerated(stub_db_first_round_taken):
    codes = asyncio.run(generate_unique_codes(stub_db_first_round_taken, 20, DEFAULT_CODE_LENGTH))
    assert len(codes) == 20
    assert len(set(codes)) == 20, "returned codes must be unique among themselves"
    # Every candidate offered in round one was reported as taken, so none of them
    # may appear in the result.
    assert not (set(codes) & stub_db_first_round_taken.taken)
    assert stub_db_first_round_taken.queries >= 2, "a collision must trigger another round"


def test_collision_check_is_globally_scoped_over_both_columns(stub_db):
    asyncio.run(generate_unique_codes(stub_db, 3, DEFAULT_CODE_LENGTH))
    sql = stub_db.last_sql
    # vouchers.code and vouchers.username each carry a platform-wide UNIQUE
    # constraint, so the lookup must cover both columns and must NOT be narrowed
    # to one operator -- that would pass here and fail on INSERT.
    assert "vouchers.code IN" in sql
    assert "vouchers.username IN" in sql
    assert "isp_operator_id" not in sql


def test_no_codes_requested_does_not_query(stub_db):
    assert asyncio.run(generate_unique_codes(stub_db, 0, DEFAULT_CODE_LENGTH)) == []
    assert stub_db.queries == 0


# ---------------------------------------------------------------------------
# Stub session: records the emitted SQL and decides what counts as "taken"
# ---------------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class StubDB:
    """Stands in for AsyncSession.execute() over the membership query."""

    def __init__(self, taken_policy=None):
        self.queries = 0
        self.last_sql = ""
        self.taken = set()
        self._policy = taken_policy or (lambda candidates, round_no: set())

    async def execute(self, stmt):
        self.queries += 1
        self.last_sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        candidates = self._candidates_from(stmt)
        hits = self._policy(candidates, self.queries)
        self.taken |= hits
        # The real query returns (code, username) pairs; a hit on either column.
        return _Result([(h, h) for h in hits])

    @staticmethod
    def _candidates_from(stmt):
        found = set()
        for param in stmt.compile().params.values():
            if isinstance(param, (list, tuple, set)):
                found.update(param)
            elif isinstance(param, str):
                found.add(param)
        return found


@pytest.fixture
def stub_db():
    return StubDB()


@pytest.fixture
def stub_db_all_taken():
    return StubDB(lambda candidates, round_no: set(candidates))


@pytest.fixture
def stub_db_first_round_taken():
    return StubDB(lambda candidates, round_no: set(candidates) if round_no == 1 else set())
