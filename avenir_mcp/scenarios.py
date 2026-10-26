# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""What if: the balance projection again, with the changes the user is weighing.

A scenario is a list of adjustments to the projection forecast_balance makes: a
category's usual spending changed, an amount added every month or once, a recurring
charge dropped. Each becomes what the projection already knows how to place: an amount
spread over a month's days, or an amount on a date. Sums are made in milliunits.
"""

from __future__ import annotations

import calendar
import datetime
from collections import defaultdict
from typing import Annotated, Any, Literal

from pydantic import Field

from avenir_mcp.amounts import MAX_AMOUNT, Amount
from avenir_mcp.classifier import normalize_payee
from avenir_mcp.client import amount_to_milliunit, milliunit_to_amount
from avenir_mcp.forecast import (
    VARIABLE_MONTHS,
    OneOff,
    Projection,
    Recurring,
    horizon,
    is_scheduled,
    months_before,
    usable,
)
from avenir_mcp.model import Model
from avenir_mcp.runway import lines_of
from avenir_mcp.schedule import Occurrence
from avenir_mcp.text import untrusted

MAX_SCENARIOS = 4
"""Scenarios compared in one call, besides the baseline: enough to weigh a choice."""
MAX_ADJUSTMENTS = 10
"""Adjustments in one scenario."""

Kind = Literal["change_spending", "add_monthly", "add_one_off", "drop_recurring"]
MonthText = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
"""A month, YYYY-MM."""
Wanted = Annotated[str, Field(min_length=1, max_length=200)]
"""A name or id the user gives."""

# The fields each kind needs, and those it may take besides.
_NEEDS: dict[str, frozenset[str]] = {
    "change_spending": frozenset({"category"}),
    "add_monthly": frozenset({"amount"}),
    "add_one_off": frozenset({"amount", "date"}),
    "drop_recurring": frozenset({"payee"}),
}
_TAKES: dict[str, frozenset[str]] = {
    "change_spending": frozenset({"percent", "monthly_spending", "start", "end", "label"}),
    "add_monthly": frozenset({"start", "end", "day", "label"}),
    "add_one_off": frozenset({"label"}),
    "drop_recurring": frozenset({"start", "end", "label"}),
}
_DEFAULT_LABELS = {"add_monthly": "monthly amount", "add_one_off": "one-off amount"}


class Adjustment(Model):
    """One change to the projection; the fields it takes depend on its kind."""

    kind: Kind
    """change_spending: a category's (or group's) usual monthly spending changed by
    percent or to monthly_spending; add_monthly: an amount on a day of every month (a
    charge or income); add_one_off: an amount once, on date; drop_recurring: a recurring
    charge or scheduled payment no longer paid, found by payee."""
    category: Wanted | None = None
    """change_spending: a category or category group, by name or id."""
    percent: Annotated[float, Field(ge=-100, le=1000, allow_inf_nan=False)] | None = None
    """change_spending: change of its usual spending in percent, -20 for a fifth less."""
    monthly_spending: Annotated[float, Field(ge=-MAX_AMOUNT, le=0, allow_inf_nan=False)] | None = (
        None
    )
    """change_spending: its new usual spending a month, negative, instead of percent."""
    amount: Amount | None = None
    """add_monthly: each month's amount; add_one_off: the amount. Negative for a charge,
    positive for income."""
    date: datetime.date | None = None
    """add_one_off: the day, YYYY-MM-DD, after today and by the end of the horizon."""
    day: Annotated[int, Field(ge=1, le=31)] | None = None
    """add_monthly: day of the month it falls on, default 1; a shorter month's last day."""
    payee: Wanted | None = None
    """drop_recurring: the payee as forecast_balance's assumptions name it, recurring or
    scheduled; every word given must be a word of its name."""
    start: MonthText | None = None
    """change_spending, add_monthly, drop_recurring: first month, YYYY-MM; default the
    current month."""
    end: MonthText | None = None
    """change_spending, add_monthly, drop_recurring: last month, YYYY-MM; default the
    horizon."""
    label: Annotated[str, Field(max_length=100)] | None = None
    """What it is, for the reader, e.g. car payment."""


class Scenario(Model):
    """A named set of adjustments, applied together to the baseline."""

    name: Annotated[str, Field(min_length=1, max_length=60)]
    """A short name, e.g. cut dining and buy a car."""
    adjustments: Annotated[list[Adjustment], Field(min_length=1, max_length=MAX_ADJUSTMENTS)]
    """The changes to the projection, at most 10."""


