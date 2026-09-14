from dataclasses import dataclass

@dataclass(frozen=True)
class Finances:
    club_id: int
    currency: str
    balance: int
    transfer_budget: int
    wage_budget_weekly: int
    payroll_spending_weekly: int
    as_of: str
