# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""When a category reaches an amount at its usual pace, and what it takes by a month.

The pace is what the category's balance grew by, on average, over the last complete
months: what was assigned to it plus its activity, spending negative. Assigned alone
would ignore what is spent out of the category, and promise too early a date. The
projection starts from what the category holds today and adds that pace each month
after this one. What a month needs to reach the amount by a date spreads what is
missing evenly over the months up to it, this one included, as YNAB counts a target by
date. Sums are made in milliunits.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from avenir_mcp.client import amount_to_milliunit, milliunit_to_amount
from avenir_mcp.model import Model
from avenir_mcp.pace import chosen_categories
from avenir_mcp.targets import describe
from avenir_mcp.text import untrusted

MAX_MONTHS = 24
"""Two years of pace, as get_spending_trends: one YNAB request per month."""
MAX_AHEAD = 600
"""Fifty years: a goal further away than that has no month worth giving."""

Source = Literal["given", "target"]
_EVERY_MONTH_OR_WEEK = {1, 2}
"""YNAB's goal_cadence for a target repeated monthly or weekly."""


class GoalProjection(Model):
    """When a category reaches an amount, and what a month needs to reach it by a date."""

    message: str
    """The conclusion in one sentence or two, for the agent to relay."""
    category_id: str
    """YNAB id of the category."""
    name: str
    """Category name."""
    group: str
    """Name of the category's group."""
    target: str
    """The category's YNAB target as the user reads it, or "no target"."""
    goal_amount: float
    """The amount to reach, in currency units."""
    goal_source: Source
    """Where goal_amount comes from: given by the caller, or the category's YNAB target."""
    target_month: str | None
    """The month to reach it by, YYYY-MM; null when none was given and the YNAB target
    has no date, or was not the amount used."""
    target_month_source: Source | None
    """Where target_month comes from; null when there is none."""
    available: float
    """What the category holds today, as YNAB counts it."""
    missing: float
    """What is still missing: goal_amount less available, 0 when it is reached."""
    months: list[str]
    """The complete months averaged, YYYY-MM, oldest first."""
    assigned: float
    """Average assigned to the category per month over those months."""
    activity: float
    """Average activity of the category per month: spending negative, refunds positive."""
    contribution: float
    """Average growth of the balance per month, assigned plus activity: the pace used."""
    reached_month: str | None
    """The month the goal is reached at that pace, YYYY-MM; this month when already
    reached; null when the pace never gets there, or not within 600 months."""
    months_to_goal: int | None
    """Months after this one until it is reached; 0 when already reached; null with
    reached_month."""
    monthly_needed: float | None
    """What each month, this one included, needs to add to reach the goal by
    target_month, rounded up to the cent; null without target_month."""
    on_track: bool | None
    """True when the pace reaches the goal by target_month; null without target_month."""
    notes: list[str]
    """What the figures assume, to tell the user."""


def _months_between(start: str, end: str) -> int:
    """Count the months from one to another.

    Args:
        start: A month, YYYY-MM.
        end: A month, YYYY-MM.

    Returns:
        How many months end comes after start; negative when it comes before.
    """
    return (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7])


def _later(month: str, count: int) -> str:
    """Give the month a number of months after another.

    Args:
        month: A month, YYYY-MM.
        count: How many months later.

    Returns:
        That month, YYYY-MM.
    """
    year, index = divmod(int(month[:4]) * 12 + int(month[5:7]) - 1 + count, 12)
    return f"{year:04d}-{index + 1:02d}"


def check_target_month(month: str, today: date) -> None:
    """Refuse a target month that is malformed, gone, or too far ahead.

    Args:
        month: The month given, YYYY-MM.
        today: The day of the question.

    Raises:
        ValueError: If it is not YYYY-MM, before the current month, or more than 600
            months ahead.
    """
    try:
        year, number = (int(part) for part in month.split("-"))
        date(year, number, 1)
    except ValueError as error:
        raise ValueError(f"target_month must be a month as YYYY-MM, got {month!r}.") from error
    ahead = _months_between(f"{today:%Y-%m}", f"{year:04d}-{number:02d}")
    if ahead < 0:
        raise ValueError("target_month must be the current month or later.")
    if ahead > MAX_AHEAD:
        raise ValueError(f"target_month must be at most {MAX_AHEAD} months ahead.")


