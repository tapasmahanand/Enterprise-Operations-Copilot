"""Seed the mock bank systems. Dates are relative to an anchor date so the demo
always shows "yesterday"; set SEED_ANCHOR_DATE=YYYY-MM-DD to pin them."""

from __future__ import annotations

import os
from datetime import date, timedelta

import psycopg

MOCK_TABLES = ["mock_tickets", "mock_transactions", "mock_cards", "mock_accounts", "mock_customers", "org_directory"]


def anchor_date() -> date:
    v = os.environ.get("SEED_ANCHOR_DATE")
    return date.fromisoformat(v) if v else date.today()


def seed_rows(anchor: date) -> dict[str, list[tuple]]:
    d = lambda n: anchor - timedelta(days=n)  # noqa: E731
    return {
        "mock_customers": [
            ("C001", "Priya Sharma", "Andheri East, Mumbai", True),
            ("C002", "Rahul Verma", "Powai, Mumbai", True),
            ("C003", "Sunita Devi", "Dharavi, Mumbai", True),
            ("C004", "Arun Nair", "Fort, Mumbai", True),
            ("C005", "Fatima Khan", "Bandra West, Mumbai", False),
            ("C006", "Karan Mehta", "Lower Parel, Mumbai", True),
        ],
        "mock_accounts": [
            ("A1001", "C001", "savings", None),
            ("A2001", "C002", "credit_card", 300000),
            ("A3001", "C003", "bsbd", None),
            ("A4001", "C004", "current_large", None),
            ("A5001", "C005", "savings", None),
            ("A6001", "C006", "credit_card", 600000),
        ],
        "mock_cards": [
            ("K1001", "A1001", "4821", "active"),
            ("K2001", "A2001", "7710", "active"),
            ("K3001", "A3001", "1093", "active"),
            ("K5001", "A5001", "5566", "active"),
            ("K6001", "A6001", "9034", "active"),
        ],
        # txn_id, account, card, txn_date, alert_sent_on, channel, merchant, amount, status, cross_border
        "mock_transactions": [
            ("T-HERO", "A1001", "K1001", d(1), d(1), "card_not_present", "QuickKart Online", 18450, "posted", False),
            ("T1002", "A1001", "K1001", d(6), d(6), "pos", "Fresh Basket Andheri", 1240, "posted", False),
            ("T1003", "A1001", "K1001", d(4), d(4), "atm", "Other-bank ATM fee", 23, "posted", False),
            ("T2001", "A2001", "K2001", d(3), d(3), "pos", "Urban Threads Phoenix Mall", 4999, "posted", False),
            ("T2002", "A2001", "K2001", d(3), d(3), "pos", "Urban Threads Phoenix Mall", 4999, "posted", False),
            ("T3001", "A3001", "K3001", d(8), d(8), "atm", "ATM withdrawal (cash not dispensed)", 5000, "failed_debited", False),
            ("T4001", "A4001", None, d(2), d(2), "upi_transfer", "UPI to vendor@okbank", 12000, "failed_debited", False),
            ("T5001", "A5001", "K5001", d(2), d(2), "card_not_present", "GameZone Credits", 7800, "posted", False),
            ("T6001", "A6001", "K6001", d(2), d(2), "card_not_present", "Skyline Travels (UAE)", 64000, "posted", True),
        ],
        # ticket_id, account, opened_on, category, description, status, notes(json)
        "mock_tickets": [
            ("TCK-2001", "A2001", d(2), "duplicate_charge", "Customer reports being charged twice at Urban Threads.", "open", "[]"),
            ("TCK-3001", "A3001", d(7), "failed_atm", "Cash not dispensed, account debited.", "open", "[]"),
            ("TCK-6001", "A6001", d(60), "dispute", "Earlier dispute: hotel booking not honoured (resolved).", "closed", "[]"),
        ],
        "org_directory": [
            ("P01", "Ananya Rao", "team_lead", "Resolution Desk A", "ananya.rao@kestrelbank.example"),
            ("P02", "Vikram Menon", "team_lead", "Resolution Desk B", "vikram.menon@kestrelbank.example"),
            ("P03", "Farah Qureshi", "ops_manager", "Retail Operations", "farah.qureshi@kestrelbank.example"),
            ("P04", "Rohan Iyer", "regional_ops_head", "Retail Operations", "rohan.iyer@kestrelbank.example"),
            ("P05", "Meera Pillai", "grievance_redressal_officer", "Customer Experience", "gro@kestrelbank.example"),
            ("P06", "Neha Bhat", "payments_ops_lead", "Payments Ops", "neha.bhat@kestrelbank.example"),
            ("P07", "Arjun Das", "cards_team_lead", "Cards", "arjun.das@kestrelbank.example"),
        ],
    }


def seed(conn: psycopg.Connection, anchor: date | None = None, force: bool | None = None) -> dict[str, int]:
    """Seed once. Re-seeding wipes demo state (blocked cards, notes), so it only
    happens when the tables are empty or RESEED=1."""
    if force is None:
        force = os.environ.get("RESEED") == "1"
    from kestrel.auth import seed_users
    seed_users(conn)   # login users are not demo state: created if missing, never reset
    if not force and conn.execute("SELECT EXISTS (SELECT 1 FROM mock_customers)").fetchone()[0]:
        return {"skipped": 1}
    rows = seed_rows(anchor or anchor_date())
    conn.execute("TRUNCATE " + ", ".join(MOCK_TABLES) + " CASCADE")
    counts = {}
    for table in reversed(MOCK_TABLES):  # parents first
        data = rows[table]
        if not data:
            continue
        ph = ", ".join(["%s"] * len(data[0]))
        with conn.cursor() as cur:
            cur.executemany(f"INSERT INTO {table} VALUES ({ph})", data)
        counts[table] = len(data)
    conn.commit()
    return counts
