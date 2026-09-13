from dataclasses import dataclass

@dataclass(frozen=True)
class Player:
    id: int
    name: str
    first_name: str
    surname: str
    attributes: dict[str,int]
    positions: list[str]
    position_ratings: dict[str,int]
    condition: float
    match_sharpness: float
    date_of_birth: str
    age: int
    age_as_of: str
    morale: str
    morale_rating: int