def one_category(categories: list[dict[str, Any]], wanted: str) -> dict[str, Any]:
    """Find the one category the caller named, by id or by name whatever the case.

    Args:
        categories: The month's categories, as YNAB returns them.
        wanted: A category name or id.

    Returns:
        The category.

    Raises:
        ValueError: If no category has that name or id, or several share the name, with
            their ids.
    """
    found = chosen_categories(categories, [wanted])
    if len(found) > 1:
        ids = ", ".join(f"{c['id']} ({untrusted(c.get('category_group_name'))})" for c in found)
        raise ValueError(
            f"{untrusted(wanted)!r} names {len(found)} categories: give the id of one: {ids}."
        )
    return found[0]


def balance_target(category: dict[str, Any]) -> tuple[int, str | None] | None:
    """Read a category's YNAB target as a balance to reach, if it is one.

    A monthly or weekly target asks for an amount each period, and monthly funding and
    debt payments too: none of them is a balance to reach.

    Args:
        category: A category as YNAB returns it, amounts in milliunits.

    Returns:
        The amount in milliunits and the month it is due by (YYYY-MM, or None), or None
        when the target is no balance to reach.
    """
    kind, amount = category.get("goal_type"), category.get("goal_target")
    due = category.get("goal_target_date")
    month = due[:7] if due else None
    if not amount:
        return None
    if kind in ("TB", "TBD"):
        return amount, month
    repeating = (
        category.get("goal_cadence") in _EVERY_MONTH_OR_WEEK
        and (category.get("goal_cadence_frequency") or 1) == 1
    )
    if kind == "NEED" and month and not repeating:
        return amount, month
    return None


def amount_to_reach(category: dict[str, Any], goal_amount: float | None) -> tuple[int, Source]:
    """Settle the amount to reach: the one given, or the category's YNAB target.

    Args:
        category: A category as YNAB returns it, amounts in milliunits.
        goal_amount: The amount given in currency units, or None.

    Returns:
        The amount in milliunits, and where it comes from.

    Raises:
        ValueError: If no amount is given and the category's YNAB target is no balance
            to reach.
    """
    if goal_amount is not None:
        return amount_to_milliunit(goal_amount), "given"
    target = balance_target(category)
    if target is None:
        raise ValueError(
            f"The target of {untrusted(category['name'])!r} is {describe(category)}: not a "
            "balance to reach. To project one, give goal_amount."
        )
    return target[0], "target"


