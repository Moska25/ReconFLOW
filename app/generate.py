"""Deterministic synthetic dataset.

Everything ReconFLOW shows is generated here from a fixed seed, so the demo is identical
on every machine and the tests can assert against known planted cases. The data is
invented; it is labelled as such in the UI, not only in the README.

The mix is deliberate. A reconciliation tool that only ever sees clean data proves
nothing, so the population is built from named scenarios and the count of each is written
to data/manifest.json. The planted totals are what the quality page compares against.
"""
from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

from app.db import DATA_DIR

SEED = 20260801
AS_OF = date(2026, 8, 1)          # the dataset's "today"; ageing is measured from here
START = date(2026, 4, 1)
END = date(2026, 7, 31)

# How many of each situation to plant. Tuned so the automatic match rate lands in the
# 60-75% band a real accounts-receivable team would recognise: good remittance discipline
# from most customers, and a stubborn tail that needs a human.
PLAN = {
    "CLEAN_EXACT_REF": 150,
    "REF_PREFIX": 14,
    "REF_CASE": 10,
    "REF_GEORGIAN": 8,
    "REF_TYPO": 18,
    "AMOUNT_ONLY": 30,
    "TOLERANCE": 14,
    "SHORT_PAY": 14,
    "OVER_PAY": 8,
    "CURRENCY_MISMATCH": 6,
    "EARLY_PAYMENT": 8,
    "MANY_TO_ONE_GROUPS": 6,      # each group is 3 invoices settled by 1 payment
    "ONE_TO_MANY_GROUPS": 7,      # each is 1 invoice settled by 2 payments
    "PARTIAL_CONFIDENCE": 25,
    "NO_PAYMENT": 72,
    "DUPLICATE": 14,
    "UNKNOWN_CUSTOMER": 7,
    "UNAPPLIED": 56,
    "REVERSAL": 9,
}

PLACES = ("Tbilisi", "Batumi", "Rustavi", "Kutaisi", "Gori", "Poti", "Telavi", "Zugdidi",
          "Mtskheta", "Kobuleti", "Marneuli", "Akhaltsikhe")
TRADES = ("Trading", "Logistics", "Foods", "Steel", "Textiles", "Pharma", "Retail",
          "Construction", "Energy", "Agro", "Systems", "Print")
SUFFIX = ("LLC", "JSC", "Group", "Co")

# A handful of customers are held in Georgian script on purpose: the reference matcher
# has to survive non-Latin remittance information.
GEORGIAN_NAMES = ("ალაზანი", "მტკვარი", "კოლხეთი", "ლიხი", "ყაზბეგი", "ბორჯომი")

JUNK_REFS = ("TRANSFER", "PAYMENT", "BANK TRANSFER", "PAYMENT FOR SERVICES", "GEO PAY",
             "SETTLEMENT", "ANGARISHSWORA", "ACCOUNT SETTLEMENT", "TRF", "MONTHLY PAYMENT",
             "", "PAYMENT AS AGREED")


def _round_amount(rng: random.Random) -> int:
    """Invoice amounts always end .00 or .50, which keeps planted odd cents unambiguous."""
    whole = rng.randrange(150, 45_000)
    return whole * 100 + rng.choice((0, 50))


def _dates(rng: random.Random) -> tuple[str, str]:
    issue = START + timedelta(days=rng.randrange((END - START).days + 1))
    terms = rng.choice((14, 30, 30, 30, 45))
    return issue.isoformat(), (issue + timedelta(days=terms)).isoformat()


def _pay_date(issue: str, rng: random.Random, low: int = 2, high: int = 38) -> str:
    landed = date.fromisoformat(issue) + timedelta(days=rng.randrange(low, high))
    return min(landed, END).isoformat()


def _typo(text: str, rng: random.Random) -> str:
    """Change exactly one character, the way a human re-keying a number would."""
    position = rng.randrange(len(text) - 4, len(text))
    digits = "0123456789"
    replacement = rng.choice([d for d in digits if d != text[position]])
    return text[:position] + replacement + text[position + 1:]


