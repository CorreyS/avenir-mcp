# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Tests for scenarios.py: the balance projection again, with what the user is weighing."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from avenir_mcp import forecast, scenarios
from avenir_mcp.schedule import Occurrence

TODAY = date(2026, 9, 24)

CATEGORIES = [
    {"id": "c-dining", "name": "Dining out", "category_group_id": "g-fun",
     "category_group_name": "Fun"},
    {"id": "c-cinema", "name": "Cinema", "category_group_id": "g-fun",
     "category_group_name": "Fun"},
    {"id": "c-rent", "name": "Rent", "category_group_id": "g-bills",
     "category_group_name": "Bills"},
]  # fmt: skip


def _tx(day: str, amount: int, category: str | None, **extra: Any) -> dict[str, Any]:
    """A transaction of the history, amounts in milliunits."""
    tx: dict[str, Any] = {
        "date": day,
        "amount": amount,
        "category_id": category,
        "payee_name": "SOMEWHERE",
        "deleted": False,
        "transfer_account_id": None,
        "subtransactions": [],
    }
    tx.update(extra)
    return tx


# Dining out: 300 a month; cinema: 30 a month; rent 900.
HISTORY = [
    _tx(f"2026-{m}-{d}", amount, category)
    for m in ("06", "07", "08")
    for d, amount, category in (("05", -300_000, "c-dining"), ("09", -30_000, "c-cinema"),
                                ("01", -900_000, "c-rent"))
]  # fmt: skip

STREAMFLIX = forecast.Recurring(payee="STREAMFLIX", amount=-13.49, day=15, months_seen=4)
SALARY = forecast.Recurring(payee="ACME PAYROLL", amount=3200.0, day=28, months_seen=4)


def _occurrence(day: str, payee: str, amount: float) -> Occurrence:
    """A scheduled amount on its date."""
    return Occurrence(
        scheduled_id=f"s-{payee}",
        date=day,
        amount=amount,
        account="Checking",
        category=None,
        payee=payee,
        memo=None,
        frequency="monthly",
        transfer=False,
    )


SCHEDULED = [
    _occurrence("2026-10-20", "TENNIS CLUB - PRELEV", -22.0),
    _occurrence("2026-11-20", "TENNIS CLUB - PRELEV", -22.0),
    _occurrence("2026-10-28", "TENNIS CLUB REFUND", 5.0),
]


def _scenario(*adjustments: dict[str, Any], name: str = "what if") -> scenarios.Scenario:
    return scenarios.Scenario.model_validate({"name": name, "adjustments": list(adjustments)})


def _changes(*adjustments: dict[str, Any], until: str = "2026-11") -> scenarios.Changes:
    return scenarios.changes(
        _scenario(*adjustments),
        today=TODAY,
        until=until,
        history=HISTORY,
        categories=CATEGORIES,
        recurring=[STREAMFLIX, SALARY],
        scheduled=SCHEDULED,
    )


# ---------------------------------------------------------------------------
# change_spending
# ---------------------------------------------------------------------------


def test_a_category_cut_by_a_percent_leaves_that_share_of_its_usual_spending() -> None:
    """A fifth of 300 a month is 60 more each month; this month only for its 6 days left."""
    found = _changes({"kind": "change_spending", "category": "dining OUT", "percent": -20})
    assert found.monthly == {"2026-09": 12.0, "2026-10": 60.0, "2026-11": 60.0}
    assert found.one_offs == []
    (applied,) = found.applied
    assert (applied.kind, applied.subject, applied.total) == (
        "change_spending",
        "Dining out",
        132.0,
    )
    assert (applied.start, applied.end) == ("2026-09", "2026-11")
    assert "-300.00 a month" in applied.detail and "+60.00 a month" in applied.detail


def test_a_group_changes_to_an_amount_from_a_month() -> None:
    """Fun spends 330 a month; at 200 a month from October, 130 is left each month."""
    found = _changes(
        {
            "kind": "change_spending",
            "category": "g-fun",
            "monthly_spending": -200,
            "start": "2026-10",
        }
    )
    assert found.monthly == {"2026-10": 130.0, "2026-11": 130.0}
    assert found.applied[0].subject == "Fun (group)"
    assert "to -200.00" in found.applied[0].detail


def test_a_category_is_found_by_id_and_a_group_by_name() -> None:
    """Ids first, then names whatever the case and the spaces around them."""
    assert scenarios.categories_named(CATEGORIES, "c-rent") == ("Rent", {"c-rent"})
    assert scenarios.categories_named(CATEGORIES, " fun ") == (
        "Fun (group)",
        {"c-dining", "c-cinema"},
    )


def test_a_deleted_category_is_not_found() -> None:
    """A category deleted in YNAB spends nothing more: its name finds the one in use."""
    gone = {"id": "c-old", "name": "Rent", "deleted": True}
    assert scenarios.categories_named([gone, *CATEGORIES], "rent") == ("Rent", {"c-rent"})


