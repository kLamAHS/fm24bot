"""Single-fixture lineup assignment (spec 7.1, 7.3, 6.2; SEL 01, SEL 02; BOT 008).

The starting eleven is an *assignment problem*: every tactic slot needs
exactly one player, every player fills at most one slot, and only players
the club is allowed to field may be used. Because each slot needs exactly
one player, the problem is a bipartite matching with additive scores and is
solved **exactly** with the Hungarian algorithm (O(n^3), pure Python) - no
heuristic is needed for the eleven itself. The bench composition is a
labelled heuristic on top of the exact eleven.

Baseline versus experiment
--------------------------
* Baseline (this module): exact maximum-score assignment of role scores from
  :mod:`fm_bot.planning.roles`, hard eligibility/exclusion/cap constraints,
  Hall-violator infeasibility explanations, an independent
  :func:`check_plan`, and a greedy bench.
* Not here: learned lineup value, opponent-specific adjustments, or joint
  multi-fixture selection (see :mod:`fm_bot.planning.minutes` for the
  sequential horizon heuristic).

Selection rules (SEL 01, SEL 02)
--------------------------------
* A player whose eligibility is observed ``False`` is never assigned, in
  either mode.
* A player whose eligibility is unavailable (missing, stale, unsupported,
  contradicted, or simply not supplied) may be used in ``advisory`` mode only;
  the plan is then ``advisory_unverified`` and :attr:`LineupPlan.submittable`
  is false. In ``submit`` mode such players are inadmissible.
* When the slots cannot be covered, the plan is ``infeasible`` with
  :class:`ConstraintConflict` records naming the colliding requirements,
  and no eleven is fabricated.
* Squad size, bench size and substitution allowances are taken from the
  request or the competition rules; when unknown they stay unknown
  (``bench_status == "unknown_rules"``) and are listed for verification.

The solver time budget is a budget, not a measurement. When it is exceeded,
the last feasible incumbent is used only after :func:`check_plan` has
revalidated every constraint independently of the solver.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..state.records import CompetitionContext, PlayerState
from ..state.status import Observed, ValueStatus
from ..state.views import FixtureView
from .roles import ROLE_WEIGHTS_VERSION, RoleScore, base_position, explain_role_score

LINEUP_SOLVER_VERSION = "lineup-hungarian-v1"
MODES: tuple[str, ...] = ("advisory", "submit")
STATUSES: tuple[str, ...] = ("legal", "advisory_unverified", "infeasible")

SCORE_SCALE = 1_000_000            # role scores (0..1) become integer costs so the solver is exact and deterministic
INADMISSIBLE_COST = 10 ** 12       # any assignment using this cost is rejected after solving
DEFAULT_TIME_BUDGET_SECONDS = 2.0  # engineering budget (spec 15.2), not a measurement
DEFAULT_MATCH_MINUTES = 90         # minutes a starter is assumed to be asked for; caps below this exclude him
BENCH_GK_COVER_HEURISTIC = True    # keep one goalkeeper on the bench when one is available and the bench has room

FLAG_ELIGIBILITY_UNVERIFIED = "eligibility_unverified"
FLAG_FORCED = "forced_assignment"
FLAG_SCORE_ADJUSTED = "score_adjusted"
FLAG_GK_COVER = "gk_cover_heuristic"
FLAG_TIME_BUDGET = "time_budget_exceeded"


class LineupSolverError(RuntimeError):
    """An internal inconsistency (for example an exact solution failing its own check)."""


class SolverTimeBudgetExceeded(Exception):
    """Raised inside the exact solver when the wall-clock budget runs out."""


# ---------------------------------------------------------------------------
# request and plan records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoleSlot:
    """One position in the selected tactic: slot index, position code (``DCR``), role and duty."""

    slot: int
    position: str
    role: str | None = None
    duty: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"slot": self.slot, "position": self.position, "role": self.role, "duty": self.duty}


def slots_from_tactic_view(view: dict[str, Any]) -> Observed:
    """Turn ``views.tactic_view`` output into ``Observed[list[RoleSlot]]``; unavailable when the tactic was not decoded."""
    if view.get("status") != "available":
        return Observed.unavailable(ValueStatus.MISSING if view.get("status") == "missing" else ValueStatus.UNSUPPORTED, "tactic_slots", view.get("reason") or f"tactic status {view.get('status')}", "views.tactic_view")
    slots = [RoleSlot(int(s["slot"]), str(s.get("position") or ""), s.get("role") if s.get("role_status") == "decoded" else None, s.get("duty")) for s in view.get("slots", [])]
    return Observed.available_value(slots, "views.tactic_view", what="tactic_slots")


def rules_observed(context: CompetitionContext | None, name: str) -> Observed:
    """Read one substitution rule (``bench_size``, ``substitutions_allowed``) from a competition context."""
    if context is None:
        return Observed.unavailable(ValueStatus.MISSING, name, "no competition rules supplied", "none")
    group = context.substitution_rules or {}
    raw = group.get(name)
    if isinstance(raw, dict) and "status" in raw:
        status = ValueStatus(raw["status"])
        if status is ValueStatus.AVAILABLE:
            return Observed.available_value(int(raw["value"]), raw.get("source") or context.source, what=name)
        return Observed.unavailable(status, name, raw.get("reason") or f"{name} {status.value} in rules profile", raw.get("source") or context.source)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return Observed.available_value(raw, context.source, what=name)
    return Observed.unavailable(ValueStatus.MISSING, name, group.get("reason") or f"{name} not in the competition rules profile", context.source)


def _missing(what: str, reason: str) -> Observed:
    return Observed.unavailable(ValueStatus.MISSING, what, reason, "none")


@dataclass
class LineupRequest:
    """Everything the selection needs for one fixture.

    ``eligibility`` maps player id to ``Observed[bool]`` (see
    :func:`eligibility_map`); players absent from the map count as missing.
    ``forced_assignments`` maps slot index to player id. ``minutes_cap`` is
    the permitted minutes per player; a cap below ``match_minutes`` keeps the
    player out of the eleven. ``score_adjustments`` are additive changes to
    role scores (used by the minutes planner for accumulated workload and
    promised minutes) and are reported on each assignment.
    """

    players: list[PlayerState]
    slots: list[RoleSlot]
    fixture: FixtureView | None = None
    eligibility: dict[int, Observed] = field(default_factory=dict)
    mode: str = "advisory"
    bench_size: Observed = field(default_factory=lambda: _missing("bench_size", "bench size not supplied"))
    substitutes_allowed: Observed = field(default_factory=lambda: _missing("substitutes_allowed", "substitution allowance not supplied"))
    excluded_player_ids: frozenset[int] = frozenset()
    forced_assignments: dict[int, int] = field(default_factory=dict)
    minutes_cap: dict[int, int] | None = None
    competition_rules: CompetitionContext | None = None
    score_adjustments: dict[int, float] = field(default_factory=dict)
    match_minutes: int = DEFAULT_MATCH_MINUTES
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if len({s.slot for s in self.slots}) != len(self.slots):
            raise ValueError("duplicate slot indices in request")
        if len({p.player_id for p in self.players}) != len(self.players):
            raise ValueError("duplicate player ids in request")
        if self.competition_rules is not None:
            if not self.bench_size.available:
                self.bench_size = rules_observed(self.competition_rules, "bench_size")
            if not self.substitutes_allowed.available:
                self.substitutes_allowed = rules_observed(self.competition_rules, "substitutions_allowed")
        forced_players = list(self.forced_assignments.values())
        if len(set(forced_players)) != len(forced_players):
            raise ValueError("a player is forced into more than one slot")

    def eligibility_of(self, player_id: int) -> Observed:
        found = self.eligibility.get(player_id)
        if found is None:
            return Observed.unavailable(ValueStatus.MISSING, "verified_eligible", "no eligibility observation supplied for this player", "none")
        return found

    def slot_by_index(self, index: int) -> RoleSlot | None:
        return next((s for s in self.slots if s.slot == index), None)


@dataclass
class Assignment:
    slot: int
    position: str
    role: str | None
    duty: str | None
    player_id: int
    player_name: str
    score: float
    familiarity: str
    flags: list[str] = field(default_factory=list)
    adjusted_score: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {"slot": self.slot, "position": self.position, "role": self.role, "duty": self.duty, "player_id": self.player_id, "player_name": self.player_name, "score": self.score, "familiarity": self.familiarity, "flags": list(self.flags), "adjusted_score": self.adjusted_score}


@dataclass
class BenchEntry:
    player_id: int
    player_name: str
    best_slot: int | None
    best_position: str | None
    score: float | None
    flags: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "player_name": self.player_name, "best_slot": self.best_slot, "best_position": self.best_position, "score": self.score, "flags": list(self.flags)}


@dataclass
class ConstraintConflict:
    """A set of requirements that cannot all hold; ``requirements`` are the human-readable terms."""

    kind: str
    message: str
    slots: list[int] = field(default_factory=list)
    players: list[int] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "slots": list(self.slots), "players": list(self.players), "requirements": list(self.requirements)}


@dataclass
class CheckResult:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "violations": list(self.violations)}


@dataclass
class LineupPlan:
    status: str
    assignments: list[Assignment]
    bench: list[BenchEntry]
    bench_status: str                       # available | unknown_rules | infeasible
    objective_value: float | None
    binding_constraints: list[str]
    conflicts: list[ConstraintConflict]
    verification_required: list[str]
    mode: str
    solver: dict[str, Any] = field(default_factory=dict)
    exclusions: dict[int, list[str]] = field(default_factory=dict)
    substitutes_allowed: Observed = field(default_factory=lambda: _missing("substitutes_allowed", "not supplied"))
    bench_size: Observed = field(default_factory=lambda: _missing("bench_size", "not supplied"))
    fixture_identity: str | None = None
    version: str = LINEUP_SOLVER_VERSION

    @property
    def submittable(self) -> bool:
        """Only a ``legal`` plan produced in ``submit`` mode with nothing left to verify may be submitted."""
        return self.status == "legal" and self.mode == "submit" and not self.verification_required and not self.conflicts

    @property
    def player_ids(self) -> list[int]:
        return [a.player_id for a in self.assignments]

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "assignments": [a.to_json() for a in self.assignments], "bench": [b.to_json() for b in self.bench], "bench_status": self.bench_status, "objective_value": self.objective_value, "binding_constraints": list(self.binding_constraints), "conflicts": [c.to_json() for c in self.conflicts], "verification_required": list(self.verification_required), "mode": self.mode, "solver": dict(self.solver), "exclusions": {str(k): list(v) for k, v in self.exclusions.items()}, "substitutes_allowed": self.substitutes_allowed.to_json(), "bench_size": self.bench_size.to_json(), "fixture_identity": self.fixture_identity, "submittable": self.submittable, "version": self.version}


# ---------------------------------------------------------------------------
# eligibility helpers
# ---------------------------------------------------------------------------

def eligibility_map(players: Iterable[PlayerState], fixture: FixtureView | dict[str, Any] | None, *, snapshot_game_date: str | None, snapshot_game_time: str | None, club_id: int | None = None, loan_contracts_supported: bool = False) -> dict[int, Observed]:
    """``verified_eligible[p, f]`` for every player, from the rules module (spec 7.1)."""
    from ..rules.eligibility import verified_eligible
    return {p.player_id: verified_eligible(p, fixture, snapshot_game_date=snapshot_game_date, snapshot_game_time=snapshot_game_time, club_id=club_id, loan_contracts_supported=loan_contracts_supported) for p in players}


def eligibility_verdict(observed: Observed, mode: str) -> tuple[bool, str | None, list[str]]:
    """``(admissible, exclusion_reason, flags)`` for one player's eligibility under a mode."""
    if observed.available:
        if observed.value is True:
            return True, None, []
        return False, f"ineligible: {observed.reason or 'observed ineligible'}", []
    reason = f"eligibility {observed.status.value}: {observed.reason or 'not verified'}"
    if mode == "submit":
        return False, reason, []
    return True, None, [FLAG_ELIGIBILITY_UNVERIFIED]


