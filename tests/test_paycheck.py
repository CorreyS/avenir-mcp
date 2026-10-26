# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Tests for paycheck.py: the bills before the next paycheck, and how low the balance gets."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest

from avenir_mcp import paycheck

from .factories import scheduled

TODAY = date(2026, 9, 25)


def _tx(  # pylint: disable=too-many-arguments
    payee: str,
    day: str,
    amount: int,
    account: str = "a-chk",
    *,
    transfer: str | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "payee_name": payee,
        "date": day,
        "amount": amount,
        "account_id": account,
        "category_id": None,
        "transfer_account_id": transfer,
        "deleted": deleted,
    }


def _account(
    key: str, kind: str, balance: float, *, on_budget: bool = True, closed: bool = False
) -> dict[str, Any]:
    """An account as client.get_accounts gives it."""
    return {"id": key, "name": key.removeprefix("a-").title(), "type": kind, "balance": balance} | {
        "on_budget": on_budget,
        "closed": closed,
    }


ACCOUNTS = [
    _account("a-chk", "checking", 1_200.0),
    _account("a-wallet", "cash", 60.0),
    _account("a-savings", "savings", 4_000.0),
    _account("a-card", "creditCard", -300.0),
    _account("a-old", "checking", 0.0, closed=True),
    _account("a-brokerage", "otherAsset", 9_000.0, on_budget=False),
]

# Every other Thursday, the last on 17 September; one paycheck a little larger.
PAYDAYS = [date(2026, 9, 17) - timedelta(days=14 * n) for n in range(10)]
SALARY = [
    _tx("VIR ACME PAYROLL", day.isoformat(), 1_900_000 if n == 1 else 1_850_000)
    for n, day in enumerate(PAYDAYS)
]


def _monthly(payee: str, day: int, amount: int, months: tuple[int, ...] = (6, 7, 8, 9)) -> Any:
    return [_tx(payee, f"2026-{m:02d}-{day:02d}", amount) for m in months]


HISTORY = (
    SALARY
    + _monthly("LANDLORD", 1, -950_000)
    + _monthly("POWERCO", 26, -64_200, (5, 6, 7, 8))
    + _monthly("GYM CLUB", 27, -40_000, (5, 6, 7, 8))
    + _monthly("STREAMFLIX", 15, -13_490)
    # Usually on the 28th; this month it was taken early, on the 22nd.
    + _monthly("MOBILE CO", 28, -19_990, (5, 6, 7, 8))
    + [_tx("MOBILE CO", "2026-09-22", -19_990)]
    + [_tx("CLOUDBOX ANNUAL", "2024-09-30", -59_880), _tx("CLOUDBOX ANNUAL", "2025-09-30", -59_880)]
    # Groceries at a different shop each month: spending that does not recur.
    + [
        _tx("MARKET ONE", "2026-06-12", -300_000),
        _tx("MARKET TWO", "2026-07-12", -300_000),
        _tx("MARKET THREE", "2026-08-12", -300_000),
    ]
    # Not this household's checking: the savings account, and a deleted payment.
    + [_tx("SAVINGS INTEREST", f"2026-0{m}-30", 5_000, "a-savings") for m in (5, 6, 7, 8)]
    + [_tx("GHOST", "2026-09-27", -999_000, deleted=True)]
)
SCHEDULES = [
    scheduled("Landlord", "2026-01-01", "2026-10-01", "monthly", account_id="a-chk"),
    scheduled(
        "Fibernet", "2026-01-28", "2026-09-28", "monthly", amount=-29_990, account_id="a-chk"
    ),
    scheduled(
        "Homesafe", "2025-10-20", "2026-10-20", "yearly", amount=-420_000, account_id="a-chk"
    ),
    # The paycheck's own schedule: the paycheck is counted once, at the end of its day.
    scheduled(
        "Acme Payroll", "2026-01-08", "2026-10-01", "everyOtherWeek", amount=1_850_000,
        account_id="a-chk",
    ),
    scheduled(
        "To savings", "2026-01-30", "2026-09-30", "monthly", amount=-200_000, account_id="a-chk",
        transfer_account_id="a-savings",
    ),
    scheduled(
        "To wallet", "2026-01-29", "2026-09-29", "monthly", amount=-50_000, account_id="a-chk",
        transfer_account_id="a-wallet",
    ),
    scheduled("Broker fee", "2026-01-27", "2026-09-27", "monthly", account_id="a-savings"),
    scheduled("Gone", "2026-01-27", "2026-09-27", "monthly", account_id="a-chk", deleted=True),
]  # fmt: skip


