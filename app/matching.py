"""Two-pass matching engine.

Pass 1 applies deterministic rules in priority order. Pass 2 scores whatever survives and
attaches a per-component breakdown, so every automated decision can be read back in plain
language: "reference differs by one character", "amount differs by GEL 0.02",
"paid 2 days after the invoice".

The engine is pure. `match()` takes lists of dicts and returns proposals; it touches no
database, no clock and no global state, so the same inputs always produce byte-identical
output. `run_matching()` is the thin persistence wrapper around it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from itertools import combinations

from app.controls import audit, now_iso, require

# Georgian letters are kept: references genuinely arrive as "ინვ-2026-0143".
_NOISE = re.compile(r"[^0-9A-ZႠ-ჿ]")
_MIN_CONTAINED = 6      # shorter fragments match by accident, so containment needs length
_CANDIDATE_FLOOR = 30   # below this a pass-2 pair is not worth carrying


@dataclass(frozen=True)
class MatchConfig:
    tol_abs_minor: int = 2          # 0.02 absolute
    tol_pct_bp: int = 50            # 0.5% expressed in basis points, kept integer
    date_window_days: int = 45
    early_grace_days: int = 5       # payments can legitimately land just before the invoice
    auto_threshold: int = 85
    propose_threshold: int = 60
    w_reference: int = 40
    w_amount: int = 30
    w_date: int = 20
    w_customer: int = 10
    max_group: int = 3

    @property
    def total_weight(self) -> int:
        return self.w_reference + self.w_amount + self.w_date + self.w_customer


DEFAULT_CONFIG = MatchConfig()


@dataclass
class Proposal:
    invoice_ids: tuple[int, ...]
    payment_ids: tuple[int, ...]
    method: str          # pass1 | pass2
    rule: str
    status: str          # auto | proposed
    confidence: int
    shape: str           # 1:1 | n:1 | 1:n
    invoice_minor: int   # GEL minor units
    payment_minor: int   # GEL minor units
    delta_minor: int     # payment - invoice, GEL minor units
    currency: str        # native currency when uniform, else MIXED
    components: list[dict] = field(default_factory=list)
    explain: list[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.invoice_ids, self.payment_ids)


# --------------------------------------------------------------------------- helpers

def normalise_ref(value: str) -> str:
    """Strip case and punctuation so 'inv-2026/0143' and 'INV 2026 0143' compare equal."""
    return _NOISE.sub("", (value or "").upper())


def tolerance_minor(invoice_minor: int, cfg: MatchConfig = DEFAULT_CONFIG) -> int:
    """Allowed absolute difference: the larger of the flat allowance and the percentage."""
    return max(cfg.tol_abs_minor, abs(invoice_minor) * cfg.tol_pct_bp // 10_000)


def within_tolerance(invoice_minor: int, delta_minor: int, cfg: MatchConfig = DEFAULT_CONFIG) -> bool:
    return abs(delta_minor) <= tolerance_minor(invoice_minor, cfg)


def _days(a: str, b: str) -> int:
    """Signed day count from a to b (payment date minus invoice date)."""
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def ref_similarity(invoice_no: str, reference: str) -> float:
    a, b = normalise_ref(invoice_no), normalise_ref(reference)
    if not a or not b:
        return 0.0
    if a == b or (len(a) >= _MIN_CONTAINED and a in b):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def ref_char_diff(invoice_no: str, reference: str) -> int:
    """How many characters separate the two references, for the human explanation."""
    a, b = normalise_ref(invoice_no), normalise_ref(reference)
    matcher = SequenceMatcher(None, a, b)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
    return changed


def name_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").upper().strip(), (b or "").upper().strip()).ratio()


def explain_amount(delta_minor: int, currency: str) -> str:
    if delta_minor == 0:
        return "amount matches exactly"
    from app.ingest import fmt_money
    word = "over" if delta_minor > 0 else "short"
    return f"amount differs by {currency} {fmt_money(abs(delta_minor))} ({word})"


def explain_date(days: int) -> str:
    if days == 0:
        return "paid on the invoice date"
    if days > 0:
        return f"paid {days} day{'s' if days != 1 else ''} after the invoice"
    return f"paid {abs(days)} day{'s' if days != 1 else ''} before the invoice was issued"


# ------------------------------------------------------------------------ scoring

def score_pair(inv: dict, pay: dict, cfg: MatchConfig = DEFAULT_CONFIG) -> tuple[int, list[dict], list[str]]:
    """Score one invoice/payment pair. Returns (confidence, components, explanations)."""
    ref_score = ref_similarity(inv["invoice_no"], pay["reference"])
    diff_chars = ref_char_diff(inv["invoice_no"], pay["reference"])
    if ref_score >= 1.0:
        ref_why = "reference matches the invoice number"
    elif diff_chars == 1:
        ref_why = "reference differs by one character"
    else:
        ref_why = f"reference differs by {diff_chars} characters"

    delta = pay["gel_minor"] - inv["gel_minor"]
    base = max(abs(inv["gel_minor"]), 1)
    amount_score = 1.0 if within_tolerance(inv["gel_minor"], delta, cfg) else max(
        0.0, 1.0 - abs(delta) / base
    )
    amount_why = explain_amount(
        pay["amount_minor"] - inv["amount_minor"] if inv["currency"] == pay["currency"] else delta,
        inv["currency"] if inv["currency"] == pay["currency"] else "GEL",
    )

    days = _days(inv["issue_date"], pay["value_date"])
    span = cfg.date_window_days
    date_score = max(0.0, 1.0 - (abs(days) / span if span else 1.0))
    date_why = explain_date(days)

    if inv["customer_code"] and pay["customer_code"] and inv["customer_code"] == pay["customer_code"]:
        cust_score, cust_why = 1.0, "same customer account"
    else:
        similarity = name_similarity(inv["customer_name"], pay["payer_name"])
        if similarity >= 0.80:
            cust_score = similarity
            cust_why = f"payer name resembles the customer ({similarity:.2f})"
        else:
            cust_score = 0.0
            cust_why = "payer does not correspond to the customer on the invoice"

    parts = [
        ("Reference similarity", ref_score, cfg.w_reference, ref_why),
        ("Amount agreement", amount_score, cfg.w_amount, amount_why),
        ("Date proximity", date_score, cfg.w_date, date_why),
        ("Customer identity", cust_score, cfg.w_customer, cust_why),
    ]
    components = [
        {
            "name": name,
            "score": round(raw, 4),
            "weight": weight,
            "contribution": round(raw * weight, 1),
            "why": why,
        }
        for name, raw, weight, why in parts
    ]
    confidence = int(round(sum(c["contribution"] for c in components)))
    if inv["currency"] != pay["currency"]:
        components.append({
            "name": "Currency check",
            "score": 0.0,
            "weight": 0,
            "contribution": 0.0,
            "why": f"invoice is in {inv['currency']} but the payment arrived in {pay['currency']}; "
                   f"compared at the {ref_rate_note()} rate",
        })
    explanations = [c["why"] for c in components]
    return min(confidence, 100), components, explanations


def ref_rate_note() -> str:
    from app.ingest import RATE_DATE
    return RATE_DATE


# --------------------------------------------------------------------------- passes

def _proposal(invs: list[dict], pays: list[dict], *, method: str, rule: str,
              confidence: int, cfg: MatchConfig, components=None, explain=None) -> Proposal:
    inv_gel = sum(i["gel_minor"] for i in invs)
    pay_gel = sum(p["gel_minor"] for p in pays)
    currencies = {i["currency"] for i in invs} | {p["currency"] for p in pays}
    shape = "1:1"
    if len(invs) > 1:
        shape = "n:1"
    elif len(pays) > 1:
        shape = "1:n"
    status = "auto" if confidence >= cfg.auto_threshold else "proposed"
    return Proposal(
        invoice_ids=tuple(sorted(i["id"] for i in invs)),
        payment_ids=tuple(sorted(p["id"] for p in pays)),
        method=method,
        rule=rule,
        status=status,
        confidence=confidence,
        shape=shape,
        invoice_minor=inv_gel,
        payment_minor=pay_gel,
        delta_minor=pay_gel - inv_gel,
        currency=currencies.pop() if len(currencies) == 1 else "MIXED",
        components=components or [],
        explain=explain or [],
    )


def _pass1(invoices: list[dict], payments: list[dict],
           cfg: MatchConfig) -> tuple[list[Proposal], list[dict], list[dict]]:
    """Deterministic rules, highest-trust first. Consumed rows leave the pool immediately."""
    open_inv = {i["id"]: i for i in invoices}
    open_pay = {p["id"]: p for p in payments}
    out: list[Proposal] = []

    def take(invs, pays):
        for i in invs:
            open_inv.pop(i["id"], None)
        for p in pays:
            open_pay.pop(p["id"], None)

    # R1 — the remittance reference carries the invoice number.
    for pay in sorted(open_pay.values(), key=lambda p: p["id"]):
        ref = normalise_ref(pay["reference"])
        if not ref:
            continue
        hits = [
            inv for inv in sorted(open_inv.values(), key=lambda i: i["id"])
            if (norm := normalise_ref(inv["invoice_no"]))
            and (norm == ref or (len(norm) >= _MIN_CONTAINED and norm in ref))
        ]
        if len(hits) != 1:
            continue                      # zero hits, or ambiguous: leave for later passes
        inv = hits[0]
        delta = pay["gel_minor"] - inv["gel_minor"]
        exact = inv["currency"] == pay["currency"] and pay["amount_minor"] == inv["amount_minor"]
        why = [
            "reference matches the invoice number",
            explain_amount(
                pay["amount_minor"] - inv["amount_minor"]
                if inv["currency"] == pay["currency"] else delta,
                inv["currency"] if inv["currency"] == pay["currency"] else "GEL",
            ),
            explain_date(_days(inv["issue_date"], pay["value_date"])),
        ]
        if inv["currency"] != pay["currency"]:
            why.append(f"currency differs: invoice {inv['currency']} vs payment {pay['currency']}")
        confidence = 100 if exact else 96
        out.append(_proposal([inv], [pay], method="pass1", rule="R1_EXACT_REF",
                             confidence=confidence, cfg=cfg, explain=why))
        take([inv], [pay])

    # R2 — exact amount, same customer, inside the date window, and unambiguous.
    for pay in sorted(open_pay.values(), key=lambda p: p["id"]):
        if not pay["customer_code"]:
            continue
        hits = [
            inv for inv in sorted(open_inv.values(), key=lambda i: i["id"])
            if inv["customer_code"] == pay["customer_code"]
            and inv["currency"] == pay["currency"]
            and inv["amount_minor"] == pay["amount_minor"]
            and -cfg.early_grace_days <= _days(inv["issue_date"], pay["value_date"]) <= cfg.date_window_days
        ]
        if len(hits) != 1:
            continue
        inv = hits[0]
        out.append(_proposal([inv], [pay], method="pass1", rule="R2_AMOUNT_CUSTOMER_DATE",
                             confidence=94, cfg=cfg, explain=[
                                 "exact amount and currency for the same customer account",
                                 explain_date(_days(inv["issue_date"], pay["value_date"])),
                                 "only one open invoice fits, so the assignment is unambiguous",
                             ]))
        take([inv], [pay])

    # R3 — same customer, difference inside tolerance.
    for pay in sorted(open_pay.values(), key=lambda p: p["id"]):
        if not pay["customer_code"]:
            continue
        hits = [
            inv for inv in sorted(open_inv.values(), key=lambda i: i["id"])
            if inv["customer_code"] == pay["customer_code"]
            and inv["currency"] == pay["currency"]
            and within_tolerance(inv["amount_minor"], pay["amount_minor"] - inv["amount_minor"], cfg)
            and -cfg.early_grace_days <= _days(inv["issue_date"], pay["value_date"]) <= cfg.date_window_days
        ]
        if len(hits) != 1:
            continue
        inv = hits[0]
        delta = pay["amount_minor"] - inv["amount_minor"]
        out.append(_proposal([inv], [pay], method="pass1", rule="R3_TOLERANCE_CUSTOMER",
                             confidence=90, cfg=cfg, explain=[
                                 "same customer account",
                                 explain_amount(delta, inv["currency"]),
                                 f"inside the configured tolerance of "
                                 f"{tolerance_minor(inv['amount_minor'], cfg) / 100:.2f} "
                                 f"({cfg.tol_abs_minor / 100:.2f} absolute or "
                                 f"{cfg.tol_pct_bp / 100:.2f}%)",
                                 explain_date(_days(inv["issue_date"], pay["value_date"])),
                             ]))
        take([inv], [pay])

    # R4 — groupings. One payment settling several invoices, then several payments
    # settling one invoice. Bounded at cfg.max_group members per side.
    by_customer: dict[str, list[dict]] = {}
    for inv in sorted(open_inv.values(), key=lambda i: i["id"]):
        if inv["customer_code"]:
            by_customer.setdefault(inv["customer_code"], []).append(inv)

    for pay in sorted(open_pay.values(), key=lambda p: p["id"]):
        if not pay["customer_code"] or pay["amount_minor"] <= 0:
            continue
        pool = [i for i in by_customer.get(pay["customer_code"], [])
                if i["id"] in open_inv and i["currency"] == pay["currency"]]
        found = None
        for size in range(2, cfg.max_group + 1):
            for combo in combinations(pool, size):
                total = sum(i["amount_minor"] for i in combo)
                if within_tolerance(total, pay["amount_minor"] - total, cfg):
                    found = combo
                    break
            if found:
                break
        if not found:
            continue
        total = sum(i["amount_minor"] for i in found)
        out.append(_proposal(list(found), [pay], method="pass1", rule="R4_GROUP_SUM",
                             confidence=88, cfg=cfg, explain=[
                                 f"one payment settles {len(found)} invoices for the same customer",
                                 "invoices " + ", ".join(i["invoice_no"] for i in found),
                                 explain_amount(pay["amount_minor"] - total, pay["currency"]),
                             ]))
        take(list(found), [pay])

    by_customer_pay: dict[str, list[dict]] = {}
    for pay in sorted(open_pay.values(), key=lambda p: p["id"]):
        if pay["customer_code"] and pay["amount_minor"] > 0:
            by_customer_pay.setdefault(pay["customer_code"], []).append(pay)

    for inv in sorted(open_inv.values(), key=lambda i: i["id"]):
        if not inv["customer_code"]:
            continue
        pool = [p for p in by_customer_pay.get(inv["customer_code"], [])
                if p["id"] in open_pay and p["currency"] == inv["currency"]]
        found = None
        for size in range(2, cfg.max_group + 1):
            for combo in combinations(pool, size):
                total = sum(p["amount_minor"] for p in combo)
                if within_tolerance(inv["amount_minor"], total - inv["amount_minor"], cfg):
                    found = combo
                    break
            if found:
                break
        if not found:
            continue
        total = sum(p["amount_minor"] for p in found)
        out.append(_proposal([inv], list(found), method="pass1", rule="R4_GROUP_SUM",
                             confidence=88, cfg=cfg, explain=[
                                 f"{len(found)} payments together settle one invoice",
                                 "payments " + ", ".join(p["payment_ref"] for p in found),
                                 explain_amount(total - inv["amount_minor"], inv["currency"]),
                             ]))
        take([inv], list(found))

    return out, list(open_inv.values()), list(open_pay.values())


def _pass2(invoices: list[dict], payments: list[dict], cfg: MatchConfig) -> list[Proposal]:
    """Probabilistic scoring over whatever pass 1 could not decide."""
    scored: list[tuple[int, int, int, list[dict], list[str]]] = []
    for inv in sorted(invoices, key=lambda i: i["id"]):
        for pay in sorted(payments, key=lambda p: p["id"]):
            confidence, components, why = score_pair(inv, pay, cfg)
            if confidence >= _CANDIDATE_FLOOR:
                scored.append((confidence, inv["id"], pay["id"], components, why))

    # Deterministic ordering: best first, ties broken by id so reruns are identical.
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    inv_by_id = {i["id"]: i for i in invoices}
    pay_by_id = {p["id"]: p for p in payments}
    used_inv: set[int] = set()
    used_pay: set[int] = set()
    out: list[Proposal] = []
    for confidence, inv_id, pay_id, components, why in scored:
        if inv_id in used_inv or pay_id in used_pay:
            continue
        if confidence < cfg.propose_threshold:
            continue
        out.append(_proposal([inv_by_id[inv_id]], [pay_by_id[pay_id]], method="pass2",
                             rule="SCORED", confidence=confidence, cfg=cfg,
                             components=components, explain=why))
        used_inv.add(inv_id)
        used_pay.add(pay_id)
    return out


def match(invoices: list[dict], payments: list[dict],
          cfg: MatchConfig = DEFAULT_CONFIG) -> list[Proposal]:
    """Pure entry point: same inputs always give the same output, in the same order."""
    pass1, left_inv, left_pay = _pass1(invoices, payments, cfg)
    pass2 = _pass2(left_inv, left_pay, cfg)
    result = pass1 + pass2
    result.sort(key=lambda p: (p.invoice_ids, p.payment_ids))
    return result


# ---------------------------------------------------------------------- persistence

def load_open(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Invoices and payments not already tied up in a confirmed manual match."""
    locked_inv = {r["invoice_id"] for r in conn.execute(
        "SELECT invoice_id FROM match_links l JOIN matches m ON m.id = l.match_id"
        " WHERE m.method = 'manual' AND m.status = 'confirmed' AND l.invoice_id IS NOT NULL")}
    locked_pay = {r["payment_id"] for r in conn.execute(
        "SELECT payment_id FROM match_links l JOIN matches m ON m.id = l.match_id"
        " WHERE m.method = 'manual' AND m.status = 'confirmed' AND l.payment_id IS NOT NULL")}
    invoices = [dict(r) for r in conn.execute("SELECT * FROM invoices ORDER BY id")
                if r["id"] not in locked_inv]
    payments = [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY id")
                if r["id"] not in locked_pay]
    return invoices, payments


