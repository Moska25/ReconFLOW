"""Bank statement formats: MT940 and CAMT.053.

Banks do not send CSV. They send SWIFT MT940 (a flat tagged text format) or ISO
20022 CAMT.053 (XML). Both carry the same thing a payments CSV carries, so both
are parsed into the row shape the CSV path already uses, and then handed to the
existing validator. That is deliberate: statement lines get the same per-row
rejection, the same reasons and the same rejected-row report as any other file,
rather than a second, weaker validation path of their own.

Nothing here parses money or dates. `ingest.validate_payment` does that, so a
statement and a CSV cannot disagree about what "1.234,56" means.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# The row shape the CSV path produces: canonical field names, string values.
FIELDS = ("payment_ref", "reference", "payer_name", "customer_code",
          "value_date", "amount", "currency")

# :61:<value date><entry date?><mark><funds code?><amount><type><references>
_LINE_61 = re.compile(
    r"^(?P<value_date>\d{6})"
    r"(?P<entry_date>\d{4})?"
    r"(?P<mark>RC|RD|C|D)"
    r"(?P<funds>[A-Z])?"
    r"(?P<amount>[\d.,]+)"
    r"(?P<kind>[NSF][A-Z0-9]{3})"
    r"(?P<refs>.*)$"
)
_DEBIT_MARKS = ("D", "RC")   # RC reverses a credit, so it moves money out
_BALANCE = re.compile(r"^[CD](?P<date>\d{6})(?P<currency>[A-Z]{3})")

# :86: is free text, but structured sub-fields appear often enough to be worth reading.
_ORDERING_PARTY = re.compile(r"/(?:ORDP|BENM)/(?P<name>[^/]+)")
_REMITTANCE = re.compile(r"/(?:REMI|EREF|SVWZ)/(?P<info>[^/]+)")


def detect(content: bytes) -> str | None:
    """Name the statement format in these bytes, or None if it is not a statement.

    Content, not file extension: a bank that names an MT940 file `.txt` still
    sends MT940, and the import route should not care what the file is called.
    """
    head = content[:4096].decode("utf-8", errors="replace")
    if "camt.053" in head or "BkToCstmrStmt" in head:
        return "camt053"
    if ":20:" in head and ":61:" in content[:65536].decode("utf-8", errors="replace"):
        return "mt940"
    return None


def as_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a statement into (headers, rows) exactly as `ingest.read_rows` would."""
    text = content.decode("utf-8", errors="replace")
    kind = detect(content)
    rows = parse_camt053(text) if kind == "camt053" else parse_mt940(text)
    return list(FIELDS), rows


# ----------------------------------------------------------------------------- MT940

def _unfold(text: str) -> list[tuple[str, str]]:
    """Group an MT940 into (tag, value) pairs, joining continuation lines.

    A line that does not open a new `:NN:` tag continues the previous one. Field
    :86: in particular is routinely wrapped across several lines.
    """
    tags: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line == "-":
            continue
        match = re.match(r"^:(?P<tag>\d{2}[A-Z]?):(?P<rest>.*)$", line)
        if match:
            tags.append((match.group("tag"), match.group("rest")))
        elif tags:
            tag, value = tags[-1]
            tags[-1] = (tag, f"{value} {line.strip()}")
    return tags