def _cents_up(milliunits: int) -> int:
    """Round milliunits up to the cent.

    Args:
        milliunits: An amount, 0 or more.

    Returns:
        The amount rounded up to a multiple of 10.
    """
    return -(-milliunits // 10) * 10


def _message(  # pylint: disable=too-many-arguments
    name: str,
    *,
    goal: int,
    available: int,
    contribution: int,
    months: int,
    ahead: int | None,
    current: str,
) -> str:
    """Say when the goal is reached.

    Args:
        name: The category's name, made safe.
        goal: The amount to reach, in milliunits.
        available: What the category holds today, in milliunits.
        contribution: The average monthly growth, in milliunits.
        months: How many months were averaged.
        ahead: Months after this one until it is reached, or None.
        current: The current month, YYYY-MM.

    Returns:
        The message.
    """
    amount = milliunit_to_amount
    left = f"({amount(goal - available):.2f} still to go)"
    if available >= goal:
        return (
            f"{name} holds {amount(available):.2f}: the {amount(goal):.2f} goal is already reached."
        )
    if not months:
        return (
            f"No complete month to learn a pace from: no date can be given for {name} to "
            f"reach {amount(goal):.2f} {left}."
        )
    over = (
        "what the last complete month added"
        if months == 1
        else f"the average of the last {months} complete months"
    )
    pace = f"At {amount(contribution):.2f} a month, {over}"
    if ahead is None and contribution > 0:
        return (
            f"{pace}, {name} would take more than {MAX_AHEAD} months to reach "
            f"{amount(goal):.2f} {left}."
        )
    if ahead is None:
        return f"{pace}, {name} never reaches {amount(goal):.2f}."
    later = "1 month" if ahead == 1 else f"{ahead} months"
    return (
        f"{pace}, {name} would reach {amount(goal):.2f} in {_later(current, ahead)}, "
        f"{later} from now {left}."
    )


def _notes(
    *,
    months: int,
    asked: int,
    far: bool,
    date_aside: bool,
    passed: str | None,
) -> list[str]:
    """Write down what the figures assume.

    Args:
        months: How many months were averaged.
        asked: How many months the caller asked for.
        far: True when the pace would take more than MAX_AHEAD months.
        date_aside: True when the YNAB target's date was left aside for an amount given.
        passed: The YNAB target's date when it has passed, else None.

    Returns:
        The notes, one sentence or two each.
    """
    notes = [
        "contribution is the average, over the months listed, of what was assigned to the "
        "category plus its activity (spending negative, refunds positive): what its "
        "balance actually grew by. Assigned alone would ignore what is spent out of the "
        "category and promise too early a date; both are given apart.",
        "The projection starts from what the category holds today and adds contribution "
        "for each month after this one: what is still to be assigned this month counts "
        "as next month's.",
        "monthly_needed spreads what is missing evenly over the months from this one to "
        "target_month, both included, as YNAB counts a target by date.",
        "The past is no promise: a raise, a large purchase out of the category or a "
        "month assigned twice changes the pace.",
    ]
    if months < asked:
        notes.append(
            f"Only {months} of the {asked} months asked hold the category as a complete "
            "month: the average is over those."
        )
    if far:
        notes.append(
            f"At this pace the goal takes more than {MAX_AHEAD} months: no month is given."
        )
    if date_aside:
        notes.append(
            "The YNAB target's date goes with its own amount and was left aside for the "
            "amount given: pass target_month for a date."
        )
    if passed:
        notes.append(
            f"The YNAB target's date, {passed}, has passed: monthly_needed is all that is "
            "missing, this month."
        )
    return notes


def projection(  # pylint: disable=too-many-arguments,too-many-locals
    category: dict[str, Any],
    past: list[tuple[str, dict[str, Any]]],
    today: date,
    months_count: int,
    *,
    goal_amount: float | None,
    target_month: str | None,
) -> GoalProjection:
    """Project when a category reaches an amount, and what a month needs to reach it by a date.

    Args:
        category: The category this month, as YNAB returns it, amounts in milliunits.
        past: (YYYY-MM, the category that month) for each complete month it appears in,
            oldest first, with its budgeted and activity in milliunits.
        today: The day of the question.
        months_count: How many complete months the caller asked to average.
        goal_amount: The amount to reach in currency units, or None for the YNAB target's.
        target_month: The month to reach it by, YYYY-MM, already checked; or None for the
            YNAB target's date, when its amount is the one used.

    Returns:
        The goal, the pace, the month it is reached and what a month needs.

    Raises:
        ValueError: If no amount is given and the category's YNAB target is no balance
            to reach.
    """
    target = balance_target(category)
    goal, goal_source = amount_to_reach(category, goal_amount)
    by: str | None = target_month
    by_source: Source | None = "given" if target_month else None
    if by is None and goal_amount is None and target is not None and target[1]:
        by, by_source = target[1], "target"
    current = f"{today:%Y-%m}"
    available = category.get("balance") or 0
    missing = max(goal - available, 0)
    count = len(past)
    assigned = sum(month.get("budgeted") or 0 for _, month in past)
    activity = sum(month.get("activity") or 0 for _, month in past)
    contribution = round((assigned + activity) / count) if count else 0

    ahead: int | None = None
    if not missing:
        ahead = 0
    elif contribution > 0:
        ahead = -(-missing // contribution)
    far = ahead is not None and ahead > MAX_AHEAD
    if far:
        ahead = None
    reached = _later(current, ahead) if ahead is not None else None

    name = untrusted(category["name"])
    message = _message(
        name,
        goal=goal,
        available=available,
        contribution=contribution,
        months=count,
        ahead=ahead,
        current=current,
    )
    needed: int | None = None
    on_track: bool | None = None
    passed = None
    if by is not None:
        span = _months_between(current, by) + 1
        if span < 1:
            passed, span = by, 1
        needed = _cents_up(-(-missing // span))
        on_track = reached is not None and reached <= by
        if missing:
            over = "1 month" if span == 1 else f"{span} months"
            verdict = "gets there in time" if on_track else "is not enough"
            message += (
                f" To reach it by {by}: {milliunit_to_amount(needed):.2f} a month over "
                f"{over}; the current pace {verdict}."
            )
    return GoalProjection(
        message=message,
        category_id=category["id"],
        name=name,
        group=untrusted(category.get("category_group_name")),
        target=describe(category),
        goal_amount=milliunit_to_amount(goal),
        goal_source=goal_source,
        target_month=by,
        target_month_source=by_source,
        available=milliunit_to_amount(available),
        missing=milliunit_to_amount(missing),
        months=[month for month, _ in past],
        assigned=milliunit_to_amount(round(assigned / count) if count else 0),
        activity=milliunit_to_amount(round(activity / count) if count else 0),
        contribution=milliunit_to_amount(contribution),
        reached_month=reached,
        months_to_goal=ahead,
        monthly_needed=milliunit_to_amount(needed) if needed is not None else None,
        on_track=on_track,
        notes=_notes(
            months=count,
            asked=months_count,
            far=far,
            date_aside=goal_amount is not None
            and target is not None
            and target[1] is not None
            and target_month is None,
            passed=passed,
        ),
    )
