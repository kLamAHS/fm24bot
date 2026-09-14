"""Versioned competition rules profiles (specification 7.1, 11.4, MAT 02).

Each competition *and stage* has its own rules profile: registration
windows, squad size, homegrown requirements, bench size, substitution
allowances and windows, tie progression and deadlines. Every field is an
:class:`~fm_bot.state.status.Observed` value so that nothing is assumed: a
profile that has not been observed for this competition leaves the bench
size ``missing`` rather than defaulting to seven, and the lineup planner
must treat that as a constraint it cannot verify.

Profiles are stored through the versioned settings table under
``competition_rules:<competition_id>:<stage>`` and expanded into
:class:`~fm_bot.state.records.CompetitionContext` records for a fixture.
This module is baseline rule bookkeeping; it contains no estimates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..state.records import CompetitionContext
from ..state.status import Observed, ValueStatus
from ..state.views import FixtureView

COMPETITION_RULES_VERSION = "competition-rules/1.0"
SETTING_PREFIX = "competition_rules"
INDEX_KEY = f"{SETTING_PREFIX}:index"
DEFAULT_STAGE = "current"        # used when a fixture does not identify its stage

RULE_FIELDS: tuple[str, ...] = ("registration_windows", "squad_size_limit", "homegrown_rule", "bench_size", "substitutions_allowed", "substitution_windows", "tie_progression", "deadlines")
SQUAD_FIELDS: tuple[str, ...] = ("squad_size_limit", "homegrown_rule", "registration_windows")
SUBSTITUTION_FIELDS: tuple[str, ...] = ("bench_size", "substitutions_allowed", "substitution_windows")

# Events that require a rules profile to be refreshed (spec 11.4).
REFRESH_EVENT_KINDS: tuple[str, ...] = ("promotion", "relegation", "season_boundary", "competition_context_changed", "stage_changed", "operator_request")


def rule_missing(what: str, reason: str = "no rules profile observed for this competition and stage") -> Observed:
    return Observed.unavailable(ValueStatus.MISSING, what, reason, "none")


@dataclass
class CompetitionRules:
    """Rules in force for one competition stage. No field has a universal default."""

    competition_id: int
    stage: str
    registration_windows: Observed      # list[{"opens","closes","kind"}]
    squad_size_limit: Observed          # int
    homegrown_rule: Observed            # {"minimum": int, "definition": str, ...}
    bench_size: Observed                # int named substitutes
    substitutions_allowed: Observed     # int
    substitution_windows: Observed      # int (stoppages allowed for substitutions)
    tie_progression: Observed           # {"kind": "league"|"single_leg"|"two_legs", "extra_time": bool, "penalties": bool, ...}
    deadlines: Observed                 # list[{"kind","date","time","description"}]
    source: str                         # e.g. "operator", "ui:competition_rules_screen"
    version: int = 0                    # settings version once stored; 0 before storage
    season: str | None = None           # e.g. "2023/24"; refreshed at season boundaries
    division_id: int | None = None      # competition tier context that changes on promotion
    competition_name: str = ""
    rules_version: str = COMPETITION_RULES_VERSION
    observed_at: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def unknown(cls, competition_id: int, stage: str = DEFAULT_STAGE, *, competition_name: str = "", reason: str = "no rules profile observed for this competition and stage") -> "CompetitionRules":
        return cls(competition_id, stage, *(rule_missing(name, reason) for name in RULE_FIELDS), source="none", competition_name=competition_name)

    def field_observed(self, name: str) -> Observed:
        if name not in RULE_FIELDS:
            raise KeyError(name)
        return getattr(self, name)

    @property
    def complete(self) -> bool:
        return all(self.field_observed(name).available for name in RULE_FIELDS)

    def missing_fields(self) -> list[str]:
        return [name for name in RULE_FIELDS if not self.field_observed(name).available]

    def to_json(self) -> dict[str, Any]:
        data = {"competition_id": self.competition_id, "stage": self.stage, "source": self.source, "version": self.version, "season": self.season, "division_id": self.division_id, "competition_name": self.competition_name, "rules_version": self.rules_version, "observed_at": self.observed_at, "notes": list(self.notes)}
        for name in RULE_FIELDS:
            data[name] = self.field_observed(name).to_json()
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CompetitionRules":
        fields = {name: _observed_from_json(data.get(name), name) for name in RULE_FIELDS}
        return cls(int(data["competition_id"]), str(data["stage"]), **fields, source=data.get("source", "none"), version=int(data.get("version", 0)), season=data.get("season"), division_id=data.get("division_id"), competition_name=data.get("competition_name", ""), rules_version=data.get("rules_version", COMPETITION_RULES_VERSION), observed_at=data.get("observed_at"), notes=list(data.get("notes") or []))


def _observed_from_json(raw: dict[str, Any] | None, what: str) -> Observed:
    if not isinstance(raw, dict):
        return rule_missing(what)
    status = ValueStatus(raw.get("status", "missing"))
    value = raw.get("value") if status is ValueStatus.AVAILABLE else None
    return Observed(value, status, raw.get("source") or "", raw.get("observed_at"), raw.get("game_time"), raw.get("reason"), what)


def setting_key(competition_id: int, stage: str | None) -> str:
    return f"{SETTING_PREFIX}:{competition_id}:{stage or DEFAULT_STAGE}"


# --------------------------------------------------------------------------
# refresh policy
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RulesEvent:
    """Something that may invalidate a stored profile (spec 11.4)."""

    kind: str                       # one of REFRESH_EVENT_KINDS or an informational kind
    season: str | None = None
    stage: str | None = None
    division_id: int | None = None
    competition_id: int | None = None
    note: str | None = None


@dataclass
class RefreshDecision:
    refresh: bool
    reasons: list[str]
    rules_version: str = COMPETITION_RULES_VERSION


def needs_refresh(profile: CompetitionRules | None, event: RulesEvent) -> RefreshDecision:
    """Decide whether a rules profile must be re-observed after ``event``.

    A missing profile always needs observing. Promotion, relegation and an
    explicit context change always refresh. A season boundary refreshes
    unless the profile already records the new season; a stage change
    refreshes when the stage differs. Unknown event kinds never refresh on
    their own, but a profile without a recorded season is refreshed by any
    event that carries one, because its provenance cannot be checked.
    """
    if profile is None:
        return RefreshDecision(True, ["no profile stored"])
    if event.competition_id is not None and event.competition_id != profile.competition_id:
        return RefreshDecision(False, ["event concerns a different competition"])
    reasons: list[str] = []
    if event.kind in ("promotion", "relegation", "competition_context_changed", "operator_request"):
        reasons.append(f"{event.kind}{f': {event.note}' if event.note else ''}")
    if event.kind == "season_boundary" and (profile.season is None or event.season is None or event.season != profile.season):
        reasons.append(f"season boundary: profile season {profile.season!r} vs event season {event.season!r}")
    if event.kind == "stage_changed" and event.stage != profile.stage:
        reasons.append(f"stage changed: profile stage {profile.stage!r} vs {event.stage!r}")
    if event.division_id is not None and profile.division_id is not None and event.division_id != profile.division_id:
        reasons.append(f"division changed: {profile.division_id} -> {event.division_id}")
    if profile.season is None and event.season is not None and not reasons:
        reasons.append("profile has no recorded season; provenance cannot be checked against the current season")
    return RefreshDecision(bool(reasons), reasons)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

class RulesProfileRegistry:
    """Stores and loads rules profiles and expands them into competition contexts.

    Storage is the versioned settings table so every change to a profile is
    journaled; ``CompetitionRules.version`` is the settings version.
    """

    def __init__(self, store):
        self.store = store

    # -- storage --
    def store_profile(self, rules: CompetitionRules) -> CompetitionRules:
        key = setting_key(rules.competition_id, rules.stage)
        version = self.store.put_setting(key, rules.to_json())
        rules.version = version
        index = self._index()
        if key not in index:
            index.append(key)
            self.store.put_setting(INDEX_KEY, index)
        return rules

    def load_profile(self, competition_id: int, stage: str | None = None) -> CompetitionRules | None:
        found = self.store.get_setting(setting_key(competition_id, stage))
        if found is None:
            return None
        body, version = found
        rules = CompetitionRules.from_json(body)
        rules.version = version
        return rules

    def _index(self) -> list[str]:
        found = self.store.get_setting(INDEX_KEY)
        return list(found[0]) if found else []

    def list_profiles(self) -> list[CompetitionRules]:
        result = []
        for key in self._index():
            _, competition_id, stage = key.split(":", 2)
            rules = self.load_profile(int(competition_id), stage)
            if rules is not None:
                result.append(rules)
        return result

    # -- contexts --
    def context_for(self, fixture: FixtureView, stage: str | None = None) -> CompetitionContext:
        """The :class:`CompetitionContext` for a fixture; missing profiles stay explicit."""
        rules = self.load_profile(fixture.competition_id, stage)
        return competition_context(fixture, rules, stage=stage)

    def contexts_for(self, fixtures: list[FixtureView], stage: str | None = None) -> list[CompetitionContext]:
        return [self.context_for(f, stage) for f in fixtures]


def _rule_group(rules: CompetitionRules, names: tuple[str, ...]) -> dict[str, Any]:
    group: dict[str, Any] = {name: rules.field_observed(name).to_json() for name in names}
    missing = [name for name in names if not rules.field_observed(name).available]
    group["status"] = "available" if not missing else ("missing" if len(missing) == len(names) else "partial")
    if missing:
        group["missing"] = missing
        group["capability"] = "competition_rules"
    return group


def competition_context(fixture: FixtureView, rules: CompetitionRules | None, *, stage: str | None = None) -> CompetitionContext:
    """Expand a rules profile (or its absence) into the record the planner consumes.

    When no profile exists both rule groups carry ``{"status": "missing", ...}``
    naming the ``competition_rules`` capability, and ``deadlines`` is empty
    with ``squad_rules["deadlines_status"] == "missing"``.
    """
    if rules is None:
        missing = {"status": "missing", "reason": f"no rules profile stored for competition {fixture.competition_id} stage {stage or DEFAULT_STAGE}", "capability": "competition_rules", "deadlines_status": "missing"}
        return CompetitionContext(fixture.competition_id, fixture.competition_name, stage, fixture.identity, dict(missing), dict(missing), [], "none", 0)
    squad_rules = _rule_group(rules, SQUAD_FIELDS)
    substitution_rules = _rule_group(rules, SUBSTITUTION_FIELDS)
    squad_rules["tie_progression"] = rules.tie_progression.to_json()
    squad_rules["deadlines_status"] = rules.deadlines.status.value
    squad_rules["season"] = rules.season
    return CompetitionContext(fixture.competition_id, fixture.competition_name or rules.competition_name, rules.stage, fixture.identity, squad_rules, substitution_rules, deadline_entries(rules), rules.source, rules.version)


def deadline_entries(rules: CompetitionRules) -> list[dict[str, Any]]:
    """Explicit deadlines plus registration-window closes, as dated entries."""
    entries: list[dict[str, Any]] = []
    base = {"competition_id": rules.competition_id, "stage": rules.stage, "source": rules.source, "rules_version": rules.version}
    if rules.deadlines.available:
        for item in rules.deadlines.value or []:
            entries.append({**base, "kind": item.get("kind", "deadline"), "date": item.get("date"), "time": item.get("time"), "description": item.get("description"), "resolved": bool(item.get("resolved", False))})
    if rules.registration_windows.available:
        for window in rules.registration_windows.value or []:
            if window.get("closes"):
                entries.append({**base, "kind": "registration_deadline", "date": window["closes"], "time": window.get("time"), "description": f"{window.get('kind', 'registration')} window closes", "opens": window.get("opens"), "resolved": bool(window.get("resolved", False))})
    entries.sort(key=lambda e: (e.get("date") or "9999-12-31", e.get("time") or ""))
    return entries
