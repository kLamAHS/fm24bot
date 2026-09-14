from dataclasses import dataclass
from .player import Player

@dataclass(frozen=True)
class Club:
    id: int
    name: str
    squad: list[Player]
