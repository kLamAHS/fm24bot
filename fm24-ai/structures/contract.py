from dataclasses import dataclass

@dataclass(frozen=True)
class Contract:
    kind: str
    club_id: int
    club_name: str
    team_id: int
    start_date: str
    end_date: str
    weekly_wage_gbp: int
    wage_basis: str
