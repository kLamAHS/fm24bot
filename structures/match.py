from dataclasses import dataclass
from .fixture import FixtureTeam

@dataclass(frozen=True)
class MatchPlayer:
    id: int
    name: str
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
    # Null is explicit until these fields pass their own UI validation.
    players: list[MatchPlayer] | None = None
    opposition_formation: str | None = None

@dataclass(frozen=True)
class MatchObservation:
    available: bool
    reason: str | None
    match: Match | None
