"""Tests for exact money units and calendar-aware recurrence (spec 8.1, FIN 01)."""
from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_DOWN
from fractions import Fraction

from ..state.units import (
    Money, Period, UnitError, count_occurrences, game_time_key, parse_game_time, payment_dates, quantize_major,
    sum_money, total_over,
)


class MoneyConstructionTests(unittest.TestCase):
    def test_minor_units_must_be_int(self):
        with self.assertRaises(UnitError):
            Money(1.5)
        with self.assertRaises(UnitError):
            Money(True)

    def test_unknown_currency_rejected(self):
        with self.assertRaises(UnitError):
            Money(100, "XXX")

    def test_period_string_is_coerced_to_enum(self):
        self.assertIs(Money(100, "GBP", "weekly").period, Period.WEEKLY)

    def test_of_major_amount_is_exact(self):
        self.assertEqual(Money.of("12.34").minor, 1234)
        self.assertEqual(Money.of(12).minor, 1200)
        self.assertEqual(Money.of(Decimal("0.01")).minor, 1)

    def test_of_rejects_floats_and_sub_minor_precision(self):
        with self.assertRaises(UnitError):
            Money.of(12.34)
        with self.assertRaises(UnitError):
            Money.of("12.345")

    def test_native_gbp_uses_whole_pound_basis(self):
        """Bridge integers are whole pounds: 3500 -> 350000 pence, weekly wage keeps its period."""
        wage = Money.native_gbp(3500, Period.WEEKLY)
        self.assertEqual(wage.minor, 350_000)
        self.assertEqual(wage.currency, "GBP")
        self.assertIs(wage.period, Period.WEEKLY)
        self.assertEqual(wage.major(), Decimal("3500"))
        with self.assertRaises(UnitError):
            Money.native_gbp(3500.0)
        with self.assertRaises(UnitError):
            Money.native_gbp(True)

    def test_json_round_trip(self):
        money = Money(-1250, "EUR", Period.MONTHLY)
        self.assertEqual(Money.from_json(money.to_json()), money)
        self.assertEqual(money.to_json(), {"minor": -1250, "currency": "EUR", "period": "monthly"})

    def test_str_shows_currency_and_period(self):
        self.assertEqual(str(Money.native_gbp(3500, Period.WEEKLY)), "GBP 3,500.00/week")
        self.assertEqual(str(Money.of("1.5")), "GBP 1.50")


class MoneyArithmeticTests(unittest.TestCase):
    def test_addition_and_subtraction_are_exact(self):
        a, b = Money(101), Money(202)
        self.assertEqual((a + b).minor, 303)
        self.assertEqual((b - a).minor, 101)
        self.assertEqual((-a).minor, -101)
        self.assertEqual(abs(Money(-5)).minor, 5)

    def test_mixed_currency_raises(self):
        """FIN 01: currency fixtures - GBP plus EUR is refused rather than summed."""
        with self.assertRaises(UnitError):
            Money(100, "GBP") + Money(100, "EUR")
        with self.assertRaises(UnitError):
            Money(100, "GBP") < Money(100, "EUR")

    def test_mixed_period_raises(self):
        """FIN 01: a weekly wage cannot be added to a monthly obligation without calendar expansion."""
        weekly = Money.native_gbp(3500, Period.WEEKLY)
        monthly = Money.native_gbp(10_000, Period.MONTHLY)
        with self.assertRaises(UnitError) as ctx:
            weekly + monthly
        self.assertIn("expand over a calendar window", str(ctx.exception))
        with self.assertRaises(UnitError):
            weekly - Money.native_gbp(1, Period.ONCE)

    def test_non_money_operand_raises(self):
        with self.assertRaises(UnitError):
            Money(100) + 100  # type: ignore[operator]

    def test_comparisons_share_units(self):
        self.assertTrue(Money(100) < Money(200))
        self.assertTrue(Money(200) >= Money(200))
        self.assertFalse(Money(100) > Money(200))

    def test_times_integral_factor_is_exact(self):
        self.assertEqual(Money(333).times(3).minor, 999)
        self.assertEqual(Money(1000).times(Fraction(1, 4)).minor, 250)

    def test_times_non_integral_requires_rounding_mode(self):
        """A result that is not a whole number of pence is refused unless rounding is explicit."""
        with self.assertRaises(UnitError):
            Money(1001).times(Fraction(1, 2))
        with self.assertRaises(UnitError):
            Money(100).times(0.5)  # type: ignore[arg-type]
        self.assertEqual(Money(1001).times(Fraction(1, 2), rounding=ROUND_HALF_EVEN).minor, 500)
        self.assertEqual(Money(1003).times(Fraction(1, 2), rounding=ROUND_HALF_EVEN).minor, 502)
        self.assertEqual(Money(1003).times(Fraction(1, 2), rounding=ROUND_DOWN).minor, 501)

    def test_with_period_relabels_without_converting(self):
        once = Money(500).with_period(Period.WEEKLY)
        self.assertEqual(once.minor, 500)
        self.assertIs(once.period, Period.WEEKLY)
        self.assertIs(once.as_once().period, Period.ONCE)

    def test_sum_money_empty_is_explicit_zero_and_checks_units(self):
        total = sum_money([], "GBP", Period.WEEKLY)
        self.assertEqual(total, Money.zero("GBP", Period.WEEKLY))
        self.assertEqual(sum_money([Money(1, period=Period.WEEKLY), Money(2, period=Period.WEEKLY)], period=Period.WEEKLY).minor, 3)
        with self.assertRaises(UnitError):
            sum_money([Money(1, period=Period.WEEKLY)])  # summing weekly into a once total