def player_exclusions(request: LineupRequest) -> dict[int, list[str]]:
    """Reasons a player may not start at all (independent of slot); empty list means admissible somewhere."""
    result: dict[int, list[str]] = {}
    for player in request.players:
        reasons: list[str] = []
        pid = player.player_id
        if pid in request.excluded_player_ids:
            reasons.append("excluded by request")
        admissible, reason, _ = eligibility_verdict(request.eligibility_of(pid), request.mode)
        if not admissible and reason:
            reasons.append(reason)
        if request.minutes_cap is not None and pid in request.minutes_cap and request.minutes_cap[pid] < request.match_minutes:
            reasons.append(f"minutes cap {request.minutes_cap[pid]} below the {request.match_minutes} minutes a starter needs")
        result[pid] = reasons
    return result


# ---------------------------------------------------------------------------
# candidate matrix
# ---------------------------------------------------------------------------

@dataclass
class _Matrix:
    slots: list[RoleSlot]
    players: list[PlayerState]
    cost: list[list[int]]                         # slots x players; INADMISSIBLE_COST where not allowed
    detail: list[list[RoleScore | None]]
    slot_reasons: list[dict[int, str]]            # per slot: player id -> why inadmissible here
    exclusions: dict[int, list[str]]

    def admissible(self, i: int, j: int) -> bool:
        return self.cost[i][j] < INADMISSIBLE_COST

    def adjusted_score(self, i: int, j: int) -> float:
        return (SCORE_SCALE - self.cost[i][j]) / SCORE_SCALE


