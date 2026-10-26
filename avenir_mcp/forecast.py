# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Project an account's balance month by month.

Three sources, each an assumption the caller can see and change: charges that
recur in the history, the average of everything else, and what the caller
expects (monthly income, one-off amounts). How much everything else varied from
month to month gives each month-end a range. Sums are made in milliunits.
"""

from __future__ import annotations

import calendar
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from typing import Any

from avenir_mcp.amounts import Amount
from avenir_mcp.classifier import normalize_payee
from avenir_mcp.client import amount_to_milliunit, milliunit_to_amount
from avenir_mcp.model import Model
from avenir_mcp.text import untrusted

LOOKBACK_MONTHS = 4
MIN_MONTHS_SEEN = 3
AMOUNT_TOLERANCE = 0.2
VARIABLE_MONTHS = 3

# Words of a payee name: letters and digits, whatever the punctuation between them.
_WORD = re.compile(r"[A-Z0-9]+")


class Recurring(Model):
    """A charge (or income) seen most months at about the same amount."""

    payee: str
    """Normalised payee name."""
    amount: float
    """Median monthly amount, negative for a charge, positive for income."""
    day: int
    """Median day of the month it falls on."""
    months_seen: int
    """How many of the last 4 full months it appeared in."""


class OneOff(Model):
    """An amount expected once, on a date."""

    date: date
    """Day it is expected, YYYY-MM-DD."""
    amount: Amount
    """Amount, negative for a payment, positive for money received."""
    label: str
    """What it is, for the reader."""


class MonthProjection(Model):
    """One projected month."""

    month: str
    """Month, YYYY-MM."""
    start: float
    """Projected balance on the first day (today's balance for the current month)."""
    inflows: float
    """Money expected in during the month."""
    outflows: float
    """Money expected out during the month, negative."""
    end: float
    """Projected balance at the end of the month."""
    lowest: float
    """Lowest projected balance within the month, day by day."""
    end_low: float | None = None
    """Low end of the range around `end`: one deviation below it (see band); null when
    the history is too short to make a range."""
    end_high: float | None = None
    """High end of the range around `end`: one deviation above it; null when the history
    is too short to make a range."""


class Projection(Model):
    """The projected months and the first one where money runs out."""

    months: list[MonthProjection]
    """One projection per month, from the current month to the horizon."""
    first_shortfall: str | None
    """First month whose lowest balance is below zero; null if none."""


class Band(Model):
    """How far each month-end could stray from the projection, from how the past months did."""

    deviation: float | None
    """One standard deviation of a month's other spending and income, net, over the months
    measured, in currency units; null when the history is too short."""
    months: list[str]
    """The complete months measured, YYYY-MM, oldest first."""
    first_possible_shortfall: str | None
    """First month whose lowest balance, less the range's half-width at its end, is below
    zero: the balance could go below zero as early as this; null if none or no range."""
    note: str
    """How the range was made, or why there is none, for the agent to relay."""


def months_before(today: date, count: int) -> list[str]:
    """List the full months before today's month.

    Args:
        today: The day the forecast is made.
        count: How many months.

    Returns:
        The months as YYYY-MM, oldest first.
    """
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        months.append(f"{year:04d}-{month:02d}")
    return sorted(months)


def held_months(months: list[dict[str, Any]], today: date, count: int) -> list[dict[str, Any]]:
    """Keep the last months a plan holds up to today's, oldest first.

    YNAB lists a plan's months with those budgeted ahead, which have nothing spent in
    them yet, and its documentation promises no order; a deleted month holds nothing
    either. Taking the end of the list as it comes would answer with an empty month and
    drop one that has gone by.

    Args:
        months: The plan's months as YNAB lists them, each with its month.
        today: The day of the question; months after its month are left out.
        count: How many months, at most, the current one included.

    Returns:
        The months kept, oldest first.
    """
    current = f"{today:%Y-%m}"
    return sorted(
        (m for m in months if not m.get("deleted") and str(m["month"])[:7] <= current),
        key=lambda m: str(m["month"]),
    )[-count:]


def lookback(today: date) -> list[str]:
    """List the full months recurring charges are looked for in.

    Args:
        today: The day of the question; its month is incomplete and not looked at.

    Returns:
        The last LOOKBACK_MONTHS full months, as YYYY-MM, oldest first.
    """
    return months_before(today, LOOKBACK_MONTHS)


def usable(tx: dict[str, Any]) -> bool:
    """Tell whether a transaction is money in or out of the accounts.

    Args:
        tx: A YNAB transaction.

    Returns:
        False for a deleted transaction or a transfer between accounts.
    """
    return not tx.get("deleted") and not tx.get("transfer_account_id")


def _is_outflow(amount: float) -> bool:
    """Tell which direction an amount goes, in milliunits or in currency units.

    Args:
        amount: A transaction amount or a monthly average.

    Returns:
        True for money out, False for money in; an amount of exactly zero is money in.
    """
    return amount < 0


def _merchant(tx: dict[str, Any]) -> str:
    """Reduce a transaction's bank label to the merchant it names.

    Args:
        tx: A YNAB transaction.

    Returns:
        The merchant, empty when the transaction names no payee.
    """
    # The empty default only marks "no payee". It is compared with what normalize_payee
    # leaves of a bank label, so another default would change a projection only for a
    # label reducing to exactly that string: nothing a test could state about a
    # forecast. On its own line, so the pragma covers no more than this one lookup.
    label = tx.get("payee_name") or ""  # pragma: no mutate
    return normalize_payee(label)


def _day_of(tx: dict[str, Any]) -> int:
    """Read the day of the month a transaction falls on.

    Args:
        tx: A YNAB transaction.

    Returns:
        The day, 1 to 31.
    """
    # YNAB dates a transaction with an ISO date, which is 10 characters long, so a
    # slice reaching further reads those same two digits. On its own line, so the
    # pragma covers no more than this one slice.
    return int(tx["date"][8:10])  # pragma: no mutate


def recurring(transactions: list[dict[str, Any]], today: date) -> list[Recurring]:
    """Find the payees seen in 3 of the last 4 full months, each month within 20 % of the median.

    Args:
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made; its month is incomplete and not looked at.

    Returns:
        One recurring charge or income per payee and direction, sorted by payee.
    """
    months = set(lookback(today))
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for tx in transactions:
        if usable(tx) and tx["date"][:7] in months:
            payee = _merchant(tx)
            if payee:
                groups[(payee, _is_outflow(tx["amount"]))].append(tx)
    found: list[Recurring] = []
    for (payee, _), txs in sorted(groups.items()):
        per_month: dict[str, int] = defaultdict(int)
        for tx in txs:
            # The slice is only a grouping key, and YNAB dates a transaction with an ISO
            # date, so a slice reaching further groups the very same transactions. On its
            # own line, so the pragma covers no more than this one slice.
            month = tx["date"][:7]  # pragma: no mutate
            per_month[month] += tx["amount"]
        if len(per_month) < MIN_MONTHS_SEEN:
            continue
        median = statistics.median(per_month.values())
        if all(abs(v - median) <= abs(median) * AMOUNT_TOLERANCE for v in per_month.values()):
            found.append(
                Recurring(
                    payee=untrusted(payee),
                    amount=milliunit_to_amount(round(median)),
                    day=int(statistics.median(_day_of(tx) for tx in txs)),
                    months_seen=len(per_month),
                )
            )
    return found


def is_scheduled(payee: str, outflow: bool, scheduled: frozenset[tuple[str, bool]]) -> bool:
    """Tell whether a bank payee is one a schedule already projects.

    A schedule names its payee briefly ("ACME PAYROLL"), while the bank label of
    the same payee often says more ("ACME PAYROLL - ACME PAYROLL - REF-FF01"). A
    schedule covers a payee when every word of its name is a word of the label,
    in the same direction of money.

    Args:
        payee: The bank payee, normalised.
        outflow: True for money out.
        scheduled: (normalised payee, is money out) pairs of the schedules.

    Returns:
        True when one of the schedules covers this payee.
    """
    words = set(_WORD.findall(payee))
    return any(
        out == outflow and (name == payee or (name_words and name_words <= words))
        for name, out in scheduled
        if (name_words := set(_WORD.findall(name)))
    )


def _other_totals(
    transactions: list[dict[str, Any]],
    today: date,
    known: list[Recurring],
    outflow: bool,
    also: frozenset[tuple[str, bool]] = frozenset(),
) -> dict[str, int]:
    """Sum one direction of money per month over the last 3 full months, recurring amounts excluded.

    Args:
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made.
        known: Recurring amounts found by :func:`recurring`, left out of the sums.
        outflow: True for money out, False for money in.
        also: Other (normalised payee, is money out) pairs to leave out, e.g. the
            payees of scheduled transactions, which are projected apart.

    Returns:
        The milliunits of each month, YYYY-MM; a month with nothing is absent.
    """
    months = set(months_before(today, VARIABLE_MONTHS))
    recurring_payees = {r.payee for r in known if _is_outflow(r.amount) == outflow}
    totals: dict[str, int] = defaultdict(int)
    for tx in transactions:
        if (
            usable(tx)
            and _is_outflow(tx["amount"]) == outflow
            and tx["date"][:7] in months
            and _merchant(tx) not in recurring_payees
            and not is_scheduled(_merchant(tx), outflow, also)
        ):
            totals[tx["date"][:7]] += tx["amount"]
    return totals


def _other_average(
    transactions: list[dict[str, Any]],
    today: date,
    known: list[Recurring],
    outflow: bool,
    also: frozenset[tuple[str, bool]] = frozenset(),
) -> float:
    """Average one direction of money over the last 3 full months, recurring amounts excluded.

    Args:
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made.
        known: Recurring amounts found by :func:`recurring`, left out of the average.
        outflow: True for money out, False for money in.
        also: Other (normalised payee, is money out) pairs to leave out, e.g. the
            payees of scheduled transactions, which are projected apart.

    Returns:
        The monthly average in currency units, negative for money out.
    """
    total = sum(_other_totals(transactions, today, known, outflow, also).values())
    return milliunit_to_amount(round(total / VARIABLE_MONTHS))


def variable_average(
    transactions: list[dict[str, Any]],
    today: date,
    known: list[Recurring],
    also: frozenset[tuple[str, bool]] = frozenset(),
) -> float:
    """Average the monthly outflow over the last 3 full months, recurring charges excluded.

    Args:
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made.
        known: Recurring amounts found by :func:`recurring`, left out of the average.
        also: Other (normalised payee, is money out) pairs to leave out, e.g. the
            payees of scheduled transactions, which are projected apart.

    Returns:
        The monthly average in currency units, negative.
    """
    return _other_average(transactions, today, known, outflow=True, also=also)


def income_average(
    transactions: list[dict[str, Any]],
    today: date,
    known: list[Recurring],
    also: frozenset[tuple[str, bool]] = frozenset(),
) -> float:
    """Average the monthly inflow over the last 3 full months, recurring income excluded.

    Args:
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made.
        known: Recurring amounts found by :func:`recurring`, left out of the average.
        also: Other (normalised payee, is money out) pairs to leave out, e.g. the
            payees of scheduled transactions, which are projected apart.

    Returns:
        The monthly average in currency units.
    """
    return _other_average(transactions, today, known, outflow=False, also=also)


def month_to_date(
    transactions: list[dict[str, Any]],
    today: date,
    known: list[Recurring],
    also: frozenset[tuple[str, bool]] = frozenset(),
) -> tuple[float, float]:
    """Sum what was spent and received since the 1st of this month, recurring amounts excluded.

    Args:
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made.
        known: Recurring amounts found by :func:`recurring`, left out of the average.
        also: Other (normalised payee, is money out) pairs to leave out, e.g. the
            payees of scheduled transactions, which are projected apart.

    Returns:
        (spent, received) in currency units; spent is negative.
    """
    this_month = today.isoformat()[:7]
    recurring_payees = {(r.payee, _is_outflow(r.amount)) for r in known}
    spent = received = 0
    for tx in transactions:
        if not usable(tx) or tx["date"][:7] != this_month:
            continue
        key = (_merchant(tx), _is_outflow(tx["amount"]))
        if key in recurring_payees or is_scheduled(*key, also):
            continue
        if _is_outflow(tx["amount"]):
            spent += tx["amount"]
        else:
            received += tx["amount"]
    return milliunit_to_amount(spent), milliunit_to_amount(received)


def horizon(today: date, until: str) -> list[tuple[int, int]]:
    """List the months from today's month to the horizon.

    Args:
        today: The day the forecast is made.
        until: Last month, YYYY-MM.

    Returns:
        (year, month) pairs, in order.
    """
    year, month = today.year, today.month
    end_year, end_month = (int(part) for part in until.split("-"))
    months = []
    while (year, month) <= (end_year, end_month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _spread(total: int, first_day: int, last_day: int) -> dict[int, int]:
    """Split milliunits evenly over days, in whole cents, summing exactly.

    Args:
        total: The amount to spread, in milliunits.
        first_day: First day of the month to receive a share.
        last_day: Last day of the month to receive a share.

    Returns:
        The amount falling on each day; empty when there is nothing or no day.
    """
    days = last_day - first_day + 1
    if days <= 0 or not total:
        return {}
    cents = total // 10
    base, extra = divmod(cents, days)
    return {first_day + i: (base + (1 if i < extra else 0)) * 10 for i in range(days)}


def _dated(  # pylint: disable=too-many-arguments
    recurring: list[Recurring],  # pylint: disable=redefined-outer-name
    one_offs: list[OneOff],
    *,
    label: str,
    first_day: int,
    days: int,
    today: date,
) -> dict[int, int]:
    """Place recurring amounts and one-offs on their days of one month.

    Args:
        recurring: Recurring amounts; only those on or after first_day count.
        one_offs: One-off amounts; only those in this month and after today count.
        label: The month, YYYY-MM.
        first_day: First day still to project.
        days: Number of days in the month; a recurring day beyond it falls on the last.
        today: The day the forecast is made.

    Returns:
        Milliunits falling on each day.
    """
    dated: dict[int, int] = defaultdict(int)
    for r in recurring:
        if r.day >= first_day:
            dated[min(r.day, days)] += amount_to_milliunit(r.amount)
    for o in one_offs:
        if o.date.isoformat().startswith(label) and o.date > today:
            dated[o.date.day] += amount_to_milliunit(o.amount)
    return dated


def _walk(
    label: str, balance: int, first_day: int, days: int, daily: list[dict[int, int]]
) -> tuple[MonthProjection, int]:
    """Walk one month day by day from first_day.

    Args:
        label: The month, YYYY-MM.
        balance: Balance at the start, in milliunits.
        first_day: First day to walk.
        days: Number of days in the month.
        daily: Amounts per day, in milliunits, one mapping per source.

    Returns:
        The month's projection, and its closing balance in milliunits.
    """
    running = lowest = balance
    for day in range(first_day, days + 1):
        running += sum(flow.get(day, 0) for flow in daily)
        lowest = min(lowest, running)
    amounts = [amount for flow in daily for amount in flow.values()]
    month = MonthProjection(
        month=label,
        start=milliunit_to_amount(balance),
        inflows=milliunit_to_amount(sum(max(a, 0) for a in amounts)),
        outflows=milliunit_to_amount(sum(min(a, 0) for a in amounts)),
        end=milliunit_to_amount(running),
        lowest=milliunit_to_amount(lowest),
    )
    return month, running


def project(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    start_balance: float,
    today: date,
    until: str,
    recurring: list[Recurring],  # pylint: disable=redefined-outer-name
    variable_monthly: float,
    monthly_income: float,
    one_offs: list[OneOff],
    spent_this_month: float = 0.0,
    received_this_month: float = 0.0,
    monthly_changes: Mapping[str, float] | None = None,
) -> Projection:
    """Walk day by day from tomorrow to the end of `until` ("YYYY-MM").

    Variable spending and income are spread evenly over the days; recurring
    amounts and one-offs fall on their dates. For the current month, only what
    is left is projected: the averages minus what was already spent
    (`spent_this_month`, negative) and received (`received_this_month`), and
    recurring amounts dated after today.

    Args:
        start_balance: Today's balance, in currency units.
        today: The day the forecast is made.
        until: Last month to project, YYYY-MM.
        recurring: Recurring charges and income, on their days.
        variable_monthly: Monthly spending besides recurring charges, negative.
        monthly_income: Monthly income besides recurring income.
        one_offs: Amounts expected once, on their dates.
        spent_this_month: Already spent since the 1st, negative.
        received_this_month: Already received since the 1st.
        monthly_changes: Amounts by month, YYYY-MM, spread evenly over the days of the
            month still to project, such as a what-if change to a category's spending.

    Returns:
        The projected months and the first one whose lowest balance is below zero.
    """
    balance = amount_to_milliunit(start_balance)
    variable = amount_to_milliunit(variable_monthly)
    income = amount_to_milliunit(monthly_income)
    changes = monthly_changes or {}
    months: list[MonthProjection] = []
    first_shortfall = None
    for year, month in horizon(today, until):
        days = calendar.monthrange(year, month)[1]
        current = (year, month) == (today.year, today.month)
        label = f"{year:04d}-{month:02d}"
        first_day = today.day + 1 if current else 1
        left_to_spend = variable - amount_to_milliunit(spent_this_month)
        left_to_receive = income - amount_to_milliunit(received_this_month)
        # Clamping at 1 milliunit instead of 0 cannot move a projection: _spread turns
        # 1 milliunit into 0 cent on every day it covers. Each clamp is alone on its
        # line, so the pragma covers no more than the clamp.
        rest_of_month_spend = min(0, left_to_spend)  # pragma: no mutate
        rest_of_month_income = max(0, left_to_receive)  # pragma: no mutate
        to_spend = rest_of_month_spend if current else variable
        to_receive = rest_of_month_income if current else income
        daily = [
            _spread(to_spend, first_day, days),
            _spread(to_receive, first_day, days),
            _dated(recurring, one_offs, label=label, first_day=first_day, days=days, today=today),
            _spread(amount_to_milliunit(changes.get(label, 0.0)), first_day, days),
        ]
        projection, balance = _walk(label, balance, first_day, days, daily)
        months.append(projection)
        if projection.lowest < 0 and first_shortfall is None:
            first_shortfall = label
    return Projection(months=months, first_shortfall=first_shortfall)


def _half_width(deviation: float, months: float) -> int:
    """Say how far a balance strays after some months, at one deviation a month.

    Args:
        deviation: One standard deviation of a month, in milliunits.
        months: Months projected so far; a share of one for the current month.

    Returns:
        The half-width of the range, in milliunits, in whole cents.
    """
    # Months stray independently (√months), and the average itself rests on only
    # VARIABLE_MONTHS months, so its own error repeats every month (months / √n).
    spread = deviation * math.sqrt(months + months**2 / VARIABLE_MONTHS)
    return round(spread / 10) * 10


def band(  # pylint: disable=too-many-arguments
    projection: Projection,
    transactions: list[dict[str, Any]],
    today: date,
    known: list[Recurring],
    *,
    also: frozenset[tuple[str, bool]] = frozenset(),
    with_income: bool = True,
) -> tuple[Projection, Band]:
    """Give each projected month-end a range, from how much the last full months varied.

    The averages the projection spreads over the days are those of the last 3 full
    months. How far each of those months strayed from them, other spending and income
    together (net), gives one standard deviation s (sample, n - 1). After H months
    projected, the current month counted for the share of its days still to come, the
    balance strays by s * sqrt(H + H^2 / n): sqrt(H) because months stray independently
    of one another, and H / sqrt(n) because the average itself rests on only n = 3
    months, so its error repeats every month. The range is one such deviation either
    side of each month-end: a typical gap, not a worst case. Recurring charges,
    schedules and one-offs count as certain, and income too when the caller gives it.

    Args:
        projection: The projection, from :func:`project`.
        transactions: The plan's transactions, amounts in milliunits.
        today: The day the forecast is made.
        known: Recurring amounts found by :func:`recurring`, left out of the measure.
        also: Other (normalised payee, is money out) pairs to leave out, e.g. the
            payees of scheduled transactions, which are projected apart.
        with_income: False when the caller gave the income: only spending strays.

    Returns:
        The projection with end_low and end_high on each month, and how they were made;
        with fewer than 3 full months of history, the projection as it was and a band
        saying why it has no range.
    """
    months = months_before(today, VARIABLE_MONTHS)
    # Any text sorting after a YYYY-MM month keeps every month out when no transaction
    # is usable at all.
    first = min((tx["date"][:7] for tx in transactions if usable(tx)), default="9999-12")
    held = [month for month in months if month >= first]
    if len(held) < VARIABLE_MONTHS:
        return projection, Band(
            deviation=None,
            months=held,
            first_possible_shortfall=None,
            note=(
                f"No range: it needs the last {VARIABLE_MONTHS} complete months of history, "
                f"and these accounts hold {len(held)}; end_low and end_high are null."
            ),
        )
    spent = _other_totals(transactions, today, known, True, also)
    received = _other_totals(transactions, today, known, False, also) if with_income else {}
    deviation = statistics.stdev(spent.get(m, 0) + received.get(m, 0) for m in months)
    banded, first_possible = _banded(projection, today, deviation)
    rounded = milliunit_to_amount(round(deviation / 10) * 10)
    return banded, Band(
        deviation=rounded,
        months=months,
        first_possible_shortfall=first_possible,
        note=_band_note(rounded, months, with_income),
    )


def _band_note(deviation: float, months: list[str], with_income: bool) -> str:
    """Say how the range was made, for the agent to relay.

    Args:
        deviation: One standard deviation of a month, in currency units.
        months: The months measured, oldest first.
        with_income: False when the caller gave the income.

    Returns:
        The note, two sentences.
    """
    measured = "spending and income" if with_income else "spending"
    certain = (
        "schedules and one-offs" if with_income else "schedules, one-offs and the income given"
    )
    how = (
        f"one standard deviation ({deviation:.2f} a month) of the other {measured} of "
        f"{months[0]} to {months[-1]}, carried over the months projected, so the range widens "
        "with each month"
        if deviation
        else f"equal to end, since the other {measured} of {months[0]} to {months[-1]} did not "
        "vary from month to month"
    )
    return (
        f"end_low and end_high: {how}. Recurring charges, {certain} count as certain: a "
        "typical gap, not a worst case."
    )


def _banded(projection: Projection, today: date, deviation: float) -> tuple[Projection, str | None]:
    """Put a range around each month-end, at one deviation a month carried over the months.

    Args:
        projection: The projection, from :func:`project`.
        today: The day the forecast is made; its month counts for the days left.
        deviation: One standard deviation of a month, in milliunits.

    Returns:
        The projection with end_low and end_high, and the first month whose lowest
        balance less the range's half-width is below zero, or None.
    """
    days = calendar.monthrange(today.year, today.month)[1]
    share = (days - today.day) / days
    months: list[MonthProjection] = []
    first_possible = None
    for index, month in enumerate(projection.months):
        half = _half_width(deviation, share + index)
        end = amount_to_milliunit(month.end)
        months.append(
            month.model_copy(
                update={
                    "end_low": milliunit_to_amount(end - half),
                    "end_high": milliunit_to_amount(end + half),
                }
            )
        )
        if first_possible is None and amount_to_milliunit(month.lowest) - half < 0:
            first_possible = month.month
    return projection.model_copy(update={"months": months}), first_possible
