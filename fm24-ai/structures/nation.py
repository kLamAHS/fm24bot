from dataclasses import dataclass

@dataclass(frozen=True)
class Nation:
    id: int
    name: str
