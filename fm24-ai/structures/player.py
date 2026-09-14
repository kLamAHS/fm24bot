from dataclasses import dataclass
from .nation import Nation
from .contract import Contract

@dataclass(frozen=True)
class Player:
    id: int
    name: str
    first_name: str
    surname: str
    attributes: dict[str,int]
    positions: list[str]
    position_ratings: dict[str,int]
    condition: float | None
    match_sharpness: float | None
    date_of_birth: str
    age: int
    age_as_of: str
    morale: str
    morale_rating: int
    readiness: dict
    primary_nationality: Nation
    contracts: list[Contract]
