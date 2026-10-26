# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Tests for goal.py: when a category reaches an amount, and what it takes by a date."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from avenir_mcp import goal

TODAY = date(2026, 10, 26)


def _cat(balance: int, **target: Any) -> dict[str, Any]:
    """The Vacation category of the month as YNAB returns it, with an optional target."""
    return {
        "id": "cat-vacation",
        "name": "Vacation",
        "category_group_name": "Savings goals",
        "category_group_id": "grp-savings",
        "hidden": False,
        "deleted": False,
        "budgeted": 300_000,
        "activity": 0,
        "balance": balance,
        "goal_type": None,
        "goal_target": None,
        "goal_target_date": None,
        "goal_cadence": None,
        "goal_cadence_frequency": None,
    } | target


def _past(*pairs: tuple[int, int]) -> list[tuple[str, dict[str, Any]]]:
    """(budgeted, activity) for each of the last months, oldest first, ending in September."""
    first = 10 - len(pairs)
    return [
        (f"2026-{first + i:02d}", {"budgeted": budgeted, "activity": activity})
        for i, (budgeted, activity) in enumerate(pairs)
    ]


SIX = _past(*[(300_000, -50_000)] * 6)
"""300 assigned and 50 spent each month from April to September: 250 a month kept."""


def test_the_month_reached_follows_the_average_net_contribution() -> None:
    """1,000 held, 2,000 to go at 250 a month: 8 months, June 2027."""
    answer = goal.projection(_cat(1_000_000), SIX, TODAY, 6, goal_amount=3_000.0, target_month=None)
    assert (answer.assigned, answer.activity, answer.contribution) == (300.0, -50.0, 250.0)
    assert answer.months == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    assert (answer.available, answer.goal_amount, answer.missing) == (1_000.0, 3_000.0, 2_000.0)
    assert answer.goal_source == "given"
    assert (answer.months_to_goal, answer.reached_month) == (8, "2027-06")
    assert (answer.target_month, answer.monthly_needed, answer.on_track) == (None, None, None)
    assert answer.target == "no target"
    assert (answer.category_id, answer.name, answer.group) == (
        "cat-vacation",
        "Vacation",
        "Savings goals",
    )
    assert answer.message == (
        "At 250.00 a month, the average of the last 6 complete months, Vacation would "
        "reach 3000.00 in 2027-06, 8 months from now (2000.00 still to go)."
    )


def test_a_target_month_gives_the_monthly_amount_needed() -> None:
    """2,000 to go from October 2026 to June 2027: 9 months, 222.23 each, rounded up.

    At 250 a month, June 2027 is on time.
    """
    answer = goal.projection(
        _cat(1_000_000), SIX, TODAY, 6, goal_amount=3_000.0, target_month="2027-06"
    )
    assert answer.target_month == "2027-06"
    assert answer.target_month_source == "given"
    assert answer.monthly_needed == 222.23
    assert answer.on_track is True
    assert answer.message.endswith(
        "To reach it by 2027-06: 222.23 a month over 9 months; the current pace gets there in time."
    )


def test_a_pace_too_slow_for_the_date_says_so() -> None:
    """By January 2027: 4 months, 500 each; 250 a month is not enough."""
    answer = goal.projection(
        _cat(1_000_000), SIX, TODAY, 6, goal_amount=3_000.0, target_month="2027-01"
    )
    assert (answer.monthly_needed, answer.on_track) == (500.0, False)
    assert answer.message.endswith("the current pace is not enough.")


def test_a_ynab_target_balance_by_date_gives_both_amount_and_month() -> None:
    """5,000 by 1 December 2027, 1,000 held: 4,000 over 15 months."""
    category = _cat(
        1_000_000, goal_type="TBD", goal_target=5_000_000, goal_target_date="2027-12-01"
    )
    answer = goal.projection(category, SIX, TODAY, 6, goal_amount=None, target_month=None)
    assert (answer.goal_amount, answer.goal_source) == (5_000.0, "target")
    assert (answer.target_month, answer.target_month_source) == ("2027-12", "target")
    assert answer.monthly_needed == 266.67
    assert (answer.months_to_goal, answer.reached_month, answer.on_track) == (16, "2028-02", False)
    assert answer.target == "5000.00 (target balance by date)"


def test_a_target_month_given_replaces_the_ynab_one() -> None:
    """The YNAB target's amount, by the month given."""
    category = _cat(
        1_000_000, goal_type="NEED", goal_target=2_000_000, goal_target_date="2027-12-01"
    )
    answer = goal.projection(category, SIX, TODAY, 6, goal_amount=None, target_month="2026-11")
    assert (answer.goal_source, answer.target_month_source) == ("target", "given")
    assert answer.monthly_needed == 500.0


def test_an_amount_given_leaves_the_ynab_targets_date_aside() -> None:
    """The YNAB date goes with its own amount: a note says to give target_month."""
    category = _cat(
        1_000_000, goal_type="NEED", goal_target=2_000_000, goal_target_date="2027-12-01"
    )
    answer = goal.projection(category, SIX, TODAY, 6, goal_amount=9_000.0, target_month=None)
    assert (answer.target_month, answer.target_month_source) == (None, None)
    assert any("pass target_month" in note for note in answer.notes)


