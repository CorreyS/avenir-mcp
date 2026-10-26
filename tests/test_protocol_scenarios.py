# SPDX-FileCopyrightText: 2026 The avenir-mcp contributors
# SPDX-License-Identifier: MIT

"""compare_scenarios through the MCP protocol."""

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

_ACCOUNTS = [
    {"id": "acc", "name": "Checking", "on_budget": True, "closed": False, "balance": 1000.0},
    {"id": "loan", "name": "Loan", "on_budget": False, "closed": False, "balance": -9000.0},
]
_CATEGORIES = [
    {"id": "c-dining", "name": "Dining out", "category_group_id": "g-fun",
     "category_group_name": "Fun", "hidden": False},
    {"id": "c-rent", "name": "Rent", "category_group_id": "g-bills",
     "category_group_name": "Bills", "hidden": False},
]  # fmt: skip


def _tx(payee: str, amount: int, day: str, category: str) -> dict[str, Any]:
    """A transaction of Checking in a category, amounts in milliunits."""
    tx: dict[str, Any] = {"account_id": "acc", "category_id": category, "date": day}
    return (
        tx
        | {"payee_name": payee, "amount": amount, "deleted": False}
        | {
            "transfer_account_id": None,
            "subtransactions": [],
        }
    )


# Dining out at a new place each month, 300 in all; Streamflix every month; salary.
_TXS = [
    tx
    for m in ("05", "06", "07", "08")
    for tx in (
        _tx(f"BISTRO {m}", -300_000, f"2026-{m}-12", "c-dining"),
        _tx("STREAMFLIX", -13_490, f"2026-{m}-15", "c-fun"),
        _tx("ACME PAYROLL", 2_000_000, f"2026-{m}-28", "c-inflow"),
    )
]


@pytest.fixture(autouse=True, name="budget")
def _budget() -> Iterator[AsyncMock]:
    """A fake YNAB on 24 September 2026, with the rent scheduled on the 3rd."""
    schedules = AsyncMock(
        return_value=[scheduled("LANDLORD", "2026-01-03", "2026-10-03", "monthly", amount=-900_000)]
    )
    with (
        patch("avenir_mcp.client.get_accounts", AsyncMock(return_value=_ACCOUNTS)),
        patch("avenir_mcp.client.get_transactions", AsyncMock(return_value=_TXS)),
        patch("avenir_mcp.client.get_categories", AsyncMock(return_value=_CATEGORIES)),
        patch("avenir_mcp.client.get_scheduled_transactions", schedules),
        patch("avenir_mcp.app.today", lambda: date(2026, 9, 24)),
    ):
        yield schedules


def _call(args: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp) as mcp_client:
            return await mcp_client.call_tool("compare_scenarios", args, raise_on_error=False)

    return asyncio.run(run())


def _compare(*scenarios: dict[str, Any], until: str = "2026-12", **extra: Any) -> dict[str, Any]:
    result = _call({"plan_id": "b1", "until": until, "scenarios": list(scenarios), **extra})
    assert not result.is_error, result.content[0].text
    data: dict[str, Any] = result.structured_content
    return data


def _forecast(args: dict[str, Any]) -> dict[str, Any]:
    async def run() -> Any:
        async with Client(server.mcp) as mcp_client:
            return await mcp_client.call_tool("forecast_balance", args, raise_on_error=False)

    data: dict[str, Any] = asyncio.run(run()).structured_content
    return data


CUT_AND_CAR = {
    "name": "cut dining, add a car",
    "adjustments": [
        {"kind": "change_spending", "category": "Dining out", "percent": -20, "start": "2026-10"},
        {"kind": "add_monthly", "amount": -450, "start": "2026-11", "day": 5, "label": "car"},
    ],
}


def test_compare_scenarios_is_read_only() -> None:
    """Weighing a change changes nothing."""

    async def run() -> Any:
        async with Client(server.mcp) as mcp_client:
            listed = await mcp_client.list_tools()
            return next(t for t in listed if t.name == "compare_scenarios")

    tool = asyncio.run(run())
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True


def test_the_baseline_is_forecast_balances_projection() -> None:
    """Same months, same ends, same lowest days, same assumptions."""
    data = _compare(CUT_AND_CAR)
    forecast = _forecast({"plan_id": "b1", "until": "2026-12"})
    baseline = data["baseline"]
    assert baseline["name"] == "baseline" and baseline["applied"] == []
    assert [(m["month"], m["end"], m["lowest"]) for m in baseline["months"]] == [
        (m["month"], m["end"], m["lowest"]) for m in forecast["months"]
    ]
    assert {m["difference"] for m in baseline["months"]} == {0.0}
    assert data["assumptions"] == forecast["assumptions"]
    assert (data["accounts"], data["start_balance"]) == (["Checking"], 1000.0)


