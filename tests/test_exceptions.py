"""Exception taxonomy: detection, ageing, and the resolution lifecycle."""
from __future__ import annotations

from datetime import date

import pytest

from app import exceptions as exc
from app.controls import ControlError
from app.matching import match

AS_OF = date(2026, 6, 1)


def codes(findings):
    return {f.reason_code for f in findings}


def derive(invoices, payments, as_of=AS_OF):
    return exc.derive_exceptions(invoices, payments, match(invoices, payments), as_of)


def test_short_payment_is_detected(make):
    """A payment materially below the invoice raises SHORT_PAY, not a silent write-off."""
    inv = make.invoice(no="INV-2026-0001", amount=1_000_00)
    pay = make.payment(reference="INV-2026-0001", amount=900_00)
    findings = derive([inv], [pay])
    assert "SHORT_PAY" in codes(findings)
    short = next(f for f in findings if f.reason_code == "SHORT_PAY")
    assert short.value_minor == 100_00


def test_overpayment_is_detected(make):
    """More money than invoiced raises OVER_PAY."""
    inv = make.invoice(no="INV-2026-0001", amount=1_000_00)
    pay = make.payment(reference="INV-2026-0001", amount=1_100_00)
    assert "OVER_PAY" in codes(derive([inv], [pay]))


def test_difference_inside_tolerance_raises_nothing(make):
    """A two-tetri rounding difference is settled, not escalated to a human."""
    inv = make.invoice(no="INV-2026-0001", amount=1_000_00)
    pay = make.payment(reference="INV-2026-0001", amount=999_98)
    findings = derive([inv], [pay])
    assert "SHORT_PAY" not in codes(findings)


def test_duplicate_payment_is_detected(make):
    """The same payer sending the same amount days later is flagged as a suspected duplicate."""
    inv = make.invoice(no="INV-2026-0001", amount=500_00)
    first = make.payment(reference="INV-2026-0001", amount=500_00, days=3)
    second = make.payment(reference="INV-2026-0001", amount=500_00, days=6)
    findings = derive([inv], [first, second])
    assert "DUPLICATE_SUSPECTED" in codes(findings)


def test_matched_instalments_are_not_flagged_as_duplicates(make):
    """Two equal payments that both settle one invoice are an instalment plan, not a duplicate."""
    inv = make.invoice(amount=1_000_00)
    pays = [make.payment(amount=500_00, reference="INSTALMENT 1", days=3),
            make.payment(amount=500_00, reference="INSTALMENT 2", days=6)]
    findings = derive([inv], pays)
    assert "DUPLICATE_SUSPECTED" not in codes(findings)


def test_duplicate_outside_the_window_is_not_flagged(make):
    """Two equal payments months apart are ordinary recurring business."""
    pays = [make.payment(amount=500_00, reference="TRANSFER", days=0),
            make.payment(amount=500_00, reference="TRANSFER", days=60)]
    findings = derive([], pays)
    assert "DUPLICATE_SUSPECTED" not in codes(findings)


def test_unknown_customer_is_detected(make):
    """Cash from a payer with no customer account is flagged as high severity."""
    pay = make.payment(reference="TRANSFER", customer=None, payer="Nobody We Know Ltd")
    findings = derive([], [pay])
    assert "UNKNOWN_CUSTOMER" in codes(findings)
    assert next(f for f in findings if f.reason_code == "UNKNOWN_CUSTOMER").severity == "high"


def test_currency_mismatch_is_detected(make):
    """An invoice and payment in different currencies raise CURRENCY_MISMATCH."""
    inv = make.invoice(no="INV-2026-0001", amount=100_00, currency="USD", gel=270_00)
    pay = make.payment(reference="INV-2026-0001", amount=100_00, currency="GEL", gel=100_00)
    assert "CURRENCY_MISMATCH" in codes(derive([inv], [pay]))


