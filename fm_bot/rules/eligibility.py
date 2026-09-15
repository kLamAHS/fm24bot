"""Separately sourced selection eligibility (specification 3.2 P0, 7.1, 7.3, SEL 01).

The bridge does not decode injury, suspension, loan absence or competition
registration. Selection therefore treats eligibility as a separate observation
stream: a verified UI reading, an operator declaration, or (for loan absence
only) the bridge's own contract list. Nothing here guesses. A player whose
eligibility has not been observed at the current in-game time is *not*
eligible for submission; he is merely unverified, and the lineup that uses
him is advisory (spec 7.3).

Baseline: everything in this module is deterministic rule logic with no
learned parameters. There is no experiment here.

Component semantics (all four are :class:`~fm_bot.state.status.Observed`):

* ``injury`` - AVAILABLE with value ``None`` means "observed fit / no injury";
  AVAILABLE with a dict (``{"description": ..., "expected_return": ...}``)
  means "observed injured".
* ``suspension`` - same convention: ``None`` is "observed not suspended", a dict
  is a positive suspension.
* ``loan_absence`` - AVAILABLE ``True`` means loaned out and unavailable to us,
  AVAILABLE ``False`` means not on loan elsewhere.
* ``registration`` - AVAILABLE dict ``{"registered": bool, ...}``. ``registered``
  ``False`` is a positive ineligibility for that competition.

An AVAILABLE value is only *verified* when its observation quality is
``ui_verified`` or it came from the bridge's contract list. A ``declared``
observation from the operator or an ``ui_unverified`` reading can prove a
player *ineligible* (we take any positive absence at face value, which is the
conservative direction) but never proves him eligible for submission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable

from ..state.records import PlayerState
from ..state.status import Observed, ValueStatus
from ..state.units import game_time_key
from ..state.views import FixtureView, missing_eligibility

ELIGIBILITY_RULES_VERSION = "eligibility-rules/1.0"

COMPONENTS: tuple[str, ...] = ("injury", "suspension", "loan_absence", "registration")
ELIGIBILITY_STATUSES: tuple[str, ...] = ("verified", "ineligible", "unverified", "missing", "stale")

QUALITY_UI_VERIFIED = "ui_verified"
QUALITY_UI_UNVERIFIED = "ui_unverified"
QUALITY_DECLARED = "declared"
QUALITY_BRIDGE = "bridge"          # used only for loan absence read from bridge contracts
OBSERVATION_QUALITIES: tuple[str, ...] = (QUALITY_UI_VERIFIED, QUALITY_UI_UNVERIFIED, QUALITY_DECLARED)
# Qualities whose *negative* reading counts towards "verified eligible".
VERIFYING_QUALITIES: frozenset[str] = frozenset({QUALITY_UI_VERIFIED, QUALITY_BRIDGE})

BRIDGE_LOAN_SOURCE = "bridge:/players contracts"


# --------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EligibilityObservation:
    """One eligibility reading for one player at one in-game moment.

    ``fixture_identity`` ``None`` means the reading describes the player's
    *current* status and applies to whichever fixture is being planned at the
    same game time; a specific identity restricts it to that fixture (for
    example a cup-tied registration reading).
    """

    player_id: int
    fixture_identity: str | None
    injury: Observed
    suspension: Observed
    loan_absence: Observed
    registration: Observed
    source: str                       # e.g. "ui:squad_screen", "operator"
    observed_at: str                  # wall-clock UTC ISO timestamp
    game_date: str
    game_time: str | None
    quality: str                      # ui_verified | ui_unverified | declared

    def __post_init__(self):
        if self.quality not in OBSERVATION_QUALITIES:
            raise ValueError(f"unknown eligibility observation quality {self.quality!r}")

    def component(self, name: str) -> Observed:
        if name not in COMPONENTS:
            raise KeyError(name)
        return getattr(self, name)

    @property
    def game_key(self) -> tuple[int, int]:
        return game_time_key(self.game_date, self.game_time)

    def applies_to(self, fixture_identity: str | None) -> bool:
        """Current readings apply to any fixture; fixture-specific readings only to that fixture."""
        return self.fixture_identity is None or self.fixture_identity == fixture_identity

    @classmethod
    def clear(cls, player_id: int, *, source: str, observed_at: str, game_date: str, game_time: str | None, quality: str = QUALITY_UI_VERIFIED, fixture_identity: str | None = None, registered: bool | None = True, competition_id: int | None = None) -> "EligibilityObservation":
        """A reading in which every component was observed negative (fit, unsuspended, present, registered).

        ``registered=None`` leaves registration missing, which is the honest
        default when the squad screen does not show competition membership.
        """
        gt = _key(game_date, game_time)
        if registered is None:
            registration = Observed.unavailable(ValueStatus.MISSING, "registration", "competition membership not shown by the source", source)
        else:
            registration = Observed.available_value({"registered": bool(registered), "competition_id": competition_id}, source, observed_at, gt, "registration")
        return cls(player_id, fixture_identity,
                   Observed.available_value(None, source, observed_at, gt, "injury"),
                   Observed.available_value(None, source, observed_at, gt, "suspension"),
                   Observed.available_value(False, source, observed_at, gt, "loan_absence"),
                   registration, source, observed_at, game_date, game_time, quality)

    def to_json(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "fixture_identity": self.fixture_identity, "injury": self.injury.to_json(), "suspension": self.suspension.to_json(), "loan_absence": self.loan_absence.to_json(), "registration": self.registration.to_json(), "source": self.source, "observed_at": self.observed_at, "game_date": self.game_date, "game_time": self.game_time, "quality": self.quality}


def _key(game_date: str, game_time: str | None) -> str:
    return f"{game_date} {game_time or '??:??'}"


def positive_absence(name: str, component: Observed) -> str | None:
    """Return the reason a component positively shows ineligibility, or None."""
    if not component.available:
        return None
    value = component.value
    if name in ("injury", "suspension"):
        if value is None:
            return None
        detail = value.get("description") if isinstance(value, dict) else str(value)
        return f"{name}: {detail or 'observed'}"
    if name == "loan_absence":
        return "loaned out to another club" if value is True else None
    if name == "registration":
        if isinstance(value, dict) and value.get("registered") is False:
            comp = value.get("competition_id")
            return f"not registered{f' for competition {comp}' if comp is not None else ''}"
        return None
    return None


def freshness_status(game_date: str | None, game_time: str | None, snapshot_game_date: str | None, snapshot_game_time: str | None) -> tuple[ValueStatus, str | None]:
    """Compare an observation's game time with the snapshot's (spec 7.1, SEL 01).

    Only a reading at the same in-game moment is current. Earlier readings
    are stale (the game may have advanced through an injury or a suspension
    since); later readings mean the snapshot itself is out of date and are
    reported as stale too, never silently treated as current. A reading
    with no game time can never verify: on the snapshot's day it is
    ``stale`` (it may predate the snapshot's time, and "same day" is not
    "same moment"), and when the snapshot's own time is unknown nothing can
    be compared, so the reading is ``missing`` verification.
    """
    if snapshot_game_date is None:
        return ValueStatus.MISSING, "snapshot has no game date to check freshness against"
    if game_date is None:
        return ValueStatus.MISSING, "observation has no game date"
    obs_day, obs_min = game_time_key(game_date, game_time)
    snap_day, snap_min = game_time_key(snapshot_game_date, snapshot_game_time)
    if obs_day != snap_day:
        direction = "earlier" if obs_day < snap_day else "later"
        return ValueStatus.STALE, f"observed {game_date} which is {direction} than the snapshot game date {snapshot_game_date}"
    if snapshot_game_time is None:
        return ValueStatus.MISSING, f"snapshot has no game time; a reading on {game_date} cannot be verified as current"
    if game_time is None:
        return ValueStatus.STALE, f"observed on {game_date} with no game time; it cannot be shown current at the snapshot game time {snapshot_game_time}"
    if obs_min == snap_min:
        return ValueStatus.AVAILABLE, None
    direction = "earlier" if obs_min < snap_min else "later"
    return ValueStatus.STALE, f"observed at {game_time} which is {direction} than the snapshot game time {snapshot_game_time} on {game_date}"


def _restate(component: Observed, status: ValueStatus, reason: str | None) -> Observed:
    if status is ValueStatus.AVAILABLE:
        return component
    return Observed.unavailable(status, component.what, reason, component.source, component.observed_at, component.game_time)


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

@runtime_checkable
class EligibilityProvider(Protocol):
    """Supplies eligibility dictionaries in the ``views.missing_eligibility`` shape.

    ``status`` is one of :data:`ELIGIBILITY_STATUSES`. Providers are also
    callable ``(player_id, fixture) -> dict`` so they plug straight into
    :func:`fm_bot.state.views.squad_states`.
    """

    def for_player(self, player_id: int, fixture: FixtureView | dict[str, Any] | None) -> dict[str, Any]: ...

    def __call__(self, player_id: int, fixture: FixtureView | dict[str, Any] | None) -> dict[str, Any]: ...


def fixture_identity_of(fixture: FixtureView | dict[str, Any] | str | None) -> str | None:
    if fixture is None:
        return None
    if isinstance(fixture, str):
        return fixture
    if isinstance(fixture, FixtureView):
        return fixture.identity
    return fixture.get("identity")


class NoEligibilityProvider:
    """No eligibility source is installed: every player is ``missing``."""

    def for_player(self, player_id: int, fixture: FixtureView | dict[str, Any] | None = None) -> dict[str, Any]:
        result = missing_eligibility(player_id, fixture if isinstance(fixture, dict) else None)
        result["fixture_identity"] = fixture_identity_of(fixture)
        result["rules_version"] = ELIGIBILITY_RULES_VERSION
        return result

    __call__ = for_player


def derive_status(components: dict[str, Observed], qualities: dict[str, str]) -> tuple[str, bool, list[str]]:
    """Combine four component readings into one eligibility status.

    Precedence: any positive absence -> ``ineligible``; any component
    missing/unsupported/null -> ``missing``; any stale -> ``stale``; all
    observed negative -> ``verified`` when every contributing source is
    verifying, otherwise ``unverified``.
    """
    reasons: list[str] = []
    for name in COMPONENTS:
        reason = positive_absence(name, components[name])
        if reason:
            reasons.append(reason)
    if reasons:
        return "ineligible", True, reasons
    unavailable = {name: components[name] for name in COMPONENTS if not components[name].available}
    if any(c.status is not ValueStatus.STALE for c in unavailable.values()):
        for name, comp in unavailable.items():
            if comp.status is not ValueStatus.STALE:
                reasons.append(f"{name} {comp.status.value}: {comp.reason or 'no observation'}")
        return "missing", False, reasons
    if unavailable:
        for name, comp in unavailable.items():
            reasons.append(f"{name} stale: {comp.reason}")
        return "stale", False, reasons
    unverifying = [name for name in COMPONENTS if qualities.get(name) not in VERIFYING_QUALITIES]
    if unverifying:
        reasons.append("observed clear but not from a verifying source: " + ", ".join(f"{n} ({qualities.get(n)})" for n in unverifying))
        return "unverified", False, reasons
    return "verified", True, ["all four components observed clear at the current game time by a verifying source"]


def _assemble(player_id: int, fixture_identity: str | None, components: dict[str, Observed], qualities: dict[str, str], sources: Iterable[str]) -> dict[str, Any]:
    status, verified, reasons = derive_status(components, qualities)
    result: dict[str, Any] = {"status": status, "verified": verified, "source": ";".join(sorted(set(sources))) or None, "reasons": reasons, "fixture_identity": fixture_identity, "rules_version": ELIGIBILITY_RULES_VERSION}
    for name in COMPONENTS:
        item = components[name].to_json()
        item["quality"] = qualities.get(name)
        result[name] = item
    return result


@dataclass
class DeclaredEligibilityProvider:
    """Eligibility from registered observations, freshness-checked against the snapshot time.

    Readings are registered with :meth:`register`. For each component the
    latest applicable reading (fixture-specific preferred over current, then
    latest game time, then latest wall-clock) is used. A reading from an
    earlier in-game moment than ``snapshot_game_date``/``snapshot_game_time``
    is reported ``stale``, never silently current.
    """

    snapshot_game_date: str | None
    snapshot_game_time: str | None
    observations: dict[int, list[EligibilityObservation]] = field(default_factory=dict)
    name: str = "declared"

    def register(self, observation: EligibilityObservation) -> None:
        self.observations.setdefault(observation.player_id, []).append(observation)

    def register_all(self, observations: Iterable[EligibilityObservation]) -> None:
        for obs in observations:
            self.register(obs)

    def _candidates(self, player_id: int, fixture_identity: str | None) -> list[EligibilityObservation]:
        items = [o for o in self.observations.get(player_id, []) if o.applies_to(fixture_identity)]
        # fixture-specific first, then latest game time, then latest wall clock
        items.sort(key=lambda o: (o.fixture_identity is not None, o.game_key, o.observed_at), reverse=True)
        return items

    def _component(self, name: str, candidates: list[EligibilityObservation]) -> tuple[Observed, str | None, str | None]:
        for obs in candidates:
            comp = obs.component(name)
            if comp.status is ValueStatus.MISSING:
                continue     # this reading did not cover the component; try an older one
            status, reason = freshness_status(obs.game_date, obs.game_time, self.snapshot_game_date, self.snapshot_game_time)
            return _restate(comp, status, reason), obs.quality, obs.source
        return Observed.unavailable(ValueStatus.MISSING, name, f"no {name} observation registered for this player", self.name), None, None

    def for_player(self, player_id: int, fixture: FixtureView | dict[str, Any] | None = None) -> dict[str, Any]:
        identity = fixture_identity_of(fixture)
        candidates = self._candidates(player_id, identity)
        components: dict[str, Observed] = {}
        qualities: dict[str, str] = {}
        sources: list[str] = []
        for name in COMPONENTS:
            comp, quality, source = self._component(name, candidates)
            components[name] = comp
            if quality:
                qualities[name] = quality
            if source:
                sources.append(source)
        return _assemble(player_id, identity, components, qualities, sources)

    __call__ = for_player


@dataclass
class CompositeProvider:
    """Ask providers in order; per component, the first non-missing reading wins.

    Contradictions between providers (both available but disagreeing) are
    reported as ``contradicted`` for that component so neither is trusted.
    """

    providers: list[Any]
    name: str = "composite"

    def for_player(self, player_id: int, fixture: FixtureView | dict[str, Any] | None = None) -> dict[str, Any]:
        identity = fixture_identity_of(fixture)
        answers = [p.for_player(player_id, fixture) if hasattr(p, "for_player") else p(player_id, fixture) for p in self.providers]
        components: dict[str, Observed] = {}
        qualities: dict[str, str] = {}
        sources: list[str] = []
        for name in COMPONENTS:
            chosen, quality = _first_non_missing(name, answers)
            components[name] = chosen
            if quality:
                qualities[name] = quality
            if chosen.source:
                sources.append(chosen.source)
        return _assemble(player_id, identity, components, qualities, sources)

    __call__ = for_player


def component_observed(eligibility: dict[str, Any], name: str) -> tuple[Observed, str | None]:
    """Rebuild a component :class:`Observed` (and its quality) from an eligibility dict."""
    raw = eligibility.get(name)
    if not isinstance(raw, dict):
        return Observed.unavailable(ValueStatus.MISSING, name, "no eligibility observation source is registered", str(eligibility.get("source") or "")), None
    status = ValueStatus(raw.get("status", "missing"))
    value = raw.get("value") if status is ValueStatus.AVAILABLE else None
    return Observed(value, status, raw.get("source") or "", raw.get("observed_at"), raw.get("game_time"), raw.get("reason"), name), raw.get("quality")


def _first_non_missing(name: str, answers: list[dict[str, Any]]) -> tuple[Observed, str | None]:
    chosen: Observed | None = None
    chosen_quality: str | None = None
    for answer in answers:
        comp, quality = component_observed(answer, name)
        if comp.status is ValueStatus.MISSING:
            continue
        if chosen is None:
            chosen, chosen_quality = comp, quality
            continue
        if chosen.available and comp.available and positive_absence(name, chosen) != positive_absence(name, comp):
            return Observed.unavailable(ValueStatus.CONTRADICTED, name, f"{chosen.source} and {comp.source} disagree", f"{chosen.source}|{comp.source}"), None
    if chosen is None:
        return Observed.unavailable(ValueStatus.MISSING, name, f"no provider observed {name}", "composite"), None
    return chosen, chosen_quality


# --------------------------------------------------------------------------
# bridge-observable loan absence
# --------------------------------------------------------------------------

def bridge_loan_absence(employment: list[dict[str, Any]], *, club_id: int | None, game_date: str | None, loan_contracts_supported: bool) -> Observed:
    """Loan absence read from the bridge's contract list.

    A ``loan`` contract at a club other than ours, in force on ``game_date``,
    is direct evidence that the player is loaned out. The *absence* of such a
    contract only proves presence when the bridge reports loan contracts at
    all (``loan_contracts`` capability) and we know which club is ours.
    """
    loans = [c for c in employment if c.get("kind") == "loan"]
    for loan in loans:
        if club_id is None:
            return Observed.unavailable(ValueStatus.MISSING, "loan_absence", "a loan contract exists but the club id needed to tell loaned-in from loaned-out is unknown", BRIDGE_LOAN_SOURCE)
        if loan.get("club_id") != club_id and _in_force(loan, game_date):
            return Observed.available_value(True, BRIDGE_LOAN_SOURCE, game_time=game_date, what="loan_absence")
    if not loan_contracts_supported:
        return Observed.unavailable(ValueStatus.UNSUPPORTED, "loan_absence", "bridge does not report loan contracts for this build", BRIDGE_LOAN_SOURCE)
    if club_id is None:
        return Observed.unavailable(ValueStatus.MISSING, "loan_absence", "own club id unknown", BRIDGE_LOAN_SOURCE)
    return Observed.available_value(False, BRIDGE_LOAN_SOURCE, game_time=game_date, what="loan_absence")


def _in_force(contract: dict[str, Any], game_date: str | None) -> bool:
    if game_date is None:
        return True
    start, end = contract.get("start_date"), contract.get("end_date")
    if start and start > game_date:
        return False
    if end and end < game_date:
        return False
    return True


# --------------------------------------------------------------------------
# selection gate
# --------------------------------------------------------------------------

def verified_eligible(state: PlayerState, fixture: FixtureView | dict[str, Any] | None, *, snapshot_game_date: str | None, snapshot_game_time: str | None, club_id: int | None = None, loan_contracts_supported: bool = False) -> Observed:
    """The ``verified_eligible[p,f]`` term of the lineup constraints (spec 7.1).

    Returns ``Observed[bool]``: AVAILABLE ``True`` only when all four components
    were observed negative by a verifying source at the snapshot's game time;
    AVAILABLE ``False`` when any component positively shows ineligibility (the
    reason names it); otherwise unavailable with the specific status (missing,
    stale, unsupported or contradicted). Loan absence from the bridge contract
    list is merged in and can contradict a provider reading.
    """
    what = "verified_eligible"
    components: dict[str, Observed] = {}
    qualities: dict[str, str] = {}
    for name in COMPONENTS:
        comp, quality = component_observed(state.eligibility, name)
        if comp.available:
            comp = _recheck(comp, snapshot_game_date, snapshot_game_time)
        components[name] = comp
        if quality:
            qualities[name] = quality
    bridge = bridge_loan_absence(state.employment, club_id=club_id, game_date=snapshot_game_date, loan_contracts_supported=loan_contracts_supported)
    components["loan_absence"], qualities["loan_absence"] = _merge_loan(components["loan_absence"], qualities.get("loan_absence"), bridge)
    reasons = [r for r in (positive_absence(name, components[name]) for name in COMPONENTS) if r]
    source = ";".join(sorted({c.source for c in components.values() if c.source}))
    if reasons:
        return Observed(False, ValueStatus.AVAILABLE, source, None, _key(snapshot_game_date or "?", snapshot_game_time), "; ".join(reasons), what)
    for status in (ValueStatus.CONTRADICTED, ValueStatus.UNSUPPORTED, ValueStatus.MISSING, ValueStatus.NULL, ValueStatus.STALE):
        blocked = [n for n in COMPONENTS if components[n].status is status]
        if blocked:
            return Observed.unavailable(status, what, "; ".join(f"{n}: {components[n].reason or status.value}" for n in blocked), source)
    unverifying = [n for n in COMPONENTS if qualities.get(n) not in VERIFYING_QUALITIES]
    if unverifying:
        return Observed.unavailable(ValueStatus.MISSING, what, "observed clear but not by a verifying source: " + ", ".join(f"{n} ({qualities.get(n)})" for n in unverifying), source)
    return Observed.available_value(True, source, game_time=_key(snapshot_game_date or "?", snapshot_game_time), what=what)


def _recheck(comp: Observed, snapshot_game_date: str | None, snapshot_game_time: str | None) -> Observed:
    """Re-verify freshness before submission (spec 7.1: reverify current eligibility)."""
    if not comp.game_time:
        return Observed.unavailable(ValueStatus.MISSING, comp.what, "observation carries no game time so freshness cannot be verified", comp.source, comp.observed_at)
    date_part, _, time_part = comp.game_time.partition(" ")
    status, reason = freshness_status(date_part, None if time_part in ("", "??:??") else time_part, snapshot_game_date, snapshot_game_time)
    return _restate(comp, status, reason)


def _merge_loan(provider: Observed, provider_quality: str | None, bridge: Observed) -> tuple[Observed, str | None]:
    if bridge.available and bridge.value is True:
        if provider.available and provider.value is False:
            return Observed.unavailable(ValueStatus.CONTRADICTED, "loan_absence", f"{provider.source} says present but {BRIDGE_LOAN_SOURCE} shows a loan to another club", f"{provider.source}|{BRIDGE_LOAN_SOURCE}"), None
        return bridge, QUALITY_BRIDGE
    if provider.available:
        return provider, provider_quality
    if bridge.available:
        return bridge, QUALITY_BRIDGE
    # neither usable: prefer the provider's more specific status unless it is plain missing
    return (provider, provider_quality) if provider.status is not ValueStatus.MISSING else (bridge, None)


def eligibility_summary(states: Iterable[PlayerState]) -> dict[str, int]:
    """Counts by eligibility status for operator reports; ``total`` is included."""
    counts = {status: 0 for status in ELIGIBILITY_STATUSES}
    counts["other"] = 0
    total = 0
    for state in states:
        total += 1
        status = (state.eligibility or {}).get("status")
        counts[status if status in counts else "other"] += 1
    counts["total"] = total
    return counts