def test_a_cut_and_a_car_payment_move_the_balance_from_their_months() -> None:
    """60 a month more from October, 450 a month less from November."""
    (found,) = _compare(CUT_AND_CAR)["scenarios"]
    assert found["name"] == "cut dining, add a car"
    assert [m["difference"] for m in found["months"]] == [0.0, 60.0, -330.0, -720.0]
    assert found["difference_at_end"] == -720.0
    assert [(a["subject"], a["total"]) for a in found["applied"]] == [
        ("Dining out", 180.0),
        ("car", -900.0),
    ]


def test_a_scenario_says_when_the_balance_would_go_below_zero() -> None:
    """A car bought outright in November takes the balance below zero."""
    data = _compare(
        {"name": "buy outright", "adjustments": [
            {"kind": "add_one_off", "amount": -9000, "date": "2026-11-15", "label": "car"}
        ]},
    )  # fmt: skip
    (found,) = data["scenarios"]
    assert data["baseline"]["first_shortfall"] is None
    assert found["first_shortfall"] == "2026-11"
    assert found["lowest_month"] == "2026-11" and found["lowest"] < 0
    assert (
        "buy outright: -9000.00 against the baseline by 2026-12, below zero from 2026-11"
        in (data["message"])
    )


def test_cancelling_a_subscription_and_a_schedule_gives_back_their_amounts() -> None:
    """Streamflix from October, the rent from December: each payment is left out."""
    (found,) = _compare(
        {"name": "cancel", "adjustments": [
            {"kind": "drop_recurring", "payee": "streamflix", "start": "2026-10"},
            {"kind": "drop_recurring", "payee": "landlord", "start": "2026-12"},
        ]},
    )["scenarios"]  # fmt: skip
    assert [m["difference"] for m in found["months"]] == [0.0, 13.49, 26.98, 940.47]


def test_several_scenarios_are_compared_with_the_same_baseline() -> None:
    """Each scenario stands alone against the baseline, in the order given."""
    other = {"name": "raise", "adjustments": [{"kind": "add_monthly", "amount": 100}]}
    data = _compare(CUT_AND_CAR, other)
    assert [s["name"] for s in data["scenarios"]] == ["cut dining, add a car", "raise"]
    assert data["scenarios"][1]["difference_at_end"] == 300.0


def test_baseline_arguments_are_those_of_forecast_balance() -> None:
    """Income, other spending and one-offs given apply to the baseline and every scenario."""
    args: dict[str, Any] = {"monthly_income": 1000, "variable_monthly": -50,
            "one_offs": [{"date": "2026-10-10", "amount": -100, "label": "tax"}]}  # fmt: skip
    data = _compare(CUT_AND_CAR, **args)
    forecast = _forecast({"plan_id": "b1", "until": "2026-12", **args})
    assert [m["end"] for m in data["baseline"]["months"]] == [m["end"] for m in forecast["months"]]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"until": "2026-13"}, "YYYY-MM"),
        ({"scenarios": []}, "scenarios"),
        ({"scenarios": [CUT_AND_CAR] * 5}, "scenarios"),
        (
            {"scenarios": [{"name": "x", "adjustments": [{"kind": "add_monthly"}]}]},
            "add_monthly needs amount",
        ),
        (
            {"scenarios": [{"name": "x", "adjustments": [
                {"kind": "change_spending", "category": "Pets", "percent": -10}]}]},
            "get_category_balances",
        ),
        (
            {"scenarios": [{"name": "x", "adjustments": [
                {"kind": "drop_recurring", "payee": "gym"}]}]},
            "STREAMFLIX",
        ),
        ({"account_ids": ["nope"]}, "list_accounts"),
    ],
)  # fmt: skip
def test_what_cannot_be_compared_is_a_tool_error(args: dict[str, Any], expected: str) -> None:
    """A bad horizon, scenario or adjustment is refused with a way forward."""
    result = _call({"plan_id": "b1", "until": "2026-12", "scenarios": [CUT_AND_CAR], **args})
    assert result.is_error
    assert expected in result.content[0].text


def test_an_impossible_adjustment_is_refused_before_any_request() -> None:
    """Fields a kind does not take are refused before YNAB is asked anything."""
    with patch("avenir_mcp.client.get_accounts", AsyncMock(return_value=[])) as accounts:
        result = _call(
            {"plan_id": "b1", "until": "2026-12", "scenarios": [
                {"name": "x", "adjustments": [{"kind": "add_one_off", "amount": -1}]}]}
        )  # fmt: skip
    assert result.is_error and "add_one_off needs date" in result.content[0].text
    accounts.assert_not_awaited()


def test_names_from_the_plan_are_shown_as_data() -> None:
    """A category named with a line break stays on one line."""
    forged = [dict(_CATEGORIES[0], name="Dining\nout")]
    with patch("avenir_mcp.client.get_categories", AsyncMock(return_value=forged)):
        (found,) = _compare(
            {"name": "x", "adjustments": [
                {"kind": "change_spending", "category": "c-dining", "percent": -10}]}
        )["scenarios"]  # fmt: skip
    assert found["applied"][0]["subject"] == "Dining out"
