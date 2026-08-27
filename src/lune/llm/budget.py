"""Asia/Taipei monthly reservation ledger for conservative cloud cost control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Final
from uuid import uuid4
from zoneinfo import ZoneInfo

from lune.config import BudgetConfig
from lune.llm.contracts import AttemptUsageFrame, ModelName

_MILLION: Final[Decimal] = Decimal(1_000_000)
PRICE_VERSION: Final[str] = "openai-standard-2026-07-30"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    cache_write_input_usd_per_million: Decimal
    output_usd_per_million: Decimal


PRICES: Final[Mapping[ModelName, ModelPrice]] = MappingProxyType(
    {
        "gpt-5.6-terra": ModelPrice(
            input_usd_per_million=Decimal("2.00"),
            cached_input_usd_per_million=Decimal("0.20"),
            cache_write_input_usd_per_million=Decimal("2.50"),
            output_usd_per_million=Decimal("12.00"),
        ),
        "gpt-5.6-luna": ModelPrice(
            input_usd_per_million=Decimal("0.20"),
            cached_input_usd_per_million=Decimal("0.02"),
            cache_write_input_usd_per_million=Decimal("0.25"),
            output_usd_per_million=Decimal("1.20"),
        ),
    }
)


class BudgetLocked(RuntimeError):
    """The request must remain local because its reservation would reach the lock."""


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    attempt_id: str
    period: str
    model: ModelName
    reserved_twd: Decimal
    twd_per_usd: Decimal
    price_version: str = PRICE_VERSION


@dataclass(frozen=True, slots=True)
class SettledAttempt:
    reservation: AttemptReservation
    charged_twd: Decimal
    estimated: bool
    usage: AttemptUsageFrame | None = None


class BudgetLedger:
    """Track confirmed cost plus all active worst-case reservations by local month."""

    def __init__(
        self,
        config: BudgetConfig | None = None,
        *,
        confirmed_twd: Mapping[str, Decimal] | None = None,
    ) -> None:
        effective = config or BudgetConfig()
        self._timezone = ZoneInfo(effective.timezone)
        self._fx = Decimal(str(effective.twd_per_usd))
        self._fallback = Decimal(str(effective.fallback_at_twd))
        self._lock = Decimal(str(effective.lock_at_twd))
        self._confirmed = dict(confirmed_twd or {})
        self._active: dict[str, AttemptReservation] = {}
        self._settled: list[SettledAttempt] = []

    @property
    def settled_attempts(self) -> tuple[SettledAttempt, ...]:
        return tuple(self._settled)

    @property
    def active_reservations(self) -> tuple[AttemptReservation, ...]:
        return tuple(self._active.values())

    def period_for(self, at: datetime) -> str:
        if at.tzinfo is None:
            raise ValueError("budget timestamps must be timezone-aware")
        return at.astimezone(self._timezone).strftime("%Y-%m")

    def total_with_reservations(self, at: datetime) -> Decimal:
        period = self.period_for(at)
        return self._confirmed.get(period, Decimal()) + sum(
            (
                reservation.reserved_twd
                for reservation in self._active.values()
                if reservation.period == period
            ),
            start=Decimal(),
        )

    def reserve_conversation(
        self,
        *,
        at: datetime,
        max_input_tokens: int,
        max_output_tokens: int,
    ) -> AttemptReservation:
        """Select Terra or Luna atomically, then reserve the selected attempt."""

        baseline = self.total_with_reservations(at)
        terra_cost = self._reservation_cost("gpt-5.6-terra", max_input_tokens, max_output_tokens)
        model: ModelName = (
            "gpt-5.6-luna" if baseline + terra_cost >= self._fallback else "gpt-5.6-terra"
        )
        return self.reserve_model(
            at=at,
            model=model,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
        )

    def reserve_model(
        self,
        *,
        at: datetime,
        model: ModelName,
        max_input_tokens: int,
        max_output_tokens: int,
    ) -> AttemptReservation:
        if max_input_tokens < 0 or max_output_tokens <= 0:
            raise ValueError("reservation token bounds must be non-negative and non-zero")
        reserved_twd = self._reservation_cost(model, max_input_tokens, max_output_tokens)
        if self.total_with_reservations(at) + reserved_twd >= self._lock:
            raise BudgetLocked("monthly cloud budget is locked")
        reservation = AttemptReservation(
            attempt_id=uuid4().hex,
            period=self.period_for(at),
            model=model,
            reserved_twd=reserved_twd,
            twd_per_usd=self._fx,
        )
        self._active[reservation.attempt_id] = reservation
        return reservation

    def settle(
        self,
        attempt_id: str,
        usage: AttemptUsageFrame | None,
    ) -> SettledAttempt:
        reservation = self._active.pop(attempt_id)
        if usage is not None and usage.attempt_id != attempt_id:
            self._active[attempt_id] = reservation
            raise ValueError("usage belongs to a different attempt")
        if usage is None:
            charge = reservation.reserved_twd
            estimated = True
        else:
            charge = self._actual_cost(reservation, usage)
            estimated = False
        self._confirmed[reservation.period] = (
            self._confirmed.get(reservation.period, Decimal()) + charge
        )
        settled = SettledAttempt(
            reservation=reservation,
            charged_twd=charge,
            estimated=estimated,
            usage=usage,
        )
        self._settled.append(settled)
        return settled

    def _reservation_cost(
        self,
        model: ModelName,
        max_input_tokens: int,
        max_output_tokens: int,
    ) -> Decimal:
        if max_input_tokens < 0 or max_output_tokens <= 0:
            raise ValueError("reservation token bounds must be non-negative and non-zero")
        price = PRICES[model]
        worst_input_rate = max(
            price.input_usd_per_million,
            price.cache_write_input_usd_per_million,
        )
        usd = (
            Decimal(max_input_tokens) * worst_input_rate
            + Decimal(max_output_tokens) * price.output_usd_per_million
        ) / _MILLION
        return usd * self._fx

    @staticmethod
    def _actual_cost(
        reservation: AttemptReservation,
        usage: AttemptUsageFrame,
    ) -> Decimal:
        price = PRICES[reservation.model]
        uncached = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_input_tokens
        usd = (
            Decimal(uncached) * price.input_usd_per_million
            + Decimal(usage.cached_input_tokens) * price.cached_input_usd_per_million
            + Decimal(usage.cache_write_input_tokens) * price.cache_write_input_usd_per_million
            + Decimal(usage.output_tokens) * price.output_usd_per_million
        ) / _MILLION
        return usd * reservation.twd_per_usd