def test_an_unknown_category_is_refused_with_where_to_find_one() -> None:
    """A name that is neither a category nor a group says where the names are."""
    with pytest.raises(ValueError, match="get_category_balances"):
        scenarios.categories_named(CATEGORIES, "Pets\u202e")


def test_usual_spending_counts_split_lines_and_refunds_but_no_transfer() -> None:
    """Each line in its category; a refund lowers the spending; moved or deleted money is not."""
    history = [
        _tx("2026-06-05", -90_000, None, subtransactions=[
            {"amount": -60_000, "category_id": "c-dining", "deleted": False},
            {"amount": -30_000, "category_id": "c-rent", "deleted": False},
        ]),
        _tx("2026-07-05", 15_000, "c-dining"),
        _tx("2026-08-05", -50_000, "c-dining", transfer_account_id="acc-2"),
        _tx("2026-08-06", -50_000, "c-dining", deleted=True),
        _tx("2026-05-06", -50_000, "c-dining"),
        _tx("2026-09-06", -50_000, "c-dining"),
    ]  # fmt: skip
    assert scenarios.usual_spending(history, TODAY, {"c-dining"}) == -15_000


# ---------------------------------------------------------------------------
# add_monthly and add_one_off
# ---------------------------------------------------------------------------


def test_a_monthly_charge_falls_on_its_day_from_its_first_month() -> None:
    """A car payment of 450 on the 5th from October."""
    found = _changes(
        {"kind": "add_monthly", "amount": -450, "start": "2026-10", "day": 5, "label": "car"}
    )
    assert found.monthly == {}
    assert [(o.date, o.amount, o.label) for o in found.one_offs] == [
        (date(2026, 10, 5), -450.0, "car"),
        (date(2026, 11, 5), -450.0, "car"),
    ]
    assert (found.applied[0].subject, found.applied[0].total) == ("car", -900.0)


def test_a_monthly_amount_stops_after_its_last_month_and_skips_a_day_gone_by() -> None:
    """From this month, on the 1st (already past), until October: October alone."""
    found = _changes({"kind": "add_monthly", "amount": 100, "end": "2026-10"})
    assert [o.date for o in found.one_offs] == [date(2026, 10, 1)]
    assert found.applied[0].subject == "monthly amount"
    assert (found.applied[0].start, found.applied[0].end) == ("2026-09", "2026-10")


def test_a_day_past_the_end_of_a_month_falls_on_its_last_day() -> None:
    """The 31st is the 30th in November."""
    found = _changes({"kind": "add_monthly", "amount": -10, "start": "2026-11", "day": 31})
    assert [o.date for o in found.one_offs] == [date(2026, 11, 30)]


def test_a_one_off_falls_on_its_date() -> None:
    """Once, with its label."""
    found = _changes({"kind": "add_one_off", "amount": -9000, "date": "2026-11-15"})
    assert [(o.date, o.amount, o.label) for o in found.one_offs] == [
        (date(2026, 11, 15), -9000.0, "one-off amount")
    ]
    assert (found.applied[0].start, found.applied[0].end) == ("2026-11-15", "2026-11-15")


# ---------------------------------------------------------------------------
# drop_recurring
# ---------------------------------------------------------------------------


def test_a_dropped_recurring_charge_is_paid_back_on_each_of_its_days() -> None:
    """Streamflix's 13.49 on the 15th no longer leaves: September's 15th is already past."""
    found = _changes({"kind": "drop_recurring", "payee": "streamflix"})
    assert [(o.date, o.amount) for o in found.one_offs] == [
        (date(2026, 10, 15), 13.49),
        (date(2026, 11, 15), 13.49),
    ]
    assert (found.applied[0].subject, found.applied[0].total) == ("STREAMFLIX", 26.98)


def test_a_schedule_is_dropped_by_a_word_of_its_payee_from_a_month() -> None:
    """Every word given must be in the payee; income of the same name stays."""
    found = _changes({"kind": "drop_recurring", "payee": "Tennis", "start": "2026-11"})
    assert [(o.date, o.amount) for o in found.one_offs] == [(date(2026, 11, 20), 22.0)]
    assert found.applied[0].subject == "TENNIS CLUB - PRELEV"


def test_dropping_a_charge_nobody_pays_names_the_ones_that_could_be() -> None:
    """Income is not a charge: the payroll cannot be dropped."""
    with pytest.raises(ValueError, match="STREAMFLIX, TENNIS CLUB - PRELEV") as refused:
        _changes({"kind": "drop_recurring", "payee": "acme payroll"})
    assert "ACME PAYROLL" not in str(refused.value)


def test_a_dropped_charge_cancels_exactly_in_the_projection() -> None:
    """The projection with the charge dropped is the projection without it."""

    def end(
        recurring: list[forecast.Recurring], extra: list[forecast.OneOff]
    ) -> list[tuple[float, float]]:
        projection = forecast.project(
            start_balance=100.0, today=TODAY, until="2027-02", recurring=recurring,
            variable_monthly=-50.0, monthly_income=0.0, one_offs=extra,
        )  # fmt: skip
        return [(m.end, m.lowest) for m in projection.months]

    found = scenarios.changes(
        _scenario({"kind": "drop_recurring", "payee": "STREAMFLIX"}),
        today=TODAY,
        until="2027-02",
        history=[],
        categories=[],
        recurring=[STREAMFLIX],
        scheduled=[],
    )
    assert end([STREAMFLIX], found.one_offs) == end([], [])
    assert end([STREAMFLIX], []) != end([], [])


