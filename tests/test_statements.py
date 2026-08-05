"""Bank statement parsing: MT940 and CAMT.053 into the payments row shape.

The two fixtures below describe the SAME ten transactions in the two formats a
bank actually sends, which is what makes the cross-format test meaningful: if
either parser drifts, the two stop agreeing.

All data here is invented. The account number is not a real IBAN.
"""
from __future__ import annotations

import pytest

from app import statements
from app.ingest import PAYMENT_ALIASES, resolve_columns, validate_payment

# value date, payer, invoice reference, amount in GEL, is a debit
TRANSACTIONS = [
    ("2026-08-03", "Alazani LLC", "INV-2026-0301", "4820,00", False),
    ("2026-08-03", "Mtkvari JSC", "INV-2026-0302", "15340,50", False),
    ("2026-08-04", "Rioni Trading", "INV-2026-0303", "980,25", False),
    ("2026-08-04", "Telavi Retail LLC", "INV-2026-0304", "22105,00", False),
    ("2026-08-05", "Gori Print 7", "INV-2026-0305", "1499,99", False),
    ("2026-08-05", "Batumi Systems Co", "INV-2026-0306", "63200,00", False),
    ("2026-08-06", "Zugdidi Retail Co", "INV-2026-0307", "745,80", False),
    ("2026-08-06", "Rustavi Construction Co", "INV-2026-0308", "31875,40", False),
    ("2026-08-07", "Kutaisi Foods", "INV-2026-0309", "5060,00", False),
    ("2026-08-07", "Alazani LLC", "INV-2026-0301", "1200,00", True),
]


def _mt940() -> str:
    lines = [
        ":20:STMT-2026-0807",
        ":25:GE00XX0000000000000000/GEL",
        ":28C:00031/001",
        ":60F:C260802GEL1543287,44",
    ]
    for index, (date, payer, invoice, amount, debit) in enumerate(TRANSACTIONS, start=1):
        yymmdd = date[2:].replace("-", "")
        mark = "D" if debit else "C"
        lines.append(f":61:{yymmdd}{yymmdd[2:]}{mark}{amount}NTRF{invoice}"
                     f"//BNK-STA-{index:04d}")
        lines.append(f":86:/ORDP/{payer}/REMI/{invoice} settlement")
    lines.append(":62F:C260807GEL1620441,88")
    return "\n".join(lines) + "\n"


def _camt053() -> str:
    entries = []
    for index, (date, payer, invoice, amount, debit) in enumerate(TRANSACTIONS, start=1):
        entries.append(f"""
    <Ntry>
      <NtryRef>BNK-STA-{index:04d}</NtryRef>
      <Amt Ccy="GEL">{amount.replace(',', '.')}</Amt>
      <CdtDbtInd>{'DBIT' if debit else 'CRDT'}</CdtDbtInd>
      <BookgDt><Dt>{date}</Dt></BookgDt>
      <ValDt><Dt>{date}</Dt></ValDt>
      <NtryDtls><TxDtls>
        <RltdPties><Dbtr><Nm>{payer}</Nm></Dbtr></RltdPties>
        <RmtInf><Ustrd>{invoice} settlement</Ustrd></RmtInf>
      </TxDtls></NtryDtls>
    </Ntry>""")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">\n'
        "  <BkToCstmrStmt><Stmt>\n"
        "    <Id>STMT-2026-0807</Id>\n"
        + "".join(entries)
        + "\n  </Stmt></BkToCstmrStmt>\n</Document>\n"
    )


def _validated(rows: list[dict]) -> list[dict]:
    """Push parsed rows through the ordinary payment validator, as import does."""
    cols = resolve_columns(list(statements.FIELDS), PAYMENT_ALIASES)
    return [validate_payment(row, cols) for row in rows]


# ------------------------------------------------------------------------- detection

def test_format_is_detected_from_content_not_from_the_file_name():
    """A bank that names an MT940 file .txt still sends MT940."""
    assert statements.detect(_mt940().encode()) == "mt940"
    assert statements.detect(_camt053().encode()) == "camt053"
    assert statements.detect(b"payment_ref,amount\nBNK-1,10.00\n") is None
    assert statements.detect(b"") is None


# ----------------------------------------------------------------------------- MT940

def test_mt940_yields_one_row_per_transaction_with_the_payment_row_shape():
    rows = statements.parse_mt940(_mt940())
    assert len(rows) == len(TRANSACTIONS)
    assert set(rows[0]) == set(statements.FIELDS)


def test_mt940_amounts_and_value_dates_survive_the_parse():
    rows = _validated(statements.parse_mt940(_mt940()))
    assert [r["value_date"] for r in rows] == [t[0] for t in TRANSACTIONS]
    assert rows[0]["amount_minor"] == 482000
    assert rows[1]["amount_minor"] == 1534050
    assert rows[4]["amount_minor"] == 149999


