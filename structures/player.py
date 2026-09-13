from dataclasses import dataclass

@dataclass(frozen=True)
class Player:
    id: int
    name: str
    first_name: str
    surname: str
    attributes: dict[str,int]