# ---------------------------------------------------------------------------
# check: what cannot be projected is refused before any request
# ---------------------------------------------------------------------------


def _refused(*adjustments: dict[str, Any], names: tuple[str, ...] = ("a",)) -> str:
    given = [
        scenarios.Scenario.model_validate({"name": name, "adjustments": list(adjustments)})
        for name in names
    ]
    with pytest.raises(ValueError) as refused:
        scenarios.check(given, TODAY, "2026-12")
    return str(refused.value)


@pytest.mark.parametrize(
    ("adjustment", "expected"),
    [
        ({"kind": "change_spending", "percent": -20}, "change_spending needs category"),
        ({"kind": "add_one_off", "amount": -5}, "add_one_off needs date"),
        ({"kind": "add_monthly", "amount": -5, "payee": "X"}, "add_monthly does not take payee"),
        ({"kind": "change_spending", "category": "Rent"}, "percent or monthly_spending"),
        (
            {"kind": "change_spending", "category": "Rent", "percent": 5, "monthly_spending": -5},
            "percent or monthly_spending",
        ),
        ({"kind": "add_monthly", "amount": -5, "start": "2026-08"}, "from 2026-09 to 2026-12"),
        ({"kind": "add_monthly", "amount": -5, "end": "2027-01"}, "from 2026-09 to 2026-12"),
        (
            {"kind": "add_monthly", "amount": -5, "start": "2026-11", "end": "2026-10"},
            "comes before",
        ),
        ({"kind": "add_one_off", "amount": -5, "date": "2026-09-24"}, "after today"),
        ({"kind": "add_one_off", "amount": -5, "date": "2027-01-01"}, "end of 2026-12"),
    ],
)
def test_an_adjustment_that_cannot_be_projected_is_refused(
    adjustment: dict[str, Any], expected: str
) -> None:
    """Each kind takes its own fields, within the projected months."""
    assert expected in _refused(adjustment)


def test_two_scenarios_of_the_same_name_are_refused() -> None:
    """The answer tells scenarios apart by name."""
    given = {"kind": "add_one_off", "amount": -5, "date": "2026-10-01"}
    assert "twice" in _refused(given, names=("Car", "car "))


def test_adjustments_that_can_be_projected_pass() -> None:
    """Every field a kind takes, within the months projected."""
    scenarios.check(
        [
            _scenario(
                {"kind": "change_spending", "category": "Rent", "percent": 5, "end": "2026-12"},
                {"kind": "add_monthly", "amount": -5, "start": "2026-09", "day": 3},
                {"kind": "add_one_off", "amount": -5, "date": "2026-12-31", "label": "x"},
                {"kind": "drop_recurring", "payee": "X", "start": "2026-10", "end": "2026-10"},
            )
        ],
        TODAY,
        "2026-12",
    )


# ---------------------------------------------------------------------------
# outcome and message
# ---------------------------------------------------------------------------


def _projection(*ends: tuple[float, float]) -> forecast.Projection:
    months = [
        forecast.MonthProjection(
            month=f"2026-{9 + i:02d}", start=0.0, inflows=0.0, outflows=0.0, end=end, lowest=low
        )
        for i, (end, low) in enumerate(ends)
    ]
    shortfall = next((m.month for m in months if m.lowest < 0), None)
    return forecast.Projection(months=months, first_shortfall=shortfall)


def test_an_outcome_compares_every_month_with_the_baseline() -> None:
    """The difference each month and at the end, and the lowest day and its month."""
    baseline = _projection((100.0, 50.0), (200.0, 80.0), (300.0, 150.0))
    projection = _projection((90.0, 40.0), (-10.0, -30.0), (-5.0, -30.0))
    found = scenarios.outcome("car", projection, baseline, [])
    assert [m.difference for m in found.months] == [-10.0, -210.0, -305.0]
    assert found.difference_at_end == -305.0
    assert (found.lowest, found.lowest_month, found.first_shortfall) == (
        -30.0,
        "2026-10",
        "2026-10",
    )


def test_the_message_names_every_scenario_against_the_baseline() -> None:
    """One sentence each: the gap at the end, below zero or not, the lowest point."""
    baseline_projection = _projection((100.0, 50.0), (200.0, 80.0))
    baseline = scenarios.outcome("baseline", baseline_projection, baseline_projection, [])
    worse = scenarios.outcome(
        "car", _projection((90.0, 40.0), (-10.0, -30.0)), baseline_projection, []
    )
    text = scenarios.message(baseline, [worse], "2026-10")
    assert text.startswith("Baseline: never below zero, lowest 50.00 in 2026-09.")
    assert "car: -210.00 against the baseline by 2026-10, below zero from 2026-10" in text
    assert text.endswith("check them with the user.")
