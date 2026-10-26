# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""get_paycheck_plan through the MCP protocol: read-only, three YNAB requests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client

from avenir_mcp import server

from .factories import scheduled
from .mcp_helpers import call, tool_schema

_ACCOUNTS = [
    {"id": "a-chk", "name": "Checking", "type": "checking", "on_budget": True, "closed": False}
    | {"balance": 1_000.0},
    {"id": "a-savings", "name": "Savings", "type": "savings", "on_budget": True, "closed": False}
    | {"balance": 5_000.0},
]
_SALARY = [
    {"payee_name": "ACME PAYROLL", "date": f"2026-0{m}-28", "amount": 2_500_000}
    | {"account_id": "a-chk", "deleted": False, "transfer_account_id": None}
    for m in (5, 6, 7, 8)
]
_SCHEDULES = [scheduled("Landlord", "2026-01-27", "2026-09-27", "monthly", account_id="a-chk")]


@pytest.fixture(name="ynab")
def _ynab() -> Iterator[dict[str, AsyncMock]]:
    mocks = {
        "get_accounts": AsyncMock(return_value=_ACCOUNTS),
        "get_transactions": AsyncMock(return_value=_SALARY),
        "get_scheduled_transactions": AsyncMock(return_value=_SCHEDULES),
    }
    with (
        patch("avenir_mcp.client.get_accounts", mocks["get_accounts"]),
        patch("avenir_mcp.client.get_transactions", mocks["get_transactions"]),
        patch("avenir_mcp.client.get_scheduled_transactions", mocks["get_scheduled_transactions"]),
        patch("avenir_mcp.app.today", lambda: date(2026, 9, 25)),
    ):
        yield mocks


def test_paycheck_plan_is_read_only() -> None:
    """Declared read-only and idempotent: a client may call it without asking."""

    async def annotations() -> Any:
        async with Client(server.mcp) as mcp_client:
            listed = await mcp_client.list_tools()
            return next(tool for tool in listed if tool.name == "get_paycheck_plan").annotations

    found = asyncio.run(annotations())
    assert found.read_only_hint is True
    assert found.idempotent_hint is True
    assert found.open_world_hint is True


def test_the_rent_before_the_salary_in_three_requests(ynab: dict[str, AsyncMock]) -> None:
    """Paid on the 28th: the rent on the 27th leaves 50.00 in checking."""
    answer = call("get_paycheck_plan", {"plan_id": "b1"}).structured_content
    assert answer["accounts"] == ["Checking"]
    assert answer["income"]["cadence"] == "monthly"
    [period] = answer["periods"]
    assert (period["pay_date"], period["lowest_balance"], period["lowest_on"]) == (
        "2026-09-28",
        50.0,
        "2026-09-27",
    )
    assert all(mock.await_count == 1 for mock in ynab.values())


def test_what_the_caller_knows_replaces_the_history(ynab: dict[str, AsyncMock]) -> None:
    """Paid on the 26th this time, 2,000.00, then monthly; savings walked too."""
    args = {
        "plan_id": "b1",
        "next_pay_date": "2026-09-26",
        "pay_amount": 2_000,
        "cadence": "monthly",
        "account_ids": ["a-chk", "a-savings"],
        "include_next_period": True,
        "include_variable_spending": True,
        "income_payee": "Acme",
    }
    answer = call("get_paycheck_plan", args).structured_content
    assert [p["pay_date"] for p in answer["periods"]] == ["2026-09-26", "2026-10-26"]
    assert answer["start_balance"] == 6_000.0
    assert answer["daily_variable_spending"] == 0.0
    assert all(mock.await_count == 1 for mock in ynab.values())


@pytest.mark.parametrize(
    ("args", "words"),
    [
        ({"account_ids": ["a-nope"]}, "Unknown account(s) a-nope"),
        ({"income_payee": "Lottery"}, "Income found: ACME PAYROLL"),
    ],
)
def test_what_cannot_be_planned_is_a_tool_error(
    ynab: dict[str, AsyncMock], args: dict[str, Any], words: str
) -> None:
    """The message says what to give instead."""
    answer = call("get_paycheck_plan", {"plan_id": "b1", **args})
    assert answer.is_error
    assert words in answer.content[0].text
    assert ynab["get_accounts"].await_count == 1


@pytest.mark.parametrize(
    "args",
    [
        {"next_pay_date": "2026-09-24"},
        {"next_pay_date": "2026-11-27"},
        {"next_pay_date": "someday"},
        {"pay_amount": 0},
        {"pay_amount": -100},
        {"cadence": "yearly"},
        {"income_payee": ""},
    ],
)
def test_arguments_out_of_range_are_refused_before_any_request(
    ynab: dict[str, AsyncMock], args: dict[str, Any]
) -> None:
    """A pay date in the past or more than 62 days ahead, a paycheck of nothing."""
    answer = call("get_paycheck_plan", {"plan_id": "b1", **args})
    assert answer.is_error
    assert all(mock.await_count == 0 for mock in ynab.values())


def test_the_schema_lists_the_rhythms() -> None:
    """The agent sees the four rhythms it may give."""
    prop = tool_schema("get_paycheck_plan")["properties"]["cadence"]
    rhythms = next(option for option in prop["anyOf"] if "enum" in option)
    assert rhythms["enum"] == ["weekly", "biweekly", "semimonthly", "monthly"]
