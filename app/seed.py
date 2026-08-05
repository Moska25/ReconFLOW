"""Seed the database. Idempotent and deterministic: same data on every run, no duplicates.

Order matters: generate the files, import them, match, derive exceptions, then plant the
two demo approvals that make the maker-checker rules demonstrable in an interview. The
last step regenerates data/quality.json by running the test suite, so /quality always
reflects a real run rather than a hand-written claim.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

from app import db, exceptions, generate, ingest, matching
from app.controls import submit_for_approval

ROOT = Path(__file__).resolve().parent.parent
QUALITY_PATH = db.DATA_DIR / "quality.json"


def ensure_files() -> dict:
    out = db.dataset_dir()
    manifest = generate.load_manifest(out)
    if not manifest:
        manifest = generate.write_csvs(out)
    return manifest


def load_customers(conn) -> int:
    path = db.dataset_dir() / "customers.csv"
    if not path.exists():
        return 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    conn.executemany(
        "INSERT OR IGNORE INTO customers (code, name, currency) VALUES (?,?,?)",
        [(r["customer_code"], r["customer_name"], r["currency"]) for r in rows],
    )
    conn.commit()
    return len(rows)


def import_all(conn) -> list[dict]:
    """Import every bundled file, then re-import one of them on purpose.

    The bank re-sending a file it already sent is the single most common way a
    reconciliation system double-posts. Seeding reproduces it so the import page shows a
    real duplicate batch rather than a description of one.
    """
    src = db.dataset_dir()
    batches = []
    order = ["invoices.csv"] + sorted(p.name for p in src.glob("payments_*.csv"))
    for name in order:
        path = src / name
        kind = "invoices" if name.startswith("invoices") else "payments"
        batches.append(ingest.import_bytes(conn, name, path.read_bytes(), kind,
                                           actor="system", role="supervisor"))

    resend = src / "payments_2026_05.csv"
    if resend.exists():
        batches.append(ingest.import_bytes(
            conn, "payments_2026_05.csv (bank resend)", resend.read_bytes(), "payments",
            actor="system", role="supervisor"))
    return batches


def plant_approvals(conn) -> list[int]:
    """Two pending items that make the controls demonstrable.

    One raised by an analyst, which the supervisor can approve. One raised by the
    supervisor, which the supervisor must be refused on: that is segregation of duties
    doing its job, and it is the more interesting of the two.
    """
    if conn.execute("SELECT COUNT(*) n FROM approvals").fetchone()["n"]:
        return []
    top = conn.execute(
        "SELECT * FROM exceptions WHERE reason_code IN ('SHORT_PAY','OVER_PAY')"
        " ORDER BY value_minor DESC LIMIT 2"
    ).fetchall()
    made = []
    plans = [
        ("n.kapanadze", "analyst", "WRITE_OFF",
         "Customer deducted an unagreed early-settlement discount. Propose write-off."),
        ("t.gogia", "supervisor", "ADJUSTMENT",
         "Rate difference on a foreign-currency receipt. Propose adjustment."),
    ]
    for row, (maker, role, action, reason) in zip(top, plans):
        made.append(submit_for_approval(
            conn,
            action_type=action,
            entity_type="exception",
            entity_id=row["id"],
            entity_label=row["entity_label"],
            payload={"reason": reason, "exception_id": row["id"],
                     "reason_code": row["reason_code"]},
            amount_minor=row["value_minor"],
            maker=maker,
            maker_role=role,
        ))
    return made


def write_quality(force: bool = False) -> dict:
    """Regenerate data/quality.json by actually running the suite."""
    if os.environ.get("VERCEL"):
        # ponytail: read-only filesystem, no test run there. The committed report is it.
        return json.loads(QUALITY_PATH.read_text(encoding="utf-8")) \
            if QUALITY_PATH.exists() else {}
    if QUALITY_PATH.exists() and not force:
        return json.loads(QUALITY_PATH.read_text(encoding="utf-8"))
    env = dict(os.environ, RECONFLOW_SEEDING="1")
    try:
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT, env=env, capture_output=True, timeout=600, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        QUALITY_PATH.write_text(json.dumps(
            {"status": "not run", "detail": f"{type(exc).__name__}: {exc}", "cases": []},
            indent=2), encoding="utf-8")
    if not QUALITY_PATH.exists():
        QUALITY_PATH.write_text(json.dumps(
            {"status": "not run",
             "detail": "pytest did not produce a report; run ./.venv/bin/python -m pytest",
             "cases": []}, indent=2), encoding="utf-8")
    return json.loads(QUALITY_PATH.read_text(encoding="utf-8"))


def seed(force: bool = False) -> dict:
    ensure_files()
    conn = db.fresh()
    if db.is_seeded(conn) and not force:
        summary = matching.summarise(conn)
        summary["skipped"] = "already seeded"
        conn.close()
        write_quality()
        return summary

    load_customers(conn)
    import_all(conn)
    stats = matching.run_matching(conn)
    stats["exceptions"] = exceptions.rebuild(conn, generate.AS_OF)
    stats["approvals"] = len(plant_approvals(conn))
    conn.close()
    write_quality(force=True)
    return stats


def main() -> None:
    force = "--force" in sys.argv
    if force and db.DB_PATH.exists():
        db.DB_PATH.unlink()
    stats = seed(force=force)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