def _build_matrix(request: LineupRequest) -> _Matrix:
    exclusions = player_exclusions(request)
    forced_by_player = {pid: slot for slot, pid in request.forced_assignments.items()}
    cost: list[list[int]] = []
    detail: list[list[RoleScore | None]] = []
    slot_reasons: list[dict[int, str]] = []
    for slot in request.slots:
        row_cost: list[int] = []
        row_detail: list[RoleScore | None] = []
        reasons: dict[int, str] = {}
        forced_here = request.forced_assignments.get(slot.slot)
        for player in request.players:
            pid = player.player_id
            score = explain_role_score(player, slot.role, slot.position)
            row_detail.append(score)
            blocked = exclusions[pid][0] if exclusions[pid] else None
            if blocked is None and forced_here is not None and pid != forced_here:
                blocked = f"slot {slot.slot} is forced to player {forced_here}"
            if blocked is None and pid in forced_by_player and forced_by_player[pid] != slot.slot:
                blocked = f"player forced into slot {forced_by_player[pid]}"
            if blocked is None and not score.score.available:
                blocked = f"no usable role score: {score.score.reason}"
            if blocked is not None:
                reasons[pid] = blocked
                row_cost.append(INADMISSIBLE_COST)
                continue
            value = score.score.value + request.score_adjustments.get(pid, 0.0)
            value = max(0.0, min(1.0, value))
            row_cost.append(SCORE_SCALE - int(round(value * SCORE_SCALE)))
        cost.append(row_cost)
        detail.append(row_detail)
        slot_reasons.append(reasons)
    return _Matrix(list(request.slots), list(request.players), cost, detail, slot_reasons, exclusions)


