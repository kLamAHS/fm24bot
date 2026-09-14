from dataclasses import dataclass, field


@dataclass(frozen=True)
class TacticPosition:
    slot: int
    position: str | None
    position_code: int
    player_id: int | None
    player_name: str | None
    role: str | None
    duty: str | None
    instructions_source: str


@dataclass(frozen=True)
class TacticSubstitute:
    slot: int
    player_id: int | None
    player_name: str | None


@dataclass(frozen=True)
class Tactics:
    available: bool
    reason: str | None = None
    selected_slot: int | None = None
    stored_name: str | None = None
    style: str | None = None
    mentality: str | None = None
    positions: list[TacticPosition] = field(default_factory=list)
    substitutes: list[TacticSubstitute] = field(default_factory=list)
