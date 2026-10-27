# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""get_upcoming_expenses through the MCP protocol: read-only, three YNAB requests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from avenir_mcp import server

from .factories import scheduled
from .mcp_helpers import call, tool_schema

RIGHT_TO_LEFT = chr(0x202E)
"""A control character a bank label can carry to reverse what is displayed."""

_CLOUD: list[dict[str, Any]] = [
    {"payee_name": "CLOUDBOX" + RIGHT_TO_LEFT, "date": day, "amount": -59_880}
    | {"category_id": "c-subs", "deleted": False, "transfer_account_id": None}
    for day in ("2024-11-14", "2025-11-14")
]
_SCHEDULES = [
    scheduled(
        "Homesafe", "2025-10-20", "2026-10-20", "yearly", amount=-420_000, category_id="c-home"
    )
]
_MONTH = {
    "month": "2026-09-01",
    "categories": [
        {"id": "c-home", "name": "Home insurance", "balance": 420_000, "deleted": False},
        {"id": "c-subs", "name": "Subscriptions", "balance": 0, "deleted": False},
    ],
}


@pytest.fixture(name="ynab")
def _ynab() -> Iterator[dict[str, AsyncMock]]:
    mocks = {
        "get_transactions": AsyncMock(return_value=_CLOUD),
        "get_scheduled_transactions": AsyncMock(return_value=_SCHEDULES),
        "get_month": AsyncMock(return_value=_MONTH),
    }
    with (
        patch("avenir_mcp.client.get_transactions", mocks["get_transactions"]),
        patch("avenir_mcp.client.get_scheduled_transactions", mocks["get_scheduled_transactions"]),
        patch("avenir_mcp.client.get_month", mocks["get_month"]),
        patch("avenir_mcp.app.today", lambda: date(2026, 9, 25)),
    ):
        yield mocks


def _tools(*, enable_writes: bool) -> dict[str, Any]:
    """The tools the server lists, by name, with writes on or off."""
    server.configure(enable_writes=enable_writes)
    return {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}


def test_upcoming_expenses_is_read_only() -> None:
    """Declared read-only and idempotent: a client may call it without asking."""
    found = _tools(enable_writes=False)["get_upcoming_expenses"].annotations
    assert found.read_only_hint is True
    assert found.idempotent_hint is True
    assert found.open_world_hint is True


def test_the_year_ahead_in_three_requests(ynab: dict[str, AsyncMock]) -> None:
    """The home insurance scheduled in October, the cloud storage found for November."""
    answer = call("get_upcoming_expenses", {"plan_id": "b1"}).structured_content
    expenses = [e for month in answer["months"] for e in month["expenses"]]
    assert [(e["date"], e["payee"], e["source"]) for e in expenses] == [
        ("2026-10-20", "Homesafe", "scheduled"),
        ("2026-11-14", "CLOUDBOX", "detected"),
    ]
    assert [(c["name"], c["on_track"]) for c in answer["categories"]] == [
        ("Subscriptions", False),
        ("Home insurance", True),
    ]
    assert all(mock.await_count == 1 for mock in ynab.values())
    ynab["get_month"].assert_awaited_once_with("b1", "current")


def test_monthly_schedules_and_a_shorter_horizon(ynab: dict[str, AsyncMock]) -> None:
    """One month ahead: the insurance only."""
    args = {"plan_id": "b1", "horizon_months": 1, "include_monthly": True}
    answer = call("get_upcoming_expenses", args).structured_content
    assert answer["until"] == "2026-10-25"
    assert [m["month"] for m in answer["months"]] == ["2026-10"]
    assert ynab["get_scheduled_transactions"].await_count == 1


@pytest.mark.parametrize("months", [0, 25])
def test_a_horizon_out_of_range_is_refused_before_any_request(
    ynab: dict[str, AsyncMock], months: int
) -> None:
    """Refused before YNAB is asked anything."""
    assert call("get_upcoming_expenses", {"plan_id": "b1", "horizon_months": months}).is_error
    assert all(mock.await_count == 0 for mock in ynab.values())


def test_the_schema_states_the_range_of_the_horizon() -> None:
    """The agent sees 1 to 24 months before calling, 12 by default."""
    prop = tool_schema("get_upcoming_expenses")["properties"]["horizon_months"]
    assert (prop["minimum"], prop["maximum"], prop["default"]) == (1, 24, 12)


def test_the_next_two_weeks(ynab: dict[str, AsyncMock]) -> None:
    """horizon_days counts days from today and replaces horizon_months."""
    args = {"plan_id": "b1", "horizon_days": 14, "horizon_months": 6, "include_monthly": True}
    answer = call("get_upcoming_expenses", args).structured_content
    assert (answer["since"], answer["until"]) == ("2026-09-25", "2026-10-09")
    assert answer["months"] == []
    assert all(mock.await_count == 1 for mock in ynab.values())


@pytest.mark.parametrize("days", [0, 367])
def test_a_horizon_in_days_out_of_range_is_refused_before_any_request(
    ynab: dict[str, AsyncMock], days: int
) -> None:
    """1 to 366 days: anything else is refused before YNAB is asked anything."""
    assert call("get_upcoming_expenses", {"plan_id": "b1", "horizon_days": days}).is_error
    assert all(mock.await_count == 0 for mock in ynab.values())


def test_the_schema_states_the_range_of_the_horizon_in_days() -> None:
    """The agent sees 1 to 366 days, none by default, and when to ask for two weeks."""
    schema = tool_schema("get_upcoming_expenses")
    prop = schema["properties"]["horizon_days"]
    days = next(choice for choice in prop["anyOf"] if choice["type"] == "integer")
    assert (days["minimum"], days["maximum"], prop["default"]) == (1, 366, None)
    description = _tools(enable_writes=False)["get_upcoming_expenses"].description
    assert "horizon_days=14, include_monthly=True" in description


def test_the_description_names_the_write_tools_only_when_they_exist() -> None:
    """Read-only, it points to YNAB itself; with writes, to move_money and set_category_target."""
    read_only = _tools(enable_writes=False)["get_upcoming_expenses"].description
    writing = _tools(enable_writes=True)["get_upcoming_expenses"].description
    assert "move_money" not in read_only
    assert "in YNAB itself" in read_only
    assert "move_money" in writing
    assert "set_category_target" in writing
