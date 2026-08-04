"""SQLite connection helper and schema bootstrap.

Money is stored as INTEGER minor units (tetri / cents) everywhere. Floats are never
used for amounts: the matching tolerance is "exactly 0.02" and a float representation
turns that boundary into a coin toss.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "reconflow.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id            INTEGER PRIMARY KEY,
  code          TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  currency      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
  id            INTEGER PRIMARY KEY,
  filename      TEXT NOT NULL,
  kind          TEXT NOT NULL,              -- invoices | payments | customers
  sha256        TEXT NOT NULL,
  status        TEXT NOT NULL,              -- imported | duplicate
  rows_total    INTEGER NOT NULL DEFAULT 0,
  rows_accepted INTEGER NOT NULL DEFAULT 0,
  rows_rejected INTEGER NOT NULL DEFAULT 0,
  rows_dupe     INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  actor         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejected_rows (
  id            INTEGER PRIMARY KEY,
  batch_id      INTEGER NOT NULL REFERENCES import_batches(id),
  row_no        INTEGER NOT NULL,
  reason        TEXT NOT NULL,              -- BAD_DATE | BAD_AMOUNT | MISSING_REFERENCE | ...
  detail        TEXT NOT NULL,
  raw           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
  id            INTEGER PRIMARY KEY,
  invoice_no    TEXT NOT NULL UNIQUE,
  customer_code TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  issue_date    TEXT NOT NULL,
  due_date      TEXT NOT NULL,
  amount_minor  INTEGER NOT NULL,
  currency      TEXT NOT NULL,
  gel_minor     INTEGER NOT NULL,
  batch_id      INTEGER REFERENCES import_batches(id)
);

CREATE TABLE IF NOT EXISTS payments (
  id            INTEGER PRIMARY KEY,
  payment_ref   TEXT NOT NULL UNIQUE,
  reference     TEXT NOT NULL,              -- the messy remittance reference as received
  customer_code TEXT,                       -- NULL when the payer could not be identified
  payer_name    TEXT NOT NULL,
  value_date    TEXT NOT NULL,
  amount_minor  INTEGER NOT NULL,           -- negative for reversals
  currency      TEXT NOT NULL,
  gel_minor     INTEGER NOT NULL,
  batch_id      INTEGER REFERENCES import_batches(id)
);

CREATE TABLE IF NOT EXISTS matches (
  id            INTEGER PRIMARY KEY,
  method        TEXT NOT NULL,              -- pass1 | pass2 | manual
  rule          TEXT NOT NULL,              -- R1_EXACT_REF | ... | SCORED | MANUAL
  status        TEXT NOT NULL,              -- auto | proposed | confirmed | rejected
  confidence    INTEGER NOT NULL,           -- 0..100
  shape         TEXT NOT NULL,              -- 1:1 | n:1 | 1:n
  invoice_minor INTEGER NOT NULL,
  payment_minor INTEGER NOT NULL,
  delta_minor   INTEGER NOT NULL,           -- payment - invoice, in GEL minor units
  currency      TEXT NOT NULL,
  breakdown     TEXT NOT NULL,              -- JSON: scoring components + why-this-match lines
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS match_links (
  match_id      INTEGER NOT NULL REFERENCES matches(id),
  invoice_id    INTEGER REFERENCES invoices(id),
  payment_id    INTEGER REFERENCES payments(id)
);
CREATE INDEX IF NOT EXISTS ix_links_match ON match_links(match_id);
CREATE INDEX IF NOT EXISTS ix_links_inv   ON match_links(invoice_id);
CREATE INDEX IF NOT EXISTS ix_links_pay   ON match_links(payment_id);

CREATE TABLE IF NOT EXISTS exceptions (
  id            INTEGER PRIMARY KEY,
  reason_code   TEXT NOT NULL,
  severity      TEXT NOT NULL,              -- high | medium | low
  entity_type   TEXT NOT NULL,              -- invoice | payment | match
  entity_id     INTEGER NOT NULL,
  entity_label  TEXT NOT NULL,
  customer_name TEXT NOT NULL,
  value_minor   INTEGER NOT NULL,           -- value at risk, GEL minor units
  opened_on     TEXT NOT NULL,
  age_days      INTEGER NOT NULL,
  bucket        TEXT NOT NULL,              -- 0-7 | 8-30 | 31-60 | 60+
  assignee      TEXT NOT NULL,
  status        TEXT NOT NULL,              -- open | investigating | resolved
  detail        TEXT NOT NULL,
  resolution    TEXT NOT NULL DEFAULT '',
  notes         TEXT NOT NULL DEFAULT '[]', -- JSON list of {ts, actor, note}
  UNIQUE (reason_code, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS approvals (
  id            INTEGER PRIMARY KEY,
  action_type   TEXT NOT NULL,              -- WRITE_OFF | ADJUSTMENT | MANUAL_MATCH
  entity_type   TEXT NOT NULL,
  entity_id     INTEGER NOT NULL,
  entity_label  TEXT NOT NULL,
  payload       TEXT NOT NULL,              -- JSON
  amount_minor  INTEGER NOT NULL,           -- GEL minor units
  maker         TEXT NOT NULL,
  maker_role    TEXT NOT NULL,
  status        TEXT NOT NULL,              -- pending | approved | rejected
  checker       TEXT NOT NULL DEFAULT '',
  checker_role  TEXT NOT NULL DEFAULT '',
  decided_at    TEXT NOT NULL DEFAULT '',
  decision_note TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);

-- Append-only. Enforced by triggers below, not by app-side good intentions.
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,
  actor         TEXT NOT NULL,
  role          TEXT NOT NULL,
  action        TEXT NOT NULL,
  entity_type   TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  before_json   TEXT NOT NULL,
  after_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(id DESC);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys on."""
    target = Path(path) if path is not None else DB_PATH
    if target != Path(":memory:"):
        target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def bootstrap(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def fresh(path: Path | str | None = None) -> sqlite3.Connection:
    """Connection with the schema already applied. Used by seeding and by tests."""
    conn = connect(path)
    bootstrap(conn)
    return conn


def is_seeded(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) AS n FROM import_batches").fetchone()
    return bool(row and row["n"])