def _plan(**options: Any) -> paycheck.PaycheckPlan:
    return paycheck.summary(
        options.pop("accounts", ACCOUNTS),
        options.pop("history", HISTORY),
        options.pop("schedules", SCHEDULES),
        TODAY,
        **options,
    )


# ---------------------------------------------------------------------------
# Income found in the history
# ---------------------------------------------------------------------------


def _income(transactions: list[dict[str, Any]]) -> list[paycheck.IncomeSource]:
    return paycheck.find_income(transactions, TODAY)


def test_a_biweekly_paycheck_is_found_with_its_next_two_dates() -> None:
    """Every other Thursday: the next on 1 October, then 15 October, at the usual amount."""
    [salary] = _income(SALARY)
    assert (salary.payee, salary.cadence, salary.times_seen) == ("ACME PAYROLL", "biweekly", 10)
    assert (salary.amount, salary.last_paid) == (1_850.0, "2026-09-17")
    assert (salary.next_pay_date, salary.following_pay_date) == ("2026-10-01", "2026-10-15")


def test_a_weekly_paycheck_is_found() -> None:
    """Every Monday, the last on 21 September: next on the 28th, then 5 October."""
    mondays = [date(2026, 9, 21) - timedelta(days=7 * n) for n in range(6)]
    [wage] = _income([_tx("CAFE WAGES", d.isoformat(), 400_000) for d in mondays])
    assert (wage.cadence, wage.next_pay_date, wage.following_pay_date) == (
        "weekly",
        "2026-09-28",
        "2026-10-05",
    )


def test_a_paycheck_twice_a_month_keeps_its_two_days() -> None:
    """The 15th and the month's last day, one moved to a Friday: next 30 September."""
    days = [
        "2026-05-15", "2026-05-29", "2026-06-15", "2026-06-30", "2026-07-15",
        "2026-07-31", "2026-08-14", "2026-08-31", "2026-09-15",
    ]  # fmt: skip
    [pay] = _income([_tx("CITY PAYROLL", d, 1_600_000) for d in days])
    assert (pay.cadence, pay.next_pay_date, pay.following_pay_date) == (
        "semimonthly",
        "2026-09-30",
        "2026-10-15",
    )


def test_a_monthly_salary_falls_on_its_usual_day() -> None:
    """The 28th: next on 28 September, then 28 October."""
    [pay] = _income([_tx("ACME SALAIRE", f"2026-0{m}-28", 3_200_000) for m in (5, 6, 7, 8)])
    assert (pay.cadence, pay.next_pay_date, pay.following_pay_date) == (
        "monthly",
        "2026-09-28",
        "2026-10-28",
    )


def test_a_payday_gone_by_without_a_paycheck_moves_on_to_the_next() -> None:
    """Paid on the 20th until August; September's has not come: the next is 20 October."""
    [pay] = _income([_tx("ACME SALAIRE", f"2026-0{m}-20", 3_200_000) for m in (5, 6, 7, 8)])
    assert (pay.last_paid, pay.next_pay_date) == ("2026-08-20", "2026-10-20")


def test_the_largest_income_over_a_month_comes_first() -> None:
    """A biweekly salary brings more each month than a monthly allowance of 1,000."""
    allowance = [_tx("FAMILY ALLOWANCE", f"2026-0{m}-05", 1_000_000) for m in (6, 7, 8, 9)]
    assert [s.payee for s in _income(allowance + SALARY)] == ["ACME PAYROLL", "FAMILY ALLOWANCE"]


@pytest.mark.parametrize(
    "days",
    [
        ["2026-07-03", "2026-08-21"],  # twice only
        ["2026-06-03", "2026-07-21", "2026-09-02"],  # 48 and 43 days: no rhythm
        ["2026-06-01", "2026-06-15", "2026-06-20", "2026-07-03"],  # three in a month
        ["2026-03-02", "2026-03-30", "2026-04-27"],  # before the four months looked at
        ["2026-06-01", "2026-06-12", "2026-06-24", "2026-07-06"],  # half-monthly, three in June
    ],
)
def test_inflows_without_a_rhythm_are_not_income(days: list[str]) -> None:
    """Refunds and one-offs are not a paycheck."""
    assert not _income([_tx("REFUND", d, 120_000) for d in days])


def test_transfers_deleted_and_future_inflows_are_not_income() -> None:
    """Money moved from savings, a deleted inflow and one dated after today."""
    days = ["2026-08-07", "2026-08-21", "2026-09-04", "2026-09-18"]
    moved = [_tx("SAVINGS", d, 500_000, transfer="a-savings") for d in days]
    gone = [_tx("ACME", d, 500_000, deleted=True) for d in days]
    later = [_tx("LATER", d, 500_000) for d in ("2026-10-02", "2026-10-16", "2026-10-30")]
    assert not _income(moved + gone + later + [_tx("", d, 500_000) for d in days])


