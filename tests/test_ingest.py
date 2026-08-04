"""Import pipeline: parsing, validation, idempotency."""
from __future__ import annotations

import pytest

from app import ingest
from app.controls import ControlError

CLEAN_INVOICES = (
    "invoice_no,customer_code,customer_name,issue_date,due_date,amount,currency\n"
    "INV-2026-0001,C001,Alazani LLC,2026-04-01,2026-05-01,1200.00,GEL\n"
    "INV-2026-0002,C002,Mtkvari JSC,2026-04-03,2026-05-03,850.50,USD\n"
).encode()

CLEAN_PAYMENTS = (
    "payment_ref,reference,customer_code,payer_name,value_date,amount,currency\n"
    "BNK-1,INV-2026-0001,C001,Alazani LLC,2026-04-10,1200.00,GEL\n"
    "BNK-2,INV-2026-0002,C002,Mtkvari JSC,2026-04-12,850.50,USD\n"
).encode()


def test_sha256_is_stable_and_content_sensitive():
    """The same bytes always hash the same, one changed byte does not."""
    assert ingest.sha256_bytes(b"abc") == ingest.sha256_bytes(b"abc")
    assert ingest.sha256_bytes(b"abc") != ingest.sha256_bytes(b"abd")


@pytest.mark.parametrize("raw,expected", [
    ("1234.56", 123456), ("0.02", 2), ("1234", 123400), ("-99.99", -9999),
])
def test_parse_decimal_dot_convention(raw, expected):
    """Plain dot-decimal amounts parse to exact minor units."""
    assert ingest.parse_decimal(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1.234,56", 123456), ("1 234,56", 123456), ("1,23", 123), ("1.234.567,89", 123456789),
])
def test_parse_decimal_comma_convention(raw, expected):
    """European comma-decimal amounts parse to the same minor units."""
    assert ingest.parse_decimal(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", 123456), ("12 345.67", 1234567), ("1,234", 123400), ("1.234", 123400),
])
def test_parse_decimal_thousands_separators(raw, expected):
    """Thousands separators are stripped, including the ambiguous lone-separator case."""
    assert ingest.parse_decimal(raw) == expected


def test_parse_decimal_parenthesised_negative():
    """Accounting-style parentheses mean a negative amount."""
    assert ingest.parse_decimal("(1234.56)") == -123456


def test_parse_decimal_ignores_currency_symbols():
    """Symbols and codes attached to the amount do not defeat parsing."""
    assert ingest.parse_decimal("₾1 234.50") == 123450
    assert ingest.parse_decimal("USD 500.00") == 50000


@pytest.mark.parametrize("raw", ["", "   ", "abc", "n/a", None])
def test_parse_decimal_rejects_garbage(raw):
    """Unparseable amounts raise rather than silently becoming zero."""
    with pytest.raises(ValueError):
        ingest.parse_decimal(raw)


@pytest.mark.parametrize("raw", ["2026-04-17", "17/04/2026", "17.04.2026", "2026/04/17"])
def test_parse_date_accepts_known_formats(raw):
    """Day-first and ISO date formats all normalise to the same ISO string."""
    assert ingest.parse_date(raw) == "2026-04-17"


@pytest.mark.parametrize("raw", ["", "not-a-date", "31/02/2026", "2026-13-45"])
def test_parse_date_rejects_impossible_dates(raw):
    """A date that cannot exist is refused, including 31 February."""
    with pytest.raises(ValueError):
        ingest.parse_date(raw)


def test_normalise_header_collapses_case_space_and_underscores():
    """Header spelling variation is flattened to one canonical form."""
    for raw in ("  Value Date ", "VALUE_DATE", "value-date", "Value  Date"):
        assert ingest.normalise_header(raw) == "value date"


def test_resolve_columns_tolerates_reordering_and_aliases():
    """Canonical fields are found regardless of order or which alias was used."""
    headers = ["ccy", "amount", "ordering party", "value date", "bank ref", "remittance info"]
    cols = ingest.resolve_columns(headers, ingest.PAYMENT_ALIASES)
    assert cols["payment_ref"] == "bank ref"
    assert cols["value_date"] == "value date"
    assert cols["currency"] == "ccy"
    assert cols["payer_name"] == "ordering party"


