# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Tests for upcoming.py: the irregular bills ahead, and whether each category is ready."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from avenir_mcp import upcoming

from .factories import NO_TARGET, scheduled

TODAY = date(2026, 9, 25)


def _tx(  # pylint: disable=too-many-arguments
    payee: str,
    day: str,
    amount: int,
    category: str | None = None,
    *,
    transfer: str | None = None,
    deleted: bool = False,
) -> dict[str, Any]:
    where = {"account_id": "acc", "transfer_account_id": transfer, "deleted": deleted}
    return where | {"payee_name": payee, "date": day, "amount": amount, "category_id": category}


def _every(payee: str, days: list[str], amount: int, category: str | None = None) -> list[Any]:
    """The same charge on each of the days."""
    return [_tx(payee, day, amount, category) for day in days]


def _cat(cat_id: str, name: str, balance: int, **goal: Any) -> dict[str, Any]:
    """A category of the current month as YNAB returns it, amounts in milliunits."""
    return (
        {
            "id": cat_id,
            "name": name,
            "balance": balance,
            "hidden": False,
            "deleted": False,
            "goal_snoozed_at": None,
            "goal_under_funded": None,
            "goal_overall_left": None,
            "goal_months_to_budget": None,
        }
        | NO_TARGET
        | goal
    )


QUARTERS = [
    "2024-12-02", "2025-03-03", "2025-06-02", "2025-09-01",
    "2025-12-01", "2026-03-02", "2026-06-01", "2026-09-01",
]  # fmt: skip
WATER = _every("AQUA SERVICES", QUARTERS, -84_000, "c-water")
CAR = _every("SHIELD AUTO INSURANCE", ["2025-04-10", "2025-10-09", "2026-04-10"], -312_000, "c-car")
CLOUD = [
    _tx("CLOUDBOX ANNUAL", "2024-11-14", -59_880, "c-subs"),
    _tx("CLOUDBOX ANNUAL", "2025-11-14", -64_880, "c-subs"),
]
STREAM = _every(
    "STREAMFLIX", [f"2026-{m:02d}-15" for m in range(1, 10)], -13_490, "c-subs"
)  # monthly: not irregular
MONTH = {
    "categories": [
        _cat("c-water", "Water", 40_000),
        _cat("c-car", "Car insurance", 100_000),
        _cat("c-subs", "Subscriptions", 13_490),
        _cat("c-hidden", "Old", 0, hidden=True),
    ]
}


def _found(transactions: list[dict[str, Any]], today: date = TODAY) -> dict[str, Any]:
    return {p.payee: p for p in upcoming.irregular(transactions, today)}


# ---------------------------------------------------------------------------
# Detection in the history
# ---------------------------------------------------------------------------


def test_a_quarterly_bill_is_found_with_high_confidence() -> None:
    """Eight bills three months apart, at the same amount: every3Months, seen 8 times."""
    water = _found(WATER + STREAM)["AQUA SERVICES"]
    assert (water.frequency, water.every, water.times_seen) == ("every3Months", 3, 8)
    assert (water.amount, water.last, water.category_id) == (-84.0, date(2026, 9, 1), "c-water")
    assert water.confidence == "high"


def test_a_monthly_charge_is_not_irregular() -> None:
    """Streaming every month is find_recurring_charges' business, not this one's."""
    assert "STREAMFLIX" not in _found(STREAM)


def test_a_yearly_charge_seen_twice_is_found_with_low_confidence_at_its_latest_amount() -> None:
    """Two charges a year apart: yearly, low confidence, the price raised last time."""
    cloud = _found(CLOUD)["CLOUDBOX ANNUAL"]
    assert (cloud.frequency, cloud.every, cloud.times_seen) == ("yearly", 12, 2)
    assert (cloud.amount, cloud.confidence) == (-64.88, "low")