# ---------------------------------------------------------------------------
# exact solver: Hungarian algorithm (minimising integer costs)
# ---------------------------------------------------------------------------

def hungarian(cost: list[list[int]], deadline: float | None = None) -> list[int]:
    """Minimum-cost assignment of rows to distinct columns (rows <= columns), O(rows^2 * cols).

    Returns the column chosen for each row. Raises
    :class:`SolverTimeBudgetExceeded` when ``deadline`` (``time.monotonic``
    seconds) passes between row iterations. Pure integer arithmetic.
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    if m < n:
        raise ValueError("hungarian needs at least as many columns as rows; pad the matrix first")
    inf = float("inf")
    u = [0] * (n + 1)
    v = [0] * (m + 1)
    p = [0] * (m + 1)        # p[j] = row matched to column j (1-based), 0 when free
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        if deadline is not None and time.monotonic() > deadline:
            raise SolverTimeBudgetExceeded()
        p[0] = i
        j0 = 0
        minv = [inf] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            row = cost[i0 - 1]
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = row[j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    result = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


def _padded(cost: list[list[int]]) -> list[list[int]]:
    n = len(cost)
    m = len(cost[0]) if cost else 0
    if m >= n:
        return cost
    return [row + [INADMISSIBLE_COST] * (n - m) for row in cost]


# ---------------------------------------------------------------------------
# feasibility: bipartite matching and Hall violators
# ---------------------------------------------------------------------------

def _max_matching(matrix: _Matrix) -> tuple[list[int], list[int]]:
    """Kuhn's augmenting-path matching on admissible pairs: ``(slot->player index or -1, player->slot index or -1)``."""
    n, m = len(matrix.slots), len(matrix.players)
    match_slot = [-1] * n
    match_player = [-1] * m

    def try_slot(i: int, seen: list[bool]) -> bool:
        for j in range(m):
            if matrix.admissible(i, j) and not seen[j]:
                seen[j] = True
                if match_player[j] == -1 or try_slot(match_player[j], seen):
                    match_player[j] = i
                    match_slot[i] = j
                    return True
        return False

    for i in range(n):
        try_slot(i, [False] * m)
    return match_slot, match_player


