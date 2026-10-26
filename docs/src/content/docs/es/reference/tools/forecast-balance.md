---
title: "forecast_balance"
description: "Project the balance month by month and say when money would run out."
---

:::note[Generado]
Generado desde el código con `python -m docsgen`; una prueba falla si deja de coincidir. Las descripciones vienen del código y se mantienen en inglés: es el idioma en que las leen los agentes.
:::

## Qué hace

Project the balance month by month and say when money would run out.

Starts from today's balance of the open on-budget accounts (or those given).
For the current month, what was already spent or received since the 1st is
deducted from the monthly averages, so only what is left is projected.
Assumes, and returns as `assumptions` so the user can correct them: YNAB's
scheduled transactions on their dates, whatever their frequency, a yearly bill
included, charges that recur in the last 4 months (same payee, stable amount),
the average of all other spending over the last 3 months, and what you pass.
A payee with a schedule is projected by that schedule alone, and transfers
between the projected accounts are left out. What you pass is the expected
monthly income (default: the last 3 months' non-recurring inflows, which may
include one-off money such as capital injections) and one-off amounts such as
a tax bill (negative) or a refund (positive). Amounts in currency units.
`lowest` is the lowest point within a month; `first_shortfall` is the first
month it goes below zero. `end_low` and `end_high` give each month-end a range:
one standard deviation of how far the last 3 full months' other spending and
income (spending alone when you pass monthly_income) strayed from their average,
widening with each month projected. It is a typical gap, not a worst case;
`band.first_possible_shortfall` is the first month the balance could go below
zero within it, and `band.note` says how it was made, or why the bounds are null
(fewer than 3 full months of history). For a month already gone use
get_monthly_summary, or get_spending_trends to compare several; for how long
today's money would last with no income at all, get_runway; for what-ifs (a
category cut, a new monthly payment, a cancelled subscription), compare_scenarios.
Four YNAB requests (accounts, transactions, scheduled transactions, categories);
changes nothing.

## Comportamiento

| | |
|---|---|
| Tipo | solo lectura |
| Confirmación | no |
| Deshacer | no |
| Idempotente | sí |
| Peticiones a YNAB | 4 para el ejemplo de abajo, con la caché vacía |

## Parámetros

| Nombre | Tipo | Obligatorio | Por defecto | Descripción |
|---|---|---|---|---|
| `plan_id` | string | sí | — | YNAB plan id or 'last-used'. |
| `until` | string | sí | — | Last month to project, YYYY-MM, at most 24 months ahead. |
| `account_ids` | array of string \| null | no | `null` | Accounts to include (from list_accounts); default all open on-budget accounts. |
| `monthly_income` | number \| null | no | `null` | Income expected each month, replacing the income found in the history (recurring or average) and scheduled in YNAB; default: what they show. |
| `variable_monthly` | number \| null | no | `null` | Monthly spending besides recurring charges (negative); default: the last 3 months' average. |
| `one_offs` | array of object \| null | no | `null` | Expected one-off amounts: {date YYYY-MM-DD, amount, label}; not those already scheduled in YNAB, which are counted. |

## Devuelve