def test_currency_mismatch_suppresses_a_misleading_short_pay(make):
    """A currency difference is reported as such, not disguised as a shortfall."""
    inv = make.invoice(no="INV-2026-0001", amount=100_00, currency="USD", gel=270_00)
    pay = make.payment(reference="INV-2026-0001", amount=100_00, currency="GEL", gel=100_00)
    found = codes(derive([inv], [pay]))
    assert "CURRENCY_MISMATCH" in found and "SHORT_PAY" not in found


def test_unpaid_invoice_raises_only_once_past_due(make):
    """An invoice still inside its terms is not an exception; a late one is."""
    current = make.invoice(issue=date(2026, 5, 25))       # due 2026-06-24, not yet late
    assert "NO_PAYMENT" not in codes(derive([current], []))

    late = make.invoice(issue=date(2026, 4, 1))           # due 2026-05-01, overdue
    assert "NO_PAYMENT" in codes(derive([late], []))


def test_unapplied_payment_is_detected(make):
    """Cash with a known payer but no identifiable invoice is UNAPPLIED_PAYMENT."""
    pay = make.payment(reference="TRANSFER", amount=777_37)
    assert "UNAPPLIED_PAYMENT" in codes(derive([], [pay]))


def test_reversal_is_detected(make):
    """A negative entry is typed as a reversal rather than treated as a payment."""
    pay = make.payment(reference="REVERSAL OF EARLIER CREDIT", amount=-500_00)
    findings = derive([], [pay])
    assert "REVERSAL" in codes(findings)
    assert next(f for f in findings if f.reason_code == "REVERSAL").value_minor == 500_00


def test_ambiguous_match_is_raised_for_a_proposal(make):
    """A proposal below the auto threshold becomes a queue item for a human."""
    inv = make.invoice(no="INV-2026-0143", amount=1_000_00)
    pay = make.payment(reference="PO-88421", amount=950_00)
    assert "AMBIGUOUS_MATCH" in codes(derive([inv], [pay]))


@pytest.mark.parametrize("age,bucket", [
    (0, "0-7"), (7, "0-7"), (8, "8-30"), (30, "8-30"),
    (31, "31-60"), (60, "31-60"), (61, "60+"), (400, "60+"),
])
def test_ageing_bucket_boundaries(age, bucket):
    """Bucket edges are inclusive on the upper bound and do not overlap."""
    assert exc.bucket_for(age) == bucket


@pytest.mark.parametrize("severity,age,breached", [
    ("high", 3, False), ("high", 4, True),
    ("medium", 7, False), ("medium", 8, True),
    ("low", 30, False), ("low", 31, True),
])
def test_sla_breach_depends_on_severity(severity, age, breached):
    """Each severity has its own clock, and the boundary day is not yet a breach."""
    assert exc.breaches_sla(severity, age) is breached


def test_age_is_measured_from_the_dataset_as_of_date(make):
    """Ageing uses the fixed as-of date, so the queue does not drift with the wall clock."""
    pay = make.payment(reference="TRANSFER", value=date(2026, 5, 1))
    finding = next(f for f in derive([], [pay]) if f.reason_code == "UNAPPLIED_PAYMENT")
    assert finding.age_days == 31
    assert finding.bucket == "31-60"


def test_derivation_is_deterministic(make):
    """The same reconciliation state yields the same queue in the same order."""
    invoices = [make.invoice(no=f"INV-2026-{i:04d}", amount=100_00 * i) for i in range(1, 9)]
    payments = [make.payment(reference="TRANSFER", amount=100_00 * i + 37) for i in range(1, 7)]
    first = derive(invoices, payments)
    second = derive(invoices, payments)
    assert [(f.reason_code, f.entity_id, f.assignee) for f in first] == \
           [(f.reason_code, f.entity_id, f.assignee) for f in second]


def test_every_reason_code_has_guidance():
    """Each code carries what it means and what to do, so the UI never shows a bare code."""
    for code, meta in exc.REASONS.items():
        assert meta["means"] and meta["action"]
        assert meta["severity"] in ("high", "medium", "low")