def _hall_violator(matrix: _Matrix, match_slot: list[int], match_player: list[int], start: int) -> tuple[list[int], list[int]]:
    """Slots reachable from an unmatched slot by alternating paths; their neighbourhood is strictly smaller (Konig)."""
    n, m = len(matrix.slots), len(matrix.players)
    seen_slots = [False] * n
    seen_players = [False] * m
    queue: deque[int] = deque([start])
    seen_slots[start] = True
    while queue:
        i = queue.popleft()
        for j in range(m):
            if matrix.admissible(i, j) and not seen_players[j]:
                seen_players[j] = True
                k = match_player[j]
                if k != -1 and not seen_slots[k]:
                    seen_slots[k] = True
                    queue.append(k)
    return [i for i in range(n) if seen_slots[i]], [j for j in range(m) if seen_players[j]]


def diagnose_conflicts(request: LineupRequest, matrix: _Matrix | None = None) -> list[ConstraintConflict]:
    """Explain why the slots cannot be covered (SEL 02). Empty when a feasible eleven exists."""
    matrix = matrix or _build_matrix(request)
    match_slot, match_player = _max_matching(matrix)
    conflicts: list[ConstraintConflict] = []
    covered: set[int] = set()
    for i, j in enumerate(match_slot):
        if j != -1 or i in covered:
            continue
        slots_idx, players_idx = _hall_violator(matrix, match_slot, match_player, i)
        covered.update(slots_idx)
        slots = [matrix.slots[k] for k in slots_idx]
        players = [matrix.players[k].player_id for k in players_idx]
        needs = ", ".join(f"{s.position}/{s.role or 'undecoded role'}" for s in slots)
        requirements = [f"{len(slots)} slot(s) [{needs}] each need exactly one distinct player", f"only {len(players)} admissible player(s) can fill them: {players or 'none'}"]
        requirements.extend(_why_missing_cover(matrix, slots_idx, players_idx))
        conflicts.append(ConstraintConflict("coverage", f"{len(slots)} required slot(s) ({needs}) but only {len(players)} admissible player(s)", [s.slot for s in slots], players, requirements))
    return conflicts


def _why_missing_cover(matrix: _Matrix, slots_idx: list[int], players_idx: list[int]) -> list[str]:
    """For a violating slot set, say why the players who are familiar with those positions were excluded."""
    bases = {base_position(matrix.slots[i].position) for i in slots_idx}
    admissible = set(players_idx)
    out: list[str] = []
    for j, player in enumerate(matrix.players):
        if j in admissible:
            continue
        familiar = bases & set(player.positions or [])
        if not familiar:
            continue
        reasons = matrix.exclusions.get(player.player_id) or [matrix.slot_reasons[i].get(player.player_id, "not admissible") for i in slots_idx if player.player_id in matrix.slot_reasons[i]]
        out.append(f"player {player.player_id} ({player.name}) plays {sorted(familiar)} but is excluded: {'; '.join(dict.fromkeys(reasons)) or 'not admissible'}")
    return out


# ---------------------------------------------------------------------------
# incumbents and bench
# ---------------------------------------------------------------------------

def _greedy_incumbent(matrix: _Matrix) -> list[int] | None:
    """Fast feasible incumbent: fill the most constrained slots first with their best free player. May fail."""
    n = len(matrix.slots)
    order = sorted(range(n), key=lambda i: sum(1 for j in range(len(matrix.players)) if matrix.admissible(i, j)))
    used: set[int] = set()
    result = [-1] * n
    for i in order:
        best = min((j for j in range(len(matrix.players)) if matrix.admissible(i, j) and j not in used), key=lambda j: matrix.cost[i][j], default=None)
        if best is None:
            return None
        used.add(best)
        result[i] = best
    return result