def run_matching(conn: sqlite3.Connection, cfg: MatchConfig = DEFAULT_CONFIG,
                 actor: str = "system", role: str = "supervisor") -> dict:
    """Re-derive every automated match. Manual confirmations are preserved untouched."""
    require(role, "rematch", "run matching")
    conn.execute(
        "DELETE FROM match_links WHERE match_id IN (SELECT id FROM matches WHERE method != 'manual')"
    )
    conn.execute("DELETE FROM matches WHERE method != 'manual'")

    invoices, payments = load_open(conn)
    proposals = match(invoices, payments, cfg)

    for prop in proposals:
        cur = conn.execute(
            "INSERT INTO matches (method, rule, status, confidence, shape, invoice_minor,"
            " payment_minor, delta_minor, currency, breakdown, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (prop.method, prop.rule, prop.status, prop.confidence, prop.shape,
             prop.invoice_minor, prop.payment_minor, prop.delta_minor, prop.currency,
             json.dumps({"components": prop.components, "explain": prop.explain},
                        ensure_ascii=False),
             now_iso()),
        )
        match_id = int(cur.lastrowid)
        for invoice_id in prop.invoice_ids:
            conn.execute("INSERT INTO match_links (match_id, invoice_id) VALUES (?,?)",
                         (match_id, invoice_id))
        for payment_id in prop.payment_ids:
            conn.execute("INSERT INTO match_links (match_id, payment_id) VALUES (?,?)",
                         (match_id, payment_id))
    conn.commit()

    stats = summarise(conn)
    audit(conn, actor, role, "MATCHING_RUN", "engine", "matching",
          before=None, after=stats)
    return stats


