"""Information modes and visibility masks (spec 1.2 and VIS 01).

``bridge_observed`` may use validated bridge fields including actual player
attributes; ``manager_visible`` may only use fields demonstrated visible to the
manager at that time. The mask is applied *before* feature generation,
retrieval, training and planning. Unknown visibility means unavailable in
manager-visible mode. Nulling the final display is insufficient and is not
what this module does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .records import Visibility
from .status import Observed, ValueStatus


class InformationMode(str, Enum):
    BRIDGE_OBSERVED = "bridge_observed"
    MANAGER_VISIBLE = "manager_visible"


# Default visibility of bridge fields by (entity, field). Attributes are
# actual memory values with no scouting mask applied, so they are privileged.
# Fields not listed are UNKNOWN and therefore unavailable in manager-visible mode.
DEFAULT_MASK: dict[tuple[str, str], Visibility] = {
    ("player", "id"): Visibility.VISIBLE,
    ("player", "name"): Visibility.VISIBLE,
    ("player", "date_of_birth"): Visibility.VISIBLE,
    ("player", "age"): Visibility.VISIBLE,
    ("player", "positions"): Visibility.VISIBLE,
    ("player", "position_ratings"): Visibility.PRIVILEGED,   # exact familiarity numbers are not the UI's display
    ("player", "attributes"): Visibility.PRIVILEGED,
    ("player", "morale"): Visibility.VISIBLE,               # own-club morale is shown in the squad view
    ("player", "morale_rating"): Visibility.PRIVILEGED,
    ("player", "condition"): Visibility.VISIBLE,
    ("player", "match_sharpness"): Visibility.VISIBLE,
    ("player", "contracts"): Visibility.VISIBLE,             # own-club contract terms are visible; other clubs' are not
    ("player", "primary_nationality"): Visibility.VISIBLE,
    ("finances", "balance"): Visibility.VISIBLE,
    ("finances", "transfer_budget"): Visibility.VISIBLE,
    ("finances", "wage_budget_weekly"): Visibility.VISIBLE,
    ("finances", "payroll_spending_weekly"): Visibility.VISIBLE,
    ("fixture", "*"): Visibility.VISIBLE,
    ("tactics", "*"): Visibility.VISIBLE,
    ("training", "*"): Visibility.VISIBLE,
    ("inbox", "*"): Visibility.VISIBLE,
    ("scouting", "*"): Visibility.VISIBLE,
    ("shortlists", "*"): Visibility.VISIBLE,
    ("transfer_targets", "*"): Visibility.VISIBLE,
    ("staff", "*"): Visibility.VISIBLE,
    ("match", "*"): Visibility.VISIBLE,
    ("game", "*"): Visibility.VISIBLE,
    ("manager", "*"): Visibility.VISIBLE,
    ("club", "*"): Visibility.VISIBLE,
}


@dataclass
class VisibilityMask:
    entries: dict[tuple[str, str], Visibility] = field(default_factory=lambda: dict(DEFAULT_MASK))
    version: int = 1

    def visibility(self, entity: str, field_name: str) -> Visibility:
        if (entity, field_name) in self.entries:
            return self.entries[(entity, field_name)]
        if (entity, "*") in self.entries:
            return self.entries[(entity, "*")]
        return Visibility.UNKNOWN

    def declare(self, entity: str, field_name: str, visibility: Visibility) -> None:
        self.entries[(entity, field_name)] = visibility
        self.version += 1


@dataclass
class MaskedField:
    entity: str
    field: str
    reason: str


@dataclass
class MaskedRecord:
    """A record after mode filtering, with lineage of what was removed."""

    entity: str
    mode: InformationMode
    fields: dict[str, Any]
    masked: list[MaskedField] = field(default_factory=list)
    mask_version: int = 1

    def get(self, name: str) -> Observed:
        if name in self.fields:
            return Observed.available_value(self.fields[name], source=f"{self.entity}.{name}", what=f"{self.entity}.{name}")
        for item in self.masked:
            if item.field == name:
                return Observed.unavailable(ValueStatus.UNSUPPORTED, f"{self.entity}.{name}", item.reason, source=f"mask:{self.mode.value}")
        return Observed.unavailable(ValueStatus.MISSING, f"{self.entity}.{name}", "field not present in the record")


def apply_mode(entity: str, record: dict[str, Any], mode: InformationMode, mask: VisibilityMask | None = None, *, other_club: bool = False) -> MaskedRecord:
    """Filter ``record`` for ``mode``. Privileged and unknown fields are removed in manager-visible mode.

    ``other_club`` marks players of other clubs, whose contract terms and
    morale are not shown to the manager without a report; those fields are
    masked as unknown in manager-visible mode.
    """
    mask = mask or VisibilityMask()
    if mode is InformationMode.BRIDGE_OBSERVED:
        return MaskedRecord(entity, mode, dict(record), [], mask.version)
    kept: dict[str, Any] = {}
    masked: list[MaskedField] = []
    for name, value in record.items():
        visibility = mask.visibility(entity, name)
        if other_club and entity == "player" and name in ("contracts", "morale", "condition", "match_sharpness"):
            visibility = Visibility.UNKNOWN
        if visibility is Visibility.VISIBLE:
            kept[name] = value
        elif visibility is Visibility.PRIVILEGED:
            masked.append(MaskedField(entity, name, "privileged field removed in manager-visible mode"))
        else:
            masked.append(MaskedField(entity, name, "visibility unknown; unavailable in manager-visible mode"))
    return MaskedRecord(entity, mode, kept, masked, mask.version)


class FeatureLineageError(ValueError):
    """A feature set declared for one information mode contains fields from another."""


def assert_features_allowed(features: dict[str, Any], mode: InformationMode, mask: VisibilityMask | None = None) -> None:
    """Guard for feature generation and training: ``features`` keys are ``entity.field`` names."""
    if mode is InformationMode.BRIDGE_OBSERVED:
        return
    mask = mask or VisibilityMask()
    offending = []
    for key in features:
        entity, _, field_name = key.partition(".")
        if not field_name:
            offending.append(key)
            continue
        if mask.visibility(entity, field_name) is not Visibility.VISIBLE:
            offending.append(key)
    if offending:
        raise FeatureLineageError(f"features not allowed in {mode.value} mode: {sorted(offending)}")


def mode_of_model(model_information_mode: str, requested: InformationMode) -> bool:
    """A model trained with privileged information cannot be relabeled manager-visible."""
    if requested is InformationMode.MANAGER_VISIBLE:
        return model_information_mode == InformationMode.MANAGER_VISIBLE.value
    return True