class PaymentDatesTests(unittest.TestCase):
    def test_once_is_a_single_payment_inside_window(self):
        first = dt.date(2024, 2, 20)
        self.assertEqual(payment_dates(Period.ONCE, first, dt.date(2024, 3, 1)), [first])
        self.assertEqual(payment_dates(Period.ONCE, first, dt.date(2024, 2, 19)), [])

    def test_weekly_dates_step_seven_days(self):
        dates = payment_dates(Period.WEEKLY, dt.date(2024, 2, 17), dt.date(2024, 3, 16))
        self.assertEqual(dates, [dt.date(2024, 2, 17), dt.date(2024, 2, 24), dt.date(2024, 3, 2), dt.date(2024, 3, 9), dt.date(2024, 3, 16)])

    def test_monthly_clamps_to_short_months_and_keeps_anchor_day(self):
        """A 31st-anchored monthly item pays on the 29th in leap-year February and returns to the 31st in March."""
        dates = payment_dates(Period.MONTHLY, dt.date(2024, 1, 31), dt.date(2024, 5, 31))
        self.assertEqual(dates, [dt.date(2024, 1, 31), dt.date(2024, 2, 29), dt.date(2024, 3, 31), dt.date(2024, 4, 30), dt.date(2024, 5, 31)])

    def test_monthly_non_leap_february_clamps_to_28(self):
        dates = payment_dates(Period.MONTHLY, dt.date(2023, 1, 30), dt.date(2023, 3, 30))
        self.assertEqual(dates, [dt.date(2023, 1, 30), dt.date(2023, 2, 28), dt.date(2023, 3, 30)])

    def test_monthly_crosses_year_boundary(self):
        dates = payment_dates(Period.MONTHLY, dt.date(2024, 11, 15), dt.date(2025, 2, 15))
        self.assertEqual(dates, [dt.date(2024, 11, 15), dt.date(2024, 12, 15), dt.date(2025, 1, 15), dt.date(2025, 2, 15)])

    def test_annual_leap_day_anchor_clamps(self):
        dates = payment_dates(Period.ANNUAL, dt.date(2024, 2, 29), dt.date(2026, 3, 1))
        self.assertEqual(dates, [dt.date(2024, 2, 29), dt.date(2025, 2, 28), dt.date(2026, 2, 28)])

    def test_last_ends_recurrence_before_window_end(self):
        """A contract end date stops payments even when the planning window runs longer."""
        dates = payment_dates(Period.WEEKLY, dt.date(2024, 2, 17), dt.date(2024, 4, 30), last=dt.date(2024, 3, 2))
        self.assertEqual(dates, [dt.date(2024, 2, 17), dt.date(2024, 2, 24), dt.date(2024, 3, 2)])
        self.assertEqual(count_occurrences(Period.WEEKLY, dt.date(2024, 2, 17), dt.date(2024, 4, 30), last=dt.date(2024, 3, 2)), 3)

    def test_first_after_window_is_empty(self):
        self.assertEqual(payment_dates(Period.WEEKLY, dt.date(2024, 5, 1), dt.date(2024, 4, 30)), [])


