"""Controls: role-based access, maker-checker approval, immutable audit trail.

This is the module that separates an operations platform from a dashboard. Three rules
live here and nowhere else:

1. Permission is checked in the domain function. A template that forgets to hide a button
   must still be unable to perform the action.
2. A maker cannot approve their own item. Segregation of duties is meaningless if the
   only thing stopping you is a disabled button.
3. Every state transition writes an audit row. The audit table is append-only at the
   database level (see db.SCHEMA triggers), so a bug in this file cannot rewrite history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

# Approval is required for value movements above this. Below it, an analyst acts alone —
# a four-eyes check on a GEL 3 rounding write-off is theatre, not control.
APPROVAL_THRESHOLD_MINOR = 50_000_00

ROLES: dict[str, dict] = {
    "analyst": {
        "label": "Analyst",
        "note": "Investigates and resolves exceptions, proposes write-offs and manual matches.",
        "permissions": {"read", "investigate", "resolve", "propose", "import", "rematch"},
    },
    "supervisor": {
        "label": "Supervisor",
        "note": "Everything an analyst can do, plus approving other people's requests.",
        "permissions": {"read", "investigate", "resolve", "propose", "import", "rematch", "approve"},
    },
    "auditor": {
        "label": "Auditor",
        "note": "Read-only. Can inspect every record and the full audit trail, can change nothing.",
        "permissions": {"read"},
    },
}

# Demo identities. Two analysts exist on purpose: maker-checker needs more than one human.
ACTORS: dict[str, str] = {
    "a.beridze": "analyst",
    "n.kapanadze": "analyst",
    "t.gogia": "supervisor",
    "audit.ext": "auditor",
}
DEFAULT_ACTOR = "a.beridze"


class ControlError(Exception):
    """A control refused the action. Carries a message meant to be shown to the user."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def role_of(actor: str) -> str:
    return ACTORS.get(actor, "auditor")


def can(role: str, permission: str) -> bool:
    return permission in ROLES.get(role, {}).get("permissions", set())


def require(role: str, permission: str, what: str = "") -> None:
    """Raise ControlError unless `role` holds `permission`."""
    if role not in ROLES:
        raise ControlError(f"Unknown role '{role}'.")
    if not can(role, permission):
        label = ROLES[role]["label"]
        detail = f" ({what})" if what else ""
        if role == "auditor":
            raise ControlError(
                f"{label} is a read-only role and cannot perform '{permission}'{detail}. "
                "Switch to Analyst or Supervisor to make changes."
            )
        raise ControlError(f"{label} does not hold the '{permission}' permission{detail}.")


# --------------------------------------------------------------------------- audit

def audit(
    conn: sqlite3.Connection,
    actor: str,
    role: str,
    action: str,
    entity_type: str,
    entity_id: str | int,
    before: object = None,
    after: object = None,
) -> int:
    """Append one immutable audit row. Never updated, never deleted."""
    cur = conn.execute(
        "INSERT INTO audit_log (ts, actor, role, action, entity_type, entity_id,"
        " before_json, after_json) VALUES (?,?,?,?,?,?,?,?)",
        (
            now_iso(),
            actor,
            role,
            action,
            entity_type,
            str(entity_id),
            json.dumps(before, ensure_ascii=False, sort_keys=True, default=str),
            json.dumps(after, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ------------------------------------------------------------------- maker-checker

def needs_approval(action_type: str, amount_minor: int) -> bool:
    """Value movements above the threshold need a second pair of eyes."""
    return abs(amount_minor) >= APPROVAL_THRESHOLD_MINOR


def submit_for_approval(
    conn: sqlite3.Connection,
    *,
    action_type: str,
    entity_type: str,
    entity_id: int,
    entity_label: str,
    payload: dict,
    amount_minor: int,
    maker: str,
    maker_role: str,
) -> int:
    """Create a pending approval. The maker must hold 'propose'."""
    require(maker_role, "propose", action_type)
    cur = conn.execute(
        "INSERT INTO approvals (action_type, entity_type, entity_id, entity_label, payload,"
        " amount_minor, maker, maker_role, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?, 'pending', ?)",
        (
            action_type,
            entity_type,
            entity_id,
            entity_label,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            amount_minor,
            maker,
            maker_role,
            now_iso(),
        ),
    )
    approval_id = int(cur.lastrowid)
    conn.commit()
    audit(
        conn, maker, maker_role, "APPROVAL_REQUESTED", "approval", approval_id,
        before=None,
        after={"action_type": action_type, "entity": entity_label,
               "amount_minor": amount_minor, "status": "pending"},
    )
    return approval_id


def decide_approval(
    conn: sqlite3.Connection,
    approval_id: int,
    decision: str,
    checker: str,
    checker_role: str,
    note: str = "",
) -> sqlite3.Row:
    """Approve or reject a pending item.

    Refuses, in this order: unknown item, already decided, wrong role, self-approval.
    Self-approval is checked here rather than in the template on purpose — hiding the
    button is a usability nicety, not a control.
    """
    if decision not in ("approved", "rejected"):
        raise ControlError(f"Decision must be 'approved' or 'rejected', got '{decision}'.")

    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ControlError(f"Approval {approval_id} does not exist.")
    if row["status"] != "pending":
        raise ControlError(
            f"Approval {approval_id} was already {row['status']} by {row['checker']} "
            f"on {row['decided_at']}. A decision cannot be revisited."
        )

    require(checker_role, "approve", f"approval {approval_id}")

    if checker == row["maker"]:
        verb = "approve" if decision == "approved" else "reject"
        raise ControlError(
            f"Segregation of duties: {checker} raised this request and cannot also "
            f"{verb} it. It needs a different approver."
        )

    before = dict(row)
    conn.execute(
        "UPDATE approvals SET status = ?, checker = ?, checker_role = ?, decided_at = ?,"
        " decision_note = ? WHERE id = ?",
        (decision, checker, checker_role, now_iso(), note, approval_id),
    )
    conn.commit()
    after = dict(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
    audit(
        conn, checker, checker_role,
        "APPROVAL_APPROVED" if decision == "approved" else "APPROVAL_REJECTED",
        "approval", approval_id,
        before={"status": before["status"], "maker": before["maker"]},
        after={"status": after["status"], "checker": checker, "note": note},
    )
    return after


def pending_approvals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM approvals WHERE status = 'pending' ORDER BY id"
    ).fetchall()


def blocked_reason(row: sqlite3.Row, actor: str, role: str) -> str:
    """Why the current user cannot decide this item — for showing in the UI.

    Mirrors decide_approval's refusals so the page can explain itself, but decide_approval
    remains the enforcement point. Empty string means the user may decide.
    """
    if row["status"] != "pending":
        return f"Already {row['status']}."
    if not can(role, "approve"):
        return f"{ROLES.get(role, {}).get('label', role)} cannot approve."
    if actor == row["maker"]:
        return "You raised this request."
    return ""