def test_mt940_debit_marks_arrive_negative():
    """A D line is money leaving. Read as a credit it would post as a receipt."""
    rows = _validated(statements.parse_mt940(_mt940()))
    assert rows[-1]["amount_minor"] == -120000
    assert all(r["amount_minor"] > 0 for r in rows[:-1])


def test_mt940_folds_both_61_and_86_onto_the_reference():
    """The invoice number turns up in either field depending on the bank."""
    rows = statements.parse_mt940(_mt940())
    assert "INV-2026-0301" in rows[0]["reference"]
    assert "settlement" in rows[0]["reference"]
    assert rows[0]["payer_name"] == "Alazani LLC"
    # both tags carry the invoice number; the reference should say it once
    assert rows[0]["reference"].count("INV-2026-0301") == 1


def test_mt940_takes_the_currency_from_the_statement_balance():
    rows = _validated(statements.parse_mt940(_mt940()))
    assert {r["currency"] for r in rows} == {"GEL"}


def test_mt940_bank_reference_becomes_the_payment_key():
    rows = statements.parse_mt940(_mt940())
    assert rows[0]["payment_ref"] == "BNK-STA-0001"
    assert len({r["payment_ref"] for r in rows}) == len(rows)


def test_mt940_continuation_lines_are_folded_into_their_tag():
    """:86: wraps across lines in real statements; the tail must not be lost."""
    text = (":20:STMT-1\n:60F:C260802GEL0,00\n"
            ":61:2608030803C100,00NTRFINV-2026-0999//BNK-1\n"
            ":86:/ORDP/Kutaisi Foods\n/REMI/INV-2026-0999 part payment\n"
            ":62F:C260803GEL100,00\n")
    row = statements.parse_mt940(text)[0]
    assert row["payer_name"] == "Kutaisi Foods"
    assert "part payment" in row["reference"]


def test_mt940_ignores_lines_it_cannot_parse_rather_than_failing_the_file():
    text = (":20:STMT-1\n:60F:C260802GEL0,00\n"
            ":61:this is not a statement line\n"
            ":61:2608030803C100,00NTRFINV-2026-0999//BNK-1\n")
    assert len(statements.parse_mt940(text)) == 1


# -------------------------------------------------------------------------- CAMT.053

def test_camt053_yields_one_row_per_entry_with_the_payment_row_shape():
    rows = statements.parse_camt053(_camt053())
    assert len(rows) == len(TRANSACTIONS)
    assert set(rows[0]) == set(statements.FIELDS)


def test_camt053_reads_cdtdbtind_so_a_debit_is_negative():
    """The amount element is unsigned. Without CdtDbtInd a debit posts as cash in."""
    rows = _validated(statements.parse_camt053(_camt053()))
    assert rows[-1]["amount_minor"] == -120000
    assert all(r["amount_minor"] > 0 for r in rows[:-1])


def test_camt053_reads_currency_payer_and_remittance():
    rows = statements.parse_camt053(_camt053())
    assert rows[0]["currency"] == "GEL"
    assert rows[0]["payer_name"] == "Alazani LLC"
    assert "INV-2026-0301" in rows[0]["reference"]


def test_camt053_parses_whatever_the_namespace_version_is():
    """Any camt.053 minor version resolves by local name."""
    xml = _camt053().replace("camt.053.001.02", "camt.053.001.08")
    assert len(statements.parse_camt053(xml)) == len(TRANSACTIONS)


def test_camt053_rejects_a_malformed_document_loudly():
    with pytest.raises(Exception):
        statements.parse_camt053("<Document><Ntry>")


# ------------------------------------------------------------------- the two agree

def test_the_two_formats_produce_the_same_payments():
    """The whole point: one bank, two file formats, one set of facts."""
    from_mt940 = _validated(statements.parse_mt940(_mt940()))
    from_camt = _validated(statements.parse_camt053(_camt053()))

    assert len(from_mt940) == len(from_camt) == len(TRANSACTIONS)
    for mt, camt in zip(from_mt940, from_camt):
        assert mt["payment_ref"] == camt["payment_ref"]
        assert mt["value_date"] == camt["value_date"]
        assert mt["amount_minor"] == camt["amount_minor"]
        assert mt["gel_minor"] == camt["gel_minor"]
        assert mt["currency"] == camt["currency"]
        assert mt["payer_name"] == camt["payer_name"]
        # both carry the invoice number, which is what the matcher reads
        assert "INV-2026-03" in mt["reference"]
        assert "INV-2026-03" in camt["reference"]
