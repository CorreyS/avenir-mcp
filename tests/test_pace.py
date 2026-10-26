# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Tests for pace.py: where each category's spending is heading by the end of the month."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from avenir_mcp import pace

from .factories import month_category, scheduled, transaction

TODAY = date(2026, 9, 20)
"""Day 20 of a 30-day month."""


def _cat(key: str, budgeted: int, available: int, **extra: Any) -> dict[str, Any]:
    """A category of the month, in Everyday unless told otherwise."""
    return month_category(key, budgeted, available, **extra)


def _tx(
    day: str, amount: int, category: str | None, *, lines: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A transaction on a budget account, split into lines when given."""
    return transaction(day, amount, category, subtransactions=lines or [])


def _every_month(day: int, amount: int, category: str, months: range) -> list[dict[str, Any]]:
    """The same payment on the same day of each month of 2026 given."""
    return [_tx(f"2026-{m:02d}-{day:02d}", amount, category) for m in months]


PAST = range(3, 9)
"""March to August 2026: the six complete months before TODAY."""

GROCERIES = [
    *_every_month(5, -100_000, "cat-groceries", PAST),
    *_every_month(15, -100_000, "cat-groceries", PAST),
    *_every_month(25, -100_000, "cat-groceries", PAST),
    _tx("2026-09-04", -120_000, "cat-groceries"),
    _tx("2026-09-18", -130_000, "cat-groceries"),
]
"""300 a month in three shops, two of them by the 20th; 250 spent so far this month."""
RENT = _every_month(28, -1_000_000, "cat-rent", PAST)
"""1,000 on the 28th of each month: not paid yet this month."""
FUN = [_tx("2026-08-10", -40_000, "cat-fun"), _tx("2026-09-12", -60_000, "cat-fun")]
"""A category too new for its history to say anything: 60 spent this month."""

CATEGORIES = [
    _cat("groceries", 300_000, 50_000),
    _cat("rent", 1_000_000, 1_000_000, group="Bills"),
    _cat("fun", 80_000, 20_000),
    _cat("insurance", 400_000, 400_000, group="Bills"),
]


def _summary(**kwargs: Any) -> pace.Pace:
    """The pace of the four categories on TODAY, over six months, nothing scheduled."""
    args: dict[str, Any] = {
        "categories": CATEGORIES,
        "transactions": [*GROCERIES, *RENT, *FUN],
        "today": TODAY,
        "months_count": 6,
        "scheduled": {},
    }
    return pace.summary(**(args | kwargs))


def _line(answer: pace.Pace, name: str) -> pace.CategoryPace:
    return next(line for line in answer.categories if line.name == name)


def test_spending_is_projected_from_the_share_usually_spent_by_this_day() -> None:
    """Two thirds of groceries are usually spent by the 20th: 250 so far heads for 375.

    Budgeted 300, 50 left: the 125 still to come leave it 75 short.
    """
    line = _line(_summary(), "Groceries")
    assert line.method == "history_share"
    assert line.typical_share == 0.67
    assert (line.budgeted, line.available, line.spent) == (300.0, 50.0, -250.0)
    assert line.projected_spent == -375.0
    assert line.projected_available == -75.0
    assert (line.likely_over, line.shortfall) == (True, 75.0)
    assert line.group == "Everyday"
    assert line.category_id == "cat-groceries"


def test_a_bill_not_due_yet_adds_what_the_rest_of_the_month_usually_holds() -> None:
    """Rent lands on the 28th: by the 20th nothing is usually spent, so dividing is no use.

    What the rest of the month usually holds, 1,000, is added instead: it uses all that
    is available, and no more.
    """
    line = _line(_summary(), "Rent")
    assert line.method == "history_rest"
    assert line.typical_share == 0.0
    assert line.spent == 0.0
    assert line.projected_spent == -1_000.0
    assert line.projected_available == 0.0
    assert (line.likely_over, line.shortfall) == (False, 0.0)


def test_what_the_rest_of_the_month_holds_is_averaged_from_the_first_month_spent() -> None:
    """Rent paid since June only: 1,000 a month over three months, not 500 over six."""
    history = [*GROCERIES, *_every_month(28, -1_000_000, "cat-rent", range(6, 9))]
    line = _line(_summary(transactions=history), "Rent")
    assert line.method == "history_rest"
    assert line.projected_spent == -1_000.0


def test_a_thin_history_falls_back_to_the_days_elapsed() -> None:
    """One month of history is too little: 60 in 20 days of 30 heads for 90, 10 over."""
    line = _line(_summary(), "Fun")
    assert line.method == "linear"
    assert line.typical_share is None
    assert line.projected_spent == -90.0
    assert line.projected_available == -10.0
    assert line.shortfall == 10.0


def test_what_is_scheduled_for_the_rest_of_the_month_is_a_floor() -> None:
    """Nothing spent on insurance yet, 400 scheduled on the 25th: 400 is projected."""
    answer = _summary(scheduled={"cat-insurance": 400_000, "cat-groceries": 50_000})
    line = _line(answer, "Insurance")
    assert line.method == "scheduled"
    assert line.scheduled == -400.0
    assert line.projected_spent == -400.0
    assert line.projected_available == 0.0
    # 250 spent and 50 scheduled stay under the 375 the history projects.
    groceries = _line(answer, "Groceries")
    assert (groceries.method, groceries.scheduled, groceries.projected_spent) == (
        "history_share",
        -50.0,
        -375.0,
    )


def test_the_categories_likely_over_come_first_the_largest_shortfall_first() -> None:
    """Then the others, the least projected to be left first."""
    answer = _summary()
    assert [line.name for line in answer.categories] == ["Groceries", "Fun", "Rent", "Insurance"]
    assert answer.likely_over == 2
    assert answer.shortfall == 85.0
    assert answer.message == (
        "Day 20 of 30 of 2026-09: 2 categories are likely to run out by the end of the "
        "month, 85.00 short in all: Groceries (75.00 short), Fun (10.00 short)."
    )


def test_no_category_likely_over_says_so() -> None:
    """Rent and insurance alone keep within what they have."""
    answer = _summary(only={"cat-rent", "cat-insurance"})
    assert [line.name for line in answer.categories] == ["Rent", "Insurance"]
    assert answer.likely_over == 0
    assert answer.shortfall == 0.0
    assert answer.message == (
        "Day 20 of 30 of 2026-09: at this pace, every category keeps within what it has "
        "until the end of the month."
    )


def test_one_category_likely_over_is_named_in_the_singular() -> None:
    """Groceries alone."""
    answer = _summary(only={"cat-groceries"})
    assert answer.message == (
        "Day 20 of 30 of 2026-09: 1 category is likely to run out by the end of the month, "
        "75.00 short in all: Groceries (75.00 short)."
    )


def test_a_limit_shortens_the_list_not_the_totals() -> None:
    """The first one listed, the totals still counting both shortfalls."""
    answer = _summary(limit=1)
    assert [line.name for line in answer.categories] == ["Groceries"]
    assert answer.more == 3
    assert answer.shortfall == 85.0
    assert any("3 more categories are not listed" in note for note in answer.notes)


def test_hidden_internal_card_and_empty_categories_are_left_out() -> None:
    """Only the user's own categories with an amount somewhere are listed."""
    categories = [
        *CATEGORIES,
        _cat("old", 10_000, 10_000, hidden=True),
        _cat("gone", 10_000, 10_000, deleted=True),
        _cat("inflow", 0, 5_000_000, group="Internal Master Category"),
        _cat("visa", 200_000, 200_000, group="Credit Card Payments"),
        _cat("nothing", 0, 0),
    ]
    answer = _summary(categories=categories)
    assert [line.name for line in answer.categories] == ["Groceries", "Fun", "Rent", "Insurance"]


def test_only_money_out_counts_split_lines_on_their_own() -> None:
    """A refund is not spending, a deleted line is not either; a split counts each line."""
    lines: list[dict[str, Any]] = [
        {"amount": -30_000, "category_id": "cat-fun"},
        {"amount": -20_000, "category_id": "cat-groceries"},
        {"amount": -99_000, "category_id": "cat-fun", "deleted": True},
    ]
    history = [
        _tx("2026-09-10", -50_000, None, lines=lines),
        _tx("2026-09-11", 15_000, "cat-fun"),
        _tx("2026-09-11", -1_000, "cat-fun") | {"deleted": True},
        _tx("2026-09-11", -500_000, None),
    ]
    answer = _summary(transactions=history, months_count=1)
    assert _line(answer, "Fun").spent == -30.0
    assert _line(answer, "Groceries").spent == -20.0


def test_months_before_the_first_transaction_are_not_counted() -> None:
    """Six months asked, history since June: three months, and a note says so."""
    history = [*_every_month(5, -100_000, "cat-groceries", range(6, 9))]
    answer = _summary(transactions=history)
    assert answer.months == ["2026-06", "2026-07", "2026-08"]
    assert _line(answer, "Groceries").method == "history_share"
    assert any("Only the last 3 of the 6 months" in note for note in answer.notes)


def test_no_history_at_all_projects_every_category_by_the_days() -> None:
    """A plan started this month: no month to learn from."""
    answer = _summary(transactions=[_tx("2026-09-02", -30_000, "cat-fun")])
    assert answer.months == []
    assert all(line.method == "linear" for line in answer.categories)
    assert _line(answer, "Fun").projected_spent == -45.0
    assert any("No complete month" in note for note in answer.notes)


def test_a_shorter_month_counts_whole_when_today_is_past_its_end() -> None:
    """On 30 March, February's 28 days are all behind: its whole spending is done."""
    history = [_tx("2026-02-27", -100_000, "cat-groceries")] * 3 + [
        _tx(f"2026-{m:02d}-31", -100_000, "cat-groceries") for m in (1,)
    ]
    history += [_tx("2025-12-28", -100_000, "cat-groceries"), _tx("2026-03-02", -10, "cat-x")]
    answer = _summary(transactions=history, today=date(2026, 3, 30), months_count=3)
    line = _line(answer, "Groceries")
    # December and February are done by day 30; January's 31st is not.
    assert line.typical_share == 0.8
    assert answer.months == ["2025-12", "2026-01", "2026-02"]


def test_on_the_last_day_every_past_month_is_whole() -> None:
    """On 30 September, what July spent on its 31st is behind too."""
    history = [
        _tx(f"2026-{m:02d}-{d:02d}", -100_000, "cat-groceries")
        for m, d in ((6, 30), (7, 31), (8, 31))
    ]
    answer = _summary(transactions=history, today=date(2026, 9, 30), months_count=3)
    assert _line(answer, "Groceries").typical_share == 1.0
    assert answer.day == 30
    assert answer.days_in_month == 30


def test_the_notes_say_how_spending_was_projected() -> None:
    """Each method, the scheduled floor and what spent leaves out."""
    notes = " ".join(_summary().notes)
    for words in ("history_share", "history_rest", "linear", "scheduled", "refunds"):
        assert words in notes


def test_scheduled_outflows_are_summed_per_category_until_the_end_of_the_month() -> None:
    """From today to the 30th: weekly twice, a split; money in and transfers are not."""
    items = [
        scheduled("rent", "2026-01-25", "2026-09-25", "monthly"),
        scheduled("gym", "2026-09-21", "2026-09-21", "weekly", amount=-10_000, category_id="c-gym"),
        scheduled("pay", "2026-01-28", "2026-09-28", "monthly", amount=2_000_000),
        scheduled("save", "2026-01-29", "2026-09-29", "monthly", category_id=None),
        scheduled("next", "2026-01-05", "2026-10-05", "monthly"),
        scheduled("old", "2026-01-22", "2026-09-22", "monthly", deleted=True),
        scheduled(
            "shop",
            "2026-09-26",
            "2026-09-26",
            "never",
            amount=-50_000,
            category_id=None,
            subtransactions=[
                {"amount": -30_000, "category_id": "c-gym", "deleted": False},
                {"amount": -20_000, "category_id": "c-food", "deleted": False},
                {"amount": -5_000, "category_id": "c-food", "deleted": True},
            ],
        ),
    ]
    assert pace.scheduled_rest(items, TODAY) == {
        "c-rent": 950_000,
        "c-gym": 50_000,
        "c-food": 20_000,
    }


def test_categories_are_chosen_by_name_or_id_whatever_the_case() -> None:
    """A name may match two categories of different groups: both are kept."""
    categories = [*CATEGORIES, _cat("groceries", 0, 0, group="Bills") | {"id": "cat-g2"}]
    found = pace.chosen_categories(categories, [" GROCERIES ", "cat-rent", "cat-groceries"])
    assert [c["id"] for c in found] == ["cat-groceries", "cat-g2", "cat-rent"]


def test_an_unknown_category_is_refused_with_the_plans_categories() -> None:
    """The error names what was not found and the categories it could be."""
    with pytest.raises(ValueError, match=r"Unknown category\(ies\) 'Food'") as error:
        pace.chosen_categories(CATEGORIES, ["Food", "rent"])
    assert "Groceries, Rent, Fun, Insurance" in str(error.value)


def test_the_choice_mixes_categories_and_groups() -> None:
    """Groceries, and every category of Bills; nothing given keeps every category."""
    assert pace.chosen(CATEGORIES, ["Groceries"], ["bills"]) == {
        "cat-groceries",
        "cat-rent",
        "cat-insurance",
    }
    assert pace.chosen(CATEGORIES, None, ["grp-everyday"]) == {"cat-groceries", "cat-fun"}
    assert pace.chosen(CATEGORIES, None, None) is None
    with pytest.raises(ValueError, match="Unknown category group"):
        pace.chosen(CATEGORIES, None, ["Food"])
