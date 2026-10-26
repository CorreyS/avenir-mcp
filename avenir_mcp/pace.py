# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Where each category's spending is heading by the end of the month in progress.

A month's spending does not fall evenly: rent lands on the 3rd, groceries every week.
Each category is projected from its own rhythm: over the last complete months, the
share of a month's spending usually done by today's day of the month. What is spent so
far, divided by that share, gives the month's spending. When that share is small, a
division would magnify a few early payments, so what the rest of the month usually
holds is added instead; when the history is too thin, the days elapsed are used. What
YNAB has scheduled for the rest of the month is a floor. Sums are made in milliunits.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from typing import Any, Literal

from avenir_mcp.client import amount_to_milliunit, milliunit_to_amount
from avenir_mcp.forecast import months_before
from avenir_mcp.model import Model
from avenir_mcp.runway import by_name_or_id, chosen_groups, lines_of
from avenir_mcp.schedule import occurrences
from avenir_mcp.text import untrusted
from avenir_mcp.triage import CARD_GROUP, INTERNAL_GROUP

MAX_MONTHS = 24
"""Two years, as get_spending_trends: older months say little about today's rhythm."""
MIN_MONTHS_SEEN = 3
"""Months with spending a category needs before its history is trusted."""
MIN_SHARE = 0.25
"""Below this share, dividing by it would multiply what is spent by more than four."""

Method = Literal["history_share", "history_rest", "linear", "scheduled"]


class CategoryPace(Model):
    """A category's month so far and where it is heading, in currency units."""

    category_id: str
    """YNAB id of the category."""
    name: str
    """Category name."""
    group: str
    """Name of the category's group."""
    budgeted: float
    """Amount assigned to the category this month."""
    available: float
    """What the category has left today, as YNAB counts it; negative when overspent."""
    spent: float
    """Money out of the category so far this month, negative; refunds not deducted."""
    scheduled: float
    """What YNAB has scheduled out of the category from today to the end of the month,
    negative; 0 when nothing is."""
    projected_spent: float
    """Money out of the category projected for the whole month, negative."""
    projected_available: float
    """What the category would have left at the end of the month: available, less the
    spending still to come."""
    likely_over: bool
    """True when the category is projected to end the month below zero."""
    shortfall: float
    """How far below zero it is projected to end, as a positive number; 0 when it is not."""
    method: Method
    """How the month's spending was projected: history_share (spent divided by the share
    usually spent by this day), history_rest (spent plus what the rest of the month
    usually holds), linear (spent scaled by the days elapsed, the history being too
    thin), scheduled (spent plus what YNAB has scheduled, which came to more)."""
    typical_share: float | None
    """Share of a month's spending usually done by this day, 0 to 1; null when the
    history is too thin to say."""


class Pace(Model):
    """The month in progress, category by category, and which ones will run out."""

    message: str
    """The conclusion in one sentence or two, for the agent to relay."""
    month: str
    """The month, YYYY-MM-01."""
    day: int
    """Today's day of the month."""
    days_in_month: int
    """How many days the month has."""
    months: list[str]
    """The complete months whose rhythm was learnt, YYYY-MM, oldest first."""
    categories: list[CategoryPace]
    """The categories likely over first, the largest shortfall first, then the others,
    the least projected to be left first; up to the limit."""
    more: int
    """Categories beyond the limit, not listed; the totals count them."""
    likely_over: int
    """How many categories are projected to end the month below zero."""
    shortfall: float
    """What those categories are projected to be short of, together."""
    notes: list[str]
    """How the figures were projected, to tell the user."""


def _usable(category: dict[str, Any]) -> bool:
    """Tell whether a category is one of the user's own, to project.

    Args:
        category: A category of the month, as YNAB returns it.

    Returns:
        False for a hidden or deleted category, YNAB's internal ones and the categories
        that pay credit cards, whose activity is not spending.
    """
    return (
        not category.get("hidden")
        and not category.get("deleted")
        and category.get("category_group_name") not in (INTERNAL_GROUP, CARD_GROUP)
    )


