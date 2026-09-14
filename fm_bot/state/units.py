"""Exact money units and calendar-aware conversions.

Amounts always carry a currency and a payment period. Arithmetic is integer
minor-unit arithmetic; mixing currencies or periods raises :class:`UnitError`
instead of producing a meaningless number. A weekly rate is never added to a
monthly obligation: callers expand recurring amounts over a calendar window
with :func:`payment_dates` / :func:`total_over` first.

The bridge reports native whole-pound GBP integers (``balance``,
``transfer_budget``, ``wage_budget_weekly``, ``weekly_wage_gbp``). Use
:meth:`Money.native_gbp` for those so the unit basis is recorded explicitly.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable


class UnitError(ValueError):
    """A money operation mixed incompatible units or lost exactness."""


class Period(str, Enum):
    ONCE = "once"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ANNUAL = "annual"


MINOR_UNITS: dict[str, int] = {"GBP": 100, "EUR": 100, "USD": 100}


@dataclass(frozen=True)
class Money:
    """An exact amount in integer minor units (pence for GBP)."""

    minor: int
    currency: str = "GBP"
    period: Period = Period.ONCE

    def __post_init__(self):
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            raise UnitError(f"minor units must be an int, got {type(self.minor).__name__}")
        if self.currency not in MINOR_UNITS:
            raise UnitError(f"unknown currency {self.currency!r}")
        if not isinstance(self.period, Period):
            object.__setattr__(self, "period", Period(self.period))

    # ----- construction -----
    @classmethod
    def of(cls, major: int | str | Decimal, currency: str = "GBP", period: Period | str = Period.ONCE) -> "Money":
        """Build from a major-unit amount (pounds). Fractions finer than the minor unit are rejected."""
        if isinstance(major, bool) or isinstance(major, float):
            raise UnitError("floats are not exact; pass an int, str or Decimal")
        try:
            value = Decimal(str(major)) if not isinstance(major, Decimal) else major
        except InvalidOperation as exc:
            raise UnitError(f"invalid amount {major!r}") from exc
        scale = MINOR_UNITS.get(currency)
        if scale is None:
            raise UnitError(f"unknown currency {currency!r}")
        minor = value * scale
        if minor != minor.to_integral_value():
            raise UnitError(f"{major} {currency} has more precision than the minor unit allows")
        return cls(int(minor), currency, Period(period))

    @classmethod
    def native_gbp(cls, pounds: int, period: Period | str = Period.ONCE) -> "Money":
        """Bridge integers are whole pounds; keep that basis explicit."""
        if isinstance(pounds, bool) or not isinstance(pounds, int):
            raise UnitError("native GBP amounts from the bridge are integers")
        return cls(pounds * MINOR_UNITS["GBP"], "GBP", Period(period))

    @classmethod
    def zero(cls, currency: str = "GBP", period: Period | str = Period.ONCE) -> "Money":
        return cls(0, currency, Period(period))

    # ----- inspection -----
    def major(self) -> Decimal:
        return Decimal(self.minor) / MINOR_UNITS[self.currency]

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    @property
    def is_negative(self) -> bool:
        return self.minor < 0

    def __str__(self) -> str:
        suffix = {Period.ONCE: "", Period.WEEKLY: "/week", Period.MONTHLY: "/month", Period.ANNUAL: "/year"}[self.period]
        return f"{self.currency} {self.major():,.2f}{suffix}"

    def to_json(self) -> dict[str, Any]:
        return {"minor": self.minor, "currency": self.currency, "period": self.period.value}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Money":
        return cls(int(data["minor"]), data["currency"], Period(data.get("period", "once")))

    # ----- arithmetic -----
    def _compatible(self, other: "Money", op: str) -> None:
        if not isinstance(other, Money):
            raise UnitError(f"cannot {op} Money and {type(other).__name__}")
        if other.currency != self.currency:
            raise UnitError(f"cannot {op} {self.currency} and {other.currency} without an explicit conversion")
        if other.period != self.period:
            raise UnitError(f"cannot {op} a {self.period.value} amount and a {other.period.value} amount; expand over a calendar window first")

    def __add__(self, other: "Money") -> "Money":
        self._compatible(other, "add")
        return Money(self.minor + other.minor, self.currency, self.period)

    def __sub__(self, other: "Money") -> "Money":
        self._compatible(other, "subtract")
        return Money(self.minor - other.minor, self.currency, self.period)

    def __neg__(self) -> "Money":
        return Money(-self.minor, self.currency, self.period)

    def __abs__(self) -> "Money":
        return Money(abs(self.minor), self.currency, self.period)

    def __lt__(self, other: "Money") -> bool:
        self._compatible(other, "compare")
        return self.minor < other.minor

    def __le__(self, other: "Money") -> bool:
        self._compatible(other, "compare")
        return self.minor <= other.minor

    def __gt__(self, other: "Money") -> bool:
        self._compatible(other, "compare")
        return self.minor > other.minor

    def __ge__(self, other: "Money") -> bool:
        self._compatible(other, "compare")
        return self.minor >= other.minor

    def times(self, factor: int | Fraction, rounding: str | None = None) -> "Money":
        """Multiply exactly. A non-integral result requires an explicit rounding mode."""
        if isinstance(factor, bool) or isinstance(factor, float):
            raise UnitError("multiply by an int or Fraction, not a float")
        result = Fraction(self.minor) * Fraction(factor)
        if result.denominator == 1:
            return Money(int(result), self.currency, self.period)
        if rounding is None:
            raise UnitError(f"{self} x {factor} is not a whole number of minor units; pass rounding=")
        quantised = Decimal(result.numerator) / Decimal(result.denominator)
        return Money(int(quantised.to_integral_value(rounding=rounding)), self.currency, self.period)

    def with_period(self, period: Period | str) -> "Money":
        """Relabel the period of a per-occurrence amount. This is not a conversion."""
        return Money(self.minor, self.currency, Period(period))

    def as_once(self) -> "Money":
        return Money(self.minor, self.currency, Period.ONCE)


def sum_money(items: Iterable[Money], currency: str = "GBP", period: Period | str = Period.ONCE) -> Money:
    """Sum amounts that share one currency and period; an empty sum is explicit zero."""
    total = Money.zero(currency, Period(period))
    for item in items:
        total = total + item
    return total


# ----- calendar-aware recurrence -----

def _clamp_day(year: int, month: int, day: int) -> dt.date:
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(day, last))


def payment_dates(period: Period | str, first: dt.date, until: dt.date, *, last: dt.date | None = None) -> list[dt.date]:
    """All payment dates in ``[first, until]`` for a recurrence anchored on ``first``.

    ``last`` optionally ends the recurrence earlier than ``until`` (a contract
    end date). Monthly recurrences keep the anchor's day-of-month and clamp to
    shorter months, matching the ordinary treatment of month-end obligations.
    """
    period = Period(period)
    stop = until if last is None else min(until, last)
    if first > stop:
        return []
    if period is Period.ONCE:
        return [first]
    dates: list[dt.date] = []
    if period is Period.WEEKLY:
        current = first
        while current <= stop:
            dates.append(current)
            current += dt.timedelta(days=7)
        return dates
    index = 0
    while True:
        if period is Period.MONTHLY:
            months = first.month - 1 + index
            year, month = first.year + months // 12, months % 12 + 1
            current = _clamp_day(year, month, first.day)
        else:  # ANNUAL
            current = _clamp_day(first.year + index, first.month, first.day)
        if current > stop:
            return dates
        dates.append(current)
        index += 1


def count_occurrences(period: Period | str, first: dt.date, until: dt.date, *, last: dt.date | None = None) -> int:
    return len(payment_dates(period, first, until, last=last))


def total_over(amount: Money, first: dt.date, until: dt.date, *, last: dt.date | None = None) -> Money:
    """Calendar-exact total of a recurring amount inside a window, as a one-off amount."""
    count = count_occurrences(amount.period, first, until, last=last)
    return Money(amount.minor * count, amount.currency, Period.ONCE)


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def parse_game_time(date_value: str, time_value: str | None) -> tuple[dt.date, int]:
    """Return (date, minutes-since-midnight). Time may be absent on some records."""
    day = parse_date(date_value)
    if time_value is None:
        return day, -1
    hours, minutes = time_value.split(":")
    return day, int(hours) * 60 + int(minutes)


def game_time_key(date_value: str, time_value: str | None) -> tuple[int, int]:
    """Monotonic comparison key for in-game time."""
    day, minutes = parse_game_time(date_value, time_value)
    return day.toordinal(), minutes


def quantize_major(value: Decimal, currency: str = "GBP") -> Decimal:
    places = Decimal(1).scaleb(-len(str(MINOR_UNITS[currency])) + 1)
    return value.quantize(places, rounding=ROUND_HALF_EVEN)