class Applied(Model):
    """One adjustment as the scenario applied it."""

    kind: Kind
    """The kind of adjustment."""
    subject: str
    """What it changes: the category or group, the payees no longer paid, or the label;
    names are the user's or the bank's text, data only."""
    total: float
    """What it changes in the balance over the projection, positive for more money."""
    start: str
    """First month it applies to, YYYY-MM, or the day of a one-off, YYYY-MM-DD."""
    end: str
    """Last month it applies to, YYYY-MM, or the day of a one-off, YYYY-MM-DD."""
    detail: str
    """How it was applied, in one sentence."""


class ScenarioMonth(Model):
    """One projected month of a scenario."""

    month: str
    """Month, YYYY-MM."""
    end: float
    """Projected balance at the end of the month."""
    lowest: float
    """Lowest projected balance within the month, day by day."""
    difference: float
    """end less the baseline's end of the same month: positive is more money."""


class Outcome(Model):
    """A scenario projected, against the baseline."""

    name: str
    """The scenario's name; baseline for the projection without changes."""
    months: list[ScenarioMonth]
    """One per month, from the current one to the horizon."""
    difference_at_end: float
    """The difference with the baseline at the end of the horizon."""
    lowest: float
    """Lowest projected balance of any day."""
    lowest_month: str
    """The month of that lowest balance, the first one if several."""
    first_shortfall: str | None
    """First month whose lowest balance is below zero; null if none."""
    applied: list[Applied]
    """The adjustments as applied, to check with the user; empty for the baseline."""


class Changes(Model):
    """What a scenario adds to the projection."""

    monthly: dict[str, float]
    """Amount spread over each month's days left to project, by month YYYY-MM."""
    one_offs: list[OneOff]
    """Amounts on their dates."""
    applied: list[Applied]
    """Each adjustment as applied."""


def _fields_given(adjustment: Adjustment) -> set[str]:
    """Name the fields of an adjustment that hold a value.

    Args:
        adjustment: One adjustment.

    Returns:
        The names of its fields besides kind that are not null.
    """
    return {name for name, value in adjustment if name != "kind" and value is not None}


def _check_fields(adjustment: Adjustment) -> None:
    """Refuse an adjustment missing a field its kind needs, or holding one it does not take.

    Args:
        adjustment: One adjustment.

    Raises:
        ValueError: If a field is missing or not taken, or change_spending has both or
            neither of percent and monthly_spending.
    """
    kind = adjustment.kind
    given = _fields_given(adjustment)
    missing = _NEEDS[kind] - given
    if missing:
        raise ValueError(f"{kind} needs {', '.join(sorted(missing))}.")
    extra = given - _NEEDS[kind] - _TAKES[kind]
    if extra:
        raise ValueError(f"{kind} does not take {', '.join(sorted(extra))}.")
    if kind == "change_spending" and (adjustment.percent is None) == (
        adjustment.monthly_spending is None
    ):
        raise ValueError("change_spending needs percent or monthly_spending, not both.")


def check(scenarios: list[Scenario], today: datetime.date, until: str) -> None:
    """Refuse what cannot be projected, before any request.

    Args:
        scenarios: The scenarios asked for.
        today: The day of the question.
        until: Last month projected, YYYY-MM, already checked.

    Raises:
        ValueError: If two scenarios share a name, an adjustment lacks a field its kind
            needs or holds one it does not take, a month is outside the horizon or an
            end comes before its start, or a one-off is dated outside the projection.
    """
    names: set[str] = set()
    current = f"{today:%Y-%m}"
    year, month = (int(part) for part in until.split("-"))
    last_day = datetime.date(year, month, calendar.monthrange(year, month)[1])
    for scenario in scenarios:
        name = scenario.name.strip().casefold()
        if name in names:
            raise ValueError(f"Scenario names must differ: {scenario.name!r} is given twice.")
        names.add(name)
        for adjustment in scenario.adjustments:
            _check_fields(adjustment)
            for value in (adjustment.start, adjustment.end):
                if value is not None and not current <= value <= until:
                    raise ValueError(
                        f"start and end must be months from {current} to {until}, got {value}."
                    )
            if adjustment.start and adjustment.end and adjustment.end < adjustment.start:
                raise ValueError(f"end {adjustment.end} comes before start {adjustment.start}.")
            when = adjustment.date
            if when is not None and not today < when <= last_day:
                raise ValueError(f"date must be after today and by the end of {until}, got {when}.")


