# ReconFLOW — roadmap

## Status

Phases 1 to 5 are built and working. The app runs on port 8012 via `./run.sh`, seeds
deterministically from a fixed seed, and serves seven pages plus two detail views. On the
bundled dataset (60 customers, 402 invoices, 411 payments over four months) the engine
matches 72.7% of payments automatically and raises 227 typed exceptions. The import
pipeline is idempotent by file hash and by row key. Maker-checker approval, role-based
access and an append-only audit log are enforced in the domain layer, not the templates.
**The suite is green: 185 tests passing** (`./.venv/bin/python -m pytest -q`). Phases 6 to 9
are not started.

## How to pick up a task

1. Read this file and `MOSKA_MAIN/shared/CONVENTIONS.md` before writing anything.
2. Work only the task ids you were assigned. Do not start adjacent work because it looks
   related — if something else needs doing, note it, do not do it.
3. Business logic goes in an importable module under `app/`; route handlers stay thin.
4. Money is integer minor units everywhere. Never introduce a float into an amount path.
5. Run `./run.sh` and `./.venv/bin/python -m pytest -q` before reporting. Both must pass.
6. **Never run a git command.** Do not commit, add, push, branch or stash. Leave the tree
   dirty; the repo owner commits.

## Phase 1 — Data generation and import

- [x] **RCN-1.1** Generate a deterministic synthetic dataset from a fixed seed.
      Files: `app/generate.py`, `data/*.csv`, `data/manifest.json`
      Done when: `write_csvs()` produces byte-identical files on repeat runs and writes a
      manifest of planted scenario counts.
- [x] **RCN-1.2** Parse both decimal conventions and thousands separators.
      Files: `app/ingest.py`, `tests/test_ingest.py`
      Done when: `parse_decimal()` returns 123456 for `1234.56`, `1.234,56`, `1,234.56` and
      `1 234,56`, and raises `ValueError` on unparseable input.
- [x] **RCN-1.3** Resolve columns by alias so order and header spelling do not matter.
      Files: `app/ingest.py`, `tests/test_ingest.py`
      Done when: `resolve_columns()` maps `"  Value Date "`, `VALUE_DATE` and `value-date`
      to the same canonical field.
- [x] **RCN-1.4** Make imports idempotent on file content.
      Files: `app/ingest.py`, `tests/test_ingest.py`
      Done when: importing identical bytes twice records a batch with status `duplicate`,
      accepts 0 rows, and leaves the row count unchanged.
- [x] **RCN-1.5** Reject bad rows individually with a reason.
      Files: `app/ingest.py`, `app/db.py`, `tests/test_ingest.py`
      Done when: a file with one malformed date imports the remaining rows and writes a
      `rejected_rows` entry with reason `BAD_DATE`.
- [x] **RCN-1.6** Normalise currency against a fixed dated rate table.
      Files: `app/ingest.py`, `tests/test_ingest.py`
      Done when: `to_gel(100_00, "USD")` returns `270_00` and an unknown currency raises.

## Phase 2 — Matching engine

- [x] **RCN-2.1** Implement deterministic pass-1 rules in priority order.
      Files: `app/matching.py`, `tests/test_matching.py`
      Done when: `match()` tags results `R1_EXACT_REF`, `R2_AMOUNT_CUSTOMER_DATE`,
      `R3_TOLERANCE_CUSTOMER` and `R4_GROUP_SUM`, and a consumed row cannot be claimed twice.
- [x] **RCN-2.2** Implement configurable tolerance as the larger of absolute and percentage.
      Files: `app/matching.py`, `tests/test_matching.py`
      Done when: `within_tolerance(100_00, 50)` is True and `within_tolerance(100_00, 51)`
      is False under the default config, and both edges are asserted inclusively.
- [x] **RCN-2.3** Implement pass-2 weighted scoring with a per-component breakdown.
      Files: `app/matching.py`, `tests/test_matching.py`
      Done when: `score_pair()` returns a confidence equal to the rounded sum of component
      contributions, each carrying a human-readable `why` string.