def chosen_categories(categories: list[dict[str, Any]], wanted: list[str]) -> list[dict[str, Any]]:
    """Find the categories the caller named, by id or by name whatever the case.

    Args:
        categories: The month's categories, as YNAB returns them.
        wanted: Category names or ids, as the caller gave them.

    Returns:
        The categories named, in the order given, each once; a name two categories
        share gives both.

    Raises:
        ValueError: If a name or id is not one of the plan's categories, with the
            categories it could be.
    """
    usable = [c for c in categories if _usable(c)]
    found, unknown = by_name_or_id(usable, wanted)
    if unknown:
        given = ", ".join(repr(untrusted(name)) for name in unknown)
        names = ", ".join(untrusted(c["name"]) for c in usable)
        raise ValueError(
            f"Unknown category(ies) {given}: give names or ids of this plan's categories, "
            f"as get_category_balances shows them: {names}."
        )
    return found


def chosen(
    categories: list[dict[str, Any]], names: list[str] | None, groups: list[str] | None
) -> set[str] | None:
    """Gather the ids of the categories named, and of every category of the groups named.

    Args:
        categories: The month's categories, as YNAB returns them.
        names: Category names or ids, or None.
        groups: Category group names or ids, or None.

    Returns:
        The ids chosen; None when neither was given, to keep every category.

    Raises:
        ValueError: If a category or a group is not one of the plan's.
    """
    if names is None and groups is None:
        return None
    ids = {c["id"] for c in chosen_categories(categories, names or [])}
    tree: dict[str, dict[str, Any]] = {}
    for category in filter(_usable, categories):
        group = tree.setdefault(
            category["category_group_id"],
            {
                "id": category["category_group_id"],
                "name": category["category_group_name"],
                "category_ids": [],
            },
        )
        group["category_ids"].append(category["id"])
    for group in chosen_groups(list(tree.values()), groups or []):
        ids.update(group["category_ids"])
    return ids


def scheduled_rest(scheduled: list[dict[str, Any]], today: date) -> dict[str, int]:
    """Add up what YNAB has scheduled out of each category from today to the month's end.

    Args:
        scheduled: The plan's scheduled transactions, as YNAB returns them.
        today: The day of the question.

    Returns:
        Milliunits out by category id, as positive numbers; a split counts each line,
        money in and lines without a category are left out.
    """
    end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    items = {item["id"]: item for item in scheduled}
    rest: dict[str, int] = defaultdict(int)
    for occurrence in occurrences(scheduled, {}, {}, today, end):
        for line in lines_of(items[occurrence.scheduled_id]):
            if line["amount"] < 0 and line.get("category_id"):
                rest[line["category_id"]] -= line["amount"]
    return dict(rest)


def _outflows(transactions: list[dict[str, Any]]) -> list[tuple[str, str, int]]:
    """List the money out of each category, line by line.

    Args:
        transactions: The plan's transactions, amounts in milliunits.

    Returns:
        (date, category id, milliunits out as a positive number) for each line.
    """
    found = []
    for tx in transactions:
        if tx.get("deleted"):
            continue
        for line in lines_of(tx):
            if line["amount"] < 0 and line.get("category_id"):
                found.append((tx["date"], line["category_id"], -line["amount"]))
    return found


def _cutoff(month: str, today: date) -> int:
    """Give the last day of a past month that matches today's point in the month.

    Args:
        month: A past month, YYYY-MM.
        today: The day of the question.

    Returns:
        Today's day, or the month's last day when it is shorter, or when today is the
        last day of its own month.
    """
    length = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
    if today.day == calendar.monthrange(today.year, today.month)[1]:
        return length
    return min(today.day, length)


