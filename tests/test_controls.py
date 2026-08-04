"""Controls: role permissions, maker-checker segregation, immutable audit."""
from __future__ import annotations

import sqlite3

import pytest

from app import controls
from app.controls import (APPROVAL_THRESHOLD_MINOR, ControlError, audit, blocked_reason,
                          decide_approval, needs_approval, require, role_of,
                          submit_for_approval)


def raise_request(conn, maker="n.kapanadze", role="analyst", amount=80_000_00):
    return submit_for_approval(
        conn, action_type="WRITE_OFF", entity_type="exception", entity_id=1,
        entity_label="INV-2026-0001", payload={"reason": "customer deduction"},
        amount_minor=amount, maker=maker, maker_role=role)


# ------------------------------------------------------------------------- roles

def test_role_lookup_defaults_to_read_only():
    """An unrecognised actor gets the least privilege, never the most."""
    assert role_of("t.gogia") == "supervisor"
    assert role_of("someone.unknown") == "auditor"


def test_auditor_holds_read_only():
    """The auditor role can read and nothing else."""
    assert controls.can("auditor", "read") is True
    for permission in ("investigate", "resolve", "propose", "approve", "import", "rematch"):
        assert controls.can("auditor", permission) is False


def test_analyst_cannot_approve():
    """An analyst may propose but never decide."""
    assert controls.can("analyst", "propose") is True
    assert controls.can("analyst", "approve") is False


def test_supervisor_can_approve():
    """Only the supervisor role carries the approve permission."""
    assert controls.can("supervisor", "approve") is True


def test_require_refuses_the_auditor_with_a_usable_message():
    """The refusal explains what to do, because a user will read it."""
    with pytest.raises(ControlError) as err:
        require("auditor", "resolve", "exception 12")
    message = str(err.value)
    assert "read-only" in message and "exception 12" in message


def test_require_refuses_an_unknown_role():
    """An unmapped role is refused rather than defaulting to permissive."""
    with pytest.raises(ControlError):
        require("administrator", "approve")


# ----------------------------------------------------------------- maker-checker

def test_threshold_decides_whether_approval_is_needed():
    """Small corrections do not need four eyes; the boundary value does."""
    assert needs_approval("WRITE_OFF", APPROVAL_THRESHOLD_MINOR - 1) is False
    assert needs_approval("WRITE_OFF", APPROVAL_THRESHOLD_MINOR) is True
    assert needs_approval("WRITE_OFF", -APPROVAL_THRESHOLD_MINOR) is True


def test_a_maker_cannot_approve_their_own_request(conn):
    """Segregation of duties: the person who raised an item cannot approve it.

    Enforced in decide_approval, so hiding the button is irrelevant to the outcome.
    """
    approval_id = raise_request(conn, maker="t.gogia", role="supervisor")

    with pytest.raises(ControlError) as err:
        decide_approval(conn, approval_id, "approved", "t.gogia", "supervisor")

    assert "Segregation of duties" in str(err.value)
    assert conn.execute("SELECT status FROM approvals WHERE id = ?",
                        (approval_id,)).fetchone()["status"] == "pending"


def test_a_maker_cannot_reject_their_own_request_either(conn):
    """The rule covers rejection as well as approval, and says so in readable English."""
    approval_id = raise_request(conn, maker="t.gogia", role="supervisor")
    with pytest.raises(ControlError) as err:
        decide_approval(conn, approval_id, "rejected", "t.gogia", "supervisor")
    assert "cannot also reject it" in str(err.value)


def test_the_self_approval_message_reads_correctly(conn):
    """The refusal names the action properly rather than mangling the verb."""
    approval_id = raise_request(conn, maker="t.gogia", role="supervisor")
    with pytest.raises(ControlError) as err:
        decide_approval(conn, approval_id, "approved", "t.gogia", "supervisor")
    assert "cannot also approve it" in str(err.value)


def test_an_analyst_cannot_approve_someone_elses_request(conn):
    """Being a different person is not enough; the role must carry the permission."""
    approval_id = raise_request(conn, maker="n.kapanadze", role="analyst")
    with pytest.raises(ControlError):
        decide_approval(conn, approval_id, "approved", "a.beridze", "analyst")


def test_an_auditor_cannot_approve(conn):
    """Read-only stays read-only in the approval queue too."""
    approval_id = raise_request(conn)
    with pytest.raises(ControlError):
        decide_approval(conn, approval_id, "approved", "audit.ext", "auditor")


def test_a_supervisor_can_approve_another_persons_request(conn):
    """The intended path works: a different person holding the right role decides."""
    approval_id = raise_request(conn, maker="n.kapanadze", role="analyst")
    row = decide_approval(conn, approval_id, "approved", "t.gogia", "supervisor",
                          note="Checked against the credit note.")
    assert row["status"] == "approved"
    assert row["checker"] == "t.gogia"
    assert row["decision_note"] == "Checked against the credit note."