- [x] **RCN-2.4** Support many-to-one and one-to-many groupings.
      Files: `app/matching.py`, `tests/test_matching.py`
      Done when: three invoices settled by one payment produce a single `n:1` proposal with
      zero difference, and two payments settling one invoice produce a `1:n` proposal.
- [x] **RCN-2.5** Guarantee determinism.
      Files: `app/matching.py`, `tests/test_matching.py`
      Done when: `match()` called twice on the same inputs returns identical keys,
      confidences and rules in identical order.
- [x] **RCN-2.6** Preserve manual confirmations across a re-match.
      Files: `app/matching.py`, `tests/test_matching.py`
      Done when: `run_matching()` leaves rows with `method='manual'` untouched and excludes
      their invoices and payments from re-matching.

## Phase 3 — Exception queue

- [x] **RCN-3.1** Define the reason-code taxonomy with severity and guidance.
      Files: `app/exceptions.py`, `tests/test_exceptions.py`
      Done when: `REASONS` holds all nine codes, each with a non-empty `means` and `action`
      and a severity in high/medium/low.
- [x] **RCN-3.2** Derive exceptions purely from the reconciliation state.
      Files: `app/exceptions.py`, `tests/test_exceptions.py`
      Done when: `derive_exceptions(invoices, payments, proposals, as_of)` detects each of
      the nine codes on a crafted fixture and returns the same order on repeat calls.
- [x] **RCN-3.3** Add ageing buckets and per-severity SLA.
      Files: `app/exceptions.py`, `tests/test_exceptions.py`
      Done when: `bucket_for()` maps 7 to `0-7`, 8 to `8-30`, 30 to `8-30`, 31 to `31-60`
      and 61 to `60+`, and `breaches_sla("high", 3)` is False while `("high", 4)` is True.
- [x] **RCN-3.4** Restrict duplicate detection to unsettled cash.
      Files: `app/exceptions.py`, `tests/test_exceptions.py`
      Done when: two equal payments that both belong to a posted match raise no
      `DUPLICATE_SUSPECTED`, while an unmatched repeat of a matched payment does.
- [x] **RCN-3.5** Implement the resolution lifecycle with notes.
      Files: `app/exceptions.py`, `tests/test_exceptions.py`
      Done when: `transition()` refuses `resolved` without a note or resolution, refuses
      reopening a resolved item, and preserves analyst work across a queue rebuild.

## Phase 4 — Controls

- [x] **RCN-4.1** Implement role-based permissions enforced in the domain layer.
      Files: `app/controls.py`, `tests/test_controls.py`
      Done when: `require("auditor", "resolve")` raises `ControlError` and the auditor holds
      only `read`.
- [x] **RCN-4.2** Implement maker-checker with self-approval refused.
      Files: `app/controls.py`, `tests/test_controls.py`
      Done when: `decide_approval()` raises when checker equals maker for both `approved`
      and `rejected`, and the approval remains `pending`.
- [x] **RCN-4.3** Make the audit log append-only at the database level.
      Files: `app/db.py`, `tests/test_controls.py`
      Done when: `UPDATE` and `DELETE` against `audit_log` both raise
      `sqlite3.IntegrityError` mentioning "append-only".
- [x] **RCN-4.4** Write an audit row on every state transition.
      Files: `app/controls.py`, `app/exceptions.py`, `app/matching.py`, `app/ingest.py`
      Done when: import, matching run, exception transition, reassignment, approval request
      and approval decision each write exactly one row capturing before and after state.

## Phase 5 — Web application

- [x] **RCN-5.1** Build the operations dashboard from computed figures only.
      Files: `app/reporting.py`, `app/templates/dashboard.html`, `tests/test_app.py`
      Done when: the rate shown on `/` equals `matching.summarise(conn)["auto_match_rate"]`.
- [x] **RCN-5.2** Build the workspace and the match detail view.
      Files: `app/main.py`, `app/templates/workspace.html`, `app/templates/match_detail.html`
      Done when: `/workspace/match/{id}` renders each scoring component with its
      contribution and explanation for a pass-2 match, and the rule reasoning for a pass-1 match.