class _History:  # pylint: disable=too-few-public-methods
    """What each category spent in the past months, in all and by this day of the month."""

    def __init__(self, outflows: list[tuple[str, str, int]], months: list[str], today: date):
        """Add up each category's spending over the months.

        Args:
            outflows: (date, category id, milliunits out) for each line.
            months: The past months to learn from, YYYY-MM.
            today: The day of the question.
        """
        cutoffs = {month: _cutoff(month, today) for month in months}
        self.total: dict[str, int] = defaultdict(int)
        self.by_today: dict[str, int] = defaultdict(int)
        seen: dict[str, set[str]] = defaultdict(set)
        for day, category, out in outflows:
            month = day[:7]
            if month not in cutoffs:
                continue
            self.total[category] += out
            seen[category].add(month)
            if int(day[8:10]) <= cutoffs[month]:
                self.by_today[category] += out
        self.seen = {category: len(found) for category, found in seen.items()}
        # A category is averaged from the first month it spent in: the months before it
        # existed would make what it usually spends look smaller than it is.
        self.span = {
            category: len([month for month in months if month >= min(found)])
            for category, found in seen.items()
        }


def _project(
    category_id: str, spent: int, history: _History, today: date
) -> tuple[int, Method, float | None]:
    """Project a category's spending for the whole month, before what is scheduled.

    Args:
        category_id: The category.
        spent: Its money out so far this month, in milliunits, positive.
        history: The past months' spending.
        today: The day of the question.

    Returns:
        The month's projected money out in milliunits, positive; the method; the share
        usually spent by today, or None when the history is too thin.
    """
    if history.seen.get(category_id, 0) < MIN_MONTHS_SEEN:
        days = calendar.monthrange(today.year, today.month)[1]
        return round(spent * days / today.day), "linear", None
    total, by_today = history.total[category_id], history.by_today[category_id]
    share = by_today / total
    if share >= MIN_SHARE:
        return round(spent * total / by_today), "history_share", round(share, 2)
    rest = round((total - by_today) / history.span[category_id])
    return spent + rest, "history_rest", round(share, 2)


def _line(
    category: dict[str, Any], spent: int, scheduled: int, history: _History, today: date
) -> CategoryPace:
    """Project one category to the end of the month.

    Args:
        category: The category of the month, as YNAB returns it, amounts in milliunits.
        spent: Its money out so far this month, in milliunits, positive.
        scheduled: What is scheduled out of it for the rest of the month, positive.
        history: The past months' spending.
        today: The day of the question.

    Returns:
        The category's line.
    """
    projected, method, share = _project(category["id"], spent, history, today)
    if spent + scheduled > projected:
        projected, method = spent + scheduled, "scheduled"
    available = category.get("balance") or 0
    left = available - (projected - spent)
    return CategoryPace(
        category_id=category["id"],
        name=untrusted(category["name"]),
        group=untrusted(category.get("category_group_name")),
        budgeted=milliunit_to_amount(category.get("budgeted") or 0),
        available=milliunit_to_amount(available),
        spent=milliunit_to_amount(-spent),
        scheduled=milliunit_to_amount(-scheduled),
        projected_spent=milliunit_to_amount(-projected),
        projected_available=milliunit_to_amount(left),
        likely_over=left < 0,
        shortfall=milliunit_to_amount(max(-left, 0)),
        method=method,
        typical_share=share,
    )


def _message(month: str, day: int, days: int, over: list[CategoryPace], short: int) -> str:
    """Say which categories will run out, if any.

    Args:
        month: The month, YYYY-MM.
        day: Today's day of the month.
        days: How many days the month has.
        over: The categories likely over, the largest shortfall first.
        short: What they are short of together, in milliunits.

    Returns:
        The message.
    """
    head = f"Day {day} of {days} of {month}: "
    if not over:
        return head + (
            "at this pace, every category keeps within what it has until the end of the month."
        )
    count = "1 category is" if len(over) == 1 else f"{len(over)} categories are"
    names = ", ".join(f"{line.name} ({line.shortfall:.2f} short)" for line in over)
    return head + (
        f"{count} likely to run out by the end of the month, "
        f"{milliunit_to_amount(short):.2f} short in all: {names}."
    )


