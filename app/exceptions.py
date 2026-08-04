"""Exception taxonomy and queue.

Anything the matching engine could not settle cleanly becomes a typed exception with a
reason code, a severity, a monetary value at risk, an ageing bucket and an owner. The
derivation is a pure function of (invoices, payments, matches, as_of) so the queue is
reproducible and testable; persistence is a separate thin step.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from app.controls import ControlError, audit, now_iso, require
from app.ingest import RATE_DATE as RATE_NOTE
from app.matching import normalise_ref, within_tolerance

# Reason code -> (label, default severity, what it means, what an analyst does about it)
REASONS: dict[str, dict] = {
    "SHORT_PAY": {
        "label": "Short payment",
        "severity": "medium",
        "means": "The payment is materially below the invoice and outside tolerance.",
        "action": "Confirm whether a deduction, dispute or credit note explains the gap.",
    },
    "OVER_PAY": {
        "label": "Overpayment",
        "severity": "medium",
        "means": "More was received than invoiced, beyond tolerance.",
        "action": "Apply to another open invoice or raise a refund.",
    },
    "DUPLICATE_SUSPECTED": {
        "label": "Suspected duplicate payment",
        "severity": "high",
        "means": "The same customer sent the same amount twice within a few days.",
        "action": "Verify against the bank statement before applying; refund if genuinely double-paid.",
    },
    "UNKNOWN_CUSTOMER": {
        "label": "Unknown customer",
        "severity": "high",
        "means": "The payer could not be tied to any customer account.",
        "action": "Identify the payer from the bank narrative, or return the funds.",
    },
    "CURRENCY_MISMATCH": {
        "label": "Currency mismatch",
        "severity": "high",
        "means": "The invoice and the payment are denominated in different currencies.",
        "action": "Re-value at the agreed rate and book the FX difference.",
    },
    "NO_PAYMENT": {
        "label": "Invoice unpaid",
        "severity": "low",
        "means": "The invoice is past due with nothing received against it.",
        "action": "Route to collections once past the agreed terms.",
    },
    "UNAPPLIED_PAYMENT": {
        "label": "Unapplied payment",
        "severity": "medium",
        "means": "Cash was received but no invoice could be identified for it.",
        "action": "Request remittance advice from the customer.",
    },
    "REVERSAL": {
        "label": "Reversal or refund",
        "severity": "medium",
        "means": "A negative entry that must be offset against the original receipt.",
        "action": "Locate the original payment and net the pair off.",
    },
    "AMBIGUOUS_MATCH": {
        "label": "Ambiguous match",
        "severity": "medium",
        "means": "A candidate was found but the confidence is too low to post automatically.",
        "action": "Confirm or reject the proposal in the workspace.",
    },
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
BUCKETS = ("0-7", "8-30", "31-60", "60+")
SLA_DAYS = {"high": 3, "medium": 7, "low": 30}

# Deterministic round-robin owners. A real deployment would use a routing table.
ASSIGNEES = ("a.beridze", "n.kapanadze", "t.gogia")

DUPLICATE_WINDOW_DAYS = 10


@dataclass
class Finding:
    reason_code: str
    entity_type: str
    entity_id: int
    entity_label: str
    customer_name: str
    value_minor: int          # GEL minor units at risk
    opened_on: str
    detail: str
    severity: str = ""
    age_days: int = 0
    bucket: str = ""
    assignee: str = ""
    status: str = "open"
    notes: list = field(default_factory=list)


def bucket_for(age_days: int) -> str:
    if age_days <= 7:
        return "0-7"
    if age_days <= 30:
        return "8-30"
    if age_days <= 60:
        return "31-60"
    return "60+"


def breaches_sla(severity: str, age_days: int) -> bool:
    return age_days > SLA_DAYS.get(severity, 30)


def derive_exceptions(invoices: list[dict], payments: list[dict],
                      proposals: list, as_of: date) -> list[Finding]:
    """Pure derivation of the exception queue from the reconciliation state."""
    inv_by_id = {i["id"]: i for i in invoices}
    pay_by_id = {p["id"]: p for p in payments}

    matched_inv: set[int] = set()
    matched_pay: set[int] = set()
    for prop in proposals:
        if prop.status in ("auto", "confirmed"):
            matched_inv.update(prop.invoice_ids)
            matched_pay.update(prop.payment_ids)

    findings: list[Finding] = []

    # 1. Differences on matches that did post.
    for prop in proposals:
        if prop.status not in ("auto", "confirmed"):
            continue
        invs = [inv_by_id[i] for i in prop.invoice_ids if i in inv_by_id]
        pays = [pay_by_id[p] for p in prop.payment_ids if p in pay_by_id]
        if not invs or not pays:
            continue
        label = "/".join(i["invoice_no"] for i in invs)
        customer = invs[0]["customer_name"]
        opened = max(p["value_date"] for p in pays)

        inv_native = sum(i["amount_minor"] for i in invs)
        # An invoice belongs to at most one match, so its id identifies this finding
        # uniquely. A constant here would collide under the table's uniqueness
        # constraint and silently keep only the first difference of each kind.
        anchor = invs[0]["id"]

        currencies = {i["currency"] for i in invs} | {p["currency"] for p in pays}
        if len(currencies) > 1:
            findings.append(Finding(
                "CURRENCY_MISMATCH", "match", anchor, label, customer,
                abs(prop.delta_minor), opened,
                f"Invoice in {invs[0]['currency']}, payment in {pays[0]['currency']}. "
                f"Compared in GEL at the {RATE_NOTE} rate.",
            ))
            continue

        delta_native = sum(p["amount_minor"] for p in pays) - inv_native
        if not within_tolerance(inv_native, delta_native):
            code = "OVER_PAY" if delta_native > 0 else "SHORT_PAY"
            findings.append(Finding(
                code, "match", anchor, label, customer, abs(prop.delta_minor), opened,
                f"Settled {prop.shape} against {label}; difference of "
                f"{abs(delta_native) / 100:,.2f} {invs[0]['currency']} outside the "
                f"matching tolerance.",
            ))

    # 2. Proposals that were not confident enough to post.
    for prop in proposals:
        if prop.status != "proposed":
            continue
        invs = [inv_by_id[i] for i in prop.invoice_ids if i in inv_by_id]
        pays = [pay_by_id[p] for p in prop.payment_ids if p in pay_by_id]
        if not invs or not pays:
            continue
        findings.append(Finding(
            "AMBIGUOUS_MATCH", "payment", pays[0]["id"], pays[0]["payment_ref"],
            invs[0]["customer_name"], abs(pays[0]["gel_minor"]),
            pays[0]["value_date"],
            f"Scored {prop.confidence}/100 against {invs[0]['invoice_no']} - above the "
            f"review floor but below the auto-post threshold.",
        ))

    # 3. Payments nothing could be done with.
    seen_dupe: set[int] = set()
    by_customer_amount: dict[tuple, list[dict]] = {}
    for pay in payments:
        if pay["amount_minor"] > 0:
            by_customer_amount.setdefault(
                (pay["customer_code"] or pay["payer_name"], pay["amount_minor"], pay["currency"]),
                [],
            ).append(pay)

    for group in by_customer_amount.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda p: (p["value_date"], p["id"]))
        for earlier, later in zip(ordered, ordered[1:]):
            # Only unsettled cash is suspicious. Two equal payments that both found a
            # home are an instalment plan, not a double payment.
            if later["id"] in matched_pay:
                continue
            gap = (date.fromisoformat(later["value_date"])
                   - date.fromisoformat(earlier["value_date"])).days
            if 0 <= gap <= DUPLICATE_WINDOW_DAYS and later["id"] not in seen_dupe:
                seen_dupe.add(later["id"])
                findings.append(Finding(
                    "DUPLICATE_SUSPECTED", "payment", later["id"], later["payment_ref"],
                    later["payer_name"], abs(later["gel_minor"]), later["value_date"],
                    f"Same payer and amount as {earlier['payment_ref']} "
                    f"({earlier['value_date']}), {gap} day(s) earlier.",
                ))

    for pay in sorted(payments, key=lambda p: p["id"]):
        if pay["amount_minor"] < 0:
            findings.append(Finding(
                "REVERSAL", "payment", pay["id"], pay["payment_ref"], pay["payer_name"],
                abs(pay["gel_minor"]), pay["value_date"],
                "Negative entry: reversal or refund awaiting offset against the original receipt.",
            ))
            continue
        if pay["id"] in matched_pay or pay["id"] in seen_dupe:
            continue
        if not pay["customer_code"]:
            findings.append(Finding(
                "UNKNOWN_CUSTOMER", "payment", pay["id"], pay["payment_ref"], pay["payer_name"],
                abs(pay["gel_minor"]), pay["value_date"],
                f"Payer '{pay['payer_name']}' does not correspond to any customer account. "
                f"Bank narrative: {pay['reference'] or '(empty)'}",
            ))
            continue
        if any(f.entity_type == "payment" and f.entity_id == pay["id"] for f in findings):
            continue
        findings.append(Finding(
            "UNAPPLIED_PAYMENT", "payment", pay["id"], pay["payment_ref"], pay["payer_name"],
            abs(pay["gel_minor"]), pay["value_date"],
            f"Cash received with no identifiable invoice. Reference as sent: "
            f"'{pay['reference'] or '(empty)'}'"
            + (" (normalises to nothing usable)" if not normalise_ref(pay["reference"]) else ""),
        ))

    # 4. Invoices with nothing against them, once past due.
    for inv in sorted(invoices, key=lambda i: i["id"]):
        if inv["id"] in matched_inv:
            continue
        if any(f.entity_type == "invoice" and f.entity_id == inv["id"] for f in findings):
            continue
        overdue = (as_of - date.fromisoformat(inv["due_date"])).days
        if overdue <= 0:
            continue
        findings.append(Finding(
            "NO_PAYMENT", "invoice", inv["id"], inv["invoice_no"], inv["customer_name"],
            abs(inv["gel_minor"]), inv["due_date"],
            f"{overdue} day(s) past the due date of {inv['due_date']} with nothing received.",
        ))

    # Enrich deterministically: severity, ageing, owner.
    findings.sort(key=lambda f: (f.reason_code, f.entity_type, f.entity_id, f.entity_label))
    for index, finding in enumerate(findings):
        finding.severity = REASONS[finding.reason_code]["severity"]
        finding.age_days = max(0, (as_of - date.fromisoformat(finding.opened_on)).days)
        finding.bucket = bucket_for(finding.age_days)
        finding.assignee = ASSIGNEES[index % len(ASSIGNEES)]
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], -f.value_minor, f.entity_label))
    return findings


# ---------------------------------------------------------------------- persistence

def persist(conn: sqlite3.Connection, findings: list[Finding]) -> int:
    """Replace the open queue with a freshly derived one, preserving analyst work."""
    kept = {
        (r["reason_code"], r["entity_type"], r["entity_id"]): r
        for r in conn.execute("SELECT * FROM exceptions WHERE status != 'open'")
    }
    conn.execute("DELETE FROM exceptions WHERE status = 'open'")
    written = 0
    for finding in findings:
        key = (finding.reason_code, finding.entity_type, finding.entity_id)
        if key in kept:
            continue  # an analyst already touched this one; leave their work alone
        conn.execute(
            "INSERT OR IGNORE INTO exceptions (reason_code, severity, entity_type, entity_id,"
            " entity_label, customer_name, value_minor, opened_on, age_days, bucket, assignee,"
            " status, detail, resolution, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'',?)",
            (finding.reason_code, finding.severity, finding.entity_type, finding.entity_id,
             finding.entity_label, finding.customer_name, finding.value_minor,
             finding.opened_on, finding.age_days, finding.bucket, finding.assignee,
             finding.status, finding.detail, json.dumps(finding.notes, ensure_ascii=False)),
        )
        written += 1
    conn.commit()
    return written


def rebuild(conn: sqlite3.Connection, as_of: date, actor: str = "system",
            role: str = "supervisor") -> int:
    from app.matching import Proposal

    invoices = [dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id")]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id")]
    proposals = []
    for m in conn.execute("SELECT * FROM matches ORDER BY id"):
        links = conn.execute("SELECT * FROM match_links WHERE match_id = ?", (m["id"],)).fetchall()
        proposals.append(Proposal(
            invoice_ids=tuple(sorted(l["invoice_id"] for l in links if l["invoice_id"])),
            payment_ids=tuple(sorted(l["payment_id"] for l in links if l["payment_id"])),
            method=m["method"], rule=m["rule"], status=m["status"], confidence=m["confidence"],
            shape=m["shape"], invoice_minor=m["invoice_minor"], payment_minor=m["payment_minor"],
            delta_minor=m["delta_minor"], currency=m["currency"],
        ))
    findings = derive_exceptions(invoices, payments, proposals, as_of)
    written = persist(conn, findings)
    audit(conn, actor, role, "EXCEPTIONS_REBUILT", "engine", "exceptions",
          before=None, after={"derived": len(findings), "written": written})
    return written


VALID_STATUS = ("open", "investigating", "resolved")


def transition(conn: sqlite3.Connection, exception_id: int, new_status: str,
               actor: str, role: str, note: str = "", resolution: str = "") -> sqlite3.Row:
    """Move an exception through its lifecycle. Enforces role and writes an audit row."""
    if new_status not in VALID_STATUS:
        raise ControlError(f"'{new_status}' is not a valid exception status.")
    require(role, "resolve" if new_status == "resolved" else "investigate",
            f"exception {exception_id}")

    row = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
    if row is None:
        raise ControlError(f"Exception {exception_id} does not exist.")
    if row["status"] == "resolved":
        raise ControlError(f"Exception {exception_id} is already resolved and is now closed.")
    if new_status == "resolved" and not (resolution or note):
        raise ControlError("A resolution note is required before an exception can be closed.")

    notes = json.loads(row["notes"] or "[]")
    if note:
        notes.append({"ts": now_iso(), "actor": actor, "role": role, "note": note})

    conn.execute(
        "UPDATE exceptions SET status = ?, resolution = ?, notes = ? WHERE id = ?",
        (new_status, resolution or row["resolution"], json.dumps(notes, ensure_ascii=False),
         exception_id),
    )
    conn.commit()
    after = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
    audit(conn, actor, role, f"EXCEPTION_{new_status.upper()}", "exception", exception_id,
          before={"status": row["status"], "resolution": row["resolution"]},
          after={"status": new_status, "resolution": after["resolution"], "note": note})
    return after


def assign(conn: sqlite3.Connection, exception_id: int, assignee: str,
           actor: str, role: str) -> sqlite3.Row:
    require(role, "investigate", f"reassign exception {exception_id}")
    row = conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
    if row is None:
        raise ControlError(f"Exception {exception_id} does not exist.")
    conn.execute("UPDATE exceptions SET assignee = ? WHERE id = ?", (assignee, exception_id))
    conn.commit()
    audit(conn, actor, role, "EXCEPTION_REASSIGNED", "exception", exception_id,
          before={"assignee": row["assignee"]}, after={"assignee": assignee})
    return conn.execute("SELECT * FROM exceptions WHERE id = ?", (exception_id,)).fetchone()