def test_a_target_balance_without_a_date_and_a_yearly_need_count_as_goals() -> None:
    """A balance to reach (TB) has no month; a yearly need is due by its date."""
    balance = _cat(0, goal_type="TB", goal_target=1_000_000)
    yearly = _cat(
        0,
        goal_type="NEED",
        goal_target=1_200_000,
        goal_target_date="2027-03-01",
        goal_cadence=13,
        goal_cadence_frequency=1,
    )
    every_three = _cat(
        0,
        goal_type="NEED",
        goal_target=300_000,
        goal_target_date="2026-12-01",
        goal_cadence=1,
        goal_cadence_frequency=3,
    )
    found = [
        goal.projection(c, SIX, TODAY, 6, goal_amount=None, target_month=None)
        for c in (balance, yearly, every_three)
    ]
    assert [(a.goal_amount, a.target_month) for a in found] == [
        (1_000.0, None),
        (1_200.0, "2027-03"),
        (300.0, "2026-12"),
    ]


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"goal_type": "NEED", "goal_target": 450_000, "goal_cadence": 1},
        {"goal_type": "NEED", "goal_target": 50_000, "goal_cadence": 2, "goal_target_date": "x"},
        {"goal_type": "MF", "goal_target": 100_000},
        {"goal_type": "DEBT", "goal_target": 400_000},
        {"goal_type": "TB", "goal_target": 0},
    ],
)
def test_without_an_amount_a_target_that_is_no_balance_to_reach_is_refused(
    target: dict[str, Any],
) -> None:
    """A monthly spending target asks for an amount each month, not a balance."""
    with pytest.raises(ValueError, match="give goal_amount"):
        goal.projection(_cat(0, **target), SIX, TODAY, 6, goal_amount=None, target_month=None)


def test_a_goal_already_reached_needs_nothing() -> None:
    """3,500 held for 3,000."""
    answer = goal.projection(
        _cat(3_500_000), SIX, TODAY, 6, goal_amount=3_000.0, target_month="2027-01"
    )
    assert (answer.missing, answer.months_to_goal, answer.reached_month) == (0.0, 0, "2026-10")
    assert (answer.monthly_needed, answer.on_track) == (0.0, True)
    assert answer.message == "Vacation holds 3500.00: the 3000.00 goal is already reached."


def test_a_category_that_loses_money_never_gets_there() -> None:
    """More spent than assigned: no month, and on_track is false."""
    answer = goal.projection(
        _cat(1_000_000),
        _past((100_000, -150_000), (100_000, -150_000)),
        TODAY,
        6,
        goal_amount=3_000.0,
        target_month="2027-06",
    )
    assert answer.contribution == -50.0
    assert (answer.reached_month, answer.months_to_goal, answer.on_track) == (None, None, False)
    assert answer.message.startswith(
        "At -50.00 a month, the average of the last 2 complete months, Vacation never "
        "reaches 3000.00."
    )
    assert any("Only 2 of the 6 months" in note for note in answer.notes)


def test_one_month_from_now_is_said_in_the_singular() -> None:
    """200 to go at 250 a month."""
    answer = goal.projection(_cat(2_800_000), SIX, TODAY, 6, goal_amount=3_000.0, target_month=None)
    assert answer.months_to_goal == 1
    assert "in 2026-11, 1 month from now" in answer.message


def test_a_pace_that_would_take_more_than_fifty_years_gives_no_month() -> None:
    """1 a month for a million."""
    answer = goal.projection(
        _cat(0), _past((1_000, 0)), TODAY, 1, goal_amount=1_000_000.0, target_month=None
    )
    assert (answer.reached_month, answer.months_to_goal) == (None, None)
    assert any("more than 600 months" in note for note in answer.notes)
    assert answer.message == (
        "At 1.00 a month, what the last complete month added, Vacation would take "
        "more than 600 months to reach 1000000.00 (1000000.00 still to go)."
    )


def test_no_complete_month_gives_no_pace() -> None:
    """A category created this month."""
    answer = goal.projection(_cat(500_000), [], TODAY, 6, goal_amount=3_000.0, target_month=None)
    assert (answer.contribution, answer.months) == (0.0, [])
    assert answer.reached_month is None
    assert answer.message == (
        "No complete month to learn a pace from: no date can be given for Vacation to "
        "reach 3000.00 (2500.00 still to go)."
    )


def test_a_ynab_target_date_already_gone_asks_for_everything_this_month() -> None:
    """Due in August, still 1,000 short: 1,000 now."""
    category = _cat(
        1_000_000, goal_type="TBD", goal_target=2_000_000, goal_target_date="2026-08-01"
    )
    answer = goal.projection(category, SIX, TODAY, 6, goal_amount=None, target_month=None)
    assert answer.monthly_needed == 1_000.0
    assert answer.on_track is False
    assert any("has passed" in note for note in answer.notes)


@pytest.mark.parametrize("month", ["2026-09", "2026-13", "June", "2077-01"])
def test_a_target_month_gone_malformed_or_too_far_is_refused(month: str) -> None:
    """The current month and the 600 after it are accepted."""
    with pytest.raises(ValueError, match="target_month"):
        goal.check_target_month(month, TODAY)
    goal.check_target_month("2026-10", TODAY)
    goal.check_target_month("2076-10", TODAY)


def test_the_category_is_found_by_name_or_id_and_must_be_one() -> None:
    """Two categories named alike are told apart by their ids."""
    other = _cat(0) | {"id": "cat-v2", "category_group_name": "Fun"}
    categories = [_cat(0), other, _cat(0) | {"id": "cat-x", "name": "Car"}]
    assert goal.one_category(categories, "cat-v2")["id"] == "cat-v2"
    assert goal.one_category(categories, "car")["id"] == "cat-x"
    with pytest.raises(ValueError, match=r"names 2 categories.*cat-vacation \(Savings goals\)"):
        goal.one_category(categories, "vacation")