def _select_bench(request: LineupRequest, matrix: _Matrix, starters: set[int]) -> tuple[list[BenchEntry], str, list[str]]:
    """Greedy bench (heuristic): best remaining admissible players by their best slot score, with optional GK cover."""
    notes: list[str] = []
    if not request.bench_size.available:
        return [], "unknown_rules", notes
    size = int(request.bench_size.value)
    candidates: list[BenchEntry] = []
    for j, player in enumerate(matrix.players):
        pid = player.player_id
        if pid in starters or matrix.exclusions.get(pid):
            continue
        _, _, flags = eligibility_verdict(request.eligibility_of(pid), request.mode)
        scored = [i for i in range(len(matrix.slots)) if matrix.detail[i][j] is not None and matrix.detail[i][j].score.available]
        if not scored:
            continue
        best_i = max(scored, key=lambda i: matrix.detail[i][j].score.value)
        score = matrix.detail[best_i][j].score.value
        candidates.append(BenchEntry(pid, player.name, matrix.slots[best_i].slot, matrix.slots[best_i].position, score, list(flags)))
    candidates.sort(key=lambda b: (-(b.score or 0.0), b.player_id))
    chosen: list[BenchEntry] = []
    if BENCH_GK_COVER_HEURISTIC and size >= 1:
        positions = {p.player_id: set(p.positions or []) for p in matrix.players}
        keeper = next((b for b in candidates if "GK" in positions[b.player_id]), None)
        if keeper is not None:
            keeper.flags.append(FLAG_GK_COVER)
            chosen.append(keeper)
            candidates.remove(keeper)
        else:
            notes.append("no eligible goalkeeper available for the bench")
    chosen.extend(candidates[: max(0, size - len(chosen))])
    if len(chosen) < size:
        notes.append(f"bench short: {len(chosen)} of {size} places filled by admissible players")
    return chosen, "available", notes


# ---------------------------------------------------------------------------
# independent constraint check
# ---------------------------------------------------------------------------

def check_plan(plan: LineupPlan, request: LineupRequest) -> CheckResult:
    """Revalidate every constraint of ``plan`` against ``request`` without trusting the solver.

    Checks: every slot exactly once, every player at most once and known,
    eligibility rules for the mode (SEL 01), exclusions, forced assignments,
    minutes caps, bench disjointness and size, and that the status label is
    honest (``legal`` only when every starter is verified eligible).
    """
    violations: list[str] = []
    known = {p.player_id for p in request.players}
    slot_ids = [s.slot for s in request.slots]
    if plan.status == "infeasible":
        if plan.assignments:
            violations.append("an infeasible plan must not carry assignments")
        return CheckResult(not violations, violations)
    assigned_slots = [a.slot for a in plan.assignments]
    if sorted(assigned_slots) != sorted(slot_ids):
        violations.append(f"slots covered {sorted(assigned_slots)} differ from required {sorted(slot_ids)}")
    starters = [a.player_id for a in plan.assignments]
    if len(set(starters)) != len(starters):
        violations.append("a player is assigned to more than one slot")
    unverified: list[int] = []
    for a in plan.assignments:
        pid = a.player_id
        if pid not in known:
            violations.append(f"player {pid} is not in the request")
            continue
        admissible, reason, flags = eligibility_verdict(request.eligibility_of(pid), request.mode)
        if not admissible:
            violations.append(f"player {pid}: {reason}")
        elif flags:
            unverified.append(pid)
        if pid in request.excluded_player_ids:
            violations.append(f"player {pid} was excluded by request")
        if request.minutes_cap is not None and pid in request.minutes_cap and request.minutes_cap[pid] < request.match_minutes:
            violations.append(f"player {pid} exceeds his minutes cap {request.minutes_cap[pid]}")
    for slot, pid in request.forced_assignments.items():
        actual = next((a.player_id for a in plan.assignments if a.slot == slot), None)
        if actual != pid:
            violations.append(f"slot {slot} was forced to player {pid} but holds {actual}")
    if unverified and plan.status == "legal":
        violations.append(f"plan labelled legal but players {unverified} are not verified eligible")
    if plan.status == "advisory_unverified" and request.mode == "submit":
        violations.append("an advisory_unverified plan cannot come from submit mode")
    bench_ids = [b.player_id for b in plan.bench]
    if set(bench_ids) & set(starters):
        violations.append("bench overlaps the starting eleven")
    if len(set(bench_ids)) != len(bench_ids):
        violations.append("duplicate player on the bench")
    if plan.bench and not request.bench_size.available:
        violations.append("bench composed although the bench size is unknown")
    if request.bench_size.available and len(bench_ids) > int(request.bench_size.value):
        violations.append(f"bench has {len(bench_ids)} players but the rules allow {request.bench_size.value}")
    for pid in bench_ids:
        if pid not in known:
            violations.append(f"bench player {pid} is not in the request")
            continue
        admissible, reason, _ = eligibility_verdict(request.eligibility_of(pid), request.mode)
        if not admissible:
            violations.append(f"bench player {pid}: {reason}")
        if pid in request.excluded_player_ids:
            violations.append(f"bench player {pid} was excluded by request")
    return CheckResult(not violations, violations)