def test_two_inflows_on_one_day_are_one_paycheck() -> None:
    """A paycheck paid in two parts: one payday, the parts added up."""
    split = SALARY + [_tx("VIR ACME PAYROLL", "2026-05-14", 150_000)]
    [salary] = _income(split)
    assert (salary.times_seen, salary.amount) == (10, 1_850.0)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_bills_before_the_next_paycheck_and_the_lowest_balance() -> None:
    """Checking and cash, 1,260.00: the rent on payday takes the balance below zero."""
    answer = _plan()
    assert (answer.accounts, answer.start_balance) == (["Chk", "Wallet"], 1_260.0)
    assert answer.income is not None and answer.income.payee == "ACME PAYROLL"
    [period] = answer.periods
    assert (period.start, period.pay_date, period.pay_amount) == (
        "2026-09-26",
        "2026-10-01",
        1_850.0,
    )
    assert [(e.date, e.payee, e.amount, e.source) for e in period.expected] == [
        ("2026-09-26", "POWERCO", -64.2, "recurring"),
        ("2026-09-27", "GYM CLUB", -40.0, "recurring"),
        ("2026-09-28", "Fibernet", -29.99, "scheduled"),
        ("2026-09-30", "CLOUDBOX ANNUAL", -59.88, "detected"),
        ("2026-09-30", "To savings", -200.0, "scheduled"),
        ("2026-10-01", "Landlord", -950.0, "scheduled"),
    ]
    assert (period.bills_total, period.variable_spending) == (-1_344.07, 0.0)
    assert (period.lowest_balance, period.lowest_on, period.balance_before_pay) == (
        -84.07,
        "2026-10-01",
        -84.07,
    )
    assert (answer.lowest_balance, answer.lowest_on) == (-84.07, "2026-10-01")
    assert answer.daily_variable_spending is None
    assert answer.message == (
        "Next paycheck on 2026-10-01, 1850.00 from ACME PAYROLL. Before it, 6 bills take "
        "1344.07, and the balance is lowest at -84.07 on 2026-10-01. The balance goes below "
        "zero."
    )


def test_the_period_after_starts_with_the_paycheck() -> None:
    """Until 15 October: 1,850.00 in, streaming out on the 15th."""
    answer = _plan(include_next_period=True)
    first, second = answer.periods
    assert (second.start, second.pay_date) == ("2026-10-02", "2026-10-15")
    assert [(e.payee, e.amount) for e in second.expected] == [("STREAMFLIX", -13.49)]
    assert (second.lowest_balance, second.lowest_on) == (1_752.44, "2026-10-15")
    assert first.lowest_balance == answer.lowest_balance
    assert "Until the one after, on 2026-10-15, it is lowest at 1752.44 on 2026-10-15." in (
        answer.message
    )


def test_variable_spending_is_spread_only_when_asked_for() -> None:
    """300.00 a month of shopping elsewhere: 9.86 a day, 59.16 over the six days."""
    answer = _plan(include_variable_spending=True)
    assert answer.daily_variable_spending == -9.86
    [period] = answer.periods
    assert (period.variable_spending, period.lowest_balance) == (-59.16, -143.23)
    assert any("9.86 a day" in note for note in answer.notes)
    assert any("not included" in note for note in _plan().notes)


def test_a_balance_that_stays_above_zero_says_nothing_of_zero() -> None:
    """Richer by 1,000.00: the lowest point is 915.93."""
    accounts = [dict(a, balance=2_200.0) if a["id"] == "a-chk" else a for a in ACCOUNTS]
    answer = _plan(accounts=accounts)
    assert answer.lowest_balance == 915.93
    assert "below zero" not in answer.message


def test_nothing_due_keeps_the_lowest_point_today() -> None:
    """No bill, no history but the salary: the balance only stays where it is."""
    answer = _plan(history=SALARY, schedules=[])
    assert (answer.lowest_balance, answer.lowest_on) == (1_260.0, "2026-09-25")
    assert answer.message.startswith(
        "Next paycheck on 2026-10-01, 1850.00 from ACME PAYROLL. Before it, 0 bills take 0.00"
    )


def test_chosen_accounts_replace_the_default() -> None:
    """Savings alone: its own schedule counts, the checking's do not; its interest is income."""
    answer = _plan(account_ids=["a-savings"], next_pay_date=date(2026, 10, 1))
    assert (answer.accounts, answer.start_balance) == (["Savings"], 4_000.0)
    assert [e.payee for e in answer.periods[0].expected] == ["Broker fee"]
    assert answer.income is not None and answer.income.payee == "SAVINGS INTEREST"


