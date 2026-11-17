"""Ledger scaffold (Stage 1: no trades are placed; this is the scaffold
Stage 3 will use) and the Kalshi taker-fee helper.

Decided numbers (Nov 16, 2026): bankroll $15.00 shared with the BTC bot,
stake $0.10/trade. The plan's $100 / $1 sizing is superseded — see README.

FEE DRAG WARNING (do not resolve here; open sizing reconciliation):
the taker fee rounds UP to the next cent per order, so at $0.10/trade the
minimum fee is $0.01/order — about 10% of the stake PER LEG before any edge
is earned. A 2-leg ticket pays ~$0.02 in fees on $0.10 staked. The Hunter's
margin threshold in Stage 2 must clear that first.
"""
import math
from decimal import Decimal

import config
import store


def fee_cents(contracts, price_dollars):
    """Kalshi taker fee in integer cents, rounded UP to the next cent.

    fee = round_up(M x 0.07 x C x P x (1-P)), M=1 for these series.
    Verified against Kalshi's fee schedule PDF in BTC Stage 0:
    100 contracts @ 50c -> $1.75; 100 @ 10c -> $0.63.
    """
    fee_dollars = (
        Decimal(str(config.FEE_RATE))
        * Decimal(contracts)
        * Decimal(str(price_dollars))
        * (Decimal(1) - Decimal(str(price_dollars)))
    )
    return math.ceil(fee_dollars * 100)


def open_ledger(conn):
    """Idempotently open the ledger at the decided $15.00 bankroll."""
    row = conn.execute("SELECT COUNT(*) AS n FROM ledger").fetchone()
    if row["n"] > 0:
        return conn.execute(
            "SELECT balance_cents FROM ledger ORDER BY id DESC LIMIT 1"
        ).fetchone()["balance_cents"]
    conn.execute(
        "INSERT INTO ledger (ts, kind, amount_cents, balance_cents, note) "
        "VALUES (?, 'open', ?, ?, ?)",
        (
            store.utcnow_iso(),
            config.BANKROLL_CENTS,
            config.BANKROLL_CENTS,
            "Ledger opened at decided $15.00 shared bankroll ($0.10/trade stake). "
            "Plan $100 sizing superseded Nov 16, 2026; reconciliation open.",
        ),
    )
    conn.commit()
    return config.BANKROLL_CENTS


def balance_cents(conn):
    row = conn.execute(
        "SELECT balance_cents FROM ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["balance_cents"] if row else None
