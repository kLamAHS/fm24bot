from dataclasses import dataclass

@dataclass(frozen=True)
class Manager:
    id: int
    name: str