def test_a_yearly_charge_with_a_schedule_is_projected_by_the_schedule_alone() -> None:
    """The cloud storage, scheduled in YNAB: once, as scheduled, not also as detected."""
    cloud = scheduled(
        "Cloudbox", "2025-09-30", "2026-09-30", "yearly", amount=-59_880, account_id="a-chk"
    )
    answer = _plan(schedules=[*SCHEDULES, cloud])
    found = [(e.payee, e.source) for e in answer.periods[0].expected if e.date == "2026-09-30"]
    assert found == [("Cloudbox", "scheduled"), ("To savings", "scheduled")]


def test_one_bill_is_named_in_the_singular() -> None:
    """One bill takes, not one bills."""
    answer = _plan(history=SALARY, schedules=SCHEDULES[:1])
    assert "Before it, 1 bill takes 950.00" in answer.message


# ---------------------------------------------------------------------------
# What the caller says
# ---------------------------------------------------------------------------


def test_a_given_pay_date_and_amount_replace_the_history() -> None:
    """Paid on 29 September instead, 2,000.00, monthly from then on."""
    answer = _plan(
        next_pay_date=date(2026, 9, 29),
        pay_amount=2_000,
        cadence="monthly",
        include_next_period=True,
    )
    first, second = answer.periods
    assert (first.pay_date, first.pay_amount) == ("2026-09-29", 2_000.0)
    assert second.pay_date == "2026-10-29"
    assert answer.message.startswith("Next paycheck on 2026-09-29, 2000.00 from ACME PAYROLL.")


@pytest.mark.parametrize(
    ("cadence", "following"),
    [
        ("weekly", "2026-10-06"),
        ("biweekly", "2026-10-13"),
        ("semimonthly", "2026-10-14"),
        ("monthly", "2026-10-29"),
    ],
)
def test_a_cadence_given_sets_the_paycheck_after(cadence: str, following: str) -> None:
    """From a pay date of 29 September."""
    answer = _plan(next_pay_date=date(2026, 9, 29), cadence=cadence, include_next_period=True)
    assert answer.periods[1].pay_date == following


def test_a_cadence_given_alone_keeps_the_next_date_found() -> None:
    """The next paycheck stays on 1 October; the one after comes a month later."""
    answer = _plan(cadence="monthly", include_next_period=True)
    assert [p.pay_date for p in answer.periods] == ["2026-10-01", "2026-11-01"]


def test_the_income_payee_picks_one_of_several() -> None:
    """Named by any of its words, whatever the case."""
    allowance = [_tx("FAMILY ALLOWANCE", f"2026-0{m}-05", 1_000_000) for m in (6, 7, 8, 9)]
    answer = _plan(history=HISTORY + allowance, income_payee="family allowance")
    assert answer.income is not None and answer.income.payee == "FAMILY ALLOWANCE"
    assert answer.periods[0].pay_date == "2026-10-05"


def test_without_a_known_payee_its_schedule_counts_as_money_in() -> None:
    """Given only a date, with no income in the history: the scheduled pay is counted."""
    answer = _plan(history=[], next_pay_date=date(2026, 10, 1))
    assert answer.income is None
    assert ("Acme Payroll", 1_850.0) in [(e.payee, e.amount) for e in answer.periods[0].expected]
    assert answer.message.startswith("Next paycheck on 2026-10-01. Before it")


@pytest.mark.parametrize(
    ("options", "words"),
    [
        ({"account_ids": ["a-nope"]}, "Unknown account(s) a-nope"),
        ({"accounts": [ACCOUNTS[2]]}, "No open checking or cash account"),
        ({"history": []}, "No regular income was found in the last 4 months: give next_pay_date"),
        (
            {"income_payee": "Lottery"},
            "No regular income was found in the last 4 months from 'Lottery'",
        ),
        (
            {"history": [], "next_pay_date": date(2026, 10, 1), "include_next_period": True},
            "Give cadence",
        ),
        (
            {
                "history": [],
                "next_pay_date": date(2026, 10, 1),
                "cadence": "weekly",
                "include_next_period": True,
            },
            "Give pay_amount",
        ),
    ],
)
def test_what_cannot_be_planned_says_what_to_give(options: dict[str, Any], words: str) -> None:
    """Each refusal names what the caller can add."""
    with pytest.raises(ValueError, match=re.escape(words)):
        _plan(**options)


def test_a_payee_not_found_lists_the_income_that_was() -> None:
    """The agent can ask again with a name that exists."""
    with pytest.raises(ValueError, match="Income found: ACME PAYROLL"):
        _plan(income_payee="Lottery")