def _notes(months: list[str], asked: int, day: int, hidden: int) -> list[str]:
    """Write down how the figures were projected.

    Args:
        months: The past months learnt from.
        asked: How many months the caller asked for.
        day: Today's day of the month.
        hidden: Categories beyond the limit.

    Returns:
        The notes, one sentence or two each.
    """
    notes = [
        "spent is the money out of each category so far this month; refunds and other "
        "money in are not deducted from it, but are in available, which is YNAB's figure.",
        f"Each category follows its own rhythm: typical_share is the share of a month's "
        f"spending it usually has done by day {day}, over the months listed. history_share "
        "divides what is spent by that share; history_rest, used when that share is under "
        f"{MIN_SHARE:.0%}, adds what the rest of the month usually holds instead; linear, "
        f"used when fewer than {MIN_MONTHS_SEEN} of the months hold spending in the "
        "category, scales what is spent by the days elapsed.",
        "What YNAB has scheduled out of a category from today to the end of the month is a "
        "floor: when spent plus scheduled comes to more, it is the projection (method "
        "scheduled).",
        "The pace is no promise: a large purchase early in the month makes it look faster "
        "than it is, and a bill paid late slower.",
    ]
    if not months:
        notes.append("No complete month of history: every category is projected by the days.")
    elif len(months) < asked:
        notes.append(
            f"Only the last {len(months)} of the {asked} months hold transactions: the "
            "rhythm is learnt from those."
        )
    if hidden:
        notes.append(f"{hidden} more categories are not listed; the totals count them.")
    return notes


def summary(  # pylint: disable=too-many-arguments,too-many-locals
    categories: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    today: date,
    months_count: int,
    *,
    scheduled: dict[str, int],
    only: set[str] | None = None,
    limit: int | None = None,
) -> Pace:
    """Project each category's spending to the end of the month in progress.

    Args:
        categories: The month's categories, as YNAB returns them, amounts in milliunits.
        transactions: The plan's transactions, amounts in milliunits.
        today: The day of the question, in the month projected.
        months_count: How many complete months to learn from, at most; months before
            the plan's first transaction are not.
        scheduled: What is scheduled out of each category for the rest of the month,
            in milliunits, as scheduled_rest gives it.
        only: The ids of the categories to project; None for every category.
        limit: How many categories to list at most; None lists them all.

    Returns:
        Each category's month so far and where it is heading, those likely over first.
    """
    # The sentinel only has to keep every month out when the plan holds no transaction at
    # all: any text sorting after a YYYY-MM month does that. On its own line, so the
    # pragma covers no more than the sentinel.
    none_yet = "9999-12"  # pragma: no mutate
    first = min((tx["date"][:7] for tx in transactions if not tx.get("deleted")), default=none_yet)
    months = [month for month in months_before(today, months_count) if month >= first]
    outflows = _outflows(transactions)
    history = _History(outflows, months, today)
    current = f"{today:%Y-%m}"
    spent: dict[str, int] = defaultdict(int)
    for day, category, out in outflows:
        if day[:7] == current:
            spent[category] += out
    lines = [
        _line(c, spent[c["id"]], scheduled.get(c["id"], 0), history, today)
        for c in categories
        if _usable(c) and (only is None or c["id"] in only)
    ]
    lines = [
        line
        for line in lines
        if line.budgeted or line.available or line.spent or line.projected_spent
    ]
    lines.sort(key=lambda line: (not line.likely_over, -line.shortfall, line.projected_available))
    over = [line for line in lines if line.likely_over]
    short = sum(amount_to_milliunit(line.shortfall) for line in over)
    shown = lines[:limit]
    days = calendar.monthrange(today.year, today.month)[1]
    return Pace(
        message=_message(current, today.day, days, over, short),
        month=f"{current}-01",
        day=today.day,
        days_in_month=days,
        months=months,
        categories=shown,
        more=len(lines) - len(shown),
        likely_over=len(over),
        shortfall=milliunit_to_amount(short),
        notes=_notes(months, months_count, today.day, len(lines) - len(shown)),
    )