def build() -> dict:
    """Return the whole dataset as plain dicts plus the manifest of planted scenarios."""
    rng = random.Random(SEED)

    customers = []
    for index in range(1, 61):
        if index <= len(GEORGIAN_NAMES):
            name = f"{GEORGIAN_NAMES[index - 1]} {rng.choice(SUFFIX)}"
        else:
            name = f"{rng.choice(PLACES)} {rng.choice(TRADES)} {rng.choice(SUFFIX)}"
        currency = rng.choices(("GEL", "USD", "EUR"), weights=(70, 20, 10))[0]
        customers.append({"customer_code": f"C{index:03d}", "customer_name": name,
                          "currency": currency})

    invoices: list[dict] = []
    payments: list[dict] = []
    manifest: dict[str, int] = {}
    counters = {"inv": 0, "pay": 0}

    def new_invoice(customer: dict, scenario: str, amount: int | None = None,
                    currency: str | None = None) -> dict:
        counters["inv"] += 1
        issue, due = _dates(rng)
        row = {
            "invoice_no": f"INV-2026-{counters['inv']:04d}",
            "customer_code": customer["customer_code"],
            "customer_name": customer["customer_name"],
            "issue_date": issue,
            "due_date": due,
            "amount": amount if amount is not None else _round_amount(rng),
            "currency": currency or customer["currency"],
            "_scenario": scenario,
        }
        invoices.append(row)
        return row

    def new_payment(customer: dict | None, reference: str, amount: int, currency: str,
                    value_date: str, scenario: str, payer: str | None = None) -> dict:
        counters["pay"] += 1
        row = {
            "payment_ref": f"BNK-2026-{counters['pay']:06d}",
            "reference": reference,
            "customer_code": customer["customer_code"] if customer else "",
            "payer_name": payer or (customer["customer_name"] if customer else "UNKNOWN"),
            "value_date": value_date,
            "amount": amount,
            "currency": currency,
            "_scenario": scenario,
        }
        payments.append(row)
        return row

    def pick() -> dict:
        return rng.choice(customers)

    # ---- straightforward cases: the reference carries the invoice number -------------
    for scenario, count in (("CLEAN_EXACT_REF", PLAN["CLEAN_EXACT_REF"]),
                            ("REF_PREFIX", PLAN["REF_PREFIX"]),
                            ("REF_CASE", PLAN["REF_CASE"]),
                            ("REF_GEORGIAN", PLAN["REF_GEORGIAN"]),
                            ("REF_TYPO", PLAN["REF_TYPO"])):
        for _ in range(count):
            customer = pick()
            invoice = new_invoice(customer, scenario)
            if scenario == "CLEAN_EXACT_REF":
                reference = invoice["invoice_no"]
            elif scenario == "REF_PREFIX":
                reference = f"PMT-{invoice['invoice_no']} PAYMENT"
            elif scenario == "REF_CASE":
                reference = invoice["invoice_no"].lower().replace("-", " ")
            elif scenario == "REF_GEORGIAN":
                reference = "ინვ-" + invoice["invoice_no"].removeprefix("INV-")
            else:
                reference = _typo(invoice["invoice_no"], rng)
            new_payment(customer, reference, invoice["amount"], invoice["currency"],
                        _pay_date(invoice["issue_date"], rng), scenario)
        manifest[scenario] = count

    # ---- no usable reference, but the amount and customer settle it -----------------
    for _ in range(PLAN["AMOUNT_ONLY"]):
        customer = pick()
        invoice = new_invoice(customer, "AMOUNT_ONLY")
        new_payment(customer, rng.choice(JUNK_REFS), invoice["amount"], invoice["currency"],
                    _pay_date(invoice["issue_date"], rng), "AMOUNT_ONLY")
    manifest["AMOUNT_ONLY"] = PLAN["AMOUNT_ONLY"]

    # ---- rounding differences inside tolerance --------------------------------------
    for _ in range(PLAN["TOLERANCE"]):
        customer = pick()
        invoice = new_invoice(customer, "TOLERANCE")
        drift = rng.choice((-2, -1, 1, 2))
        new_payment(customer, rng.choice(JUNK_REFS), invoice["amount"] + drift,
                    invoice["currency"], _pay_date(invoice["issue_date"], rng), "TOLERANCE")
    manifest["TOLERANCE"] = PLAN["TOLERANCE"]

    # ---- differences beyond tolerance: real short pays and overpayments -------------
    for _ in range(PLAN["SHORT_PAY"]):
        customer = pick()
        invoice = new_invoice(customer, "SHORT_PAY")
        shortfall = max(500, int(invoice["amount"] * rng.uniform(0.03, 0.12)))
        new_payment(customer, invoice["invoice_no"], invoice["amount"] - shortfall,
                    invoice["currency"], _pay_date(invoice["issue_date"], rng), "SHORT_PAY")
    manifest["SHORT_PAY"] = PLAN["SHORT_PAY"]

    for _ in range(PLAN["OVER_PAY"]):
        customer = pick()
        invoice = new_invoice(customer, "OVER_PAY")
        excess = max(500, int(invoice["amount"] * rng.uniform(0.02, 0.09)))
        new_payment(customer, invoice["invoice_no"], invoice["amount"] + excess,
                    invoice["currency"], _pay_date(invoice["issue_date"], rng), "OVER_PAY")
    manifest["OVER_PAY"] = PLAN["OVER_PAY"]

    # ---- invoiced in one currency, paid in another ----------------------------------
    for _ in range(PLAN["CURRENCY_MISMATCH"]):
        customer = pick()
        invoice = new_invoice(customer, "CURRENCY_MISMATCH", currency="USD")
        new_payment(customer, invoice["invoice_no"], invoice["amount"], "GEL",
                    _pay_date(invoice["issue_date"], rng), "CURRENCY_MISMATCH")
    manifest["CURRENCY_MISMATCH"] = PLAN["CURRENCY_MISMATCH"]

    # ---- cash that arrived before the invoice was raised ----------------------------
    for _ in range(PLAN["EARLY_PAYMENT"]):
        customer = pick()
        invoice = new_invoice(customer, "EARLY_PAYMENT")
        early = date.fromisoformat(invoice["issue_date"]) - timedelta(days=rng.randrange(1, 5))
        new_payment(customer, invoice["invoice_no"], invoice["amount"], invoice["currency"],
                    max(early, START).isoformat(), "EARLY_PAYMENT")
    manifest["EARLY_PAYMENT"] = PLAN["EARLY_PAYMENT"]

    # ---- one payment settling several invoices --------------------------------------
    for _ in range(PLAN["MANY_TO_ONE_GROUPS"]):
        customer = pick()
        group = [new_invoice(customer, "MANY_TO_ONE") for _ in range(3)]
        total = sum(i["amount"] for i in group)
        latest = max(i["issue_date"] for i in group)
        new_payment(customer, "CONSOLIDATED PAYMENT", total, group[0]["currency"],
                    _pay_date(latest, rng, 3, 20), "MANY_TO_ONE")
    manifest["MANY_TO_ONE_GROUPS"] = PLAN["MANY_TO_ONE_GROUPS"]

    # ---- several payments settling one invoice --------------------------------------
    for _ in range(PLAN["ONE_TO_MANY_GROUPS"]):
        customer = pick()
        invoice = new_invoice(customer, "ONE_TO_MANY")
        first = invoice["amount"] // 2
        for index, part in enumerate((first, invoice["amount"] - first)):
            new_payment(customer, f"INSTALMENT {index + 1} OF 2", part, invoice["currency"],
                        _pay_date(invoice["issue_date"], rng, 2 + index * 12, 14 + index * 12),
                        "ONE_TO_MANY")
    manifest["ONE_TO_MANY_GROUPS"] = PLAN["ONE_TO_MANY_GROUPS"]

    # ---- enough signal to propose, not enough to post --------------------------------
    # The customer quoted a purchase order rather than the invoice, and rounded the amount.
    for _ in range(PLAN["PARTIAL_CONFIDENCE"]):
        customer = pick()
        invoice = new_invoice(customer, "PARTIAL_CONFIDENCE")
        off = max(300, int(invoice["amount"] * rng.uniform(0.03, 0.07)))
        new_payment(customer, f"PO-{rng.randrange(40000, 99999)}",
                    invoice["amount"] - off, invoice["currency"],
                    _pay_date(invoice["issue_date"], rng, 2, 20), "PARTIAL_CONFIDENCE")
    manifest["PARTIAL_CONFIDENCE"] = PLAN["PARTIAL_CONFIDENCE"]

    # ---- invoices with nothing against them ------------------------------------------
    for _ in range(PLAN["NO_PAYMENT"]):
        new_invoice(pick(), "NO_PAYMENT")
    manifest["NO_PAYMENT"] = PLAN["NO_PAYMENT"]

    # ---- the same money twice ---------------------------------------------------------
    clean = [p for p in payments if p["_scenario"] == "CLEAN_EXACT_REF"]
    for original in rng.sample(clean, PLAN["DUPLICATE"]):
        repeat = date.fromisoformat(original["value_date"]) + timedelta(days=rng.randrange(2, 7))
        new_payment(
            next(c for c in customers if c["customer_code"] == original["customer_code"]),
            original["reference"], original["amount"], original["currency"],
            min(repeat, END).isoformat(), "DUPLICATE",
        )
    manifest["DUPLICATE"] = PLAN["DUPLICATE"]

    # ---- money from someone we do not recognise ---------------------------------------
    for index in range(PLAN["UNKNOWN_CUSTOMER"]):
        new_payment(None, rng.choice(JUNK_REFS), _round_amount(rng), "GEL",
                    _pay_date(START.isoformat(), rng, 10, 110), "UNKNOWN_CUSTOMER",
                    payer=f"{rng.choice(PLACES)} {rng.choice(TRADES)} {index + 1}")
    manifest["UNKNOWN_CUSTOMER"] = PLAN["UNKNOWN_CUSTOMER"]

    # ---- cash we cannot place ---------------------------------------------------------
    # Odd cents on purpose: invoices always end .00 or .50, so these can never match on
    # amount alone and the outcome is driven by the missing reference, not by luck.
    for _ in range(PLAN["UNAPPLIED"]):
        customer = pick()
        amount = _round_amount(rng) + rng.choice((19, 37, 63, 83))
        new_payment(customer, rng.choice(JUNK_REFS), amount, customer["currency"],
                    _pay_date(START.isoformat(), rng, 5, 115), "UNAPPLIED")
    manifest["UNAPPLIED"] = PLAN["UNAPPLIED"]

    # ---- reversals and refunds ---------------------------------------------------------
    for _ in range(PLAN["REVERSAL"]):
        customer = pick()
        new_payment(customer, "REVERSAL OF EARLIER CREDIT", -_round_amount(rng),
                    customer["currency"], _pay_date(START.isoformat(), rng, 20, 115),
                    "REVERSAL")
    manifest["REVERSAL"] = PLAN["REVERSAL"]

    return {"customers": customers, "invoices": invoices, "payments": payments,
            "manifest": manifest}


