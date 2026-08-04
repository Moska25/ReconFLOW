"""Read-only aggregations for the pages.

All SQL lives here rather than in the route handlers, so main.py stays a routing layer
and every number on a page is a function that can be called from a test.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from app.exceptions import BUCKETS, REASONS, SLA_DAYS, breaches_sla
from app.ingest import RATES

MATCHED_STATUS = ("auto", "confirmed")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else {}


def payment_status_map(conn: sqlite3.Connection) -> dict[int, str]:
    status: dict[int, str] = {}
    for row in conn.execute(
        "SELECT l.payment_id pid, m.status s FROM match_links l"
        " JOIN matches m ON m.id = l.match_id WHERE l.payment_id IS NOT NULL"
    ):
        current = status.get(row["pid"])
        if current in MATCHED_STATUS:
            continue
        status[row["pid"]] = row["s"]
    return status


def invoice_status_map(conn: sqlite3.Connection) -> dict[int, str]:
    status: dict[int, str] = {}
    for row in conn.execute(
        "SELECT l.invoice_id iid, m.status s FROM match_links l"
        " JOIN matches m ON m.id = l.match_id WHERE l.invoice_id IS NOT NULL"
    ):
        if status.get(row["iid"]) in MATCHED_STATUS:
            continue
        status[row["iid"]] = row["s"]
    return status


def unapplied_by_currency(conn: sqlite3.Connection) -> list[dict]:
    """Cash received that no confirmed match has consumed, grouped by currency."""
    return _rows(conn, """
        SELECT p.currency, COUNT(*) n, SUM(p.amount_minor) native, SUM(p.gel_minor) gel
        FROM payments p
        WHERE p.id NOT IN (
            SELECT l.payment_id FROM match_links l JOIN matches m ON m.id = l.match_id
            WHERE l.payment_id IS NOT NULL AND m.status IN ('auto','confirmed'))
        GROUP BY p.currency ORDER BY gel DESC
    """)


def unpaid_by_currency(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn, """
        SELECT i.currency, COUNT(*) n, SUM(i.amount_minor) native, SUM(i.gel_minor) gel
        FROM invoices i
        WHERE i.id NOT IN (
            SELECT l.invoice_id FROM match_links l JOIN matches m ON m.id = l.match_id
            WHERE l.invoice_id IS NOT NULL AND m.status IN ('auto','confirmed'))
        GROUP BY i.currency ORDER BY gel DESC
    """)


def exceptions_by_reason(conn: sqlite3.Connection) -> list[dict]:
    rows = _rows(conn, """
        SELECT reason_code, severity, COUNT(*) n, SUM(value_minor) value,
               SUM(status = 'resolved') resolved
        FROM exceptions GROUP BY reason_code, severity
    """)
    for row in rows:
        row["label"] = REASONS[row["reason_code"]]["label"]
        row["means"] = REASONS[row["reason_code"]]["means"]
    rows.sort(key=lambda r: -r["value"])
    return rows


def ageing(conn: sqlite3.Connection) -> list[dict]:
    counts = {b: {"bucket": b, "n": 0, "value": 0} for b in BUCKETS}
    for row in conn.execute(
        "SELECT bucket, COUNT(*) n, SUM(value_minor) v FROM exceptions"
        " WHERE status != 'resolved' GROUP BY bucket"
    ):
        if row["bucket"] in counts:
            counts[row["bucket"]].update(n=row["n"], value=row["v"] or 0)
    return list(counts.values())


def sla_breaches(conn: sqlite3.Connection) -> list[dict]:
    rows = _rows(conn, "SELECT * FROM exceptions WHERE status != 'resolved'")
    breached = [r for r in rows if breaches_sla(r["severity"], r["age_days"])]
    breached.sort(key=lambda r: (-r["value_minor"], r["id"]))
    return breached


def import_health(conn: sqlite3.Connection) -> dict:
    totals = _one(conn, """
        SELECT COUNT(*) batches,
               SUM(status = 'duplicate') duplicates,
               SUM(status = 'rejected') rejected_files,
               COALESCE(SUM(rows_accepted), 0) accepted,
               COALESCE(SUM(rows_rejected), 0) rejected,
               COALESCE(SUM(rows_dupe), 0) skipped
        FROM import_batches
    """)
    totals["reject_reasons"] = _rows(conn, """
        SELECT reason, COUNT(*) n FROM rejected_rows GROUP BY reason ORDER BY n DESC
    """)
    return totals


def dashboard(conn: sqlite3.Connection, as_of: date) -> dict:
    from app.matching import DEFAULT_CONFIG, summarise

    stats = summarise(conn)
    reasons = exceptions_by_reason(conn)
    open_exc = _one(conn, "SELECT COUNT(*) n, COALESCE(SUM(value_minor),0) v"
                          " FROM exceptions WHERE status != 'resolved'")
    breaches = sla_breaches(conn)
    cfg = DEFAULT_CONFIG
    return {
        "as_of": as_of.isoformat(),
        "stats": stats,
        "rule_max": max(stats["by_rule"].values(), default=1) or 1,
        "sla_days": SLA_DAYS,
        "auto_threshold": cfg.auto_threshold,
        "propose_threshold": cfg.propose_threshold,
        # With no usable reference the scorer can reach only amount+date+customer.
        "ref_free_ceiling": cfg.w_amount + cfg.w_date + cfg.w_customer,
        "unapplied": unapplied_by_currency(conn),
        "unpaid": unpaid_by_currency(conn),
        "reasons": reasons,
        "reason_max": max([r["value"] for r in reasons], default=1) or 1,
        "ageing": ageing(conn),
        "ageing_max": max([b["n"] for b in ageing(conn)], default=1) or 1,
        "open_exceptions": open_exc.get("n", 0),
        "open_value": open_exc.get("v", 0),
        "breaches": breaches,
        "approvals_pending": _one(
            conn, "SELECT COUNT(*) n FROM approvals WHERE status = 'pending'").get("n", 0),
        "imports": import_health(conn),
        "activity": _rows(conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 12"),
        "rates": {code: str(rate) for code, rate in RATES.items()},
    }


# ------------------------------------------------------------------------ workspace

def matches(conn: sqlite3.Connection, status: str = "", rule: str = "",
            currency: str = "", query: str = "", limit: int = 200) -> list[dict]:
    sql = ["SELECT * FROM matches WHERE 1=1"]
    params: list = []
    if status:
        sql.append("AND status = ?")
        params.append(status)
    if rule:
        sql.append("AND rule = ?")
        params.append(rule)
    if currency:
        sql.append("AND currency = ?")
        params.append(currency)
    sql.append("ORDER BY confidence DESC, id LIMIT ?")
    params.append(limit)
    rows = _rows(conn, " ".join(sql), tuple(params))
    for row in rows:
        row.update(sides(conn, row["id"]))
    if query:
        needle = query.strip().upper()
        rows = [r for r in rows
                if needle in r["invoice_label"].upper()
                or needle in r["payment_label"].upper()
                or needle in r["customer"].upper()]
    return rows


def sides(conn: sqlite3.Connection, match_id: int) -> dict:
    invoices = _rows(conn, "SELECT i.* FROM invoices i JOIN match_links l ON l.invoice_id = i.id"
                           " WHERE l.match_id = ? ORDER BY i.id", (match_id,))
    payments = _rows(conn, "SELECT p.* FROM payments p JOIN match_links l ON l.payment_id = p.id"
                           " WHERE l.match_id = ? ORDER BY p.id", (match_id,))
    return {
        "invoices": invoices,
        "payments": payments,
        "invoice_label": ", ".join(i["invoice_no"] for i in invoices) or "-",
        "payment_label": ", ".join(p["payment_ref"] for p in payments) or "-",
        "customer": (invoices[0]["customer_name"] if invoices
                     else payments[0]["payer_name"] if payments else "-"),
    }


def match_detail(conn: sqlite3.Connection, match_id: int) -> dict | None:
    row = _one(conn, "SELECT * FROM matches WHERE id = ?", (match_id,))
    if not row:
        return None
    row.update(sides(conn, match_id))
    row["breakdown"] = json.loads(row["breakdown"] or "{}")
    return row


def open_invoices(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    return _rows(conn, """
        SELECT * FROM invoices WHERE id NOT IN (
            SELECT l.invoice_id FROM match_links l JOIN matches m ON m.id = l.match_id
            WHERE l.invoice_id IS NOT NULL AND m.status IN ('auto','confirmed'))
        ORDER BY gel_minor DESC LIMIT ?
    """, (limit,))


def open_payments(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    return _rows(conn, """
        SELECT * FROM payments WHERE id NOT IN (
            SELECT l.payment_id FROM match_links l JOIN matches m ON m.id = l.match_id
            WHERE l.payment_id IS NOT NULL AND m.status IN ('auto','confirmed'))
        ORDER BY ABS(gel_minor) DESC LIMIT ?
    """, (limit,))


def counts(conn: sqlite3.Connection) -> dict:
    return {
        "matches": _one(conn, "SELECT COUNT(*) n FROM matches").get("n", 0),
        "auto": _one(conn, "SELECT COUNT(*) n FROM matches WHERE status='auto'").get("n", 0),
        "proposed": _one(conn, "SELECT COUNT(*) n FROM matches WHERE status='proposed'").get("n", 0),
        "confirmed": _one(conn, "SELECT COUNT(*) n FROM matches WHERE status='confirmed'").get("n", 0),
        "open_invoices": len(open_invoices(conn, limit=10_000)),
        "open_payments": len(open_payments(conn, limit=10_000)),
    }


# ----------------------------------------------------------------------- exceptions

def exception_list(conn: sqlite3.Connection, reason: str = "", severity: str = "",
                   status: str = "", bucket: str = "", assignee: str = "",
                   limit: int = 300) -> list[dict]:
    sql = ["SELECT * FROM exceptions WHERE 1=1"]
    params: list = []
    for column, value in (("reason_code", reason), ("severity", severity),
                          ("status", status), ("bucket", bucket), ("assignee", assignee)):
        if value:
            sql.append(f"AND {column} = ?")
            params.append(value)
    sql.append("ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
               " value_minor DESC LIMIT ?")
    params.append(limit)
    rows = _rows(conn, " ".join(sql), tuple(params))
    for row in rows:
        row["breached"] = breaches_sla(row["severity"], row["age_days"]) and row["status"] != "resolved"
        row["label"] = REASONS[row["reason_code"]]["label"]
    return rows


def exception_detail(conn: sqlite3.Connection, exception_id: int) -> dict | None:
    row = _one(conn, "SELECT * FROM exceptions WHERE id = ?", (exception_id,))
    if not row:
        return None
    row["notes"] = json.loads(row["notes"] or "[]")
    row["meta"] = REASONS[row["reason_code"]]
    row["breached"] = breaches_sla(row["severity"], row["age_days"]) and row["status"] != "resolved"
    row["related"] = related_entity(conn, row)
    row["approvals"] = _rows(
        conn, "SELECT * FROM approvals WHERE entity_type='exception' AND entity_id = ?"
              " ORDER BY id DESC", (exception_id,))
    row["audit"] = _rows(
        conn, "SELECT * FROM audit_log WHERE entity_type='exception' AND entity_id = ?"
              " ORDER BY id DESC", (str(exception_id),))
    return row


def related_entity(conn: sqlite3.Connection, row: dict) -> dict:
    if row["entity_type"] == "payment":
        return _one(conn, "SELECT * FROM payments WHERE id = ?", (row["entity_id"],))
    if row["entity_type"] == "invoice":
        return _one(conn, "SELECT * FROM invoices WHERE id = ?", (row["entity_id"],))
    if row["entity_type"] == "match":
        found = _one(conn, "SELECT m.* FROM matches m JOIN match_links l ON l.match_id = m.id"
                           " WHERE l.invoice_id = ? LIMIT 1", (row["entity_id"],))
        if found:
            found.update(sides(conn, found["id"]))
        return found
    return {}


def distinct(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    return [r[column] for r in conn.execute(
        f"SELECT DISTINCT {column} AS {column} FROM {table} ORDER BY {column}") if r[column]]


# ---------------------------------------------------------------------------- audit

def audit_list(conn: sqlite3.Connection, actor: str = "", action: str = "",
               entity_type: str = "", limit: int = 300) -> list[dict]:
    sql = ["SELECT * FROM audit_log WHERE 1=1"]
    params: list = []
    for column, value in (("actor", actor), ("action", action), ("entity_type", entity_type)):
        if value:
            sql.append(f"AND {column} = ?")
            params.append(value)
    sql.append("ORDER BY id DESC LIMIT ?")
    params.append(limit)
    return _rows(conn, " ".join(sql), tuple(params))
