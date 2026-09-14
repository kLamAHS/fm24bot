"""Shared offline harness for the execution tests (adapter, lifecycle, executor, verification, reconciliation).

The fake bridge's ``/tactics`` route is linked to the :class:`FakeAdapter`
state, so a tactic or lineup selected through the fake UI is what the bridge
reports afterwards - the same independent corroboration the real verifier
relies on. Money is exact GBP; wages weekly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..bridge_client.client import BridgeClient
from ..bridge_client.transport import FakeTransport
from ..execution.adapter import FAKE_WORKFLOWS, FakeAdapter
from ..execution.executor import SingleWriterExecutor
from ..execution.lifecycle import IntentFactory, enqueue, validate
from ..rules.authority import AuthorityLimits, AuthorityMode, AuthorityProfile
from ..rules.capabilities import CapabilityRegistry
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import ActionIntent, ActionState, DecisionSnapshot
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from ..state.store import Store
from ..state.units import Money, Period
from . import fixtures as fx

ROUTES = ["/tactics", "/finances", "/squad"]
WEEKLY_WAGE = Money.native_gbp(4000, Period.WEEKLY)
OFFER_COMMITMENTS = [{"counterparty": "Brad Target", "category": "wages", "amount": WEEKLY_WAGE.to_json(), "due_date": "2024-02-24", "end_date": "2026-06-30", "trigger": None}]
OFFERS = {"offer-1": {"agreement_id": "agr-1", "commitments": OFFER_COMMITMENTS}}


def linked_world(adapter: FakeAdapter, *, session: str = fx.SESSION, game_date: str = fx.GAME_DATE) -> dict[str, Any]:
    """The fixture world with ``/tactics`` reporting whatever the fake UI has committed."""
    names = {spec[0]: spec[1] for spec in fx.SQUAD_SPEC}

    def tactics():
        payload = fx.tactics_payload()
        payload["stored_name"] = adapter.tactic_catalog.get(adapter.selected_tactic_id)
        if adapter.lineup_ids:
            for slot, pid in zip(payload["positions"], adapter.lineup_ids):
                slot["player_id"], slot["player_name"] = pid, names.get(pid, f"player {pid}")
        return FakeTransport.envelope(payload, session_id=session)

    return fx.world(session=session, game_date=game_date, overrides={"/tactics": tactics})


def scoped_profile(*families: str) -> AuthorityProfile:
    limits = AuthorityLimits(max_weekly_wage_commitment=Money.native_gbp(5000, Period.WEEKLY), max_total_fee_commitment=Money.native_gbp(500000, Period.ONCE), max_contract_years=4)
    return AuthorityProfile(AuthorityMode.SCOPED_EXECUTION, set(families or ("tactics", "selection", "training", "contracts")), limits, 3)


@dataclass
class Harness:
    store: Store
    career_id: str
    branch_id: str
    adapter: FakeAdapter
    client: BridgeClient
    registry: CapabilityRegistry
    profile: AuthorityProfile
    factory: IntentFactory

    def snapshot(self, routes: list[str] | None = None) -> DecisionSnapshot:
        snap = SnapshotCollector(self.client, self.store).collect(SnapshotRequirements(routes=list(routes or ROUTES)), CollectionContext(self.career_id, self.branch_id, lineage_confirmed=True))
        assert snap.valid, snap.consistency_reasons
        return snap

    def executor(self, owner: str = "exec-1", *, workflows: dict | None = None, **kw) -> SingleWriterExecutor:
        return SingleWriterExecutor(self.store, self.adapter, owner_id=owner, registry=self.registry, profile=self.profile, career_id=self.career_id, branch_id=self.branch_id, workflows=FAKE_WORKFLOWS if workflows is None else workflows, **kw)

    def tactic_intent(self, snap: DecisionSnapshot, *, decision_id: str = "dec-tactic-1", tactic_id: str = "counter-02", name: str | None = "4-2-3-1 Counter", previous: str | None = "balanced-01") -> ActionIntent:
        params: dict[str, Any] = {"tactic_catalog_id": tactic_id, "catalog_version": 1}
        if name is not None:
            params["catalog_name"] = name
        if previous is not None:
            params["previous_tactic_catalog_id"] = previous
        return self.factory.create("select_validated_tactic", "tactics.select", snap, {"routes": ["/tactics"]}, params, verification="selected_tactic_matches_catalog", decision_id=decision_id)

    def contract_intent(self, snap: DecisionSnapshot, *, decision_id: str = "dec-contract-1", offer_id: str = "offer-1") -> ActionIntent:
        params = {"offer_id": offer_id, "weekly_wage": WEEKLY_WAGE.to_json(), "contract_years": 2, "expected_commitments": OFFER_COMMITMENTS}
        return self.factory.create("commit.contract", "contracts.accept", snap, {"routes": ["/finances"], "player_ids": [2001]}, params, verification="contract_accepted_with_obligations", decision_id=decision_id)

    def training_intent(self, snap: DecisionSnapshot, *, decision_id: str = "dec-training-1", settings: dict[str, Any] | None = None) -> ActionIntent:
        return self.factory.create("set.training", "training.set", snap, {"routes": ["/squad"]}, {"settings": settings or {"intensity": "Double", "rest_percent": 20}, "previous_settings": {"intensity": "Normal"}}, verification="training_settings_reread", decision_id=decision_id)

    def ready(self, intent: ActionIntent, snap: DecisionSnapshot) -> ActionIntent:
        """PROPOSED -> VALIDATED -> QUEUED, asserting the intent is inside scope."""
        result = validate(intent, self.registry, self.profile, snap, self.store)
        assert result.state is ActionState.VALIDATED, result.to_json()
        return enqueue(self.store, intent)


def harness(adapter: FakeAdapter | None = None, *, profile: AuthorityProfile | None = None, provide: tuple[str, ...] = ("contract_cash_flows", "training_schedule", "training_program_settings"), adapter_capabilities: bool = True) -> Harness:
    """A registered career, a linked fake bridge and a fake UI.

    ``provide`` names capabilities supplied by a test-only operator (nothing
    in the fixture bridge decodes contract cash flows). ``adapter_capabilities``
    registers what the fake UI reports, as the connection step would.
    """
    store = Store.memory()
    career, branch, _ = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    adapter = adapter or FakeAdapter(offers=OFFERS)
    client = BridgeClient(FakeTransport(linked_world(adapter)), store, context={"career_id": career.career_id, "branch_id": branch.branch_id})
    registry = CapabilityRegistry.from_status(fx.status_payload(), supported_builds=(fx.BUILD,))
    for name in provide:
        registry.provide(name, "operator", "test-only provision")
    if adapter_capabilities:
        for name in adapter.capabilities():
            registry.provide(name, f"ui_adapter:{adapter.name}", "reported by the fake adapter")
    return Harness(store, career.career_id, branch.branch_id, adapter, client, registry, profile or scoped_profile(), IntentFactory(store))
