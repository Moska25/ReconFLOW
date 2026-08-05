# ReconFLOW — roadmap

## Status

Phases 1 to 5 are built and working. The app runs on port 8012 via `./run.sh`, seeds
deterministically from a fixed seed, and serves seven pages plus two detail views. On the
bundled dataset (60 customers, 402 invoices, 411 payments over four months) the engine
matches 72.7% of payments automatically and raises 227 typed exceptions. The import
pipeline is idempotent by file hash and by row key. Maker-checker approval, role-based
access and an append-only audit log are enforced in the domain layer, not the templates.
Bank statements in SWIFT MT940 and ISO 20022 CAMT.053 import through that same pipeline.
**The suite is green: 214 tests passing** (`./.venv/bin/python -m pytest -q`).

Phase 10 has landed: the app now wears the "ledger terminal" identity - parchment document
surfaces for approvals and the audit log, ruled figure columns, a rubber stamp for approval
state, the match confidence drawn as a stacked contribution bar against the auto-post
threshold, a value-at-risk strip in the header, and a severity/ageing rhythm in the
exception queue. It was a restyle only: no matching, control, ingestion or reporting logic
changed, and every displayed figure is the one the engine already computed. Phase 11's
screenshots are in `docs/screenshots/` and the hero is linked from the README.

A second design pass then took the boxes out: the dashboard leads with one figure instead of
four equal cards, filter bars are rails rather than cards floating on cards, supporting
figures are hairline-led, and the audit log states what changed per entry instead of printing
two columns of raw JSON.

Phase 6 (bank statement formats) and Phase 12 (showcase polish) are done. Errors render in
the app shell with their real status code, the app has a favicon and page metadata, every
table is captioned with column scope behind a working skip link, the workspace draws each
match against the auto-post threshold, the exception detail carries an SLA clock, `/quality`
reads as an evidence sheet rather than 231 rows, and every page survives an empty database.
**Phases 7, 8 and 9 are not started** and are the next work: analyst-feedback learning,
SLA notification rules, and period close.

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

## Phase 6 — Bank statement formats (done)

**One deviation from the task text, decided during the build.** RCN-6.1 asked for rows
carrying "the same keys `validate_payment()` produces" - that is, already-validated output
with `amount_minor` and `gel_minor`. Both parsers instead return the *input* shape
(`amount` and `value_date` as strings, keyed by the same canonical field names) and hand it
to `validate_payment()` unchanged. The reason is RCN-6.3: the rejected-row machinery only
runs if validation runs. Returning finished rows would have given statements a second,
weaker validation path and no rejection report, which is the opposite of the point. The
observable outcome asked for is unchanged - a statement produces payment rows - and a
malformed statement line now lands in the rejected-row report with a reason like any CSV row.

- [x] **RCN-6.1** Parse MT940 statements into the existing payment row shape.
      Files: `app/statements.py` (new), `tests/test_statements.py` (new)
      Done when: `parse_mt940(text)` returns rows with the same keys
      `validate_payment()` produces, tags 61/86 fields onto `reference`, and a fixture
      statement of 10 transactions yields 10 rows with matching amounts and value dates.
      Landed as: tags 20/25/28C/60F/61/86/62F. Currency comes from the balance line, the
      bank reference after `//` becomes the payment key, and `:86:` `/ORDP/` and `/REMI/`
      sub-fields give the payer and the remittance text. Continuation lines are folded into
      their tag, and a line that does not parse is skipped rather than failing the file.
- [x] **RCN-6.2** Parse CAMT.053 XML into the same shape.
      Files: `app/statements.py`, `tests/test_statements.py`
      Done when: `parse_camt053(xml)` reads `Ntry` entries including `CdtDbtInd` so debit
      entries arrive as negative amounts, and a fixture file round-trips to the same rows
      as its MT940 equivalent.
      Landed as: elements are resolved by local name, so any camt.053 minor version parses.
      `test_the_two_formats_produce_the_same_payments` runs both fixtures through
      `validate_payment` and compares every field.
- [x] **RCN-6.3** Accept statement uploads through the import route.
      Files: `app/ingest.py`, `app/main.py`, `app/templates/import.html`
      Done when: uploading a `.sta` or `.xml` file is detected by content and imported
      through the existing batch, hashing and rejected-row machinery unchanged.
      Landed as: a four-line branch in `import_bytes` swaps `read_rows` for
      `statements.as_rows`; nothing downstream changed. A statement always imports as
      payments whatever the form's dropdown said. Two synthetic sample statements ship at
      `data/samples/`, both settling the same eight unpaid GEL invoices in this dataset.

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