# ---------------------------------------------------------------------------
# solve
# ---------------------------------------------------------------------------

def solve(request: LineupRequest) -> LineupPlan:
    """Exact best eleven for one fixture, or an explained infeasibility.

    Returns a :class:`LineupPlan` whose status is ``legal`` (every starter
    verified eligible), ``advisory_unverified`` (advisory mode used players
    whose eligibility is not verified; never submit) or ``infeasible`` (the
    conflicts say which requirements collide). The plan always passes
    :func:`check_plan` unless it is infeasible.
    """
    started = time.monotonic()
    deadline = started + max(0.0, request.time_budget_seconds)
    matrix = _build_matrix(request)
    solver: dict[str, Any] = {"version": LINEUP_SOLVER_VERSION, "weights_version": ROLE_WEIGHTS_VERSION, "time_budget_seconds": request.time_budget_seconds, "time_budget_exceeded": False, "method": "hungarian_exact", "independent_check": None}
    if not request.slots:
        return _infeasible(request, matrix, [ConstraintConflict("no_slots", "the tactic has no decoded slots to fill", requirements=["at least one slot is required"])], solver, started)
    conflicts = diagnose_conflicts(request, matrix)
    if conflicts:
        return _infeasible(request, matrix, conflicts, solver, started)
    try:
        columns = hungarian(_padded(matrix.cost), deadline)
    except SolverTimeBudgetExceeded:
        solver["time_budget_exceeded"] = True
        columns = _greedy_incumbent(matrix)
        solver["method"] = "greedy_incumbent_after_time_budget"
        if columns is None:
            match_slot, _ = _max_matching(matrix)
            columns = match_slot
            solver["method"] = "matching_incumbent_after_time_budget"
    if any(j == -1 or j >= len(matrix.players) or not matrix.admissible(i, j) for i, j in enumerate(columns)):
        # cannot happen after a clean diagnosis; report honestly rather than fabricate
        return _infeasible(request, matrix, diagnose_conflicts(request, matrix) or [ConstraintConflict("solver", "the solver returned an inadmissible assignment", requirements=["internal consistency"])], solver, started)
    plan = _plan_from_columns(request, matrix, columns, solver)
    check = check_plan(plan, request)
    solver["independent_check"] = check.to_json()
    solver["elapsed_seconds"] = round(time.monotonic() - started, 6)
    if not check.ok:
        if solver["method"] == "hungarian_exact":
            raise LineupSolverError(f"exact solution failed its independent check: {check.violations}")
        return _infeasible(request, matrix, [ConstraintConflict("incumbent_rejected", "solver time budget exceeded and the incumbent failed the independent constraint check", requirements=check.violations)], solver, started)
    return plan