- [x] **RCN-5.3** Build the exception queue and detail with investigation actions.
      Files: `app/main.py`, `app/templates/exceptions.html`, `app/templates/exception_detail.html`
      Done when: filters by reason, severity, status, bucket and owner each return 200 and
      an empty result renders an `.empty` block.
- [x] **RCN-5.4** Build the approvals queue that refuses the wrong actor over HTTP.
      Files: `app/main.py`, `app/templates/approvals.html`, `tests/test_app.py`
      Done when: posting a decision as the maker returns a page containing "Segregation of
      duties" and leaves the approval `pending`.
- [x] **RCN-5.5** Build the audit page and the import page with batch history.
      Files: `app/main.py`, `app/templates/audit.html`, `app/templates/import.html`
      Done when: `/import` lists each batch with accepted/rejected/duplicate counts and the
      reason for every rejected row.
- [x] **RCN-5.6** Generate the quality page from the test run.
      Files: `tests/conftest.py`, `app/seed.py`, `app/templates/quality.html`
      Done when: a pytest run writes `data/quality.json` and seeding regenerates it, so
      `/quality` is never empty.
- [x] **RCN-5.7** Hold the layout at 375px with no horizontal page scroll.
      Files: `app/static/app.css`, all templates
      Done when: at a 375px viewport `document.documentElement.scrollWidth` equals
      `clientWidth` on every route and every `table.data` sits inside a `.table-wrap`.

## Phase 6 — Bank statement formats

- [ ] **RCN-6.1** Parse MT940 statements into the existing payment row shape.
      Files: `app/statements.py` (new), `tests/test_statements.py` (new)
      Done when: `parse_mt940(text)` returns rows with the same keys
      `validate_payment()` produces, tags 61/86 fields onto `reference`, and a fixture
      statement of 10 transactions yields 10 rows with matching amounts and value dates.
- [ ] **RCN-6.2** Parse CAMT.053 XML into the same shape.
      Files: `app/statements.py`, `tests/test_statements.py`
      Done when: `parse_camt053(xml)` reads `Ntry` entries including `CdtDbtInd` so debit
      entries arrive as negative amounts, and a fixture file round-trips to the same rows
      as its MT940 equivalent.
- [ ] **RCN-6.3** Accept statement uploads through the import route.
      Files: `app/ingest.py`, `app/main.py`, `app/templates/import.html`
      Done when: uploading a `.sta` or `.xml` file is detected by content and imported
      through the existing batch, hashing and rejected-row machinery unchanged.

## Phase 7 — Learning from analyst decisions

- [ ] **RCN-7.1** Record every manual match and rejection as labelled training data.
      Files: `app/db.py`, `app/matching.py`, `tests/test_matching.py`
      Done when: confirming or rejecting a proposal writes a `match_feedback` row holding
      the component scores at decision time and the analyst's verdict.
- [ ] **RCN-7.2** Fit component weights from accumulated feedback.
      Files: `app/learning.py` (new), `tests/test_learning.py` (new)
      Done when: `fit_weights(feedback)` returns weights summing to 100 for at least 50
      labelled rows, falls back to `DEFAULT_CONFIG` below that, and is deterministic for a
      fixed input ordering.
- [ ] **RCN-7.3** Show fitted weights against defaults before adopting them.
      Files: `app/templates/quality.html`, `app/main.py`
      Done when: `/quality` renders both weight sets side by side with the count of
      feedback rows behind the fitted one, and adoption is an explicit action rather than
      automatic.

## Phase 8 — SLA notification rules

- [ ] **RCN-8.1** Make SLA thresholds configurable per reason code rather than per severity.
      Files: `app/exceptions.py`, `tests/test_exceptions.py`
      Done when: `SLA_DAYS` accepts a reason-code override and `breaches_sla()` prefers the
      code-specific value when one is set.
- [ ] **RCN-8.2** Evaluate notification rules and record what would be sent.
      Files: `app/notifications.py` (new), `tests/test_notifications.py` (new)
      Done when: `due_notifications(conn, as_of)` returns one entry per breaching exception
      per escalation step, never repeats a step already recorded, and writes nothing when
      run twice for the same as_of date.