def test_twice_a_year_and_every_four_months_have_their_own_names() -> None:
    """Six months apart is twiceAYear; four months apart, every4Months."""
    found = _found(
        CAR + _every("GYM PASS", ["2025-09-20", "2026-01-20", "2026-05-20", "2026-09-20"], -90_000)
    )
    assert found["SHIELD AUTO INSURANCE"].frequency == "twiceAYear"
    assert found["GYM PASS"].frequency == "every4Months"


def test_every_other_month_needs_three_charges_to_be_believed() -> None:
    """Two purchases two months apart are a coincidence; three are a rhythm."""
    two = _every("HAIRDRESSER", ["2026-05-12", "2026-07-11"], -35_000)
    assert "HAIRDRESSER" not in _found(two)
    three = two + [_tx("HAIRDRESSER", "2026-09-10", -36_000)]
    assert _found(three)["HAIRDRESSER"].frequency == "everyOtherMonth"


@pytest.mark.parametrize(
    "days",
    [
        ["2025-12-01", "2026-03-02", "2026-09-01"],  # three months, then six
        ["2026-02-01", "2026-04-15", "2026-06-01"],  # 73 days, then 47
        ["2024-10-01"],  # once
    ],
)
def test_an_uneven_rhythm_is_not_a_pattern(days: list[str]) -> None:
    """Every gap must match the same cadence."""
    assert not _found(_every("HARDWARE STORE", days, -50_000))


def test_amounts_far_from_their_median_are_not_one_bill() -> None:
    """Within 20 % of the median, as forecast_balance asks of a monthly charge."""
    days = ["2025-12-01", "2026-03-02", "2026-06-01", "2026-09-01"]
    varied = [
        _tx("GARDEN CENTRE", d, a) for d, a in zip(days, (-50_000, -50_000, -50_000, -95_000))
    ]
    assert not _found(varied)


def test_what_is_not_a_charge_of_the_last_24_months_is_left_out() -> None:
    """Income, transfers, deleted ones, the future, older than 24 full months, no payee."""
    days = ["2026-03-02", "2026-06-01", "2026-09-01"]
    found = _found(
        _every("ACME PAYROLL", days, 900_000)
        + [_tx("SAVINGS", d, -100_000, transfer="acc-savings") for d in days]
        + [_tx("GHOST", d, -100_000, deleted=True) for d in days]
        + _every("LATER", ["2026-10-01", "2027-01-01", "2027-04-01"], -100_000)
        + _every("ANCIENT", ["2024-03-01", "2024-06-01", "2024-09-02"], -100_000)
        + _every("", days, -100_000)
    )
    assert not found


def test_two_charges_on_one_day_count_as_one_bill() -> None:
    """A bill paid in two parts the same day: one date, the amounts added up."""
    halves = [tx for tx in WATER if tx["date"] != "2025-03-03"] + [
        _tx("AQUA SERVICES", "2025-03-03", -42_000, "c-water"),
        _tx("AQUA SERVICES", "2025-03-03", -42_000, "c-water"),
    ]
    water = _found(halves)["AQUA SERVICES"]
    assert (water.times_seen, water.frequency) == (8, "every3Months")


def test_a_charge_that_stopped_coming_is_left_out() -> None:
    """Twice a year, last paid in December: June's is over two months late, so stopped."""
    stopped = _every("OLD MAGAZINE", ["2025-06-10", "2025-12-10"], -45_000)
    assert not _found(stopped)


def test_the_category_is_the_one_used_most_often() -> None:
    """Categorised twice as Car insurance, once as nothing: Car insurance."""
    mixed = [dict(tx) for tx in CAR]
    mixed[0]["category_id"] = None
    assert _found(mixed)["SHIELD AUTO INSURANCE"].category_id == "c-car"
    bare = [dict(tx, category_id=None) for tx in CAR]
    assert _found(bare)["SHIELD AUTO INSURANCE"].category_id is None


# ---------------------------------------------------------------------------
# Dates projected
# ---------------------------------------------------------------------------


