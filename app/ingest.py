"""Import pipeline: defensive CSV ingestion with idempotency and a rejected-rows report.

Design rules, all of which exist because real remittance files break all of them:

* A file is identified by the sha256 of its bytes. Re-importing the identical file is
  recorded and skipped, never double-posted. Finance systems that double-post are worse
  than finance systems that lose data.
* Column order and header spelling are not trusted. Headers are normalised and resolved
  through alias sets.
* Both `1234.56` and `1.234,56` are parsed, along with thousands separators and
  parenthesised negatives.
* One bad row rejects that row, with a reason, and nothing else. A 400-row file with one
  malformed date still imports 399 rows.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app import statements
from app.controls import audit, now_iso, require

# Fixed FX table. A real system would hold a dated rate curve; this is a single snapshot
# and the UI states the date so nobody mistakes it for a live rate.
RATE_DATE = "2026-08-01"
RATES: dict[str, Decimal] = {
    "GEL": Decimal("1.0000"),
    "USD": Decimal("2.7000"),
    "EUR": Decimal("2.9500"),
}
CURRENCIES = tuple(RATES)

# Dates are read as day-first (31/07/2026). Georgian and European bank exports are
# day-first; an ambiguous 03/04/2026 is therefore 3 April, not 4 March.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d")

_SYMBOLS = str.maketrans({"₾": "", "$": "", "€": "", " ": " "})

INVOICE_ALIASES = {
    "invoice_no": ("invoice no", "invoice number", "invoice", "inv no", "doc no", "document"),
    "customer_code": ("customer code", "cust code", "customer id", "account", "acct"),
    "customer_name": ("customer name", "customer", "client", "client name"),
    "issue_date": ("issue date", "invoice date", "date", "issued"),
    "due_date": ("due date", "due", "payment due"),
    "amount": ("amount", "gross amount", "total", "value", "amount due"),
    "currency": ("currency", "ccy", "curr"),
}

PAYMENT_ALIASES = {
    "payment_ref": ("payment ref", "payment reference", "txn id", "transaction id", "bank ref"),
    "reference": ("reference", "remittance info", "remittance", "narrative", "details",
                  "payment details", "description"),
    "payer_name": ("payer name", "payer", "customer", "ordering party", "counterparty"),
    "customer_code": ("customer code", "cust code", "customer id", "account"),
    "value_date": ("value date", "date", "booking date", "posted"),
    "amount": ("amount", "credit amount", "value"),
    "currency": ("currency", "ccy", "curr"),
}

REQUIRED = {
    "invoices": ("invoice_no", "customer_name", "issue_date", "amount", "currency"),
    "payments": ("payment_ref", "payer_name", "value_date", "amount", "currency"),
}


# --------------------------------------------------------------------------- helpers

def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalise_header(raw: str) -> str:
    """'  Value Date ' / 'VALUE_DATE' / 'value-date' all become 'value date'."""
    return re.sub(r"[\s_\-]+", " ", raw.strip().lower()).strip()


def _is_grouping(s: str, sep: str) -> bool:
    """True when a lone separator is a thousands group rather than a decimal point.

    '1.234' and '1,234' are 1234: exactly three trailing digits behind a 1-3 digit lead.
    '1234.56' is not (two trailing digits); '12345.678' is not (five-digit lead, which
    is not a valid grouping).
    """
    head, _, tail = s.rpartition(sep)
    return len(tail) == 3 and tail.isdigit() and 1 <= len(head) <= 3 and head.isdigit()


def parse_decimal(raw: str) -> int:
    """Parse a money string into integer minor units. Raises ValueError on garbage.

    Handles 1234.56, 1,234.56, 1.234,56, 1 234,56, (1234.56), ₾1 234.56, 1234.
    The separator convention is inferred per value: whichever of '.' or ',' appears
    last is the decimal separator; a lone separator followed by exactly three digits
    is read as a thousands separator.
    """
    if raw is None:
        raise ValueError("empty amount")
    s = str(raw).translate(_SYMBOLS).strip()
    for code in CURRENCIES:
        s = re.sub(rf"\b{code}\b", "", s, flags=re.IGNORECASE)
    s = s.strip()
    if not s:
        raise ValueError("empty amount")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith("-"):
        negative, s = True, s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    s = s.replace(" ", "")
    if not s:
        raise ValueError("empty amount")

    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # Both present: the rightmost is the decimal point, the other groups thousands.
        if last_dot > last_comma:
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    else:
        for sep in (",", "."):
            if sep not in s:
                continue
            if s.count(sep) > 1 or _is_grouping(s, sep):
                s = s.replace(sep, "")
            elif sep == ",":
                s = s.replace(",", ".")
            break

    if not re.fullmatch(r"\d*\.?\d*", s) or not re.search(r"\d", s):
        raise ValueError(f"not a number: {raw!r}")

    try:
        value = Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise ValueError(f"not a number: {raw!r}") from exc
    minor = int(value * 100)
    return -minor if negative else minor


def parse_date(raw: str) -> str:
    """Return an ISO date string, or raise ValueError."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty date")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw!r}")