def test_findings_persist_without_colliding(conn, make):
    """Several differences of the same kind are all stored, not collapsed into one row."""
    invoices, payments = [], []
    for i in range(1, 5):
        invoices.append(make.invoice(no=f"INV-2026-{i:04d}", amount=1_000_00))
        payments.append(make.payment(reference=f"INV-2026-{i:04d}", amount=900_00))
    findings = derive(invoices, payments)
    exc.persist(conn, findings)
    stored = conn.execute(
        "SELECT COUNT(*) n FROM exceptions WHERE reason_code = 'SHORT_PAY'").fetchone()["n"]
    assert stored == 4


# ------------------------------------------------------------------------ lifecycle

@pytest.fixture
def one_exception(conn, make):
    inv = make.invoice(no="INV-2026-0001", amount=1_000_00)
    pay = make.payment(reference="INV-2026-0001", amount=900_00)
    exc.persist(conn, derive([inv], [pay]))
    return conn.execute("SELECT id FROM exceptions LIMIT 1").fetchone()["id"]


def test_transition_to_investigating_records_the_note(conn, one_exception):
    """Moving an item forward keeps the analyst's note against it."""
    row = exc.transition(conn, one_exception, "investigating", "a.beridze", "analyst",
                         note="Chased the customer for remittance advice.")
    assert row["status"] == "investigating"
    assert "remittance advice" in row["notes"]


def test_resolving_requires_a_reason(conn, one_exception):
    """An exception cannot be closed silently."""
    with pytest.raises(ControlError):
        exc.transition(conn, one_exception, "resolved", "a.beridze", "analyst")


def test_resolved_items_cannot_be_reopened(conn, one_exception):
    """Once closed, the record is closed; the audit trail holds the history."""
    exc.transition(conn, one_exception, "resolved", "a.beridze", "analyst",
                   resolution="Credit note issued.")
    with pytest.raises(ControlError):
        exc.transition(conn, one_exception, "investigating", "a.beridze", "analyst")


def test_auditor_cannot_change_an_exception(conn, one_exception):
    """The read-only role is refused by the domain function itself."""
    with pytest.raises(ControlError):
        exc.transition(conn, one_exception, "investigating", "audit.ext", "auditor")
    assert conn.execute("SELECT status FROM exceptions WHERE id = ?",
                        (one_exception,)).fetchone()["status"] == "open"


def test_every_transition_writes_an_audit_row(conn, one_exception):
    """State changes are all recorded, with the before and after state."""
    exc.transition(conn, one_exception, "investigating", "a.beridze", "analyst", note="looking")
    exc.transition(conn, one_exception, "resolved", "t.gogia", "supervisor",
                   resolution="Written off")
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type = 'exception' ORDER BY id").fetchall()
    assert [r["action"] for r in rows] == ["EXCEPTION_INVESTIGATING", "EXCEPTION_RESOLVED"]
    assert '"status": "open"' in rows[0]["before_json"]


def test_reassignment_is_audited(conn, one_exception):
    """Changing the owner is a state change and is logged like any other."""
    exc.assign(conn, one_exception, "n.kapanadze", "t.gogia", "supervisor")
    row = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'EXCEPTION_REASSIGNED'").fetchone()
    assert row is not None
    assert "n.kapanadze" in row["after_json"]


def test_analyst_work_survives_a_rebuild(conn, one_exception):
    """Re-deriving the queue does not discard an item somebody is already working on."""
    exc.transition(conn, one_exception, "investigating", "a.beridze", "analyst", note="mine")
    exc.persist(conn, [])
    row = conn.execute("SELECT * FROM exceptions WHERE id = ?", (one_exception,)).fetchone()
    assert row is not None and row["status"] == "investigating"


def test_unknown_status_is_refused(conn, one_exception):
    """An invalid lifecycle state is rejected rather than written to the database."""
    with pytest.raises(ControlError):
        exc.transition(conn, one_exception, "cancelled", "a.beridze", "analyst")
