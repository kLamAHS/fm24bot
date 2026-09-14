"""Missing-value semantics.

Null, missing, stale, unsupported and contradicted are distinct states. None
of them is ever silently converted to zero, False or an empty container. Code
that needs a value calls :meth:`Observed.require`, which raises
:class:`Unavailable` with the specific status instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class ValueStatus(str, Enum):
    AVAILABLE = "available"      # a validated value was observed
    NULL = "null"                # the source explicitly reported null for a supported field
    MISSING = "missing"          # the field or route was not collected at all
    STALE = "stale"              # a value exists but its freshness check failed
    UNSUPPORTED = "unsupported"  # the bridge or adapter does not decode this field
    CONTRADICTED = "contradicted"  # two sources disagree; neither is trusted

    @property
    def usable(self) -> bool:
        return self is ValueStatus.AVAILABLE


class Unavailable(LookupError):
    """Raised when a required value has no usable observation."""

    def __init__(self, status: ValueStatus, what: str, reason: str | None = None):
        self.status = status
        self.what = what
        self.reason = reason
        detail = f"{what} is {status.value}"
        if reason:
            detail += f": {reason}"
        super().__init__(detail)


@dataclass(frozen=True)
class Observed(Generic[T]):
    """A value together with its status and provenance.

    ``value`` is only meaningful when ``status`` is AVAILABLE. Every other
    status carries ``value=None`` by construction.
    """

    value: T | None
    status: ValueStatus
    source: str = ""             # observation id, route or adapter name
    observed_at: str | None = None   # wall-clock UTC ISO timestamp
    game_time: str | None = None     # in-game "YYYY-MM-DD HH:MM"
    reason: str | None = None
    what: str = "value"

    def __post_init__(self):
        if self.status is not ValueStatus.AVAILABLE and self.value is not None:
            raise ValueError(f"{self.what}: non-available status {self.status.value} cannot carry a value")

    @property
    def available(self) -> bool:
        return self.status is ValueStatus.AVAILABLE

    def require(self) -> T:
        if self.status is ValueStatus.AVAILABLE:
            return self.value  # type: ignore[return-value]
        raise Unavailable(self.status, self.what, self.reason)

    def map(self, fn) -> "Observed":
        if not self.available:
            return self
        return Observed(fn(self.value), ValueStatus.AVAILABLE, self.source, self.observed_at, self.game_time, None, self.what)

    def to_json(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "status": self.status.value,
            "source": self.source,
            "observed_at": self.observed_at,
            "game_time": self.game_time,
            "reason": self.reason,
            "what": self.what,
        }

    @staticmethod
    def available_value(value: T, source: str = "", observed_at: str | None = None, game_time: str | None = None, what: str = "value") -> "Observed[T]":
        return Observed(value, ValueStatus.AVAILABLE, source, observed_at, game_time, None, what)

    @staticmethod
    def unavailable(status: ValueStatus, what: str, reason: str | None = None, source: str = "", observed_at: str | None = None, game_time: str | None = None) -> "Observed":
        if status is ValueStatus.AVAILABLE:
            raise ValueError("use available_value for available observations")
        return Observed(None, status, source, observed_at, game_time, reason, what)


def null(what: str, source: str = "", **kw) -> Observed:
    return Observed.unavailable(ValueStatus.NULL, what, source=source, **kw)


def missing(what: str, reason: str | None = None, source: str = "", **kw) -> Observed:
    return Observed.unavailable(ValueStatus.MISSING, what, reason, source=source, **kw)


def stale(what: str, reason: str | None = None, source: str = "", **kw) -> Observed:
    return Observed.unavailable(ValueStatus.STALE, what, reason, source=source, **kw)


def unsupported(what: str, reason: str | None = None, source: str = "", **kw) -> Observed:
    return Observed.unavailable(ValueStatus.UNSUPPORTED, what, reason, source=source, **kw)


def contradicted(what: str, reason: str | None = None, source: str = "", **kw) -> Observed:
    return Observed.unavailable(ValueStatus.CONTRADICTED, what, reason, source=source, **kw)


@dataclass
class MissingCapabilityReport:
    """Explains why a piece of work could not proceed.

    Used everywhere a mandatory decision is blocked so the operator sees the
    specific missing capability rather than a generic failure.
    """

    blocked_action: str
    missing: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)

    def add(self, capability: str, reason: str) -> None:
        if capability not in self.missing:
            self.missing.append(capability)
        self.reasons[capability] = reason

    @property
    def blocked(self) -> bool:
        return bool(self.missing)

    def to_json(self) -> dict[str, Any]:
        return {"blocked_action": self.blocked_action, "missing": list(self.missing), "reasons": dict(self.reasons)}