def to_gel(minor: int, currency: str) -> int:
    """Convert minor units of `currency` into GEL minor units at the fixed RATE_DATE rate."""
    rate = RATES.get(currency.upper())
    if rate is None:
        raise ValueError(f"unknown currency: {currency!r}")
    return int((Decimal(minor) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_money(minor: int | None, currency: str = "GEL") -> str:
    """1234567 -> '12,345.67'. Display only; never feed the result back into arithmetic."""
    if minor is None:
        return "-"
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(int(minor)), 100)
    return f"{sign}{whole:,}.{frac:02d}"


def read_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Decode and parse a CSV into normalised-header dicts. Delimiter is sniffed."""
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [normalise_header(h) for h in (reader.fieldnames or [])]
    rows: list[dict[str, str]] = []
    for raw in reader:
        rows.append({normalise_header(k): (v or "") for k, v in raw.items() if k is not None})
    return headers, rows


def resolve_columns(headers: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Map canonical field -> actual header, tolerating order and spelling variation."""
    found: dict[str, str] = {}
    for field, options in aliases.items():
        if field in headers:
            found[field] = field
            continue
        for option in options:
            if option in headers:
                found[field] = option
                break
    return found


def _get(row: dict[str, str], cols: dict[str, str], field: str) -> str:
    key = cols.get(field)
    return (row.get(key, "") if key else "").strip()


# ----------------------------------------------------------------------- validation

class Reject(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def validate_invoice(row: dict[str, str], cols: dict[str, str]) -> dict:
    invoice_no = _get(row, cols, "invoice_no")
    if not invoice_no:
        raise Reject("MISSING_REFERENCE", "invoice number is blank")

    currency = _get(row, cols, "currency").upper()
    if currency not in RATES:
        raise Reject("UNKNOWN_CURRENCY", f"currency {currency or '(blank)'} is not in the rate table")

    try:
        amount = parse_decimal(_get(row, cols, "amount"))
    except ValueError as exc:
        raise Reject("BAD_AMOUNT", str(exc)) from exc
    if amount <= 0:
        raise Reject("NEGATIVE_AMOUNT", f"invoice amount {fmt_money(amount)} is not positive")

    try:
        issue_date = parse_date(_get(row, cols, "issue_date"))
    except ValueError as exc:
        raise Reject("BAD_DATE", str(exc)) from exc
    try:
        due_date = parse_date(_get(row, cols, "due_date")) if cols.get("due_date") else issue_date
    except ValueError as exc:
        raise Reject("BAD_DATE", f"due date - {exc}") from exc

    return {
        "invoice_no": invoice_no,
        "customer_code": _get(row, cols, "customer_code"),
        "customer_name": _get(row, cols, "customer_name"),
        "issue_date": issue_date,
        "due_date": due_date,
        "amount_minor": amount,
        "currency": currency,
        "gel_minor": to_gel(amount, currency),
    }


def validate_payment(row: dict[str, str], cols: dict[str, str]) -> dict:
    payment_ref = _get(row, cols, "payment_ref")
    if not payment_ref:
        raise Reject("MISSING_REFERENCE", "bank transaction id is blank")

    currency = _get(row, cols, "currency").upper()
    if currency not in RATES:
        raise Reject("UNKNOWN_CURRENCY", f"currency {currency or '(blank)'} is not in the rate table")

    try:
        amount = parse_decimal(_get(row, cols, "amount"))
    except ValueError as exc:
        raise Reject("BAD_AMOUNT", str(exc)) from exc
    if amount == 0:
        raise Reject("BAD_AMOUNT", "payment amount is zero")

    try:
        value_date = parse_date(_get(row, cols, "value_date"))
    except ValueError as exc:
        raise Reject("BAD_DATE", str(exc)) from exc

    code = _get(row, cols, "customer_code")
    return {
        "payment_ref": payment_ref,
        "reference": _get(row, cols, "reference"),
        "customer_code": code or None,
        "payer_name": _get(row, cols, "payer_name"),
        "value_date": value_date,
        "amount_minor": amount,       # negative is legitimate: reversals and refunds
        "currency": currency,
        "gel_minor": to_gel(amount, currency),
    }


# --------------------------------------------------------------------------- import

def import_bytes(
    conn: sqlite3.Connection,
    filename: str,
    content: bytes,
    kind: str,
    actor: str = "system",
    role: str = "supervisor",
) -> dict:
    """Import one file. Returns a batch summary dict; never raises on bad data."""
    require(role, "import", filename)
    digest = sha256_bytes(content)

    prior = conn.execute(
        "SELECT id, filename, created_at FROM import_batches"
        " WHERE sha256 = ? AND status = 'imported' ORDER BY id LIMIT 1",
        (digest,),
    ).fetchone()

    statement_format = statements.detect(content)
    if statement_format:
        # A bank statement is payments by definition, whatever the form said.
        kind = "payments"
        headers, rows = statements.as_rows(content)
    else:
        headers, rows = read_rows(content)

    if prior is not None:
        # Identical bytes already posted. Record the attempt, post nothing.
        batch_id = _new_batch(conn, filename, kind, digest, "duplicate", actor,
                              rows_total=len(rows), rows_dupe=len(rows))
        conn.execute(
            "INSERT INTO rejected_rows (batch_id, row_no, reason, detail, raw) VALUES (?,?,?,?,?)",
            (batch_id, 0, "DUPLICATE_FILE",
             f"identical content already imported as batch {prior['id']} "
             f"({prior['filename']}, {prior['created_at']})", digest),
        )
        conn.commit()
        audit(conn, actor, role, "IMPORT_SKIPPED_DUPLICATE", "batch", batch_id,
              before={"sha256": digest}, after={"prior_batch": prior["id"], "rows_skipped": len(rows)})
        return _summary(conn, batch_id)

    aliases = INVOICE_ALIASES if kind == "invoices" else PAYMENT_ALIASES
    cols = resolve_columns(headers, aliases)
    missing = [f for f in REQUIRED[kind] if f not in cols]
    if missing:
        batch_id = _new_batch(conn, filename, kind, digest, "rejected", actor,
                              rows_total=len(rows), rows_rejected=len(rows))
        conn.execute(
            "INSERT INTO rejected_rows (batch_id, row_no, reason, detail, raw) VALUES (?,?,?,?,?)",
            (batch_id, 0, "MISSING_COLUMN",
             f"required column(s) not found: {', '.join(missing)}", ", ".join(headers)),
        )
        conn.commit()
        audit(conn, actor, role, "IMPORT_REJECTED", "batch", batch_id,
              before=None, after={"missing_columns": missing})
        return _summary(conn, batch_id)

    batch_id = _new_batch(conn, filename, kind, digest, "imported", actor, rows_total=len(rows))
    validate = validate_invoice if kind == "invoices" else validate_payment
    key_col, key_field = ("invoice_no", "invoice_no") if kind == "invoices" else ("payment_ref", "payment_ref")
    table = "invoices" if kind == "invoices" else "payments"

    accepted = rejected = duplicate = 0
    for index, row in enumerate(rows, start=1):
        try:
            clean = validate(row, cols)
        except Reject as rej:
            rejected += 1
            conn.execute(
                "INSERT INTO rejected_rows (batch_id, row_no, reason, detail, raw) VALUES (?,?,?,?,?)",
                (batch_id, index, rej.reason, rej.detail,
                 " | ".join(f"{k}={v}" for k, v in row.items() if v)[:400]),
            )
            continue

        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE {key_col} = ?", (clean[key_field],)
        ).fetchone()
        if exists:
            duplicate += 1
            continue

        clean["batch_id"] = batch_id
        fields = ", ".join(clean)
        marks = ", ".join("?" * len(clean))
        conn.execute(f"INSERT INTO {table} ({fields}) VALUES ({marks})", tuple(clean.values()))
        accepted += 1

    conn.execute(
        "UPDATE import_batches SET rows_accepted = ?, rows_rejected = ?, rows_dupe = ? WHERE id = ?",
        (accepted, rejected, duplicate, batch_id),
    )
    conn.commit()
    audit(conn, actor, role, "IMPORT_COMPLETED", "batch", batch_id,
          before={"sha256": digest, "filename": filename},
          after={"accepted": accepted, "rejected": rejected, "duplicate": duplicate})
    return _summary(conn, batch_id)


def _new_batch(conn, filename, kind, digest, status, actor, *, rows_total=0,
               rows_accepted=0, rows_rejected=0, rows_dupe=0) -> int:
    from app.controls import now_iso
    cur = conn.execute(
        "INSERT INTO import_batches (filename, kind, sha256, status, rows_total,"
        " rows_accepted, rows_rejected, rows_dupe, created_at, actor)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (filename, kind, digest, status, rows_total, rows_accepted, rows_rejected,
         rows_dupe, now_iso(), actor),
    )
    conn.commit()
    return int(cur.lastrowid)


def _summary(conn: sqlite3.Connection, batch_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone())