| Campo | Tipo | Descripción |
|---|---|---|
| `message` | string | The conclusion in one sentence, for the agent to relay. |
| `accounts` | array of string | Names of the accounts projected together. |
| `start_balance` | number | Their total balance today. |
| `assumptions` | object | Everything the projection assumed, to check with the user. |
| `assumptions.recurring` | array of object | Charges and income found recurring in the history. |
| `assumptions.variable_monthly` | number | Monthly spending besides recurring charges, negative. |
| `assumptions.monthly_income` | number | Monthly income assumed, besides recurring income. |
| `assumptions.one_offs` | array of object | One-off amounts given by the caller. |
| `assumptions.scheduled` | array of object | Scheduled transactions of the projected accounts, from tomorrow to the last month; they replace what the history suggests for the same payees. Transfers between the projected accounts are left out. |
| `months` | array of object | The projected months. |
| `months[].month` | string | Month, YYYY-MM. |
| `months[].start` | number | Projected balance on the first day (today's balance for the current month). |
| `months[].inflows` | number | Money expected in during the month. |
| `months[].outflows` | number | Money expected out during the month, negative. |
| `months[].end` | number | Projected balance at the end of the month. |
| `months[].lowest` | number | Lowest projected balance within the month, day by day. |
| `months[].end_low` | number \| null | Low end of the range around `end`: one deviation below it (see band); null when the history is too short to make a range. |
| `months[].end_high` | number \| null | High end of the range around `end`: one deviation above it; null when the history is too short to make a range. |
| `first_shortfall` | string \| null | First month whose lowest balance is below zero; null if none. |
| `band` | object | How the range around each month-end (end_low, end_high) was made, or why there is none, and the first month the balance could go below zero within it. |
| `band.deviation` | number \| null | One standard deviation of a month's other spending and income, net, over the months measured, in currency units; null when the history is too short. |
| `band.months` | array of string | The complete months measured, YYYY-MM, oldest first. |
| `band.first_possible_shortfall` | string \| null | First month whose lowest balance, less the range's half-width at its end, is below zero: the balance could go below zero as early as this; null if none or no range. |
| `band.note` | string | How the range was made, or why there is none, for the agent to relay. |

## Ejemplo

Argumentos:

```json
{
  "plan_id": "demo-budget",
  "until": "2026-12",
  "monthly_income": 3200
}
```

Respuesta sobre el presupuesto de demostración:

```json
{
  "message": "The balance stays above zero until 2026-12. This rests on the assumptions listed: check them with the user.",
  "accounts": [
    "Cuenta corriente",
    "Ahorro"
  ],
  "start_balance": 3134.48,
  "assumptions": {
    "recurring": [
      {
        "payee": "CHEZ LUCIE",
        "amount": -49.2,
        "day": 10,
        "months_seen": 3
      },
      {
        "payee": "MARKET FRESH",
        "amount": -395.9,
        "day": 14,
        "months_seen": 3
      },
      {
        "payee": "RAIL CO",
        "amount": -45.0,
        "day": 18,
        "months_seen": 3
      },
      {
        "payee": "STREAMFLIX",
        "amount": -13.49,
        "day": 15,
        "months_seen": 3
      },
      {
        "payee": "SUSHI GO",
        "amount": -64.5,
        "day": 24,
        "months_seen": 3
      },
      {
        "payee": "TENNIS CLUB - PRELEV",
        "amount": -22.0,
        "day": 20,
        "months_seen": 3
      }
    ],
    "variable_monthly": 0.0,
    "monthly_income": 3200.0,
    "one_offs": [],
    "scheduled": [
      {
        "scheduled_id": "sch-rent",
        "date": "2026-10-03",
        "amount": -950.0,
        "account": "Cuenta corriente",
        "category": "Alquiler",
        "payee": "LANDLORD SARL",
        "memo": null,
        "frequency": "monthly",
        "transfer": false
      },
      {
        "scheduled_id": "sch-internet",
        "date": "2026-10-06",
        "amount": -29.99,
        "account": "Cuenta corriente",
        "category": "Fibra",
        "payee": "FIBERNET - PRELEV",
        "memo": null,
        "frequency": "monthly",
        "transfer": false
      },
      {
        "scheduled_id": "sch-phone",
        "date": "2026-10-08",
        "amount": -19.99,
        "account": "Cuenta corriente",
        "category": "Teléfono",
        "payee": "TELCO MOBILE - PRELEV",
        "memo": null,
        "frequency": "monthly",
        "transfer": false
      },
      {
        "scheduled_id": "sch-power",
        "date": "2026-10-12",
        "amount": -64.2,
        "account": "Cuenta corriente",
        "category": "Luz",
        "payee": "POWERCO ENERGIE",
        "memo": null,
        "frequency": "monthly",
        "transfer": false
      },
      {
        "scheduled_id": "sch-insurance",
        "date": "2026-10-20",
        "amount": -420.0,
        "account": "Cuenta corriente",
        "category": "Seguro del hogar",
        "payee": "HOMESAFE INSURANCE",
        "memo": null,
        "frequency": "yearly",
        "transfer": false
      },
      {
        "scheduled_id": "sch-rent",
        "date": "2026-11-03",
        "amount": -950.0,
        "account": "Cuenta corriente",
        "category": "Alquiler",
        "payee": "LANDLORD SARL",
        "memo": null,
        "frequency": "monthly",
        "transfer": false
      },
      "… 8 more"
    ]
  },
  "months": [
    {
      "month": "2026-09",
      "start": 3134.48,
      "inflows": 3200.0,
      "outflows": 0.0,
      "end": 6334.48,
      "lowest": 3134.48,
      "end_low": 6334.48,
      "end_high": 6334.48
    },
    {
      "month": "2026-10",
      "start": 6334.48,
      "inflows": 3200.0,
      "outflows": -2074.27,
      "end": 7460.21,
      "lowest": 5694.17,
      "end_low": 7460.21,
      "end_high": 7460.21
    },
    {
      "month": "2026-11",
      "start": 7460.21,
      "inflows": 3200.0,
      "outflows": -1654.27,
      "end": 9005.94,
      "lowest": 6830.22,
      "end_low": 9005.94,
      "end_high": 9005.94
    },
    {
      "month": "2026-12",
      "start": 9005.94,
      "inflows": 3200.0,
      "outflows": -2134.27,
      "end": 10071.67,
      "lowest": 7885.63,
      "end_low": 10071.67,
      "end_high": 10071.67
    }
  ],
  "first_shortfall": null,
  "band": {
    "deviation": 0.0,
    "months": [
      "2026-06",
      "2026-07",
      "2026-08"
    ],
    "first_possible_shortfall": null,
    "note": "end_low and end_high: equal to end, since the other spending of 2026-06 to 2026-08 did not vary from month to month. Recurring charges, schedules, one-offs and the income given count as certain: a typical gap, not a worst case."
  }
}
```

## Errores

- `until must be the current month or later.`
- `until must be at most 24 months ahead.`
- `until must be a month as YYYY-MM, got '{until}'.`
- `Unknown account(s) {unknown}: use ids from list_accounts.`

Los errores propios de YNAB llegan como `Error calling tool '<tool>': YNAB <status>: <detail>`.

## Ver también

- [Planificar](/avenir-mcp/es/guides/plan-ahead/)