## Phase 10 — Visual identity: "ledger terminal" (done)

Design spec: `MOSKA_MAIN/shared/UI_DIRECTION.md`, ReconFLOW section. Restyle only:
matching, controls, ingestion and every computed figure stay exactly as they are.

- [x] **RCN-10.1** Add the parchment document surface, ruled-ledger table hairlines, and a
      monetary type scale that makes amounts optically dominant.
      Files: `app/static/app.css`
      Done when: amounts are the most prominent element in any row and text on the
      parchment surface still passes WCAG AA.
      Landed as: `.doc` scopes the palette variables, so every base.css component inside it
      re-skins to parchment without a variant of its own. Figures run at 14.5px/560 against
      13px body text with a rule down the left of the `.num` column. Contrast on parchment
      measured in the browser: ink 15.9:1, muted 6.5:1, faint 5.5:1, all against the ruled
      surface rather than the flat one.
- [x] **RCN-10.2** Build the rotated rubber-stamp component for APPROVED / REFUSED / PENDING.
      Files: `app/templates/approvals.html`, `audit.html`, `app/static/app.css`
      Done when: state is legible without colour alone and the stamp never overlaps or
      obscures text at 375px.
      Landed as: the word plus the frame carries the state - solid for approved, dashed for
      pending, a diagonal cancel bar behind the word for refused. Margins hold the clearance
      the rotation eats and the rotation drops to 3 degrees below 640px. Measured: zero
      overlaps against any text box on `/approvals` and `/audit` at both 375px and 1440px,
      closest clearance 11px.
- [x] **RCN-10.3** Replace the numeric confidence breakdown with a stacked contribution bar
      (amount / date / customer / reference) with the auto-post threshold drawn as a line.
      Files: `app/templates/match_detail.html`, `app/static/app.css`
      Done when: a match scoring 60 visibly falls short of the 85 threshold line.
      Landed as: on match 294 (confidence 60) the stack ends at 59.5% of the track, the
      threshold line sits at 84.8%, and the gap is hatched - 79px of visible shortfall at a
      375px viewport. The component `why` strings are kept as the legend beneath.
- [x] **RCN-10.4** Add a persistent value-at-risk strip to the header.
      Files: `app/templates/_layout.html`, `app/reporting.py`, `app/static/app.css`
      Done when: unapplied value and open exception count are visible on every page,
      fed by the existing reporting query with no new SQL.
      Landed as: `reporting.value_at_risk()` composes `unapplied_by_currency()` and
      `ageing()`; it writes no SQL of its own. Assembled in `ctx()` so it is on every page.
      The topbar wraps rather than squeezing navigation when the window is narrow.
- [x] **RCN-10.5** Give exception severity and ageing a visual rhythm (leading severity rule
      per row, ageing as a small bar) instead of plain pills.
      Files: `app/templates/exceptions.html`, `app/static/app.css`
      Landed as: an inset rule on the first cell keyed to severity, a HIGH/MED/LOW tag so
      severity never rests on colour, and age as a bar scaled to the oldest item in the
      current result set alongside the day count.

## Phase 11 — Showcase assets (done)

- [x] **RCN-11.1** Capture screenshots into `docs/screenshots/`: hero (dashboard), match
      confidence breakdown, approvals with stamps, audit log, plus one at 375px.
      Done when: five captioned PNGs exist, taken after Phase 10 lands.
      Landed as seven: `hero.png`, `match-confidence.png`, `approvals-stamps.png`,
      `audit-log.png`, `exception-queue.png`, `mobile-375.png`, `mobile-375-queue.png`.
      Note for whoever re-shoots these: Chrome's `--headless --screenshot` clamps
      `--window-size` at 500px and ignores `<meta viewport>`, so a "375px" CLI capture is
      really a crop of a desktop layout. Drive `Emulation.setDeviceMetricsOverride` over the
      DevTools protocol instead.
- [x] **RCN-11.2** Link the hero image at the top of README.md.

## Phase 12 — Showcase polish (done)

The engine is done and honest; these are the things a recruiter meets in the first two
minutes and the things an accessibility audit fails on. Restyle and presentation only:
no matching, control or ingestion behaviour changes, and no displayed figure changes.

**One real bug fell out of RCN-12.7.** `reporting.import_health()` summed without
`COALESCE` on two columns, so over an empty `import_batches` table SQLite returned NULL and
a first run printed "None" where a count belongs. Fixed at the query. Everything else in
this phase was presentation.

