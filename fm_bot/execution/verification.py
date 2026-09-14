"""Independent postcondition verification (spec 12.2, BOT 009).

A click that "worked" and a screen that changed are not evidence that the
club's state changed the way the intent wanted. Every plan here reads the
committed state back through the adapter and, when the bridge exposes the
same fact, corroborates it against a fresh snapshot:

* ``selected_tactic_matches_catalog`` - the selected tactic id and name equal the catalog entry;
* ``lineup_matches_selection``        - the selected player ids (in slot order) and roles are exactly the intended ones;
* ``training_settings_reread``        - the committed training settings reread equal the intended ones;
* ``contract_accepted_with_obligations`` - the agreement exists and its obligations equal the intended commitments;
* ``navigation_only``                 - the identified screen is the intended one.

Exact machine values are compared. The only tolerances are for values the UI
displays rounded (:data:`DISPLAY_TOLERANCES`); money is never rounded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from ..state.records import ActionIntent, DecisionSnapshot
from ..state.status import Observed, ValueStatus
from ..state.units import Money
from .adapter import ScreenObservation, UIAdapter

VERIFICATION_VERSION = "execution.verification/1"

# Tolerances apply only to fields the UI shows rounded; the key is the field
# name suffix, the value the maximum absolute difference accepted. Money and
# identifiers never appear here.
DISPLAY_TOLERANCES: dict[str, int] = {
    "_percent": 1,          # condition / sharpness shown as whole percentages
}

# Minimum recognition confidence for a screen to count as identified in navigation checks.
NAVIGATION_MIN_CONFIDENCE = 0.9


class VerdictKind(str, Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass
class Evidence:
    """Observation ids plus the objects behind them, taken before or after execution."""

    observation_ids: list[str] = field(default_factory=list)
    snapshot: DecisionSnapshot | None = None
    screen: ScreenObservation | None = None
    step_results: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"observation_ids": list(self.observation_ids), "snapshot_id": self.snapshot.snapshot_id if self.snapshot else None, "screen": self.screen.to_json() if self.screen else None, "step_results": list(self.step_results)}


@dataclass
class Verdict:
    kind: VerdictKind
    plan: str
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    readbacks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def confirmed(self) -> bool:
        return self.kind is VerdictKind.CONFIRMED

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "plan": self.plan, "reasons": list(self.reasons), "details": dict(self.details), "readbacks": list(self.readbacks), "version": VERIFICATION_VERSION}


def _confirmed(plan: str, effect: dict[str, Any], readbacks: list[Observed], reasons: list[str] | None = None) -> Verdict:
    return Verdict(VerdictKind.CONFIRMED, plan, reasons or ["readback equals the intended state"], {"effect": effect}, [r.to_json() for r in readbacks])


def _failed(plan: str, reasons: list[str], readbacks: list[Observed], details: dict[str, Any] | None = None) -> Verdict:
    return Verdict(VerdictKind.FAILED, plan, reasons, details or {}, [r.to_json() for r in readbacks])


def _uncertain(plan: str, reasons: list[str], readbacks: list[Observed], details: dict[str, Any] | None = None) -> Verdict:
    return Verdict(VerdictKind.UNCERTAIN, plan, reasons, details or {}, [r.to_json() for r in readbacks])


def values_match(observed: Any, expected: Any, field_name: str = "") -> bool:
    """Exact equality, except a documented display tolerance for rounded percentage fields."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        return set(expected) == set(observed) and all(values_match(observed[k], expected[k], k) for k in expected)
    if isinstance(expected, (list, tuple)) and isinstance(observed, (list, tuple)):
        return len(expected) == len(observed) and all(values_match(o, e, field_name) for o, e in zip(observed, expected))
    if isinstance(expected, bool) or isinstance(observed, bool):
        return observed == expected
    if isinstance(expected, (int, float)) and isinstance(observed, (int, float)):
        for suffix, tolerance in DISPLAY_TOLERANCES.items():
            if field_name.endswith(suffix):
                return abs(observed - expected) <= tolerance
        return observed == expected
    return observed == expected


# ----- plans -----