def _mt940_date(yymmdd: str) -> str:
    # ponytail: MT940 carries no century. These statements are current, so 20yy.
    # If this ever has to read archives, take the century from the statement header.
    return f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def parse_mt940(text: str) -> list[dict[str, str]]:
    """Parse SWIFT MT940 into payment rows.

    :61: carries the money and the references; :86: carries the remittance
    information the matcher actually needs. Both are folded onto `reference`,
    because a payer's invoice number turns up in either one depending on the bank.
    """
    statement_ref, currency = "", ""
    rows: list[dict[str, str]] = []

    for tag, value in _unfold(text):
        if tag == "20":
            statement_ref = value.strip()
        elif tag in ("60F", "60M", "62F", "62M") and not currency:
            balance = _BALANCE.match(value.strip())
            if balance:
                currency = balance.group("currency")
        elif tag == "61":
            line = _LINE_61.match(value.strip())
            if not line:
                continue
            custref, _, bankref = line.group("refs").partition("//")
            custref, bankref = custref.strip(), bankref.strip()
            sign = "-" if line.group("mark") in _DEBIT_MARKS else ""
            rows.append({
                "payment_ref": bankref or custref or f"{statement_ref}-{len(rows) + 1}",
                "reference": custref,
                "payer_name": "",
                "customer_code": "",
                "value_date": _mt940_date(line.group("value_date")),
                "amount": f"{sign}{line.group('amount')}",
                "currency": currency,
            })
        elif tag == "86" and rows:
            info = " ".join(value.split())
            party = _ORDERING_PARTY.search(info)
            remittance = _REMITTANCE.search(info)
            row = rows[-1]
            if party:
                row["payer_name"] = party.group("name").strip()
            detail = (remittance.group("info").strip() if remittance
                      else re.sub(r"/[A-Z]{4}/[^/]*", " ", info).strip())
            # :61: and :86: usually both carry the invoice number; say it once
            existing = row["reference"]
            if existing and existing in detail:
                row["reference"] = detail
            else:
                row["reference"] = " ".join(filter(None, (existing, detail)))

    if currency:
        for row in rows:
            row["currency"] = row["currency"] or currency
    return rows


# -------------------------------------------------------------------------- CAMT.053

def _tag(element: ET.Element) -> str:
    """Local name, with the ISO 20022 namespace stripped."""
    return element.tag.rpartition("}")[2]


def _find(element: ET.Element, *path: str) -> ET.Element | None:
    """Walk by local name, so any camt.053 minor version resolves the same way."""
    current: ET.Element | None = element
    for name in path:
        if current is None:
            return None
        current = next((child for child in current if _tag(child) == name), None)
    return current


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def parse_camt053(xml: str) -> list[dict[str, str]]:
    """Parse ISO 20022 CAMT.053 into payment rows.

    `CdtDbtInd` is the field that matters and the one most often ignored: the
    amount in an entry is unsigned, so a debit read without it posts as incoming
    cash. Debits arrive here negative, the same way a reversal does in the CSV.
    """
    root = ET.fromstring(xml)
    rows: list[dict[str, str]] = []

    for entry in root.iter():
        if _tag(entry) != "Ntry":
            continue
        amount = _find(entry, "Amt")
        if amount is None:
            continue
        debit = _text(_find(entry, "CdtDbtInd")).upper() == "DBIT"
        value_date = (_text(_find(entry, "ValDt", "Dt"))
                      or _text(_find(entry, "ValDt", "DtTm"))[:10]
                      or _text(_find(entry, "BookgDt", "Dt")))

        details = _find(entry, "NtryDtls", "TxDtls")
        reference = payer = code = ""
        if details is not None:
            reference = " ".join(
                _text(part) for part in details.iter()
                if _tag(part) == "Ustrd" and _text(part)
            )
            reference = reference or _text(_find(details, "Refs", "EndToEndId"))
            payer = _text(_find(details, "RltdPties", "Dbtr", "Nm"))
            code = _text(_find(details, "RltdPties", "DbtrAcct", "Id", "Othr", "Id"))

        rows.append({
            "payment_ref": (_text(_find(entry, "NtryRef"))
                            or _text(_find(entry, "AcctSvcrRef"))
                            or f"NTRY-{len(rows) + 1}"),
            "reference": reference,
            "payer_name": payer,
            "customer_code": code,
            "value_date": value_date,
            "amount": f"{'-' if debit else ''}{_text(amount)}",
            "currency": amount.get("Ccy", ""),
        })
    return rows