def _plan_from_columns(request: LineupRequest, matrix: _Matrix, columns: list[int], solver: dict[str, Any]) -> LineupPlan:
    assignments: list[Assignment] = []
    binding: list[str] = []
    verification: list[str] = []
    unverified: list[int] = []
    objective = 0.0
    for i, j in enumerate(columns):
        slot, player = matrix.slots[i], matrix.players[j]
        detail = matrix.detail[i][j]
        flags = list(detail.flags)
        _, _, elig_flags = eligibility_verdict(request.eligibility_of(player.player_id), request.mode)
        flags.extend(elig_flags)
        if elig_flags:
            unverified.append(player.player_id)
        if request.forced_assignments.get(slot.slot) == player.player_id:
            flags.append(FLAG_FORCED)
            binding.append(f"slot {slot.slot} ({slot.position}) forced to {player.name}")
        adjusted = matrix.adjusted_score(i, j)
        if player.player_id in request.score_adjustments:
            flags.append(FLAG_SCORE_ADJUSTED)
        if detail.familiarity == "unfamiliar":
            binding.append(f"{player.name} starts out of position at {slot.position} (unfamiliar)")
        if "role_fallback" in detail.flags:
            verification.append(f"slot {slot.slot} ({slot.position}) role not decoded; scored as {detail.role} by fallback")
        objective += detail.score.value
        assignments.append(Assignment(slot.slot, slot.position, detail.role, slot.duty, player.player_id, player.name, detail.score.value, detail.familiarity, flags, adjusted))
    assignments.sort(key=lambda a: a.slot)
    excluded = {pid: reasons for pid, reasons in matrix.exclusions.items() if reasons}
    if excluded:
        binding.append(f"{len(excluded)} player(s) excluded before assignment (see exclusions)")
    if request.minutes_cap:
        capped = [pid for pid, cap in request.minutes_cap.items() if cap < request.match_minutes]
        if capped:
            binding.append(f"minutes caps kept {len(capped)} player(s) out of the eleven: {capped}")
    bench, bench_status, bench_notes = _select_bench(request, matrix, {a.player_id for a in assignments})
    binding.extend(bench_notes)
    if bench_status == "unknown_rules":
        verification.append(f"bench size unknown ({request.bench_size.reason}); capability competition_rules")
    if not request.substitutes_allowed.available:
        verification.append(f"substitution allowance unknown ({request.substitutes_allowed.reason}); capability competition_rules")
    if unverified:
        verification.append(f"eligibility not verified for starters {sorted(unverified)}; capabilities eligibility_injury/eligibility_suspension/eligibility_loan_absence/eligibility_registration")
    if solver.get("time_budget_exceeded"):
        verification.append("solver time budget exceeded; incumbent accepted only after independent check")
    status = "advisory_unverified" if unverified else "legal"
    return LineupPlan(status, assignments, bench, bench_status, round(objective, 9), binding, [], verification, request.mode, solver, excluded, request.substitutes_allowed, request.bench_size, request.fixture.identity if request.fixture else None)


def shared_conflict_cause(request: LineupRequest, matrix: _Matrix, conflicts: list[ConstraintConflict]) -> str | None:
    """One sentence when every slot is short for the same reason, else ``None``.

    Eleven slots reported short of players read as eleven separate problems
    when they are one: nobody is admissible because, say, no player's
    eligibility has been verified for this fixture. The operator's main flow
    should say that once and name what would resolve it (spec 15.1), while
    the per-slot conflicts stay in the plan for the detail view (SEL 02).
    """
    if not conflicts or any(c.players for c in conflicts):
        return None
    if {slot for c in conflicts for slot in c.slots} != {s.slot for s in request.slots}:
        return None
    reasons = [matrix.exclusions.get(p.player_id) or [] for p in request.players]
    if not reasons or any(not r for r in reasons):
        return None            # somebody is admissible, so the shortage is not universal
    common = set(reasons[0])
    for other in reasons[1:]:
        common &= set(other)
    if not common:
        return None
    cause = "; ".join(sorted(common))
    return f"no player is admissible for any of the {len(request.slots)} slots: every one of the {len(request.players)} in the squad is held out because {cause}"


def _infeasible(request: LineupRequest, matrix: _Matrix, conflicts: list[ConstraintConflict], solver: dict[str, Any], started: float) -> LineupPlan:
    solver = dict(solver)
    solver["elapsed_seconds"] = round(time.monotonic() - started, 6)
    excluded = {pid: reasons for pid, reasons in matrix.exclusions.items() if reasons}
    shared = shared_conflict_cause(request, matrix, conflicts)
    binding = [shared] if shared else [c.message for c in conflicts]
    verification = ["submission stopped: no legal lineup exists under the current constraints"]
    return LineupPlan("infeasible", [], [], "infeasible", None, binding, conflicts, verification, request.mode, solver, excluded, request.substitutes_allowed, request.bench_size, request.fixture.identity if request.fixture else None)
