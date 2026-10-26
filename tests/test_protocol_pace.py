# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""get_category_pace through the MCP protocol: read-only, three YNAB requests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

from avenir_mcp import server

from .factories import month_category, scheduled, transaction
from .mcp_helpers import call, tool_schema

_CATEGORIES = [
    month_category("groceries", 300_000, 50_000),
    month_category("rent", 1_000_000, 1_000_000, group="Bills"),
]
_SPENT = [
    transaction(f"2026-{month:02d}-{day:02d}", -100_000, "cat-groceries")
    for month in range(3, 9)
    for day in (5, 15, 25)
] + [transaction("2026-09-04", -250_000, "cat-groceries")]
_SCHEDULED = [scheduled("landlord", "2026-01-28", "2026-09-28", "monthly", category_id="cat-rent")]


@pytest.fixture(name="ynab")
def _ynab() -> Iterator[dict[str, AsyncMock]]:
    mocks = {
        "get_month_categories": AsyncMock(return_value=_CATEGORIES),
        "get_transactions": AsyncMock(return_value=_SPENT),
        "get_scheduled_transactions": AsyncMock(return_value=_SCHEDULED),
    }
    with (
        patch("avenir_mcp.client.get_month_categories", mocks["get_month_categories"]),
        patch("avenir_mcp.client.get_transactions", mocks["get_transactions"]),
        patch("avenir_mcp.client.get_scheduled_transactions", mocks["get_scheduled_transactions"]),
        patch("avenir_mcp.app.today", lambda: date(2026, 9, 20)),
    ):
        yield mocks


def test_category_pace_is_read_only() -> None:
    """Declared read-only and idempotent: a client may call it without asking."""

    async def annotations() -> Any:
        async with Client(server.mcp) as mcp_client:
            listed = await mcp_client.list_tools()
            return next(tool for tool in listed if tool.name == "get_category_pace").annotations

    found = asyncio.run(annotations())
    assert found.read_only_hint is True
    assert found.idempotent_hint is True
    assert found.open_world_hint is True


def test_the_month_in_progress_in_three_requests(ynab: dict[str, AsyncMock]) -> None:
    """Groceries heads for 375 of its 300; rent's 950 scheduled leaves 50."""
    answer = call("get_category_pace", {"plan_id": "b1"}).structured_content
    lines = {line["name"]: line for line in answer["categories"]}
    assert list(lines) == ["Groceries", "Rent"]
    assert (lines["Groceries"]["projected_spent"], lines["Groceries"]["shortfall"]) == (
        -375.0,
        75.0,
    )
    assert (lines["Rent"]["method"], lines["Rent"]["projected_available"]) == ("scheduled", 50.0)
    assert (answer["month"], answer["day"], answer["likely_over"]) == ("2026-09-01", 20, 1)
    ynab["get_month_categories"].assert_awaited_once_with("b1", "current")
    assert all(mock.await_count == 1 for mock in ynab.values())


def test_categories_and_groups_narrow_the_answer(ynab: dict[str, AsyncMock]) -> None:
    """By the group's name, or the category's id; the month may be named."""
    args: dict[str, Any] = {"plan_id": "b1", "groups": ["bills"], "month": "2026-09-01"}
    answer = call("get_category_pace", args).structured_content
    assert [line["name"] for line in answer["categories"]] == ["Rent"]
    ynab["get_month_categories"].assert_awaited_once_with("b1", "2026-09-01")
    args = {"plan_id": "b1", "categories": ["cat-groceries"], "limit": 1}
    answer = call("get_category_pace", args).structured_content
    assert [line["name"] for line in answer["categories"]] == ["Groceries"]


def test_an_unknown_category_is_refused_before_the_transactions_are_read(
    ynab: dict[str, AsyncMock],
) -> None:
    """The error lists the plan's categories, so the agent can ask again."""
    answer = call("get_category_pace", {"plan_id": "b1", "categories": ["Food"]})
    assert answer.is_error
    assert "'Food'" in answer.content[0].text
    assert "Groceries, Rent" in answer.content[0].text
    ynab["get_transactions"].assert_not_awaited()
    ynab["get_scheduled_transactions"].assert_not_awaited()


def test_a_month_not_in_progress_is_refused_before_any_request(
    ynab: dict[str, AsyncMock],
) -> None:
    """A month gone by has nothing left to project: the error says where to look."""
    answer = call("get_category_pace", {"plan_id": "b1", "month": "2026-08-01"})
    assert answer.is_error
    assert "get_budget_vs_actual" in answer.content[0].text
    assert all(mock.await_count == 0 for mock in ynab.values())


@pytest.mark.parametrize(
    "args",
    [
        {"month": "2026-09-20"},
        {"months_count": 0},
        {"months_count": 25},
        {"limit": 0},
        {"limit": 201},
        {"categories": []},
        {"groups": [""]},
    ],
)
def test_arguments_out_of_range_are_refused_before_any_request(
    ynab: dict[str, AsyncMock], args: dict[str, Any]
) -> None:
    """Refused before YNAB is asked anything."""
    answer = call("get_category_pace", {"plan_id": "b1", **args})
    assert answer.is_error
    assert all(mock.await_count == 0 for mock in ynab.values())


def test_the_schema_states_the_ranges() -> None:
    """The agent sees 1 to 24 months and 1 to 200 categories before calling."""
    props = tool_schema("get_category_pace")["properties"]
    months = props["months_count"]
    assert (months["minimum"], months["maximum"], months["default"]) == (1, 24, 6)
    limit = next(option for option in props["limit"]["anyOf"] if option.get("type") == "integer")
    assert (limit["minimum"], limit["maximum"]) == (1, 200)