def categories_named(categories: list[dict[str, Any]], wanted: str) -> tuple[str, set[str]]:
    """Find the category, or the group of categories, the user named.

    Args:
        categories: The plan's categories, each with its id, name, category_group_id and
            category_group_name; deleted ones are left out.
        wanted: A category or group, by id or by name whatever the case.

    Returns:
        The name to show, a group's marked "(group)", and the ids of its categories.

    Raises:
        ValueError: If no category or group has that id or name.
    """
    key = wanted.strip()
    folded = key.casefold()
    live = [c for c in categories if not c.get("deleted")]
    for field, group in (("id", False), ("category_group_id", True)):
        found = [c for c in live if c.get(field) == key]
        if found:
            return _named(found, group)
    for field, group in (("name", False), ("category_group_name", True)):
        found = [c for c in live if str(c.get(field) or "").strip().casefold() == folded]
        if found:
            return _named(found, group)
    raise ValueError(
        f"Unknown category or group {untrusted(wanted)!r}: give a name or id as "
        "get_category_balances or list_category_groups shows it."
    )


def _named(found: list[dict[str, Any]], group: bool) -> tuple[str, set[str]]:
    """Name what was found and give its categories' ids.

    Args:
        found: The categories found, at least one.
        group: True when they were found as a group.

    Returns:
        The name to show and the ids.
    """
    first = found[0]
    name = f"{untrusted(first['category_group_name'])} (group)" if group else first["name"]
    return untrusted(name), {c["id"] for c in found}


def usual_spending(
    transactions: list[dict[str, Any]], today: datetime.date, category_ids: set[str]
) -> int:
    """Average what some categories spent a month, over the months forecast averages.

    Args:
        transactions: The projected accounts' transactions, amounts in milliunits.
        today: The day of the question; its month is incomplete and not averaged.
        category_ids: The categories.

    Returns:
        Milliunits a month, negative for spending; refunds lower it.
    """
    months = set(months_before(today, VARIABLE_MONTHS))
    total: int = sum(
        line["amount"]
        for tx in transactions
        if usable(tx) and tx["date"][:7] in months
        for line in lines_of(tx)
        if line.get("category_id") in category_ids
    )
    return round(total / VARIABLE_MONTHS)


def _months(adjustment: Adjustment, today: datetime.date, until: str) -> list[tuple[int, int, str]]:
    """List the projected months an adjustment applies to.

    Args:
        adjustment: An adjustment applying month after month.
        today: The day of the question.
        until: Last month projected, YYYY-MM.

    Returns:
        (year, month, YYYY-MM) of each month from its start to its end.
    """
    first = adjustment.start or f"{today:%Y-%m}"
    last = adjustment.end or until
    months = [(year, month, f"{year:04d}-{month:02d}") for year, month in horizon(today, until)]
    return [item for item in months if first <= item[2] <= last]


def _cents(milliunits: float) -> int:
    """Round milliunits to whole cents.

    Args:
        milliunits: An amount in milliunits.

    Returns:
        The milliunits of the nearest whole cent.
    """
    return round(milliunits / 10) * 10


def _new_spending(  # pylint: disable=too-many-locals
    adjustment: Adjustment,
    today: datetime.date,
    until: str,
    history: list[dict[str, Any]],
    categories: list[dict[str, Any]],
) -> tuple[dict[str, int], Applied]:
    """Change a category's usual spending, spread over each month's days.

    Args:
        adjustment: A change_spending adjustment, checked.
        today: The day of the question.
        until: Last month projected, YYYY-MM.
        history: The projected accounts' transactions, amounts in milliunits.
        categories: The plan's categories.

    Returns:
        The milliunits added to each month, and the adjustment as applied.
    """
    subject, ids = categories_named(categories, adjustment.category or "")
    usual = usual_spending(history, today, ids)
    if adjustment.percent is not None:
        per_month = _cents(usual * adjustment.percent / 100)
        how = f"{adjustment.percent:+g} %"
    else:
        spending = adjustment.monthly_spending or 0.0
        per_month = _cents(amount_to_milliunit(spending) - usual)
        how = f"to {spending:.2f}"
    monthly: dict[str, int] = {}
    days = calendar.monthrange(today.year, today.month)[1]
    for year, month, label in _months(adjustment, today, until):
        current = (year, month) == (today.year, today.month)
        monthly[label] = _cents(per_month * (days - today.day) / days) if current else per_month
    measured = months_before(today, VARIABLE_MONTHS)
    detail = (
        f"Usual spending {milliunit_to_amount(usual):.2f} a month ({measured[0]} to "
        f"{measured[-1]}), {how}: {milliunit_to_amount(per_month):+.2f} a month, spread "
        "over each month's days; the current month counts for its days left."
    )
    return monthly, _applied(adjustment, subject, sum(monthly.values()), monthly, detail)