def test_a_decision_cannot_be_revisited(conn):
    """Once decided, an item is closed and a second decision is refused."""
    approval_id = raise_request(conn)
    decide_approval(conn, approval_id, "approved", "t.gogia", "supervisor")
    with pytest.raises(ControlError) as err:
        decide_approval(conn, approval_id, "rejected", "t.gogia", "supervisor")
    assert "already approved" in str(err.value)


def test_an_unknown_decision_is_refused(conn):
    """Only approve and reject exist; anything else is an error."""
    approval_id = raise_request(conn)
    with pytest.raises(ControlError):
        decide_approval(conn, approval_id, "maybe", "t.gogia", "supervisor")


def test_deciding_a_missing_approval_is_refused(conn):
    """A non-existent approval id fails loudly."""
    with pytest.raises(ControlError):
        decide_approval(conn, 999, "approved", "t.gogia", "supervisor")


def test_an_auditor_cannot_raise_a_request(conn):
    """The read-only role cannot even start the maker-checker flow."""
    with pytest.raises(ControlError):
        raise_request(conn, maker="audit.ext", role="auditor")


def test_blocked_reason_agrees_with_enforcement(conn):
    """What the UI says is blocked is exactly what the domain function refuses."""
    approval_id = raise_request(conn, maker="t.gogia", role="supervisor")
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()

    assert blocked_reason(row, "t.gogia", "supervisor") == "You raised this request."
    assert blocked_reason(row, "a.beridze", "analyst") != ""
    assert blocked_reason(row, "audit.ext", "auditor") != ""
    assert blocked_reason(row, "n.kapanadze", "supervisor") == ""

    for actor, role in (("t.gogia", "supervisor"), ("a.beridze", "analyst"),
                        ("audit.ext", "auditor")):
        with pytest.raises(ControlError):
            decide_approval(conn, approval_id, "approved", actor, role)


# ------------------------------------------------------------------------- audit

def test_raising_a_request_is_audited(conn):
    """The request itself is an event, not only the decision."""
    approval_id = raise_request(conn)
    row = conn.execute("SELECT * FROM audit_log WHERE action = 'APPROVAL_REQUESTED'").fetchone()
    assert row["entity_id"] == str(approval_id)
    assert row["actor"] == "n.kapanadze"


def test_both_decisions_are_audited(conn):
    """Approval and rejection each write their own distinguishable entry."""
    first = raise_request(conn)
    second = raise_request(conn)
    decide_approval(conn, first, "approved", "t.gogia", "supervisor")
    decide_approval(conn, second, "rejected", "t.gogia", "supervisor")
    actions = {r["action"] for r in conn.execute("SELECT action FROM audit_log")}
    assert {"APPROVAL_APPROVED", "APPROVAL_REJECTED"} <= actions


def test_audit_captures_before_and_after_state(conn):
    """An entry records what changed, not merely that something changed."""
    approval_id = raise_request(conn)
    decide_approval(conn, approval_id, "approved", "t.gogia", "supervisor", note="ok")
    row = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'APPROVAL_APPROVED'").fetchone()
    assert '"status": "pending"' in row["before_json"]
    assert '"status": "approved"' in row["after_json"]
    assert row["role"] == "supervisor"


def test_a_refused_action_leaves_no_audit_entry(conn):
    """Nothing happened, so nothing is logged: the trail records changes, not attempts."""
    approval_id = raise_request(conn, maker="t.gogia", role="supervisor")
    before = conn.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"]
    with pytest.raises(ControlError):
        decide_approval(conn, approval_id, "approved", "t.gogia", "supervisor")
    after = conn.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"]
    assert before == after


def test_the_audit_log_cannot_be_updated(conn):
    """Immutability is enforced by a database trigger, not by convention."""
    audit(conn, "a.beridze", "analyst", "TEST", "thing", 1, before={"a": 1}, after={"a": 2})
    with pytest.raises(sqlite3.IntegrityError) as err:
        conn.execute("UPDATE audit_log SET actor = 'someone.else' WHERE id = 1")
    assert "append-only" in str(err.value)


def test_the_audit_log_cannot_be_deleted_from(conn):
    """The same trigger blocks deletion, so history cannot be quietly trimmed."""
    audit(conn, "a.beridze", "analyst", "TEST", "thing", 1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"] == 1


def test_audit_survives_an_attempted_mass_delete(conn):
    """A blanket delete is refused as a whole, leaving every row in place."""
    for index in range(3):
        audit(conn, "a.beridze", "analyst", "TEST", "thing", index)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log")
    assert conn.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"] == 3


def test_audit_serialises_non_json_values(conn):
    """Dates and other objects are recorded rather than crashing the write."""
    from datetime import date
    entry = audit(conn, "a.beridze", "analyst", "TEST", "thing", 1,
                  after={"when": date(2026, 6, 1)})
    row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (entry,)).fetchone()
    assert "2026-06-01" in row["after_json"]