# --------------------------------------------------------------------------- CSV output

# Rows that must be rejected by the import validator. They are planted inside otherwise
# good files, because that is how they arrive: one bad line in four hundred.
BAD_INVOICES = [
    {"invoice_no": "INV-2026-9001", "customer_code": "C004", "customer_name": "Gori Pharma LLC",
     "issue_date": "31/02/2026", "due_date": "2026-03-31", "amount": "1250.00", "currency": "GEL"},
    {"invoice_no": "INV-2026-9002", "customer_code": "C011", "customer_name": "Poti Agro JSC",
     "issue_date": "2026-05-04", "due_date": "2026-06-03", "amount": "-450.00", "currency": "GEL"},
    {"invoice_no": "", "customer_code": "C019", "customer_name": "Telavi Foods Co",
     "issue_date": "2026-05-11", "due_date": "2026-06-10", "amount": "980.00", "currency": "GEL"},
]

BAD_PAYMENTS = [
    {"payment_ref": "BNK-2026-900001", "reference": "TRANSFER", "customer_code": "C002",
     "payer_name": "Batumi Steel JSC", "value_date": "not-a-date", "amount": "800.00",
     "currency": "GEL"},
    {"payment_ref": "BNK-2026-900002", "reference": "TRANSFER", "customer_code": "C007",
     "payer_name": "Kutaisi Retail LLC", "value_date": "2026-05-19", "amount": "0.00",
     "currency": "GEL"},
    {"payment_ref": "", "reference": "TRANSFER", "customer_code": "C012",
     "payer_name": "Rustavi Energy Group", "value_date": "2026-05-21", "amount": "1500.00",
     "currency": "GEL"},
    {"payment_ref": "BNK-2026-900004", "reference": "TRANSFER", "customer_code": "C021",
     "payer_name": "Zugdidi Print Co", "value_date": "2026-06-02", "amount": "640.00",
     "currency": "GBP"},
    {"payment_ref": "BNK-2026-900005", "reference": "TRANSFER", "customer_code": "C030",
     "payer_name": "Gori Textiles LLC", "value_date": "2026-06-09", "amount": "not a number",
     "currency": "GEL"},
]