def verify_selected_tactic(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "selected_tactic_matches_catalog"
    readback = adapter.readback("selected_tactic", {})
    if not readback.available:
        return _uncertain(plan, [f"tactic readback {readback.status.value}: {readback.reason}"], [readback])
    expected_id = intent.parameters.get("tactic_catalog_id")
    expected_name = intent.parameters.get("catalog_name")
    observed = readback.value or {}
    reasons: list[str] = []
    if observed.get("tactic_id") != expected_id:
        reasons.append(f"selected tactic id {observed.get('tactic_id')!r} != catalog entry {expected_id!r}")
    if expected_name is not None and observed.get("name") != expected_name:
        reasons.append(f"selected tactic name {observed.get('name')!r} != catalog name {expected_name!r}")
    if reasons:
        return _failed(plan, reasons, [readback], {"observed": observed})
    bridge_name = _bridge_tactic_name(after.snapshot)
    if expected_name is not None and bridge_name is not None and bridge_name != expected_name:
        return _uncertain(plan, [f"UI readback says {expected_name!r} but the bridge reports stored tactic {bridge_name!r}; sources contradict"], [readback], {"observed": observed, "bridge_stored_name": bridge_name})
    return _confirmed(plan, {"tactic_id": expected_id, "name": observed.get("name"), "bridge_corroborated": bridge_name == expected_name if expected_name is not None else None}, [readback])


def _bridge_tactic_name(snapshot: DecisionSnapshot | None) -> str | None:
    if snapshot is None:
        return None
    tactics = snapshot.routes.get("/tactics")
    if isinstance(tactics, dict) and tactics.get("available"):
        return tactics.get("stored_name")
    return None


def _bridge_lineup_ids(snapshot: DecisionSnapshot | None) -> list[int] | None:
    if snapshot is None:
        return None
    tactics = snapshot.routes.get("/tactics")
    if isinstance(tactics, dict) and tactics.get("available") and isinstance(tactics.get("positions"), list):
        return [slot.get("player_id") for slot in tactics["positions"]]
    return None


def verify_lineup(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "lineup_matches_selection"
    readback = adapter.readback("lineup", {})
    if not readback.available:
        return _uncertain(plan, [f"lineup readback {readback.status.value}: {readback.reason}"], [readback])
    expected_ids = list(intent.parameters.get("player_ids") or [])
    expected_roles = dict(intent.parameters.get("roles") or {})
    observed = readback.value or {}
    reasons: list[str] = []
    if list(observed.get("player_ids") or []) != expected_ids:
        reasons.append(f"selected player ids {observed.get('player_ids')} != intended {expected_ids} (slot order matters)")
    if dict(observed.get("roles") or {}) != expected_roles:
        reasons.append(f"selected roles {observed.get('roles')} != intended {expected_roles}")
    if reasons:
        return _failed(plan, reasons, [readback], {"observed": observed})
    bridge_ids = _bridge_lineup_ids(after.snapshot)
    if bridge_ids is not None and bridge_ids != expected_ids:
        return _uncertain(plan, [f"UI readback matches but the bridge reports lineup {bridge_ids}; sources contradict"], [readback], {"observed": observed, "bridge_lineup": bridge_ids})
    return _confirmed(plan, {"player_ids": expected_ids, "roles": expected_roles, "bridge_corroborated": bridge_ids == expected_ids if bridge_ids is not None else None}, [readback])


def verify_training(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "training_settings_reread"
    readback = adapter.readback("training_settings", {})
    if not readback.available:
        return _uncertain(plan, [f"training readback {readback.status.value}: {readback.reason}"], [readback])
    expected = dict(intent.parameters.get("settings") or {})
    observed = readback.value or {}
    if not values_match(observed, expected):
        return _failed(plan, [f"committed training settings {observed} != intended {expected}"], [readback], {"observed": observed})
    return _confirmed(plan, {"settings": observed}, [readback])


def _commitment_key(item: dict[str, Any]) -> tuple:
    amount = item.get("amount")
    money = Money.from_json(amount) if isinstance(amount, dict) else amount
    return (item.get("counterparty"), item.get("category"), str(money), item.get("due_date"), item.get("end_date"), item.get("trigger"))


def verify_contract(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "contract_accepted_with_obligations"
    offer_id = intent.parameters.get("offer_id")
    readback = adapter.readback("agreement", {"offer_id": offer_id})
    if readback.status is ValueStatus.NULL:
        return _failed(plan, [f"no agreement exists for offer {offer_id!r}: {readback.reason}"], [readback])
    if not readback.available:
        return _uncertain(plan, [f"agreement readback {readback.status.value}: {readback.reason}"], [readback])
    observed = readback.value or {}
    reasons: list[str] = []
    if not observed.get("agreement_id"):
        reasons.append("agreement readback carries no agreement id")
    expected = intent.parameters.get("expected_commitments")
    if expected is None:
        return _uncertain(plan, ["intent names no expected commitments; obligations cannot be verified"], [readback], {"observed": observed})
    observed_keys = sorted(_commitment_key(c) for c in observed.get("commitments", []))
    expected_keys = sorted(_commitment_key(c) for c in expected)
    if observed_keys != expected_keys:
        reasons.append(f"resulting obligations {observed_keys} != intended {expected_keys}")
    if reasons:
        return _failed(plan, reasons, [readback], {"observed": observed})
    return _confirmed(plan, {"agreement_id": observed.get("agreement_id"), "offer_id": offer_id, "commitments": list(observed.get("commitments", []))}, [readback])


def verify_navigation(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    plan = "navigation_only"
    expected = intent.parameters.get("target") or intent.parameters.get("expected_screen")
    observation = adapter.identify_screen()
    readback = Observed.available_value(observation.to_json(), f"ui:{getattr(adapter, 'name', 'adapter')}:screen", observation.observed_at, what="screen")
    if not observation.identified or observation.confidence < NAVIGATION_MIN_CONFIDENCE:
        return _uncertain(plan, [f"screen unidentified or low confidence ({observation.confidence:.2f}): {observation.reason}"], [readback])
    if expected is None:
        return _uncertain(plan, ["intent names no target screen"], [readback])
    if observation.screen_id != expected:
        return _failed(plan, [f"screen {observation.screen_id!r} shown, expected {expected!r}"], [readback])
    return _confirmed(plan, {"screen_id": observation.screen_id}, [readback])


PLANS: dict[str, Callable[[ActionIntent, Evidence, Evidence, UIAdapter], Verdict]] = {
    "selected_tactic_matches_catalog": verify_selected_tactic,
    "lineup_matches_selection": verify_lineup,
    "training_settings_reread": verify_training,
    "contract_accepted_with_obligations": verify_contract,
    "navigation_only": verify_navigation,
}


def verify(intent: ActionIntent, before: Evidence, after: Evidence, adapter: UIAdapter) -> Verdict:
    """Establish the intended effect by independent readback; never by step success alone.

    ``before``/``after`` evidence is attached for the audit trail. Step results
    in ``after`` are recorded but never decide the verdict.
    """
    plan = PLANS.get(intent.verification)
    if plan is None:
        return Verdict(VerdictKind.UNCERTAIN, intent.verification, [f"no verification plan named {intent.verification!r}; effect cannot be established"], {"before": before.to_json(), "after": after.to_json()})
    verdict = plan(intent, before, after, adapter)
    verdict.details["before"] = before.to_json()
    verdict.details["after"] = after.to_json()
    return verdict
