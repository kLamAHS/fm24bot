from dataclasses import dataclass
from .fixture import FixtureTeam

@dataclass(frozen=True)
class MatchPlayer:
    id: int | None
    name: str | None
    side: str
    shirt_number: int
    started: bool
    appeared: bool
    substituted_in: bool
    substituted_out: bool
    rating: float | None
    rating_may_be_provisional: bool
    goals: int
    shots: int
    shots_on_target: int
    yellow_cards: int
    condition: float | None = None
    red_cards: int | None = None
    injury: str | None = None
    identity_status: str = 'resolved'
    starting_position: str | None = None
    last_position: str | None = None
    condition_basis: str = 'retained_match_statistics'
    condition_may_lag: bool = True

@dataclass(frozen=True)
class FormationSlot:
    player_id: int | None
    player_name: str | None
    shirt_number: int
    position: str | None

@dataclass(frozen=True)
class MatchFormation:
    team_id: int
    slots: list[FormationSlot]
    basis: str = 'starting_lineup'
    name: str | None = None

@dataclass(frozen=True)
class TeamMatchStats:
    shots: int
    shots_on_target: int
    xg: float
    corners: int
    fouls: int
    yellow_cards: int
    passes_attempted: int
    passes_completed: int
    pass_completion: int | None
    possession: int | None
    possession_basis: str = 'inferred_from_completed_pass_share'

@dataclass(frozen=True)
class Match:
    fixture_date: str
    kickoff_time: str
    competition_id: int
    competition_name: str
    home: FixtureTeam
    away: FixtureTeam
    home_score: int
    away_score: int
    minute: int
    second: int
    phase: str
    home_stats: TeamMatchStats
    away_stats: TeamMatchStats
    source: str = 'match_viewer'
    timeline: str = 'unclassified'
    # Field-specific basis/status values distinguish validated data from unknowns.
    players: list[MatchPlayer] | None = None
    opposition_formation: MatchFormation | None = None
    clock_basis: str = 'retained_match_statistics'

@dataclass(frozen=True)
class MatchObservation:
    available: bool
    reason: str | None
    match: Match | None