def _applied(
    adjustment: Adjustment, subject: str, total: int, monthly: dict[str, int], detail: str
) -> Applied:
    """Describe an adjustment applying month after month.

    Args:
        adjustment: The adjustment.
        subject: What it changes.
        total: What it changes in the balance, in milliunits.
        monthly: The months it applies to, as keys; at least one.
        detail: How it was applied.

    Returns:
        The adjustment as applied.
    """
    months = sorted(monthly)
    return Applied(
        kind=adjustment.kind,
        subject=subject,
        total=milliunit_to_amount(total),
        start=months[0],
        end=months[-1],
        detail=detail,
    )


def _monthly_amounts(
    adjustment: Adjustment, today: datetime.date, until: str
) -> tuple[list[OneOff], Applied]:
    """Add an amount on a day of every month.

    Args:
        adjustment: An add_monthly adjustment, checked.
        today: The day of the question; a day already past this month is not counted.
        until: Last month projected, YYYY-MM.

    Returns:
        The amounts on their dates, and the adjustment as applied.
    """
    label = adjustment.label or _DEFAULT_LABELS["add_monthly"]
    amount = adjustment.amount or 0.0
    wanted_day = adjustment.day or 1
    dates = []
    months: dict[str, int] = {}
    for year, month, key in _months(adjustment, today, until):
        months[key] = 0
        day = datetime.date(year, month, min(wanted_day, calendar.monthrange(year, month)[1]))
        if day > today:
            dates.append(OneOff(date=day, amount=amount, label=label))
    total = amount_to_milliunit(amount) * len(dates)
    detail = f"{amount:.2f} on day {wanted_day} of each month, from the first one after today."
    return dates, _applied(adjustment, label, total, months, detail)


def _left_out(  # pylint: disable=too-many-locals
    adjustment: Adjustment,
    today: datetime.date,
    until: str,
    recurring: list[Recurring],
    scheduled: list[Occurrence],
) -> tuple[list[OneOff], Applied]:
    """Leave out a recurring charge or a schedule, paying it back on each of its days.

    An amount of the opposite sign on the very day of each charge cancels it exactly,
    in the month's end and in its lowest day alike.

    Args:
        adjustment: A drop_recurring adjustment, checked.
        today: The day of the question.
        until: Last month projected, YYYY-MM.
        recurring: The recurring amounts the projection places.
        scheduled: The scheduled occurrences the projection places.

    Returns:
        The amounts that cancel the charges, and the adjustment as applied.

    Raises:
        ValueError: If no recurring charge or scheduled payment matches the payee.
    """
    wanted = frozenset({(normalize_payee(adjustment.payee or ""), True)})
    charges = [r for r in recurring if r.amount < 0 and is_scheduled(r.payee, True, wanted)]
    planned = [
        o
        for o in scheduled
        if o.amount < 0 and is_scheduled(normalize_payee(o.payee), True, wanted)
    ]
    if not charges and not planned:
        known = sorted(
            {r.payee for r in recurring if r.amount < 0}
            | {untrusted(o.payee) for o in scheduled if o.amount < 0}
        )
        raise ValueError(
            f"No recurring charge or scheduled payment matches {untrusted(adjustment.payee)!r}: "
            f"the projection's charges are {', '.join(known) or 'none'}."
        )
    months = _months(adjustment, today, until)
    label = f"without {adjustment.payee}"
    back: list[OneOff] = []
    for charge in charges:
        for year, month, _ in months:
            day = datetime.date(year, month, min(charge.day, calendar.monthrange(year, month)[1]))
            if day > today:
                back.append(OneOff(date=day, amount=-charge.amount, label=label))
    labels = {key for _, _, key in months}
    for item in planned:
        if item.date[:7] in labels:
            back.append(
                OneOff(
                    date=datetime.date.fromisoformat(item.date), amount=-item.amount, label=label
                )
            )
    subject = ", ".join(sorted({c.payee for c in charges} | {untrusted(o.payee) for o in planned}))
    total = sum(amount_to_milliunit(o.amount) for o in back)
    detail = f"{len(back)} payment(s) of {subject} left out: {milliunit_to_amount(total):+.2f}."
    return back, _applied(adjustment, subject, total, {key: 0 for key in labels}, detail)


