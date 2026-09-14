from dataclasses import dataclass
from .nation import Nation
from .contract import Contract


@dataclass(frozen=True)
class Staff:
    id: int
    name: str
    primary_nationality: Nation
    team_id: int
    current_team: bool
    departments: list[str]
    job: str | None
    job_code: int
    employment: Contract | None