INVOICE_HEADER = ["invoice_no", "customer_code", "customer_name", "issue_date", "due_date",
                  "amount", "currency"]
PAYMENT_HEADER = ["payment_ref", "reference", "customer_code", "payer_name", "value_date",
                  "amount", "currency"]

# The July bank file arrives in a different shape: columns reordered, headers spaced and
# capitalised differently, amounts in European convention. Nothing about that is exotic.
PAYMENT_HEADER_ALT = ["  Value Date ", "AMOUNT", "Ccy", "Payment Reference",
                      "Remittance Info", "Ordering Party", "Customer Code"]


def _money(minor: int) -> str:
    return f"{minor / 100:.2f}"


def _money_eu(minor: int) -> str:
    """1234567 -> '12.345,67' - the way a European bank export writes it."""
    whole, frac = divmod(abs(minor), 100)
    grouped = f"{whole:,}".replace(",", ".")
    return f"{'-' if minor < 0 else ''}{grouped},{frac:02d}"


def write_csvs(out_dir: Path | None = None) -> dict:
    """Write the dataset to CSV. Deterministic: identical bytes on every run."""
    out = Path(out_dir) if out_dir else DATA_DIR
    out.mkdir(parents=True, exist_ok=True)
    dataset = build()

    with (out / "customers.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["customer_code", "customer_name", "currency"])
        writer.writeheader()
        writer.writerows(dataset["customers"])

    rows = [{k: (_money(v) if k == "amount" else v) for k, v in inv.items()
             if not k.startswith("_")} for inv in dataset["invoices"]]
    rows[120:120] = [BAD_INVOICES[0]]
    rows[240:240] = [BAD_INVOICES[1]]
    rows[330:330] = [BAD_INVOICES[2]]
    with (out / "invoices.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=INVOICE_HEADER)
        writer.writeheader()
        writer.writerows(rows)

    months: dict[str, list[dict]] = {}
    for pay in dataset["payments"]:
        clean = {k: v for k, v in pay.items() if not k.startswith("_")}
        months.setdefault(pay["value_date"][:7], []).append(clean)

    bad_by_month = {"2026-05": BAD_PAYMENTS[0:3], "2026-06": BAD_PAYMENTS[3:5]}
    written = []
    for month in sorted(months):
        batch = list(months[month])
        for bad in bad_by_month.get(month, []):
            batch.insert(min(len(batch), 17), dict(bad))
        name = f"payments_{month.replace('-', '_')}.csv"
        path = out / name
        european = month == "2026-07"
        with path.open("w", newline="", encoding="utf-8") as fh:
            if european:
                writer = csv.writer(fh)
                writer.writerow(PAYMENT_HEADER_ALT)
                for row in batch:
                    amount = row["amount"]
                    writer.writerow([
                        date.fromisoformat(row["value_date"]).strftime("%d/%m/%Y")
                        if row["value_date"] != "not-a-date" else row["value_date"],
                        _money_eu(amount) if isinstance(amount, int) else amount,
                        row["currency"], row["payment_ref"], row["reference"],
                        row["payer_name"], row["customer_code"],
                    ])
            else:
                writer = csv.DictWriter(fh, fieldnames=PAYMENT_HEADER)
                writer.writeheader()
                for row in batch:
                    out_row = dict(row)
                    if isinstance(out_row["amount"], int):
                        out_row["amount"] = _money(out_row["amount"])
                    writer.writerow(out_row)
        written.append(name)

    manifest = {
        "generated_from_seed": SEED,
        "as_of": AS_OF.isoformat(),
        "window": [START.isoformat(), END.isoformat()],
        "customers": len(dataset["customers"]),
        "invoice_rows": len(dataset["invoices"]),
        "payment_rows": len(dataset["payments"]),
        "planted_bad_rows": len(BAD_INVOICES) + len(BAD_PAYMENTS),
        "scenarios": dataset["manifest"],
        "files": ["customers.csv", "invoices.csv"] + written,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def load_manifest(out_dir: Path | None = None) -> dict:
    path = (Path(out_dir) if out_dir else DATA_DIR) / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(write_csvs(), indent=2, ensure_ascii=False))