class TotalOverTests(unittest.TestCase):
    def test_weekly_wage_over_a_month_is_calendar_exact(self):
        """FIN 01: a weekly wage summed over March 2024 pays 5 times (5 Fridays), not 4 or 4.33.

        The result is a one-off amount so it can be added to other one-off totals.
        """
        wage = Money.native_gbp(3500, Period.WEEKLY)
        total = total_over(wage, dt.date(2024, 3, 1), dt.date(2024, 3, 31))
        self.assertEqual(total.minor, 5 * 350_000)
        self.assertIs(total.period, Period.ONCE)
        # February 2024 anchored on the 2nd pays on 2, 9, 16, 23: four payments.
        self.assertEqual(total_over(wage, dt.date(2024, 2, 2), dt.date(2024, 2, 29)).minor, 4 * 350_000)

    def test_weekly_and_monthly_only_combine_after_expansion(self):
        """FIN 01: weekly wage and monthly loan fee are expanded over the same window and then summed as one-off amounts."""
        wage = Money.native_gbp(3500, Period.WEEKLY)
        loan_fee = Money.native_gbp(10_000, Period.MONTHLY)
        with self.assertRaises(UnitError):
            wage + loan_fee
        window = (dt.date(2024, 3, 1), dt.date(2024, 5, 31))
        combined = total_over(wage, *window) + total_over(loan_fee, *window)
        # 14 Fridays (1 Mar .. 31 May 2024) + 3 monthly payments.
        self.assertEqual(combined.minor, 14 * 350_000 + 3 * 1_000_000)

    def test_double_counting_is_visible_through_counts(self):
        """FIN 01: an obligation with a contract end date inside the window is not charged past the end."""
        wage = Money.native_gbp(1000, Period.WEEKLY)
        full = total_over(wage, dt.date(2024, 3, 1), dt.date(2024, 5, 31))
        ended = total_over(wage, dt.date(2024, 3, 1), dt.date(2024, 5, 31), last=dt.date(2024, 3, 31))
        self.assertEqual(full.minor, 14 * 100_000)
        self.assertEqual(ended.minor, 5 * 100_000)
        self.assertLess(ended, full)

    def test_once_total_over_counts_at_most_one(self):
        fee = Money.native_gbp(250_000)
        self.assertEqual(total_over(fee, dt.date(2024, 3, 1), dt.date(2024, 12, 31)).minor, fee.minor)
        self.assertEqual(total_over(fee, dt.date(2025, 3, 1), dt.date(2024, 12, 31)).minor, 0)


class GameTimeTests(unittest.TestCase):
    def test_game_time_key_orders_dates_then_minutes(self):
        self.assertLess(game_time_key("2024-02-17", "10:00"), game_time_key("2024-02-17", "15:00"))
        self.assertLess(game_time_key("2024-02-17", "23:59"), game_time_key("2024-02-18", "00:00"))

    def test_absent_time_sorts_before_any_time_on_the_same_day(self):
        self.assertEqual(parse_game_time("2024-02-17", None), (dt.date(2024, 2, 17), -1))
        self.assertLess(game_time_key("2024-02-17", None), game_time_key("2024-02-17", "00:00"))

    def test_quantize_major_to_pence(self):
        self.assertEqual(quantize_major(Decimal("12.345")), Decimal("12.34"))
        self.assertEqual(quantize_major(Decimal("12.355")), Decimal("12.36"))


if __name__ == "__main__":
    unittest.main()
