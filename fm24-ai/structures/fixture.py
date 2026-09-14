from dataclasses import dataclass

@dataclass(frozen=True)
class FixtureTeam:
    team_id: int
    club_id: int
    club_name: str

@dataclass(frozen=True)
class Fixture:
    date: str
    time: str
    competition_id: int
    competition_name: str
    home: FixtureTeam
    away: FixtureTeam
    status: str
    home_score: int | None
    away_score: int | None

@dataclass(frozen=True)
class Fixtures:
    club_id: int
    team_id: int
    calendar_year: int
    as_of: str
    fixtures: list[Fixture]