def test_a_pattern_is_projected_at_its_cadence_up_to_the_horizon() -> None:
    """The water bill on the first Monday's day, every three months."""
    water = _found(WATER)["AQUA SERVICES"]
    assert upcoming.dates_of(water, TODAY, date(2027, 6, 30)) == [
        date(2026, 12, 1),
        date(2027, 3, 1),
        date(2027, 6, 1),
    ]


def test_a_charge_late_by_less_than_a_month_is_still_expected_on_the_first_day() -> None:
    """Due on 10 September, not seen by the 25th: it is expected today."""
    late = _found(_every("SHIELD AUTO", ["2025-09-10", "2026-03-10"], -312_000))["SHIELD AUTO"]
    assert upcoming.dates_of(late, TODAY, date(2027, 3, 31)) == [
        TODAY,
        date(2027, 3, 10),
    ]
    assert upcoming.dates_of(late, date(2026, 9, 26), date(2026, 12, 31)) == [date(2026, 9, 26)]
    # Looked at from further ahead, a charge missed by more than a month is not expected.
    assert upcoming.dates_of(late, date(2026, 11, 1), date(2027, 3, 31)) == [date(2027, 3, 10)]


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------

SCHEDULES = [
    scheduled(
        "Home insurance",
        "2025-10-20",
        "2026-10-20",
        "yearly",
        amount=-420_000,
        category_id="c-home",
    ),
    scheduled("Landlord", "2026-01-03", "2026-10-03", "monthly", category_id="c-rent"),
    scheduled(
        "Property tax", "2026-11-15", "2026-11-15", "never", amount=-1_426_000, category_id=None
    ),
    scheduled("Bonus", "2025-12-15", "2026-12-15", "yearly", amount=500_000),
    scheduled(
        "To savings", "2026-01-31", "2026-12-31", "yearly", transfer_account_id="acc-savings"
    ),
    scheduled("Gone", "2026-01-31", "2026-12-31", "yearly", deleted=True),
    # The bank writes the car insurer's label longer: its schedule covers what is detected.
    scheduled(
        "Shield Auto",
        "2026-04-10",
        "2026-10-10",
        "twiceAYear",
        amount=-312_000,
        category_id="c-car",
    ),
]
HOME = _cat("c-home", "Home insurance", 315_000)
RENT = _cat("c-rent", "Rent", 0)
EVERY = {"categories": [*MONTH["categories"], HOME, RENT]}


def _summary(**options: Any) -> upcoming.UpcomingExpenses:
    history = options.pop("history", WATER + CAR + CLOUD + STREAM)
    return upcoming.summary(
        history, options.pop("schedules", SCHEDULES), options.pop("month", EVERY), TODAY, **options
    )


def test_expenses_are_grouped_by_month_from_both_sources() -> None:
    """Scheduled yearly and one-off bills, and charges found in the history, month by month."""
    answer = _summary(horizon_months=3)
    assert (answer.since, answer.until) == ("2026-09-25", "2026-12-25")
    assert [m.month for m in answer.months] == ["2026-10", "2026-11", "2026-12"]
    october, november, december = answer.months
    assert [(e.date, e.payee, e.amount, e.source) for e in october.expenses] == [
        ("2026-10-10", "Shield Auto", -312.0, "scheduled"),
        ("2026-10-20", "Home insurance", -420.0, "scheduled"),
    ]
    assert october.total == -732.0
    assert [(e.payee, e.source, e.confidence) for e in november.expenses] == [
        ("CLOUDBOX ANNUAL", "detected", "low"),
        ("Property tax", "scheduled", None),
    ]
    assert [(e.payee, e.frequency, e.category) for e in december.expenses] == [
        ("AQUA SERVICES", "every3Months", "Water")
    ]
    assert answer.total == -2_306.88


def test_a_charge_a_schedule_covers_is_not_counted_twice() -> None:
    """The insurer's charges in the history are left to its schedule."""
    answer = _summary()
    payees = [e.payee for m in answer.months for e in m.expenses]
    assert "SHIELD AUTO INSURANCE" not in payees
    assert payees.count("Shield Auto") == 2


