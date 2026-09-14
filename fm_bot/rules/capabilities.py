"""Capability registry and dependency-based action blocking (BOT 005).

Capabilities come from three places: the bridge's ``/status`` report
(``capabilities`` supported, ``unresolved`` unsupported), verified UI adapter
observations, and bot-side proposed capabilities that no component supplies
yet. Gates apply to the specific action or model requiring a capability; one
missing subsystem never disables unrelated supported work.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from ..state.status import MissingCapabilityReport


class CapabilityStatus(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"    # explicitly unresolved by a provider
    MISSING = "missing"            # nobody provides it
    DEGRADED = "degraded"          # provided but currently unavailable (e.g. disconnected)


@dataclass
class Capability:
    name: str
    status: CapabilityStatus
    provider: str                  # "bridge", "ui_adapter", "operator", "none"
    note: str | None = None


# Proposed capabilities from the observation extension backlog (spec 3.2).
# These are names the bot needs; a name here is not an existing bridge endpoint.
PROPOSED_CAPABILITIES: dict[str, str] = {
    "career_registration": "explicit career registration, branch tracking, load detection and save manifest",
    "eligibility_injury": "injury status per player",
    "eligibility_suspension": "suspension status per player",
    "eligibility_loan_absence": "loaned-out absence per player",
    "eligibility_registration": "competition registration per player",
    "competition_rules": "squad, substitution, tie and registration rules per competition and stage",
    "competition_standings": "standings and objectives",
    "pending_actions": "complete pending actions, deadlines and mandatory decisions",
    "inbox_text": "inbox message text and visible options",
    "ui_action_adapter": "installed and validated UI action adapter",
    "save_restore": "reliable save/load workflow for laboratory branches",
    "contract_cash_flows": "complete contract cash flows, clauses, bonuses, fees and obligations",
    "tactic_catalog_readback": "readback of full tactical settings for a catalog entry",
    "tactic_selection_ui": "validated tactic selection workflow",
    "match_event_order": "verified fixture identity, event order, active players, red cards and substitution rules",
    "training_outcomes": "training outcomes, workload and visible medical reports",
    "staff_ratings": "staff ratings, coaching workload, facilities and board request state",
    "promises": "promises, playing-time expectations, relationships and leadership",
    "language_model": "configured language model for text-heavy decisions",
}

# Action kinds -> capabilities they require. Planners consult this before proposing.
ACTION_REQUIREMENTS: dict[str, list[str]] = {
    "observe": [],
    "advise.lineup": ["selected_tactic", "team_roster", "player_attributes_47", "positions"],
    "submit.lineup": ["ui_action_adapter", "selected_tactic", "team_roster", "eligibility_injury", "eligibility_suspension", "eligibility_loan_absence", "eligibility_registration", "competition_rules"],
    "advise.minutes": ["fixtures", "team_roster"],
    "advise.finance": ["club_finances", "transfer_budget", "wage_budget"],
    "commit.contract": ["ui_action_adapter", "contract_cash_flows", "club_finances", "transfer_budget", "wage_budget"],
    "commit.transfer_offer": ["ui_action_adapter", "contract_cash_flows", "club_finances", "transfer_budget", "wage_budget"],
    "select_validated_tactic": ["ui_action_adapter", "tactic_catalog_readback", "tactic_selection_ui", "selected_tactic"],
    "set.training": ["ui_action_adapter", "training_schedule", "training_program_settings"],
    "progress.continue": ["ui_action_adapter", "pending_actions", "inbox_metadata"],
    "respond.inbox": ["ui_action_adapter", "inbox_text", "pending_actions"],
    "match.substitute": ["ui_action_adapter", "match_viewer", "match_event_order", "competition_rules"],
    "lab.restore_checkpoint": ["save_restore", "career_registration"],
    "scouting.assign": ["ui_action_adapter", "scout_report_metadata"],
}


@dataclass
class CapabilityRegistry:
    capabilities: dict[str, Capability] = field(default_factory=dict)
    build: str | None = None
    connected: bool = False

    @classmethod
    def from_status(cls, status_payload: dict[str, Any] | None, *, supported_builds: Iterable[str]) -> "CapabilityRegistry":
        registry = cls()
        for name, note in PROPOSED_CAPABILITIES.items():
            registry.capabilities[name] = Capability(name, CapabilityStatus.MISSING, "none", note)
        if not status_payload:
            return registry
        registry.connected = bool(status_payload.get("connected"))
        registry.build = status_payload.get("build")
        build_ok = registry.build in set(supported_builds)
        for name in status_payload.get("capabilities", []) or []:
            status = CapabilityStatus.SUPPORTED if registry.connected and build_ok else CapabilityStatus.DEGRADED
            registry.capabilities[name] = Capability(name, status, "bridge", None if status is CapabilityStatus.SUPPORTED else "bridge disconnected or build unsupported")
        for name in status_payload.get("unresolved", []) or []:
            registry.capabilities[name] = Capability(name, CapabilityStatus.UNSUPPORTED, "bridge", "reported unresolved by the bridge")
        return registry

    def provide(self, name: str, provider: str, note: str | None = None) -> None:
        """A verified adapter or operator supplies a capability."""
        self.capabilities[name] = Capability(name, CapabilityStatus.SUPPORTED, provider, note)

    def withdraw(self, name: str, reason: str) -> None:
        current = self.capabilities.get(name)
        provider = current.provider if current else "none"
        self.capabilities[name] = Capability(name, CapabilityStatus.DEGRADED, provider, reason)

    def status(self, name: str) -> CapabilityStatus:
        item = self.capabilities.get(name)
        return item.status if item else CapabilityStatus.MISSING

    def supported(self, name: str) -> bool:
        return self.status(name) is CapabilityStatus.SUPPORTED

    def requirements(self, action_kind: str) -> list[str]:
        if action_kind in ACTION_REQUIREMENTS:
            return list(ACTION_REQUIREMENTS[action_kind])
        family = action_kind.split(".")[0]
        return list(ACTION_REQUIREMENTS.get(family, []))

    def check(self, action_kind: str, extra: Iterable[str] = ()) -> MissingCapabilityReport:
        report = MissingCapabilityReport(action_kind)
        for name in [*self.requirements(action_kind), *extra]:
            item = self.capabilities.get(name)
            if item is None:
                report.add(name, "no provider registered")
            elif item.status is not CapabilityStatus.SUPPORTED:
                report.add(name, f"{item.status.value} ({item.provider}): {item.note or 'no detail'}")
        return report

    def summary(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {status.value: [] for status in CapabilityStatus}
        for name, item in sorted(self.capabilities.items()):
            result[item.status.value].append(name)
        return result