- [ ] **RCN-8.3** Surface the notification log in the UI.
      Files: `app/templates/exceptions.html`, `app/main.py`
      Done when: an exception detail page lists the escalation steps recorded against it
      with their timestamps.

## Phase 9 — Period close

- [ ] **RCN-9.1** Add an accounting period table with an open/closed state.
      Files: `app/db.py`, `app/periods.py` (new), `tests/test_periods.py` (new)
      Done when: `close_period(conn, "2026-06", actor, role)` refuses while any exception
      dated in that period is unresolved, and requires the supervisor role.
- [ ] **RCN-9.2** Refuse writes against a closed period.
      Files: `app/periods.py`, `app/ingest.py`, `app/exceptions.py`, `tests/test_periods.py`
      Done when: importing a row or resolving an exception dated inside a closed period
      raises `ControlError` naming the period.
- [ ] **RCN-9.3** Produce a close pack summarising the period.
      Files: `app/reporting.py`, `app/templates/close.html` (new), `app/main.py`
      Done when: `/close/2026-06` renders opening and closing unapplied cash, matched value,
      write-offs approved, and the exceptions carried forward.

## Deliberately out of scope

- **Real authentication and user management** — the portfolio point is authorisation logic,
  and a login form would add surface without adding insight.
- **Double-entry general ledger postings** — reconciliation ends at "this cash belongs to
  this invoice"; the ledger is a different system.
- **Live FX rate feeds** — an external dependency that would break offline running, which
  the shared conventions require.
- **Collections and dunning workflow** — a genuinely separate product area; `NO_PAYMENT`
  hands off and stops there.
- **Pagination** — tables cap at 200 or 300 rows with the count shown. Add it when a
  dataset outgrows that, not before.
- **Multi-tenancy** — one organisation, one ledger. Nothing about the domain logic gets
  more interesting with a tenant id threaded through it.

## Demo script (5 minutes)

1. `./run.sh`, open **http://127.0.0.1:8012/**. Point at the auto-match rate of 72.7% and
   say what the denominator is: 411 payments, of which 299 posted with no human involved.
2. Go to **http://127.0.0.1:8012/workspace/match/194**. Walk the four components. Make the
   point that with no usable reference the ceiling is 60 against an auto threshold of 85,
   so an amount coincidence can never post a payment.
3. Go to **http://127.0.0.1:8012/exceptions?severity=high** and open the top item. Show that
   it states what happened, what it means and what to do.
4. Go to **http://127.0.0.1:8012/approvals**. Switch to `t.gogia`, approve the item
   `t.gogia` raised: refused for segregation of duties. Approve the item `n.kapanadze`
   raised: it works. Switch to `audit.ext` and try again: refused as read-only. Say that
   this is enforced in `decide_approval()`, and the buttons stay live on purpose.
5. Go to **http://127.0.0.1:8012/import**. Show the duplicate batch: 103 rows seen, 0
   accepted. Show the eight rejected rows and their reasons. Finish on
   **http://127.0.0.1:8012/quality** for the test evidence and the planted-scenario table.

## Resume bullets

- "Built a reconciliation engine that matches 72.7% of payments to invoices automatically
  across 411 payments, using deterministic rules plus a weighted scorer that renders a
  per-component explanation for every automated decision." — earned by RCN-2.1, RCN-2.3,
  RCN-2.4, RCN-5.2.
- "Implemented segregation-of-duties controls — maker-checker approval with self-approval
  refused in the domain layer, role-based authorisation, and a database-enforced
  append-only audit trail — verified by 185 tests including the refusal paths driven over
  HTTP." — earned by RCN-4.1 through RCN-4.4, RCN-5.4.
- "Designed a defensive CSV import pipeline with content-hash idempotency, per-row
  validation and a rejected-row report, tolerant of column reordering and both European and
  Anglo decimal conventions." — earned by RCN-1.2 through RCN-1.6.
- *NOT YET EARNED* — "Parsed MT940 and CAMT.053 bank statements into the reconciliation
  pipeline." Requires RCN-6.1 and RCN-6.2.
- *NOT YET EARNED* — "Improved match rates using a feedback loop trained on analyst
  decisions." Requires RCN-7.1 through RCN-7.3.