def changes(  # pylint: disable=too-many-arguments,too-many-locals
    scenario: Scenario,
    *,
    today: datetime.date,
    until: str,
    history: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    recurring: list[Recurring],
    scheduled: list[Occurrence],
) -> Changes:
    """Turn a scenario's adjustments into what the projection places.

    Args:
        scenario: The scenario, checked by :func:`check`.
        today: The day of the question.
        until: Last month projected, YYYY-MM.
        history: The projected accounts' transactions, amounts in milliunits.
        categories: The plan's categories.
        recurring: The recurring amounts the baseline places.
        scheduled: The scheduled occurrences the baseline places.

    Returns:
        Amounts spread over months, amounts on dates, and each adjustment as applied.
    """
    monthly: dict[str, int] = defaultdict(int)
    one_offs: list[OneOff] = []
    applied: list[Applied] = []
    for adjustment in scenario.adjustments:
        if adjustment.kind == "change_spending":
            spread, done = _new_spending(adjustment, today, until, history, categories)
            for key, value in spread.items():
                monthly[key] += value
        elif adjustment.kind == "add_monthly":
            dates, done = _monthly_amounts(adjustment, today, until)
            one_offs += dates
        elif adjustment.kind == "add_one_off":
            when = adjustment.date or today
            amount = adjustment.amount or 0.0
            label = adjustment.label or _DEFAULT_LABELS["add_one_off"]
            one_offs.append(OneOff(date=when, amount=amount, label=label))
            done = Applied(
                kind="add_one_off",
                subject=label,
                total=amount,
                start=when.isoformat(),
                end=when.isoformat(),
                detail=f"{amount:.2f} on {when}.",
            )
        else:
            dates, done = _left_out(adjustment, today, until, recurring, scheduled)
            one_offs += dates
        applied.append(done)
    return Changes(
        monthly={key: milliunit_to_amount(value) for key, value in sorted(monthly.items())},
        one_offs=one_offs,
        applied=applied,
    )


def outcome(
    name: str, projection: Projection, baseline: Projection, applied: list[Applied]
) -> Outcome:
    """Set a projection against the baseline, month by month.

    Args:
        name: The scenario's name.
        projection: The scenario projected.
        baseline: The projection without changes, over the same months.
        applied: The adjustments as applied.

    Returns:
        The months with their difference, the lowest point and the first shortfall.
    """
    months = [
        ScenarioMonth(
            month=month.month,
            end=month.end,
            lowest=month.lowest,
            difference=milliunit_to_amount(
                amount_to_milliunit(month.end) - amount_to_milliunit(base.end)
            ),
        )
        for month, base in zip(projection.months, baseline.months, strict=True)
    ]
    low = min(months, key=lambda m: m.lowest)
    return Outcome(
        name=name,
        months=months,
        difference_at_end=months[-1].difference,
        lowest=low.lowest,
        lowest_month=low.month,
        first_shortfall=projection.first_shortfall,
        applied=applied,
    )


def _verdict(found: Outcome) -> str:
    """Say whether and when a projection goes below zero, and its lowest point.

    Args:
        found: A projection against the baseline.

    Returns:
        A clause for the message.
    """
    below = (
        f"below zero from {found.first_shortfall}" if found.first_shortfall else "never below zero"
    )
    return f"{below}, lowest {found.lowest:.2f} in {found.lowest_month}"


def message(baseline: Outcome, outcomes: list[Outcome], until: str) -> str:
    """Sum up the baseline and each scenario in a sentence each.

    Args:
        baseline: The projection without changes.
        outcomes: Each scenario against it.
        until: Last month projected, YYYY-MM.

    Returns:
        The message for the agent to relay.
    """
    parts = [f"Baseline: {_verdict(baseline)}."]
    parts += [
        f"{found.name}: {found.difference_at_end:+.2f} against the baseline by {until}, "
        f"{_verdict(found)}."
        for found in outcomes
    ]
    parts.append("This rests on the assumptions listed: check them with the user.")
    return " ".join(parts)
