# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""get_goal_projection through the MCP protocol: read-only, two YNAB requests and one a month."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

from avenir_mcp import server

from .mcp_helpers import call, tool_schema


def _vacation(budgeted: int, activity: int, balance: int, **target: Any) -> dict[str, Any]:
    return {
        "id": "cat-vacation",
        "name": "Vacation",
        "category_group_id": "grp-savings",
        "category_group_name": "Savings",
        "hidden": False,
        "deleted": False,
        "budgeted": budgeted,
        "activity": activity,
        "balance": balance,
    } | target


_GROCERIES = _vacation(400_000, -380_000, 20_000) | {"id": "cat-groceries", "name": "Groceries"}
_MONTHS = {
    # The current month: 1,000 held, a target of 3,000 by June 2027.
    "current": [
        _vacation(
            300_000,
            0,
            1_000_000,
            goal_type="TBD",
            goal_target=3_000_000,
            goal_target_date="2027-06-01",
        ),
        _GROCERIES,
    ],
    "2026-06-01": [_GROCERIES],
    "2026-07-01": [_vacation(300_000, -50_000, 250_000), _GROCERIES],
    "2026-08-01": [_vacation(300_000, -50_000, 500_000), _GROCERIES],
    "2026-09-01": [_vacation(300_000, -50_000, 750_000), _GROCERIES],
}
# YNAB lists the months budgeted ahead too, in no promised order.
_LISTED = [{"month": m} for m in ("2026-11-01", "2026-07-01", "2026-06-01", "2026-08-01")] + [
    {"month": "2026-10-01"},
    {"month": "2026-09-01"},
]


@pytest.fixture(name="ynab")
def _ynab() -> Iterator[dict[str, AsyncMock]]:
    async def month_categories(_plan_id: str, month: str) -> list[dict[str, Any]]:
        return _MONTHS[month]

    mocks = {
        "get_month_categories": AsyncMock(side_effect=month_categories),
        "get_months": AsyncMock(return_value=_LISTED),
    }
    with (
        patch("avenir_mcp.client.get_month_categories", mocks["get_month_categories"]),
        patch("avenir_mcp.client.get_months", mocks["get_months"]),
        patch("avenir_mcp.app.today", lambda: date(2026, 10, 26)),
    ):
        yield mocks


def test_goal_projection_is_read_only() -> None:
    """Declared read-only and idempotent: a client may call it without asking."""

    async def annotations() -> Any:
        async with Client(server.mcp) as mcp_client:
            listed = await mcp_client.list_tools()
            return next(tool for tool in listed if tool.name == "get_goal_projection").annotations

    found = asyncio.run(annotations())
    assert found.read_only_hint is True
    assert found.idempotent_hint is True
    assert found.open_world_hint is True


def test_the_ynab_target_at_the_pace_of_the_months_held(ynab: dict[str, AsyncMock]) -> None:
    """2,000 to go at 250 a month: June 2027, just in time; 222.23 a month needed.

    Four months asked, June to September: the current month and the one budgeted ahead
    are left out, and June does not hold the category. One request for the category, one
    for the list of months, then one a month.
    """
    args = {"plan_id": "b1", "category": "vacation", "months_count": 4}
    answer = call("get_goal_projection", args).structured_content
    assert answer["months"] == ["2026-07", "2026-08", "2026-09"]
    assert (answer["contribution"], answer["goal_amount"], answer["goal_source"]) == (
        250.0,
        3_000.0,
        "target",
    )
    assert (answer["reached_month"], answer["target_month"]) == ("2027-06", "2027-06")
    assert (answer["monthly_needed"], answer["on_track"]) == (222.23, True)
    asked = [c.args[1] for c in ynab["get_month_categories"].await_args_list]
    assert asked == ["current", "2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"]
    ynab["get_months"].assert_awaited_once_with("b1")


@pytest.mark.usefixtures("ynab")
def test_an_amount_and_a_month_given_by_the_caller() -> None:
    """20,000 by December 2028, by the category's id."""
    args = {
        "plan_id": "b1",
        "category": "cat-vacation",
        "goal_amount": 20_000,
        "target_month": "2028-12",
        "months_count": 1,
    }
    answer = call("get_goal_projection", args).structured_content
    assert (answer["goal_source"], answer["target_month_source"]) == ("given", "given")
    assert answer["months"] == ["2026-09"]
    assert answer["monthly_needed"] == 703.71
    assert answer["reached_month"] == "2033-02"


def test_an_unknown_category_is_refused_after_one_request(ynab: dict[str, AsyncMock]) -> None:
    """The error names the plan's categories; the months are not read."""
    answer = call("get_goal_projection", {"plan_id": "b1", "category": "House"})
    assert answer.is_error
    assert "Vacation, Groceries" in answer.content[0].text
    assert ynab["get_month_categories"].await_count == 1
    ynab["get_months"].assert_not_awaited()


def test_a_category_with_no_balance_to_reach_needs_an_amount(
    ynab: dict[str, AsyncMock],
) -> None:
    """Groceries has no target: the error asks for goal_amount before the months are read."""
    answer = call("get_goal_projection", {"plan_id": "b1", "category": "Groceries"})
    assert answer.is_error
    assert "give goal_amount" in answer.content[0].text
    ynab["get_months"].assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        {"target_month": "2026-09"},
        {"target_month": "next year"},
        {"goal_amount": 0},
        {"goal_amount": -5},
        {"months_count": 0},
        {"months_count": 25},
        {"category": ""},
    ],
)
def test_arguments_out_of_range_are_refused_before_any_request(
    ynab: dict[str, AsyncMock], args: dict[str, Any]
) -> None:
    """Refused before YNAB is asked anything."""
    answer = call("get_goal_projection", {"plan_id": "b1", "category": "Vacation", **args})
    assert answer.is_error
    assert all(mock.await_count == 0 for mock in ynab.values())


def test_the_schema_states_the_ranges() -> None:
    """The agent sees 1 to 24 months and a positive amount before calling."""
    props = tool_schema("get_goal_projection")["properties"]
    months = props["months_count"]
    assert (months["minimum"], months["maximum"], months["default"]) == (1, 24, 6)
    amount = next(o for o in props["goal_amount"]["anyOf"] if o.get("type") == "number")
    assert amount["exclusiveMinimum"] == 0
