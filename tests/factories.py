# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""Invented YNAB objects for tests, with every field YNAB sends."""

from __future__ import annotations

from typing import Any


def scheduled(
    sched_id: str, first: str, next_: str, frequency: str, **extra: Any
) -> dict[str, Any]:
    """A scheduled transaction as YNAB returns it: rent of 950.00 on Checking by default.

    Args:
        sched_id: Its id, also its payee's name.
        first: The first date it was scheduled on.
        next_: Its next date.
        frequency: YNAB's frequency, e.g. "monthly".
        **extra: Fields to change, e.g. amount or transfer_account_id.

    Returns:
        The scheduled transaction.
    """
    item: dict[str, Any] = {
        "id": sched_id,
        "date_first": first,
        "date_next": next_,
        "frequency": frequency,
        "amount": -950_000,
        "memo": None,
        "account_id": "acc",
        "payee_name": sched_id,
        "category_id": "c-rent",
        "transfer_account_id": None,
        "subtransactions": [],
        "deleted": False,
    }
    item.update(extra)
    return item


def month_category(
    key: str, budgeted: int, available: int, group: str = "Everyday", **extra: Any
) -> dict[str, Any]:
    """A category of a month as YNAB returns it, named after its key, nothing hidden.

    Args:
        key: Its name in lower case; its id is cat-<key>.
        budgeted: Milliunits assigned this month.
        available: Milliunits available; the activity is what makes up the difference.
        group: The name of its group, whose id is grp-<name in lower case>.
        **extra: Fields to change, e.g. hidden or a target's.

    Returns:
        The category.
    """
    return {
        "id": f"cat-{key}",
        "name": key.title(),
        "category_group_id": f"grp-{group.lower()}",
        "category_group_name": group,
        "hidden": False,
        "deleted": False,
        "budgeted": budgeted,
        "activity": available - budgeted,
        "balance": available,
    } | extra


def transaction(day: str, amount: int, category: str | None, **extra: Any) -> dict[str, Any]:
    """A transaction on a budget account as YNAB returns it, not split.

    Args:
        day: Its date, YYYY-MM-DD.
        amount: Milliunits, negative for money out.
        category: Its category id, or None.
        **extra: Fields to change, e.g. subtransactions or deleted.

    Returns:
        The transaction.
    """
    return {
        "date": day,
        "amount": amount,
        "category_id": category,
        "transfer_account_id": None,
        "deleted": False,
        "subtransactions": [],
    } | extra
