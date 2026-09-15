"""The bot's run loop: one coordinator for observing, planning and acting (spec 4.2, 12.4, 15.1).

Four run levels, in the order the spec gives them:

1. **Connection or load** - identify the registered career against what the
   bridge reports, settle (repeat ``/game`` reads until the clock stops
   moving), validate capabilities, and reconcile every intent left in flight
   by an earlier process before any new work.
2. **Stable decision point** - mandatory deadlines and inbox decisions come
   before any optional optimisation; anything the bot cannot read blocks
   Continue and names the missing capability.
3. **Weekly or material event** - the shared planner refreshes squad,
   minutes, contracts, scouting and finance plans.
4. **Supported match** - only verified state changes at permitted
   intervention points may drive actions. The current ``/match`` feed has an
   unclassified timeline, so this version refuses every live action and only
   records what it saw (MAT 01).

Events (a new message, an offer, a fixture change, a budget change, a squad
change, a reported injury) are detected by diffing successive snapshots.
Polling is the fallback, with intervals from the operator settings: 5 s idle
and 2 s in a paused match viewer are engineering defaults, not demonstrated
safe rates, and errors back off exponentially. A manager lock refuses a
second instance on the same store; a Stop control cancels queued work and
prevents the next UI input. The clock and the sleep function are injected so
tests never wait.

Baseline only: nothing here learns. Every decision the orchestrator records
carries the setting versions it was made under.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from .bridge_client.client import BridgeClient
from .execution.adapter import FAKE_WORKFLOWS
from .execution.executor import ExecutionReport, SingleWriterExecutor, WriterLockHeld
from .execution.lifecycle import IntentFactory, LifecycleError, enqueue, validate
from .execution.reconciliation import reconcile_on_restart
from .interactions.inbox import InboxBlocker, InboxTextProvider, continue_blocked_by_inbox, inbox_items, unresolved_mandatory
from .interface.controls import Settings
from .interface.notify import Notifier
from .interface.status import JOURNAL_CONNECTION, JOURNAL_STOP, JOURNAL_STOP_CLEARED, OperatorView, build_view
from .rules.capabilities import CapabilityRegistry
from .rules.deadlines import EXECUTION_MODES, ContinueGate, LineupStatus, PendingAction, continue_gate, pending_actions
from .state.identity import BranchIdentity, CareerIdentity, CareerRegistry, new_id, utc_now
from .state.records import ActionState, ConsistencyStatus, Decision, DecisionSnapshot
from .state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements, anchor_of
from .state.status import MissingCapabilityReport
from .state.store import StoreError
from .state.units import parse_date
from .state.views import upcoming_fixtures

ORCHESTRATOR_VERSION = "orchestrator/1"

MANAGER_LOCK = "manager"
# A manager lock whose heartbeat is older than this is treated as abandoned. Budget, not a measurement.
MANAGER_LOCK_STALE_SECONDS = 300.0
# Routes every pass collects; optional ones may be unavailable without invalidating the snapshot.
DEFAULT_ROUTES: tuple[str, ...] = ("/squad", "/finances", "/fixtures", "/tactics", "/inbox")
OPTIONAL_ROUTES: tuple[str, ...] = ("/match", "/training")
# Fixtures whose competition rules are consulted at a decision point.
RULES_HORIZON_FIXTURES = 3
MATCH_LIVE_ACTIONS_REFUSED = "the /match feed's timeline is unclassified and no verified intervention point exists; live actions are refused and only observations are recorded (MAT 01)"
CONTINUE_ACTION_KIND = "progress.continue"
INBOX_RESPONSE_KIND = "respond.inbox"
# Candidate kinds the orchestrator may hand to the single UI writer when the planner marks them proposed.
# Continue is deliberately absent: it goes through the Continue gate (``maybe_continue``), never as a plain action.
EXECUTABLE_KINDS: tuple[str, ...] = ("submit.lineup", "respond.inbox", "commit.contract", "commit.transfer_offer", "select_validated_tactic", "set.training")
# Fallbacks when the planner module does not export its own scope/verification tables.
DEFAULT_AUTHORITY_SCOPES: dict[str, str] = {"submit.lineup": "selection.submit_lineup", "progress.continue": "progression.continue", "commit.transfer_offer": "transfers.offer", "commit.contract": "contracts.commit", "respond.inbox": "inbox.respond", "select_validated_tactic": "tactics.select", "set.training": "training.set"}
DEFAULT_VERIFICATION_PLANS: dict[str, str] = {"submit.lineup": "lineup_matches_selection", "progress.continue": "navigation_only", "commit.contract": "contract_accepted_with_obligations", "commit.transfer_offer": "contract_accepted_with_obligations", "respond.inbox": "navigation_only", "select_validated_tactic": "selected_tactic_matches_catalog", "set.training": "training_settings_reread"}

JOURNAL_PASS = "orchestrator.pass"
JOURNAL_PLAN = "orchestrator.plan"
JOURNAL_MATCH = "orchestrator.match_observed"
JOURNAL_UNAVAILABLE = "orchestrator.observations_unavailable"
JOURNAL_BOUNDARY = "orchestrator.expected_boundary"
JOURNAL_SETTLE = "orchestrator.settle"


class OrchestratorError(RuntimeError):
    pass


class ManagerLockHeld(OrchestratorError):
    """Another bot instance already manages this game."""


class RunLevel(str, Enum):
    CONNECT = "connect"
    DECISION_POINT = "decision_point"
    WEEKLY = "weekly"
    MATCH = "match"


class PassStatus(str, Enum):
    STOPPED = "stopped"
    LOCK_LOST = "lock_lost"
    DISCONNECTED = "disconnected"
    BUILD_UNSUPPORTED = "build_unsupported"
    IDENTITY_MISMATCH = "identity_mismatch"
    INCONSISTENT = "inconsistent"
    UNCHANGED = "unchanged"
    MATCH_OBSERVED = "match_observed"
    PLANNED = "planned"
    ACTED = "acted"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Clock, sleep and Stop
# ---------------------------------------------------------------------------


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def utc_now(self) -> str: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> str:
        return utc_now()


class FakeClock:
    """A clock tests move by hand; ``sleep`` bound to :meth:`advance` never waits."""

    def __init__(self, start: float = 0.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def utc_now(self) -> str:
        return f"2026-01-01T00:00:{int(self.now) % 60:02d}+00:00"

    def advance(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class StopControl:
    """The visible Stop: once engaged, queued work is cancelled and no further UI input is sent."""

    def __init__(self, store=None):
        self.store = store
        self.engaged = False
        self.reason: str | None = None
        self.at: str | None = None

    def engage(self, reason: str = "operator stop") -> None:
        self.engaged, self.reason, self.at = True, reason, utc_now()
        if self.store is not None:
            self.store.journal(JOURNAL_STOP, {"reason": reason, "at": self.at})

    def clear(self) -> None:
        self.engaged, self.reason = False, None
        self.at = utc_now()
        if self.store is not None:
            self.store.journal(JOURNAL_STOP_CLEARED, {"at": self.at})

    def to_json(self) -> dict[str, Any]:
        return {"engaged": self.engaged, "reason": self.reason, "at": self.at, "status": "engaged" if self.engaged else "cleared"}


# ---------------------------------------------------------------------------
# Event triggers from successive snapshots
# ---------------------------------------------------------------------------

TRIGGER_KINDS = ("date_advanced", "new_message", "offer", "fixture_change", "budget_change", "squad_change", "injury")
OFFER_PATTERNS = ("offer", "bid")
BUDGET_FIELDS = ("balance", "transfer_budget", "wage_budget_weekly", "payroll_spending_weekly")


@dataclass(frozen=True)
class Trigger:
    kind: str
    description: str
    before: Any = None
    after: Any = None

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "description": self.description, "before": self.before, "after": self.after}


def _message_index(snapshot: DecisionSnapshot) -> dict[int, dict[str, Any]]:
    inbox = snapshot.routes.get("/inbox") or {}
    return {m.get("id"): m for m in inbox.get("messages", []) or []}


def _fixture_index(snapshot: DecisionSnapshot) -> dict[str, str]:
    payload = snapshot.routes.get("/fixtures") or {}
    return {f"{f.get('date')}T{f.get('time')} {f.get('home', {}).get('club_id')}-{f.get('away', {}).get('club_id')} comp:{f.get('competition_id')}": f.get("status", "unknown") for f in payload.get("fixtures", []) or []}


def _squad_ids(snapshot: DecisionSnapshot) -> set[int]:
    squad = snapshot.routes.get("/squad")
    if squad is None:
        squad = (snapshot.routes.get("/club") or {}).get("squad") or []
    return {p.get("id") for p in squad}


def detect_triggers(previous: DecisionSnapshot | None, current: DecisionSnapshot, *, injuries_before: dict[int, bool] | None = None, injuries_after: dict[int, bool] | None = None) -> list[Trigger]:
    """What changed between two consistent snapshots. No previous snapshot means no triggers (the first pass plans anyway).

    Injuries are only reported when an eligibility provider supplied injury
    readings for both snapshots (``player_id -> True`` when injured); the
    bridge itself does not decode injuries, so nothing is inferred from
    condition or morale.
    """
    if previous is None:
        return []
    triggers: list[Trigger] = []
    if (previous.game_date, previous.game_time) != (current.game_date, current.game_time):
        triggers.append(Trigger("date_advanced", f"game time moved {previous.game_date} {previous.game_time} -> {current.game_date} {current.game_time}", f"{previous.game_date} {previous.game_time}", f"{current.game_date} {current.game_time}"))
    before_messages, after_messages = _message_index(previous), _message_index(current)
    for message_id, message in after_messages.items():
        if message_id in before_messages:
            continue
        event_type = message.get("event_type") or ""
        triggers.append(Trigger("new_message", f"new inbox message {message_id} ({event_type})", None, message_id))
        if any(p in event_type.lower() for p in OFFER_PATTERNS):
            triggers.append(Trigger("offer", f"message {message_id} looks like an offer ({event_type}); text not decoded", None, message_id))
    before_fixtures, after_fixtures = _fixture_index(previous), _fixture_index(current)
    if before_fixtures != after_fixtures:
        changed = sorted(k for k in set(before_fixtures) | set(after_fixtures) if before_fixtures.get(k) != after_fixtures.get(k))
        triggers.append(Trigger("fixture_change", f"{len(changed)} fixture entries changed", None, changed))
    before_fin, after_fin = previous.routes.get("/finances") or {}, current.routes.get("/finances") or {}
    for name in BUDGET_FIELDS:
        if before_fin.get(name) != after_fin.get(name):
            triggers.append(Trigger("budget_change", f"{name} changed {before_fin.get(name)} -> {after_fin.get(name)} (whole pounds as reported)", before_fin.get(name), after_fin.get(name)))
    before_ids, after_ids = _squad_ids(previous), _squad_ids(current)
    if before_ids != after_ids:
        triggers.append(Trigger("squad_change", f"squad changed: joined {sorted(after_ids - before_ids)}, left {sorted(before_ids - after_ids)}", sorted(before_ids - after_ids), sorted(after_ids - before_ids)))
    if injuries_before is not None and injuries_after is not None:
        for pid, injured in injuries_after.items():
            if injured and not injuries_before.get(pid):
                triggers.append(Trigger("injury", f"player {pid} reported injured by the eligibility provider", False, True))
    return triggers


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class SettleResult:
    settled: bool
    reads: int
    game_date: str | None
    game_time: str | None
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ConnectResult:
    status: PassStatus | str
    connected: bool
    build_supported: bool
    career_matched: bool | None
    problems: list[str] = field(default_factory=list)
    settle: SettleResult | None = None
    reconciled: list[dict[str, Any]] = field(default_factory=list)
    snapshot_id: str | None = None
    capabilities: dict[str, list[str]] = field(default_factory=dict)
    game_date: str | None = None
    game_time: str | None = None

    @property
    def ok(self) -> bool:
        return self.connected and self.build_supported and self.career_matched is True

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status.value if isinstance(self.status, PassStatus) else self.status, "connected": self.connected, "build_supported": self.build_supported, "career_matched": self.career_matched, "problems": list(self.problems), "settle": self.settle.to_json() if self.settle else None, "reconciled": list(self.reconciled), "snapshot_id": self.snapshot_id, "capabilities": self.capabilities, "game_date": self.game_date, "game_time": self.game_time}


@dataclass
class DecisionPointResult:
    pending: list[PendingAction]
    blockers: list[InboxBlocker]
    gate: ContinueGate
    reports: list[MissingCapabilityReport]
    proposals: list[dict[str, Any]]           # respond.inbox proposals for blockers that can be answered now

    @property
    def mandatory_clear(self) -> bool:
        return not self.blockers and not any(p.blocks_continue and not p.resolved for p in self.pending)

    def to_json(self) -> dict[str, Any]:
        return {"pending": [p.to_json() for p in self.pending], "blockers": [b.to_json() for b in self.blockers], "gate": self.gate.to_json(), "reports": [r.to_json() for r in self.reports], "proposals": list(self.proposals), "mandatory_clear": self.mandatory_clear}


@dataclass
class PlanOutcome:
    status: str                              # planned | unavailable | skipped
    decisions: list[Decision] = field(default_factory=list)
    next_action: dict[str, Any] | None = None
    missing: list[MissingCapabilityReport] = field(default_factory=list)
    summary_lines: list[str] = field(default_factory=list)
    lineup_status: LineupStatus | None = None
    reason: str | None = None
    report: Any = None

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "decision_ids": [d.decision_id for d in self.decisions], "next_action": self.next_action, "missing": [m.to_json() for m in self.missing], "summary_lines": list(self.summary_lines), "lineup_status": self.lineup_status.__dict__ if self.lineup_status else None, "reason": self.reason}


@dataclass
class MatchObservation:
    available: bool
    live_actions_allowed: bool
    reason: str
    snapshot_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PassResult:
    level: RunLevel
    status: PassStatus
    snapshot_id: str | None = None
    game_date: str | None = None
    game_time: str | None = None
    changed: bool = False
    triggers: list[Trigger] = field(default_factory=list)
    decision_point: DecisionPointResult | None = None
    plan: PlanOutcome | None = None
    next_action: dict[str, Any] | None = None
    executed: ExecutionReport | None = None
    continue_gate: ContinueGate | None = None
    blocked: list[MissingCapabilityReport] = field(default_factory=list)
    notifications: int = 0
    next_poll_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"level": self.level.value, "status": self.status.value, "snapshot_id": self.snapshot_id, "game_date": self.game_date, "game_time": self.game_time, "changed": self.changed, "triggers": [t.to_json() for t in self.triggers], "decision_point": self.decision_point.to_json() if self.decision_point else None, "plan": self.plan.to_json() if self.plan else None, "next_action": self.next_action, "executed": self.executed.to_json() if self.executed else None, "continue_gate": self.continue_gate.to_json() if self.continue_gate else None, "blocked": [b.to_json() for b in self.blocked], "notifications": self.notifications, "next_poll_seconds": self.next_poll_seconds, "notes": list(self.notes), "version": ORCHESTRATOR_VERSION}


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def plan_due(last_plan_date: str | None, game_date: str | None) -> bool:
    """A weekly plan is due on the first pass and whenever the ISO week of the game date changes."""
    if last_plan_date is None or game_date is None:
        return True
    previous, current = parse_date(last_plan_date).isocalendar(), parse_date(game_date).isocalendar()
    return (previous[0], previous[1]) != (current[0], current[1])


def _candidates(report: Any) -> list[Any]:
    return list(getattr(report, "candidates", []) or [])


def lineup_status_of(report: Any) -> LineupStatus:
    """Translate the planner's lineup candidates into the Continue gate's vocabulary without inventing verification.

    A ``submit.lineup`` candidate the planner marked proposed and submittable
    is the only thing that counts as verified; an advisory eleven with
    unverified starters stays unverified, an infeasible one infeasible, and
    no eleven at all is missing (or not required when there is no fixture).
    """
    candidates = _candidates(report)
    if not candidates and getattr(report, "lineup", None) is None:
        return LineupStatus("missing", reasons=["the planner produced no lineup"])
    submit = next((c for c in candidates if getattr(c, "kind", None) == "submit.lineup"), None)
    advisory = next((c for c in candidates if getattr(c, "kind", None) == "advise.lineup"), None)
    identity = None
    for candidate in (submit, advisory):
        fixture = (getattr(candidate, "payload", None) or {}).get("fixture") if candidate is not None else None
        if fixture and fixture.get("identity"):
            identity = fixture["identity"]
            break
    if submit is not None and getattr(submit, "status", None) == "proposed" and getattr(submit, "submittable", False):
        return LineupStatus("verified", identity)
    if advisory is None:
        return LineupStatus("missing", identity, reasons=["no lineup candidate was produced"])
    reasons = list(getattr(advisory, "reasons", []) or [])
    if getattr(advisory, "status", None) == "blocked":
        if any("no upcoming fixture" in r for r in reasons):
            return LineupStatus("not_required", identity, reasons=reasons)
        return LineupStatus("missing", identity, reasons=reasons or ["lineup blocked"])
    if getattr(advisory, "status", None) == "infeasible":
        return LineupStatus("infeasible", identity, reasons=reasons or ["no legal eleven"])
    if getattr(advisory, "unverified", True):
        return LineupStatus("unverified", identity, reasons=reasons or ["eligibility not verified for every starter"])
    return LineupStatus("verified", identity)


def _planner_tables() -> tuple[dict[str, str], dict[str, str]]:
    try:
        from .planning import planner as module
    except ImportError:
        return dict(DEFAULT_AUTHORITY_SCOPES), dict(DEFAULT_VERIFICATION_PLANS)
    return {**DEFAULT_AUTHORITY_SCOPES, **getattr(module, "AUTHORITY_SCOPES", {})}, {**DEFAULT_VERIFICATION_PLANS, **getattr(module, "VERIFICATION_PLANS", {})}


def next_action_of(report: Any, decisions: list[Decision]) -> dict[str, Any] | None:
    """The first candidate the planner marked proposed that the UI writer could carry out, as an action dict.

    Returns ``None`` when nothing is proposed (advice-only plans, blocked
    candidates). The action carries the decision id of its horizon so the
    audit chain (intent -> decision -> snapshot) is intact.
    """
    explicit = getattr(report, "next_action", None)
    if isinstance(explicit, dict):
        return dict(explicit)
    scopes, plans = _planner_tables()
    by_horizon = {d.kind.removeprefix("plan."): d.decision_id for d in decisions}
    for candidate in _candidates(report):
        kind = getattr(candidate, "kind", None)
        if kind not in EXECUTABLE_KINDS or getattr(candidate, "status", None) != "proposed":
            continue
        if kind == "submit.lineup" and (getattr(candidate, "unverified", True) or not getattr(candidate, "submittable", False)):
            continue
        return {"kind": kind, "authority_scope": scopes.get(kind, kind), "targets": dict(getattr(candidate, "targets", {}) or {}), "parameters": dict(getattr(candidate, "parameters", {}) or {}), "verification": plans.get(kind, "navigation_only"), "required_capabilities": list(getattr(candidate, "requires", []) or []), "description": getattr(candidate, "description", kind), "candidate_id": getattr(candidate, "candidate_id", None), "decision_id": by_horizon.get(getattr(candidate, "horizon", ""))}
    return None


def plan_outcome_of(report: Any) -> PlanOutcome:
    """Read a planner report (``planning.planner.PlanReport`` or a test double) into a :class:`PlanOutcome`.

    Missing-capability reports are kept only when they block, one per gated
    action, so the operator sees exactly which prerequisites are unresolved.
    """
    decisions = list(getattr(report, "decisions", []) or [])
    summary = list(getattr(report, "summaries", None) or getattr(report, "summary_lines", None) or [])
    reports_attr = getattr(report, "capability_reports", None)
    if isinstance(reports_attr, dict):
        raw_reports = list(reports_attr.values())
    else:
        raw_reports = list(getattr(report, "missing_capabilities", None) or [])
    seen: set[str] = set()
    missing: list[MissingCapabilityReport] = []
    for item in raw_reports:
        if item.blocked and item.blocked_action not in seen:
            seen.add(item.blocked_action)
            missing.append(item)
    status = "planned" if getattr(report, "status", "planned") != "blocked" else "blocked"
    return PlanOutcome(status, decisions, next_action_of(report, decisions), missing, summary, lineup_status_of(report), reason="; ".join(getattr(report, "reasons", []) or []) or None, report=report)


def run_planner(snapshot: DecisionSnapshot, *, store, settings: Settings, capabilities: CapabilityRegistry | None, eligibility=None, rules=None, promises=None, inbox_text=None, build: str | None = None, planner: Callable[..., Any] | None = None) -> PlanOutcome:
    """Run the shared planner for one snapshot and persist its decisions with the setting versions they were made under.

    The planner is asked not to persist anything itself so that every stored
    decision carries the operator's setting versions (spec 15.1). A missing
    planner module is reported as an unavailable plan, never as an empty one.
    """
    plan_once = planner
    if plan_once is None:
        try:
            from .planning import planner as module
        except ImportError as exc:
            report = MissingCapabilityReport("plan.weekly")
            report.add("planning.planner", f"shared planner not importable ({exc.__class__.__name__}: {exc})")
            return PlanOutcome("unavailable", missing=[report], reason=str(exc))
        plan_once = module.plan_once
        providers = module.Providers(rules=rules, promises=promises, inbox_text=inbox_text, build=build)
        if eligibility is not None:
            providers.eligibility = eligibility
    else:
        providers = {"eligibility": eligibility, "rules": rules, "promises": promises, "inbox_text": inbox_text, "build": build}
    report = plan_once(snapshot, store=None, objective_profile=settings.club_objective(), authority=settings.authority_profile(), capabilities=capabilities, providers=providers)
    outcome = plan_outcome_of(report)
    if store is not None:
        for decision in outcome.decisions:
            if store.get_decision(decision.decision_id) is None:
                settings.stamp_decision(decision)
                store.insert_decision(decision)
    return outcome


class Orchestrator:
    """Coordinates the bridge, the store, the planner, the executor and the operator surface.

    ``client`` reads the bridge, ``adapter`` is the only writer to the game
    UI, ``settings`` are the operator's persisted controls. ``eligibility``,
    ``rules`` (a ``RulesProfileRegistry``), ``promises`` and
    ``inbox_text_provider`` are optional providers handed to the planner and
    the gates; ``provisions`` lists capability names those providers supply.
    ``planner`` overrides the lazily imported ``planning.planner.plan_once``.
    """

    def __init__(self, store, client: BridgeClient, adapter, settings: Settings, *, career: CareerIdentity, branch: BranchIdentity, lineage_confirmed: bool = False, eligibility=None, rules=None, promises=None, inbox_text_provider: InboxTextProvider | None = None, provisions: dict[str, str] | None = None, notifier: Notifier | None = None, clock: Clock | None = None, sleep: Callable[[float], None] | None = None, owner_id: str | None = None, planner: Callable[..., Any] | None = None, workflows: dict[str, Any] | None = None, routes: tuple[str, ...] = DEFAULT_ROUTES, optional_routes: tuple[str, ...] = OPTIONAL_ROUTES, stable_point: Callable[[], bool] | None = None, lock_stale_seconds: float = MANAGER_LOCK_STALE_SECONDS):
        self.store, self.client, self.adapter, self.settings = store, client, adapter, settings
        self.career, self.branch = career, branch
        self.lineage_confirmed = lineage_confirmed
        self.eligibility, self.rules, self.promises = eligibility, rules, promises
        self.inbox_text_provider = inbox_text_provider
        self.provisions = dict(provisions or {})
        self.notifier = notifier or Notifier(store=store)
        self.clock: Clock = clock or SystemClock()
        self.sleep: Callable[[float], None] = sleep or time.sleep
        self.owner_id = owner_id or new_id("bot")
        self.planner = planner
        self.workflows = dict(workflows) if workflows is not None else (dict(FAKE_WORKFLOWS) if getattr(adapter, "name", "") == "fake" else {})
        self.routes, self.optional_routes = tuple(routes), tuple(optional_routes)
        self.stable_point = stable_point
        self.stop_control = StopControl(store)
        self.registry = CareerRegistry(store)
        self.collector = SnapshotCollector(client, store)
        self.factory = IntentFactory(store)
        self.capabilities = CapabilityRegistry.from_status(None, supported_builds=client.supported_builds)
        self.connected = False
        self.execution_enabled = False
        self.consecutive_errors = 0
        self.previous_snapshot: DecisionSnapshot | None = None
        self.last_plan_date: str | None = None
        self.last_decision_by_kind: dict[str, Decision] = {}
        self.last_pass: PassResult | None = None
        self.last_decided: PassResult | None = None        # the most recent pass that reached a decision point
        self.last_connection: ConnectResult | None = None
        self._anchor = None
        self._sequence = 0
        self._bot_progressed = False
        self._executor: SingleWriterExecutor | None = None
        if not store.acquire_lock(MANAGER_LOCK, self.owner_id, stale_after_seconds=lock_stale_seconds):
            raise ManagerLockHeld(f"manager lock is held by {store.lock_owner(MANAGER_LOCK)!r}; one bot instance per game")
        self.lock_held = True
        self._connect_snapshot: DecisionSnapshot | None = None
        if settings.authority_profile_version == 0:
            settings.save()          # defaults become explicit, versioned rows so decisions and audits cite real versions
        store.journal("orchestrator.started", {"owner_id": self.owner_id, "career_id": career.career_id, "branch_id": branch.branch_id, "adapter": getattr(adapter, "name", "?"), "version": ORCHESTRATOR_VERSION}, self.owner_id)

    # ----- lifecycle -----
    def close(self) -> None:
        if self._executor is not None:
            self._executor.close()
            self._executor = None
        if self.lock_held:
            self.store.release_lock(MANAGER_LOCK, self.owner_id)
            self.lock_held = False
            self.store.journal("orchestrator.closed", {"owner_id": self.owner_id}, self.owner_id)

    def heartbeat(self) -> bool:
        ok = self.store.heartbeat_lock(MANAGER_LOCK, self.owner_id)
        if self._executor is not None:
            self._executor.heartbeat()
        return ok

    def stop(self, reason: str = "operator stop") -> list[str]:
        """Engage Stop: cancel queued work and make sure the next UI input never goes out (ACT 03)."""
        self.stop_control.engage(reason)
        return self._cancel_queued(reason)

    def _cancel_queued(self, reason: str) -> list[str]:
        if self._executor is not None:
            return self._executor.stop(reason)
        self.adapter.stop(reason)
        cancelled: list[str] = []
        for intent in self.store.list_intents([ActionState.QUEUED], branch_id=self.branch.branch_id):
            self.store.update_intent_state(intent, ActionState.CANCELLED, f"stop: {reason}")
            cancelled.append(intent.action_id)
        return cancelled

    def resume(self) -> None:
        """Clear Stop. The stopped executor is discarded; a fresh one is created on the next action."""
        self.stop_control.clear()
        flag = getattr(self.adapter, "stop_flag", None)
        if flag is not None:
            flag.clear()
        if self._executor is not None:
            self._executor.close()
            self._executor = None

    # ----- polling policy -----
    def _intervals(self) -> dict[str, float]:
        return self.settings.poll_intervals()

    def next_interval(self, *, match_paused: bool = False) -> float:
        """Idle or paused-match interval with exponential backoff on consecutive errors (engineering defaults)."""
        intervals = self._intervals()
        base = intervals["paused_match_seconds"] if match_paused else intervals["idle_seconds"]
        return min(base * (intervals["backoff_factor"] ** self.consecutive_errors), intervals["max_backoff_seconds"])

    # ----- level 1: connection or load -----
    def settle(self) -> SettleResult:
        """Read ``/game`` until the in-game clock is unchanged for ``settle_reads`` consecutive reads."""
        intervals = self._intervals()
        needed, limit, gap = int(intervals["settle_reads"]), int(intervals["settle_max_reads"]), intervals["settle_interval_seconds"]
        last: tuple[str | None, str | None] | None = None
        streak, reads, reasons = 0, 0, []
        while reads < limit:
            response = self.client.game()
            reads += 1
            if not response.ok:
                reasons.append(f"read {reads}: /game unavailable ({response.error})")
                streak, last = 0, None
            else:
                current = (response.data.get("date"), response.data.get("time"))
                streak = streak + 1 if current == last else 1
                last = current
                if streak >= needed:
                    result = SettleResult(True, reads, last[0], last[1], reasons)
                    self.store.journal(JOURNAL_SETTLE, result.to_json(), self.owner_id)
                    return result
            self.sleep(gap)
        reasons.append(f"clock still moving or unavailable after {reads} reads")
        result = SettleResult(False, reads, last[0] if last else None, last[1] if last else None, reasons)
        self.store.journal(JOURNAL_SETTLE, result.to_json(), self.owner_id)
        return result

    def identify_career(self, status_body: dict[str, Any]) -> tuple[bool | None, list[str]]:
        manager, club = self.client.manager(), self.client.club()
        if not manager.ok or not club.ok:
            return None, [f"identity routes unavailable: manager {manager.error}, club {club.error}"]
        return self.registry.matches_registration(self.career, status_body, manager.data.get("id"), club.data.get("id"))

    def validate_capabilities(self, status_body: dict[str, Any] | None) -> CapabilityRegistry:
        registry = CapabilityRegistry.from_status(status_body, supported_builds=self.client.supported_builds)
        for name in self.adapter.capabilities():
            registry.provide(name, f"ui_adapter:{getattr(self.adapter, 'name', 'adapter')}", "reported by the adapter; verified per workflow")
        for name, provider in self.provisions.items():
            registry.provide(name, provider, "provided to the orchestrator")
        self.capabilities = registry
        if self._executor is not None:
            self._executor.registry = registry
        return registry

    def connect(self) -> ConnectResult:
        """Level 1: identify the career, settle, validate capabilities, reconcile unfinished intents."""
        state = self.client.connection_state()
        self.store.journal(JOURNAL_CONNECTION, {k: v for k, v in state.items() if k != "status"}, self.owner_id)
        if not state["connected"]:
            self._unavailable(PassStatus.DISCONNECTED, state.get("reason"))
            return self._finish_connect(ConnectResult(PassStatus.DISCONNECTED, False, False, None, [state.get("reason") or "disconnected"]))
        if not state["build_supported"]:
            self._unavailable(PassStatus.BUILD_UNSUPPORTED, state.get("reason"))
            return self._finish_connect(ConnectResult(PassStatus.BUILD_UNSUPPORTED, True, False, None, [state.get("reason") or "build unsupported"]))
        matched, problems = self.identify_career(state["status"])
        if matched is not True:
            self.execution_enabled = False
            return self._finish_connect(ConnectResult(PassStatus.IDENTITY_MISMATCH, True, True, matched, problems))
        settled = self.settle()
        registry = self.validate_capabilities(state["status"])
        snapshot = self.collect("connect")
        reconciled = []
        if snapshot.valid:
            reconciled = [d.to_json() for d in reconcile_on_restart(self.store, self.adapter, snapshot, branch_id=self.branch.branch_id)]
        self.execution_enabled = snapshot.valid and settled.settled
        self.connected = self.execution_enabled            # an unsettled clock or an inconsistent snapshot means: try again next pass
        self._connect_snapshot = snapshot if self.connected else None
        problems = [] if self.execution_enabled else [("snapshot " + snapshot.consistency.value + ": " + "; ".join(snapshot.consistency_reasons)) if not snapshot.valid else "clock unsettled: " + "; ".join(settled.reasons)]
        if (snapshot.continuity or {}).get("requires_lineage_confirmation"):
            problems.append("the operator must confirm this save is the registered career (register --confirm-lineage) before the bot trusts it")
        result = ConnectResult(PassStatus.PLANNED if self.execution_enabled else PassStatus.INCONSISTENT, True, True, True, problems, settled, reconciled, snapshot.snapshot_id, registry.summary(), snapshot.game_date, snapshot.game_time)
        return self._finish_connect(result)

    def _finish_connect(self, result: ConnectResult) -> ConnectResult:
        self.last_connection = result
        self.store.journal("orchestrator.connect", result.to_json(), self.owner_id)
        return result

    def _unavailable(self, status: PassStatus, reason: str | None, snapshot: DecisionSnapshot | None = None) -> None:
        """503, disconnect or build mismatch: stop action execution and mark observations unavailable (spec 12.4)."""
        self.connected = False
        self.execution_enabled = False
        self.store.journal(JOURNAL_UNAVAILABLE, {"status": status.value, "reason": reason, "snapshot_id": snapshot.snapshot_id if snapshot else None, "observation_ids": list(snapshot.observation_ids) if snapshot else [], "execution": "disabled"}, self.owner_id)

    # ----- collection -----
    def collect(self, label: str = "pass") -> DecisionSnapshot:
        requirements = SnapshotRequirements(routes=[*self.routes, *self.optional_routes], optional=list(self.optional_routes), label=label)
        context = CollectionContext(self.career.career_id, self.branch.branch_id, self.settings.information_mode(), self._anchor, self.lineage_confirmed, self._bot_progressed, self.stable_point, self._sequence)
        snapshot = self.collector.collect(requirements, context)
        self._sequence = context.sequence
        if snapshot.valid:
            self._anchor = anchor_of(snapshot)
            self._bot_progressed = False
        elif snapshot.consistency is ConsistencyStatus.DISCONNECTED:
            self._unavailable(PassStatus.DISCONNECTED, "; ".join(snapshot.consistency_reasons), snapshot)
        elif snapshot.consistency is ConsistencyStatus.BUILD_UNSUPPORTED:
            self._unavailable(PassStatus.BUILD_UNSUPPORTED, "; ".join(snapshot.consistency_reasons), snapshot)
        return snapshot

    # ----- level 2: stable decision point -----
    def _deadline_text_provider(self):
        provider = self.inbox_text_provider
        if provider is None:
            return None

        def read(message: dict[str, Any]) -> dict[str, Any] | None:
            observed = provider.get_text(message.get("id"), game_time=None)
            if not observed.available or observed.value.requires_decision is None:
                return None
            text = observed.value
            return {"requires_decision": text.requires_decision, "text": text.subject, "options": text.fixed_option_ids, "deadline": text.deadline}
        return read

    def _rules_contexts(self, snapshot: DecisionSnapshot) -> list:
        if self.rules is None:
            return []
        return list(self.rules.contexts_for(upcoming_fixtures(snapshot, RULES_HORIZON_FIXTURES)))

    def _inbox_proposal(self, blocker: InboxBlocker, snapshot: DecisionSnapshot) -> dict[str, Any]:
        return {"kind": INBOX_RESPONSE_KIND, "authority_scope": "inbox.respond", "targets": {"routes": ["/inbox"], "message_id": blocker.item.message_id}, "parameters": {"message_id": blocker.item.message_id, "legal_option_ids": list(blocker.legal_option_ids)}, "verification": "navigation_only", "required_capabilities": list(self.capabilities.requirements(INBOX_RESPONSE_KIND)), "description": blocker.description}

    def decision_point(self, snapshot: DecisionSnapshot, lineup_status: LineupStatus | None = None) -> DecisionPointResult:
        """Level 2: mandatory deadlines and inbox decisions, before any optional work."""
        game_time = f"{snapshot.game_date} {snapshot.game_time}" if snapshot.game_date else None
        pending = pending_actions(snapshot, self._deadline_text_provider())
        blockers = unresolved_mandatory(inbox_items(snapshot), self.inbox_text_provider, game_time=game_time, pending_actions_supported=self.capabilities.supported("pending_actions"))
        gate = continue_gate(snapshot, pending, self._rules_contexts(snapshot), lineup_status, authority_mode=self.settings.authority_mode(), capabilities=self.capabilities)
        merged = MissingCapabilityReport(CONTINUE_ACTION_KIND)
        for source in (gate.missing_capabilities, continue_blocked_by_inbox(blockers)):
            for name in source.missing:
                merged.add(name, "; ".join(r for r in (merged.reasons.get(name), source.reasons.get(name)) if r))
        reports = [merged] if merged.blocked else []
        proposals = [self._inbox_proposal(b, snapshot) for b in blockers if b.resolvable_now]
        if gate.blockers:
            self.notifier.required_action("the calendar cannot move on", gate.blockers, ref_id=self.branch.branch_id)
        for report in reports:
            self.notifier.unsupported_workflow(report, ref_id=self.branch.branch_id)
        return DecisionPointResult(pending, blockers, gate, reports, proposals)

    # ----- level 3: weekly or material event -----
    def _bridge_build(self) -> str | None:
        status = getattr(self.client, "last_status", None) or {}
        return status.get("build") if isinstance(status, dict) else None

    def plan(self, snapshot: DecisionSnapshot) -> PlanOutcome:
        """Level 3: refresh squad, minutes, contracts, scouting and finance plans through the shared planner.

        Decisions are persisted with the setting versions they were made
        under; a material change from the previous decision of the same kind
        is reported through the notifier. Blocked capability reports from
        the planner are listed as unresolved prerequisites (they are not
        mandatory workflows, so they do not notify).
        """
        outcome = run_planner(snapshot, store=self.store, settings=self.settings, capabilities=self.capabilities, eligibility=self.eligibility, rules=self.rules, promises=self.promises, inbox_text=self._deadline_text_provider(), build=self._bridge_build(), planner=self.planner)
        self.last_plan_date = snapshot.game_date          # do not re-plan every poll; the next week or a trigger retries
        if outcome.status == "unavailable":
            for report in outcome.missing:
                self.notifier.unsupported_workflow(report, ref_id=self.branch.branch_id)
        for decision in outcome.decisions:
            self._record_decision(decision)
        self.store.journal(JOURNAL_PLAN, {**outcome.to_json(), "settings": self.settings.stamp(), "snapshot_id": snapshot.snapshot_id}, snapshot.snapshot_id)
        return outcome

    def _record_decision(self, decision: Decision) -> None:
        """Persist a decision with the setting versions it was made under, and report a material plan change."""
        if self.store.get_decision(decision.decision_id) is None:
            self.settings.stamp_decision(decision)
            self.store.insert_decision(decision)
        self.notifier.plan_changed(self.last_decision_by_kind.get(decision.kind), decision)
        self.last_decision_by_kind[decision.kind] = decision

    # ----- execution -----
    def _executor_for(self) -> SingleWriterExecutor:
        profile = self.settings.authority_profile()
        if self._executor is None:
            try:
                self._executor = SingleWriterExecutor(self.store, self.adapter, owner_id=f"{self.owner_id}:ui", registry=self.capabilities, profile=profile, career_id=self.career.career_id, branch_id=self.branch.branch_id, workflows=self.workflows)
            except WriterLockHeld as exc:
                raise ManagerLockHeld(str(exc)) from exc
        self._executor.profile = profile
        self._executor.registry = self.capabilities
        return self._executor

    def _execution_permitted(self, kind: str) -> str | None:
        if self.stop_control.engaged:
            return f"Stop is engaged ({self.stop_control.reason}); no input will be sent"
        if not self.execution_enabled:
            return "execution disabled: bridge unavailable, unsettled or career unconfirmed"
        if self.settings.authority_mode() not in EXECUTION_MODES:
            return f"authority mode {self.settings.authority_mode().value}: {kind} is recorded as advice only"
        return None

    def _wrap_decision(self, action: dict[str, Any], snapshot: DecisionSnapshot, *, reasons: list[str], constraints: list[dict[str, Any]] | None = None) -> Decision:
        decision = Decision(new_id("dec"), self.settings.club_objective().version, snapshot.snapshot_id, [action], list(constraints or []), [], action, reasons, information_mode=snapshot.information_mode, kind=f"execution.{action['kind']}")
        self._record_decision(decision)
        return decision

    def execute(self, action: dict[str, Any], snapshot: DecisionSnapshot, *, decision_id: str | None = None, reasons: list[str] | None = None) -> ExecutionReport | MissingCapabilityReport | str:
        """Turn a proposed action into an intent and run it through the single UI writer.

        Returns the executor's report, a :class:`MissingCapabilityReport`
        when a capability gate blocks the action, or a text reason when the
        action is not executed (advice-only mode, Stop, disabled execution,
        outside scope, duplicate).
        """
        kind = action["kind"]
        refusal = self._execution_permitted(kind)
        if refusal:
            self.store.journal("orchestrator.not_executed", {"kind": kind, "reason": refusal}, snapshot.snapshot_id)
            return refusal
        report = self.capabilities.check(kind, extra=action.get("required_capabilities", []))
        if report.blocked:
            self.notifier.unsupported_workflow(report, ref_id=snapshot.snapshot_id)
            return report
        decision_id = decision_id or action.get("decision_id")
        if decision_id is None or self.store.get_decision(decision_id) is None:
            decision_id = self._wrap_decision(action, snapshot, reasons=reasons or [action.get("description", "proposed by the planner")]).decision_id
        try:
            intent = self.factory.create(kind, action["authority_scope"], snapshot, dict(action.get("targets", {})), dict(action.get("parameters", {})), required_capabilities=action.get("required_capabilities"), verification=action.get("verification", "navigation_only"), decision_id=decision_id)
        except (LifecycleError, StoreError) as exc:
            self.store.journal("orchestrator.not_executed", {"kind": kind, "reason": str(exc)}, snapshot.snapshot_id)
            return f"intent not created: {exc}"
        validation = validate(intent, self.capabilities, self.settings.authority_profile(), snapshot, self.store)
        if not validation.ok:
            self.store.journal("orchestrator.not_executed", {"kind": kind, "action_id": intent.action_id, "validation": validation.to_json()}, intent.action_id)
            return f"{intent.action_id} {validation.state.value}: " + "; ".join(validation.reasons)
        enqueue(self.store, intent)
        executor = self._executor_for()
        executor.enqueue(intent)
        result = executor.run_next(lambda: self.collect("pre-execution"))
        if result is None:
            return f"{intent.action_id} was not run"
        self._after_execution(result, action)
        return result

    def _after_execution(self, result: ExecutionReport, action: dict[str, Any]) -> None:
        description = action.get("description") or action["kind"]
        if result.state is ActionState.CONFIRMED:
            self.notifier.completed(result.action_id, description, detail=result.reason)
            if action["kind"] == CONTINUE_ACTION_KIND:
                self._bot_progressed = True
                self.settle()
                self.collect("post-progression")
        elif result.state in (ActionState.FAILED, ActionState.UNCERTAIN):
            self.notifier.failed(description, f"{result.state.value}: {result.reason}", ref_id=result.action_id)
        elif result.state is ActionState.CANCELLED:
            self.notifier.failed(description, f"cancelled: {result.reason}", ref_id=result.action_id)

    # ----- Continue -----
    def continue_action(self, gate: ContinueGate) -> dict[str, Any]:
        boundary = gate.next_boundary.to_json() if gate.next_boundary else None
        return {"kind": CONTINUE_ACTION_KIND, "authority_scope": "progression.continue", "targets": {"routes": ["/inbox", "/fixtures"]}, "parameters": {"expected_boundary": boundary}, "verification": "navigation_only", "required_capabilities": list(self.capabilities.requirements(CONTINUE_ACTION_KIND)), "description": f"move the calendar on to {boundary['description'] if boundary else 'the next decision point'}"}

    def maybe_continue(self, snapshot: DecisionSnapshot, gate: ContinueGate, point: DecisionPointResult | None = None) -> ExecutionReport | MissingCapabilityReport | str:
        """Before Continue, the gate must allow it, every mandatory item must be clear and the profile must permit pressing it (spec 12.4)."""
        profile = self.settings.authority_profile()
        if profile.mode not in EXECUTION_MODES or not profile.limits.allow_continue:
            return "Continue is not permitted by the authority profile"
        if point is not None and not point.mandatory_clear:
            return "calendar blocked: " + "; ".join([b.description for b in point.blockers] + [p.description for p in point.pending if p.blocks_continue and not p.resolved])
        if not gate.allowed:
            return "calendar blocked: " + "; ".join(gate.blockers or [f"missing {', '.join(gate.missing_capabilities.missing)}"])
        action = self.continue_action(gate)
        self.store.journal(JOURNAL_BOUNDARY, {"snapshot_id": snapshot.snapshot_id, "boundary": action["parameters"]["expected_boundary"]}, snapshot.snapshot_id)
        return self.execute(action, snapshot, reasons=["calendar clear: no pending decision, lineup verified, no registration deadline"])

    # ----- level 4: supported match -----
    def match_level(self, snapshot: DecisionSnapshot) -> MatchObservation:
        payload = snapshot.routes.get("/match")
        if not payload or not payload.get("available"):
            reason = (payload or {}).get("reason") or "/match not collected"
            return MatchObservation(False, False, reason, snapshot.snapshot_id)
        observation = MatchObservation(True, False, MATCH_LIVE_ACTIONS_REFUSED, snapshot.snapshot_id)
        self.store.journal(JOURNAL_MATCH, {**observation.to_json(), "observation_ids": list(snapshot.observation_ids), "timeline": "unclassified", "match_event_order": self.capabilities.status("match_event_order").value}, snapshot.snapshot_id)
        return observation

    # ----- one pass -----
    def run_once(self) -> PassResult:
        before = len(self.notifier.history)
        result = self._pass()
        result.notifications = len(self.notifier.history) - before
        self.last_pass = result
        self.store.journal(JOURNAL_PASS, {k: v for k, v in result.to_json().items() if k not in ("decision_point", "plan")}, result.snapshot_id)
        return result

    def _pass(self) -> PassResult:
        if not self.heartbeat():
            return PassResult(RunLevel.CONNECT, PassStatus.LOCK_LOST, notes=["manager lock lost; another instance may have taken over"], next_poll_seconds=self.next_interval())
        if self.stop_control.engaged:
            cancelled = self._cancel_queued(self.stop_control.reason or "operator stop") if self.store.list_intents([ActionState.QUEUED], branch_id=self.branch.branch_id) else []
            return PassResult(RunLevel.DECISION_POINT, PassStatus.STOPPED, notes=[f"Stop engaged: {self.stop_control.reason}"] + ([f"cancelled {cancelled}"] if cancelled else []), next_poll_seconds=self.next_interval())
        if not self.connected:
            connection = self.connect()
            if not connection.ok or connection.status is PassStatus.INCONSISTENT:
                self.consecutive_errors += 1
                status = connection.status if isinstance(connection.status, PassStatus) else PassStatus.INCONSISTENT
                return PassResult(RunLevel.CONNECT, status, connection.snapshot_id, connection.game_date, connection.game_time, notes=list(connection.problems), next_poll_seconds=self.next_interval())
        snapshot, self._connect_snapshot = (self._connect_snapshot or self.collect()), None
        if not snapshot.valid:
            self.consecutive_errors += 1
            status = {ConsistencyStatus.DISCONNECTED: PassStatus.DISCONNECTED, ConsistencyStatus.BUILD_UNSUPPORTED: PassStatus.BUILD_UNSUPPORTED}.get(snapshot.consistency, PassStatus.INCONSISTENT)
            return PassResult(RunLevel.CONNECT, status, snapshot.snapshot_id, snapshot.game_date, snapshot.game_time, notes=list(snapshot.consistency_reasons), next_poll_seconds=self.next_interval())
        self.consecutive_errors = 0
        triggers = detect_triggers(self.previous_snapshot, snapshot)
        match = self.match_level(snapshot)
        if match.available:
            self.previous_snapshot = snapshot
            return PassResult(RunLevel.MATCH, PassStatus.MATCH_OBSERVED, snapshot.snapshot_id, snapshot.game_date, snapshot.game_time, bool(triggers), triggers, notes=[match.reason], next_poll_seconds=self.next_interval(match_paused=True))
        changed = self.previous_snapshot is None or bool(triggers) or plan_due(self.last_plan_date, snapshot.game_date)
        if not changed:
            self.notifier.poll_unchanged()
            self.previous_snapshot = snapshot
            return PassResult(RunLevel.DECISION_POINT, PassStatus.UNCHANGED, snapshot.snapshot_id, snapshot.game_date, snapshot.game_time, False, [], next_poll_seconds=self.next_interval())
        result = self._decide_and_act(snapshot, triggers)
        self.previous_snapshot = snapshot
        self.last_decided = result
        result.next_poll_seconds = self.next_interval()
        return result

    def _decide_and_act(self, snapshot: DecisionSnapshot, triggers: list[Trigger]) -> PassResult:
        point = self.decision_point(snapshot)                       # mandatory first
        plan = self.plan(snapshot)                                  # optional optimisation second
        gate = continue_gate(snapshot, point.pending, self._rules_contexts(snapshot), plan.lineup_status, authority_mode=self.settings.authority_mode(), capabilities=self.capabilities)
        blocked = list(point.reports)
        blocked.extend(m for m in plan.missing if m.blocked_action not in {b.blocked_action for b in blocked})
        next_action = point.proposals[0] if point.proposals else plan.next_action
        level = RunLevel.WEEKLY if plan.status == "planned" else RunLevel.DECISION_POINT
        result = PassResult(level, PassStatus.PLANNED, snapshot.snapshot_id, snapshot.game_date, snapshot.game_time, True, triggers, point, plan, next_action, None, gate, blocked)
        executed: ExecutionReport | MissingCapabilityReport | str | None = None
        if next_action is not None:
            executed = self.execute(next_action, snapshot)
        else:
            executed = self.maybe_continue(snapshot, gate, point)
        if isinstance(executed, ExecutionReport):
            result.executed, result.status = executed, PassStatus.ACTED
        elif isinstance(executed, MissingCapabilityReport):
            result.blocked.append(executed)
            result.status = PassStatus.BLOCKED
        elif isinstance(executed, str):
            result.notes.append(executed)
        if blocked and result.status is PassStatus.PLANNED and next_action is None:
            result.status = PassStatus.BLOCKED
        return result

    def run(self, max_iterations: int = 1) -> list[PassResult]:
        """Run up to ``max_iterations`` passes, sleeping the pass's interval in between; stops early on Stop."""
        results: list[PassResult] = []
        for index in range(max_iterations):
            result = self.run_once()
            results.append(result)
            if result.status is PassStatus.STOPPED or index == max_iterations - 1:
                break
            self.sleep(result.next_poll_seconds)
        return results

    # ----- operator surface -----
    def status_view(self) -> OperatorView:
        """The operator screen: what was decided last (next action, prerequisites, gate) plus the latest pass's notes."""
        last, decided = self.last_pass, self.last_decided
        connection = None
        if self.last_connection is not None:
            connection = {"connected": self.last_connection.connected, "build_supported": self.last_connection.build_supported, "reason": "; ".join(self.last_connection.problems) or None, "career_matched": self.last_connection.career_matched}
        prerequisites = list(decided.blocked) if decided else []
        gate = decided.continue_gate.to_json() if decided and decided.continue_gate else None
        pending = [p.to_json() for p in decided.decision_point.pending if p.blocks_continue and not p.resolved] if decided and decided.decision_point else []
        notes = list(last.notes) if last else []
        if last is not None and last.status is PassStatus.UNCHANGED:
            notes.append("nothing has changed since the last decision")
        return build_view(self.store, settings=self.settings, snapshot=self.previous_snapshot, career=self.career, branch=self.branch, lineage_confirmed=self.lineage_confirmed, connection=connection, next_action=decided.next_action if decided else None, prerequisites=prerequisites, continue_gate=gate, stop=self.stop_control.to_json(), pending=pending, notes=notes)