def test_monthly_schedules_join_only_when_asked_for() -> None:
    """Rent is monthly: left out by default, listed with include_monthly."""
    assert all(e.payee != "Landlord" for m in _summary().months for e in m.expenses)
    answer = _summary(include_monthly=True, horizon_months=1)
    assert [e.payee for m in answer.months for e in m.expenses][:1] == ["Landlord"]


def test_money_in_transfers_and_deleted_schedules_are_not_expenses() -> None:
    """The yearly bonus, the transfer to savings and the deleted schedule stay out."""
    payees = {e.payee for m in _summary().months for e in m.expenses}
    assert not payees & {"Bonus", "To savings", "Gone"}


def test_a_category_with_no_target_needs_the_rest_spread_over_the_months_left() -> None:
    """Home insurance holds 315.00 for 420.00 due in October: 105.00 short, 52.50 a month."""
    home = next(c for c in _summary().categories if c.name == "Home insurance")
    assert (home.category_id, home.available, home.target) == ("c-home", 315.0, "no target")
    assert (home.due, home.first_due, home.expected_funding) == (420.0, "2026-10-20", 0.0)
    assert (home.on_track, home.shortfall, home.extra_per_month) == (False, 105.0, 52.5)


def test_a_monthly_target_is_counted_until_the_bill() -> None:
    """The water bill, 84.00 in December: 40 + 10 + 3 x 15 = 95.00 by then, enough.

    40.00 there, 10.00 still due this month, then 15.00 a month.
    """
    month = {
        "categories": [
            _cat(
                "c-water",
                "Water",
                40_000,
                goal_type="NEED",
                goal_target=15_000,
                goal_cadence=1,
                goal_cadence_frequency=1,
                goal_under_funded=10_000,
            )
        ]
    }
    water = next(c for c in _summary(month=month, horizon_months=3).categories)
    assert (water.name, water.target, water.due) == ("Water", "15.00 each month", 84.0)
    assert (water.expected_funding, water.on_track, water.shortfall) == (55.0, True, 0.0)


@pytest.mark.parametrize(
    ("goal", "funding"),
    [
        # 80.00 left to fund by a date two months away: half of it by October's bill.
        (
            {
                "goal_target_date": "2026-11-01",
                "goal_overall_left": 80_000,
                "goal_months_to_budget": 2,
            },
            80.0,
        ),
        # A target due long ago with nothing left: nothing more comes.
        ({"goal_target_date": "2026-01-01", "goal_overall_left": 0}, 0.0),
        # Yearly 600.00: 50.00 a month, one month after this one.
        ({"goal_cadence": 13, "goal_cadence_frequency": 1, "goal_target": 600_000}, 50.0),
        # Weekly 10.00: 52 weeks over 12 months.
        ({"goal_cadence": 2, "goal_cadence_frequency": 1, "goal_target": 10_000}, 43.33),
        # Every 3 months (YNAB's cadence 4): a third of 90.00 a month.
        ({"goal_cadence": 4, "goal_target": 90_000}, 30.0),
        # Every other year (YNAB's cadence 14): 240.00 over 24 months.
        ({"goal_cadence": 14, "goal_target": 240_000}, 10.0),
        # Monthly funding and debt payments are monthly amounts.
        ({"goal_type": "MF", "goal_target": 25_000}, 25.0),
        # A balance to reach with no date and no rhythm promises nothing.
        ({"goal_type": "TB", "goal_target": 500_000}, 0.0),
        # A snoozed target asks for nothing.
        ({"goal_cadence": 1, "goal_target": 25_000, "goal_snoozed_at": "2026-09-01"}, 0.0),
    ],
)
def test_each_kind_of_target_adds_what_it_asks_for_by_the_bill(
    goal: dict[str, Any], funding: float
) -> None:
    """The car insurance in October: what the target adds by then, as YNAB sets targets."""
    month = {"categories": [_cat("c-car", "Car insurance", 0, **({"goal_type": "NEED"} | goal))]}
    car = next(c for c in _summary(month=month, horizon_months=1).categories)
    assert car.expected_funding == funding


