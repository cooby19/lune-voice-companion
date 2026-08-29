from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lune.config import BudgetConfig
from lune.llm.budget import BudgetLedger, BudgetLocked
from lune.llm.contracts import AttemptUsageFrame, ModelName

NOW = datetime(2026, 8, 27, 2, tzinfo=UTC)


def _reservation_cost(model: ModelName) -> Decimal:
    ledger = BudgetLedger()
    reservation = ledger.reserve_model(
        at=NOW,
        model=model,
        max_input_tokens=8_000,
        max_output_tokens=192,
    )
    return reservation.reserved_twd


def test_fallback_boundary_uses_post_reservation_amount() -> None:
    terra = _reservation_cost("gpt-5.6-terra")
    period = "2026-08"
    below = BudgetLedger(confirmed_twd={period: Decimal("700") - terra - Decimal("0.000001")})
    boundary = BudgetLedger(confirmed_twd={period: Decimal("700") - terra})

    assert (
        below.reserve_conversation(at=NOW, max_input_tokens=8_000, max_output_tokens=192).model
        == "gpt-5.6-terra"
    )
    assert (
        boundary.reserve_conversation(at=NOW, max_input_tokens=8_000, max_output_tokens=192).model
        == "gpt-5.6-luna"
    )


def test_lock_boundary_uses_selected_luna_reservation() -> None:
    luna = _reservation_cost("gpt-5.6-luna")
    ledger = BudgetLedger(confirmed_twd={"2026-08": Decimal("900") - luna})

    with pytest.raises(BudgetLocked):
        ledger.reserve_conversation(at=NOW, max_input_tokens=8_000, max_output_tokens=192)


def test_active_reservations_count_and_new_taipei_month_rolls_over() -> None:
    terra = _reservation_cost("gpt-5.6-terra")
    ledger = BudgetLedger(confirmed_twd={"2026-08": Decimal("700") - terra * Decimal("1.5")})
    first = ledger.reserve_conversation(at=NOW, max_input_tokens=8_000, max_output_tokens=192)
    second = ledger.reserve_conversation(at=NOW, max_input_tokens=8_000, max_output_tokens=192)
    september = datetime(2026, 8, 31, 16, tzinfo=UTC)
    new_month = ledger.reserve_conversation(
        at=september,
        max_input_tokens=8_000,
        max_output_tokens=192,
    )

    assert first.model == "gpt-5.6-terra"
    assert second.model == "gpt-5.6-luna"
    assert new_month.period == "2026-09"
    assert new_month.model == "gpt-5.6-terra"


def test_actual_usage_uses_attempt_fx_and_cache_token_types() -> None:
    ledger = BudgetLedger(BudgetConfig(twd_per_usd=30.0))
    reservation = ledger.reserve_model(
        at=NOW,
        model="gpt-5.6-terra",
        max_input_tokens=8_000,
        max_output_tokens=192,
    )
    usage = AttemptUsageFrame(
        generation_id=1,
        attempt_id=reservation.attempt_id,
        input_tokens=1_000,
        cached_input_tokens=200,
        cache_write_input_tokens=300,
        output_tokens=100,
    )

    settled = ledger.settle(reservation.attempt_id, usage)

    expected_usd = (
        Decimal(500) * Decimal("2.00")
        + Decimal(200) * Decimal("0.20")
        + Decimal(300) * Decimal("2.50")
        + Decimal(100) * Decimal("12.00")
    ) / Decimal(1_000_000)
    assert settled.charged_twd == expected_usd * Decimal(30)
    assert not settled.estimated
    assert settled.reservation.twd_per_usd == Decimal(30)


def test_missing_usage_charges_full_reservation_conservatively() -> None:
    ledger = BudgetLedger()
    reservation = ledger.reserve_model(
        at=NOW,
        model="gpt-5.6-terra",
        max_input_tokens=8_000,
        max_output_tokens=192,
    )

    settled = ledger.settle(reservation.attempt_id, None)

    assert settled.estimated
    assert settled.charged_twd == reservation.reserved_twd


def test_budget_requires_aware_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        BudgetLedger().reserve_conversation(
            at=datetime(2026, 8, 27),
            max_input_tokens=100,
            max_output_tokens=10,
        )


def test_a_local_attempt_costs_nothing_and_settles_at_zero() -> None:
    ledger = BudgetLedger()
    reservation = ledger.reserve_model(
        at=NOW,
        model="qwen3.5-4b-q4-local",
        max_input_tokens=8_000,
        max_output_tokens=192,
    )
    assert reservation.reserved_twd == Decimal(0)

    settled = ledger.settle(
        reservation.attempt_id,
        AttemptUsageFrame(
            generation_id=1,
            attempt_id=reservation.attempt_id,
            input_tokens=8_000,
            output_tokens=192,
        ),
    )
    assert settled.charged_twd == Decimal(0)
    assert ledger.total_with_reservations(NOW) == Decimal(0)


def test_the_cloud_lock_does_not_block_a_free_local_attempt() -> None:
    ledger = BudgetLedger(confirmed_twd={"2026-08": Decimal("950")})

    with pytest.raises(BudgetLocked):
        ledger.reserve_conversation(at=NOW, max_input_tokens=8_000, max_output_tokens=192)

    reservation = ledger.reserve_model(
        at=NOW,
        model="qwen3.5-4b-q4-local",
        max_input_tokens=8_000,
        max_output_tokens=192,
    )
    assert reservation.model == "qwen3.5-4b-q4-local"