def summarise(conn: sqlite3.Connection) -> dict:
    """Headline numbers, all counted from the database rather than assumed."""
    invoices = conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"]
    payments = conn.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"]
    auto_pay = conn.execute(
        "SELECT COUNT(DISTINCT l.payment_id) n FROM match_links l JOIN matches m ON m.id = l.match_id"
        " WHERE l.payment_id IS NOT NULL AND m.status IN ('auto','confirmed')"
    ).fetchone()["n"]
    proposed_pay = conn.execute(
        "SELECT COUNT(DISTINCT l.payment_id) n FROM match_links l JOIN matches m ON m.id = l.match_id"
        " WHERE l.payment_id IS NOT NULL AND m.status = 'proposed'"
    ).fetchone()["n"]
    matched_inv = conn.execute(
        "SELECT COUNT(DISTINCT l.invoice_id) n FROM match_links l JOIN matches m ON m.id = l.match_id"
        " WHERE l.invoice_id IS NOT NULL AND m.status IN ('auto','confirmed')"
    ).fetchone()["n"]
    by_rule = {r["rule"]: r["n"] for r in conn.execute(
        "SELECT rule, COUNT(*) n FROM matches GROUP BY rule ORDER BY rule")}
    return {
        "invoices": invoices,
        "payments": payments,
        "auto_matched_payments": auto_pay,
        "proposed_payments": proposed_pay,
        "matched_invoices": matched_inv,
        "auto_match_rate": round(100.0 * auto_pay / payments, 1) if payments else 0.0,
        "by_rule": by_rule,
    }