def test_two_bills_of_one_category_add_up_and_the_worst_month_sets_the_pace() -> None:
    """Car insurance: 312.00 in October and April, 100.00 there, nothing planned."""
    car = next(c for c in _summary().categories if c.name == "Car insurance")
    assert (car.due, car.first_due, car.shortfall) == (624.0, "2026-10-10", 524.0)
    # October needs 212.00 over two months (106.00); April 524.00 over eight (65.50).
    assert car.extra_per_month == 106.0


def test_a_ready_category_is_on_track_and_short_by_nothing() -> None:
    """Subscriptions holds enough for the cloud storage."""
    month = {"categories": [_cat("c-subs", "Subscriptions", 100_000)]}
    subs = _summary(month=month).categories[0]
    assert (subs.on_track, subs.shortfall, subs.extra_per_month) == (True, 0.0, 0.0)


def test_expenses_without_a_known_category_are_listed_but_not_judged() -> None:
    """The property tax has no category, and the cloud storage's is not in the month."""
    month = {"categories": [c for c in EVERY["categories"] if c["id"] != "c-subs"]}
    answer = _summary(month=month)
    assert {c.category_id for c in answer.categories} == {"c-home", "c-car", "c-water"}
    cloud = next(e for m in answer.months for e in m.expenses if e.payee == "CLOUDBOX ANNUAL")
    assert (cloud.category, cloud.category_id) == (None, None)
    assert answer.notes[-1].startswith("2 of the bills have no category")
    assert not any("no category" in note for note in _summary(schedules=[]).notes)


def test_a_split_schedule_has_no_category_to_judge() -> None:
    """Its lines carry the categories: the bill is listed as Split."""
    split = scheduled(
        "Garage", "2026-11-01", "2026-11-01", "never", subtransactions=[{"deleted": False}]
    )
    answer = _summary(schedules=[split], history=[])
    [garage] = answer.months[0].expenses
    assert (garage.category, garage.category_id) == ("Split", None)
    assert answer.message == "1 upcoming expense, 950.00 in all, between 2026-09-25 and 2027-09-25."


def test_the_message_names_the_categories_short_the_largest_need_first() -> None:
    """Car insurance needs the most each month, then Home insurance, then Water."""
    answer = _summary()
    assert answer.total == -2_870.88
    assert answer.message == (
        "9 upcoming expenses, 2870.88 in all, between 2026-09-25 and 2027-09-25. "
        "4 of 4 categories are short: Car insurance needs 106.00 more a month, "
        "Home insurance needs 52.50 more a month, Water needs 22.77 more a month, and 1 more."
    )
    assert [c.name for c in answer.categories] == [
        "Car insurance",
        "Home insurance",
        "Water",
        "Subscriptions",
    ]


def test_the_message_says_when_every_category_is_ready() -> None:
    """Enough everywhere: the message says so, and lists them after any short one."""
    rich = {"categories": [_cat(c, n, 10_000_000) for c, n in (("c-water", "W"), ("c-car", "C"))]}
    answer = _summary(month=rich, schedules=[], history=WATER + CAR)
    assert answer.message.endswith(" Every category they come out of is on track.")
    assert [
        c.on_track
        for c in _summary(month=rich | {"categories": [*rich["categories"], HOME]}).categories
    ] == [False, True, True]


def test_nothing_ahead_says_so() -> None:
    """No schedule and no history: nothing to plan for."""
    answer = _summary(history=[], schedules=[])
    assert answer.months == [] and answer.categories == [] and answer.total == 0.0
    assert answer.message == "No upcoming expense was found between 2026-09-25 and 2027-09-25."