def test_to_gel_uses_the_fixed_rate_table():
    """Foreign currency converts at the published fixed rate, not a live one."""
    assert ingest.to_gel(100_00, "GEL") == 100_00
    assert ingest.to_gel(100_00, "USD") == 270_00
    assert ingest.to_gel(100_00, "EUR") == 295_00


def test_to_gel_rejects_an_unknown_currency():
    """A currency with no published rate is an error, never a guess."""
    with pytest.raises(ValueError):
        ingest.to_gel(100_00, "GBP")


def test_import_accepts_a_clean_file(conn):
    """A well-formed file posts every row and records the counts."""
    batch = ingest.import_bytes(conn, "invoices.csv", CLEAN_INVOICES, "invoices")
    assert batch["status"] == "imported"
    assert (batch["rows_total"], batch["rows_accepted"], batch["rows_rejected"]) == (2, 2, 0)
    assert conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"] == 2


def test_importing_the_same_file_twice_posts_nothing(conn):
    """Idempotency: identical bytes are recorded as a duplicate and never double-posted."""
    ingest.import_bytes(conn, "invoices.csv", CLEAN_INVOICES, "invoices")
    before = conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"]

    second = ingest.import_bytes(conn, "invoices-resent.csv", CLEAN_INVOICES, "invoices")

    assert second["status"] == "duplicate"
    assert second["rows_accepted"] == 0
    assert second["rows_dupe"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"] == before


def test_duplicate_file_records_why_it_was_skipped(conn):
    """The duplicate batch explains which earlier batch already held that content."""
    first = ingest.import_bytes(conn, "invoices.csv", CLEAN_INVOICES, "invoices")
    second = ingest.import_bytes(conn, "again.csv", CLEAN_INVOICES, "invoices")
    row = conn.execute("SELECT * FROM rejected_rows WHERE batch_id = ?",
                       (second["id"],)).fetchone()
    assert row["reason"] == "DUPLICATE_FILE"
    assert str(first["id"]) in row["detail"]


def test_import_tolerates_reordered_columns_and_header_noise(conn):
    """A file with shuffled, differently spelled headers still imports correctly."""
    content = (
        "  Value Date ,AMOUNT,Ccy,Payment Reference,Remittance Info,Ordering Party,Customer Code\n"
        "10/04/2026,1200.00,GEL,BNK-9,INV-2026-0001,Alazani LLC,C001\n"
    ).encode()
    batch = ingest.import_bytes(conn, "odd.csv", content, "payments")
    assert batch["rows_accepted"] == 1
    row = conn.execute("SELECT * FROM payments").fetchone()
    assert row["value_date"] == "2026-04-10"
    assert row["amount_minor"] == 120000
    assert row["payment_ref"] == "BNK-9"


def test_import_parses_european_decimals(conn):
    """A file written 1.234,56 imports to the same minor units as 1234.56."""
    content = (
        "payment_ref,reference,customer_code,payer_name,value_date,amount,currency\n"
        "BNK-EU,INV-1,C001,Alazani LLC,11/04/2026,\"1.234,56\",GEL\n"
    ).encode()
    ingest.import_bytes(conn, "eu.csv", content, "payments")
    assert conn.execute("SELECT amount_minor FROM payments").fetchone()[0] == 123456


def test_one_bad_date_rejects_only_that_row(conn):
    """A malformed row is refused with a reason while the rest of the file posts."""
    content = CLEAN_INVOICES.decode().replace("2026-04-03", "31/02/2026").encode()
    batch = ingest.import_bytes(conn, "mixed.csv", content, "invoices")
    assert batch["rows_accepted"] == 1
    assert batch["rows_rejected"] == 1
    reason = conn.execute("SELECT reason FROM rejected_rows WHERE batch_id = ?",
                          (batch["id"],)).fetchone()["reason"]
    assert reason == "BAD_DATE"


def test_negative_invoice_amount_is_rejected(conn):
    """An invoice for a negative amount is refused; only payments may be negative."""
    content = CLEAN_INVOICES.decode().replace("1200.00", "-1200.00").encode()
    batch = ingest.import_bytes(conn, "neg.csv", content, "invoices")
    assert batch["rows_rejected"] == 1
    assert conn.execute("SELECT reason FROM rejected_rows WHERE batch_id = ?",
                        (batch["id"],)).fetchone()["reason"] == "NEGATIVE_AMOUNT"


def test_missing_reference_is_rejected(conn):
    """A row with no invoice number cannot be identified later, so it is refused."""
    content = CLEAN_INVOICES.decode().replace("INV-2026-0001", "").encode()
    batch = ingest.import_bytes(conn, "noref.csv", content, "invoices")
    assert batch["rows_rejected"] == 1
    assert conn.execute("SELECT reason FROM rejected_rows WHERE batch_id = ?",
                        (batch["id"],)).fetchone()["reason"] == "MISSING_REFERENCE"


def test_unknown_currency_is_rejected(conn):
    """A currency with no rate is refused rather than converted at a guess."""
    content = CLEAN_PAYMENTS.decode().replace(",GEL", ",GBP").encode()
    batch = ingest.import_bytes(conn, "gbp.csv", content, "payments")
    assert batch["rows_rejected"] >= 1
    reasons = {r["reason"] for r in conn.execute(
        "SELECT reason FROM rejected_rows WHERE batch_id = ?", (batch["id"],))}
    assert "UNKNOWN_CURRENCY" in reasons


def test_missing_required_column_rejects_the_file_with_a_reason(conn):
    """A file without an amount column is rejected as a whole, and says which column."""
    content = ("invoice_no,customer_name,issue_date,currency\n"
               "INV-1,Alazani LLC,2026-04-01,GEL\n").encode()
    batch = ingest.import_bytes(conn, "short.csv", content, "invoices")
    assert batch["status"] == "rejected"
    row = conn.execute("SELECT * FROM rejected_rows WHERE batch_id = ?",
                       (batch["id"],)).fetchone()
    assert row["reason"] == "MISSING_COLUMN"
    assert "amount" in row["detail"]


def test_row_already_present_is_skipped_not_duplicated(conn):
    """A different file repeating a known invoice number skips that row only."""
    ingest.import_bytes(conn, "a.csv", CLEAN_INVOICES, "invoices")
    extended = CLEAN_INVOICES.decode() + \
        "INV-2026-0003,C003,Kolkheti Co,2026-04-05,2026-05-05,300.00,GEL\n"
    batch = ingest.import_bytes(conn, "b.csv", extended.encode(), "invoices")
    assert batch["rows_accepted"] == 1
    assert batch["rows_dupe"] == 2
    assert conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"] == 3


def test_import_writes_an_audit_row(conn):
    """Importing is a state change, so it lands in the audit log."""
    batch = ingest.import_bytes(conn, "invoices.csv", CLEAN_INVOICES, "invoices",
                                actor="a.beridze", role="analyst")
    row = conn.execute("SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id DESC",
                       (str(batch["id"]),)).fetchone()
    assert row["action"] == "IMPORT_COMPLETED"
    assert row["actor"] == "a.beridze"


def test_auditor_cannot_import(conn):
    """The read-only role is refused at the import boundary, not in the template."""
    with pytest.raises(ControlError):
        ingest.import_bytes(conn, "invoices.csv", CLEAN_INVOICES, "invoices",
                            actor="audit.ext", role="auditor")
    assert conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"] == 0


def test_fmt_money_groups_thousands_and_keeps_two_decimals():
    """Display formatting is exact and never rounds a stored value away."""
    assert ingest.fmt_money(123456789) == "1,234,567.89"
    assert ingest.fmt_money(-2) == "-0.02"
    assert ingest.fmt_money(0) == "0.00"