- [x] **RCN-12.1** Serve errors inside the page shell instead of as raw JSON.
      Files: `app/main.py`, `app/templates/error.html` (new), `tests/test_app.py`
      Done when: `GET /nope` returns 404 as a rendered page carrying an `h1`, a `.lede` and
      a route back, `POST /audit` returns 405 rendered the same way, and neither response
      body contains `{"detail"`. The handler must not swallow the status code: a 404 still
      answers 404, because a showcase that returns 200 for a missing page is lying to the
      crawler as well as to the reader.
      Landed as: one Starlette exception handler renders `error.html` for every HTTP
      error and passes the status through, so 404 answers 404 and 405 answers 405.
- [x] **RCN-12.2** Give the app an identity in the browser chrome.
      Files: `app/main.py`, `app/templates/_layout.html`, `app/static/favicon.svg` (new)
      Done when: `/favicon.svg` and `/favicon.ico` both return 200 from a local file with no
      CDN, every page carries a `meta name="description"` and a `theme-color`, and a detail
      page's `<title>` names the record it shows rather than repeating the project name.
      Landed as: `app/static/favicon.svg`, two ledger columns brought level, served at
      both `/favicon.svg` and `/favicon.ico` from the one local file. No CDN anywhere.
- [x] **RCN-12.3** Accessibility pass over the shared shell.
      Files: `app/templates/_layout.html`, every template, `app/static/app.css`
      Done when: a "Skip to content" link is the first focusable element on every page and
      moves focus to `<main id="content">`; every `table.data` carries a `<caption>` (visually
      hidden is fine) and `scope` on its header cells; the flash message region is
      `aria-live="polite"`; and `nav` / `main` / `footer` are landmarks with accessible names.
      The skip link must be visible when focused, not permanently hidden.
      Landed as: 87 header cells took `scope`, 17 tables took a screen-reader caption.
      The skip link carries no transition, so it appears on the frame it is focused
      rather than depending on animation running at all; measured at top 12px, fully
      on screen, over CDP with focus emulation on.
- [x] **RCN-12.4** Make the workspace table show the engine's decision, not only its number.
      Files: `app/templates/workspace.html`, `app/static/app.css`
      Done when: every match row draws its confidence on a shared scale with the auto-post
      threshold marked, so a reader scanning the table can see which rows cleared the bar
      without reading a single number, and the column still reports the numeric confidence
      for anyone who wants it.
      Landed as: a 74px bar per row filled to the score, with the auto-post threshold
      drawn as a tick. Filtering to `proposed` gives a column of bars that all stop
      short of it.
- [x] **RCN-12.5** Put the SLA clock on the exception detail page.
      Files: `app/templates/exception_detail.html`, `app/exceptions.py`, `app/static/app.css`
      Done when: the page shows days elapsed against the threshold for that item's severity
      as a bar, a breach is legible without relying on colour, and the figure comes from
      `SLA_DAYS` rather than being restated in the template.
      Landed as: days elapsed over the limit for that severity, hatched when breached
      so it survives greyscale, plus a sentence stating the overrun in days.
- [x] **RCN-12.6** Rebuild `/quality` as an evidence sheet rather than a wall of rows.
      Files: `app/templates/quality.html`, `app/main.py`
      Done when: the page opens with the properties the suite proves, grouped by the area
      they protect, with counts; the full case list is still reachable but behind a
      `<details>` disclosure; and the page renders from `data/quality.json` exactly as now,
      with no hand-written claim about what passed.
      Landed as: six areas, each naming what it protects, with pass counts from the run
      and the case list behind a `<details>`. A module the list does not name still
      appears, so the page cannot quietly hide one. 231 rows became 6 cards.
- [x] **RCN-12.7** Make every page survive an empty database.
      Files: `tests/test_app.py`, whichever templates assume rows exist
      Done when: a test pointing the app at a freshly bootstrapped, unseeded database gets
      200 from all seven nav routes, each showing its `.empty` state, with no
      `ZeroDivisionError`, no `None` formatted as money and no blank panel.

      Landed as: the test found a real first-run bug (NULL sums in `import_health`),
      now fixed. Every nav route serves 200 against an empty database.
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
   accepted. Show the eight rejected rows and their reasons. Upload
   `data/samples/statement_mt940.sta`, press re-run matching, and point out that the
   format was recognised from the content and validated by the same code as a CSV. Finish on
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
- "Parsed SWIFT MT940 and ISO 20022 CAMT.053 bank statements into an existing reconciliation
  pipeline, detecting the format from file content and reusing the same per-row validation
  and rejection reporting, with both formats proven to produce identical payments from
  matched fixtures." — earned by RCN-6.1 through RCN-6.3.
- *NOT YET EARNED* — "Improved match rates using a feedback loop trained on analyst
  decisions." Requires RCN-7.1 through RCN-7.3.
