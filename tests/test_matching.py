"""Matching engine: rules, scoring, boundaries and determinism."""
from __future__ import annotations

from dataclasses import replace

import pytest

from app import matching
from app.matching import DEFAULT_CONFIG, MatchConfig, match, score_pair

EXACT_ONLY = replace(DEFAULT_CONFIG, tol_pct_bp=0)   # percentage disabled, flat 0.02 only


def rules(proposals):
    return sorted(p.rule for p in proposals)


def test_normalise_ref_strips_case_and_punctuation():
    """References differing only in case or punctuation compare equal."""
    assert matching.normalise_ref("inv-2026/0143") == matching.normalise_ref("INV 2026 0143")
    assert matching.normalise_ref("") == ""


def test_exact_reference_matches_first(make):
    """A remittance quoting the invoice number matches on rule R1 with full confidence."""
    inv = make.invoice(no="INV-2026-0001")
    pay = make.payment(reference="INV-2026-0001")
    result = match([inv], [pay])
    assert len(result) == 1
    assert result[0].rule == "R1_EXACT_REF"
    assert result[0].confidence == 100
    assert result[0].status == "auto"


def test_reference_containment_survives_prefixes(make):
    """'PMT-INV-2026-0001 PAYMENT' still resolves to the invoice it contains."""
    inv = make.invoice(no="INV-2026-0001")
    pay = make.payment(reference="PMT-INV-2026-0001 PAYMENT")
    result = match([inv], [pay])
    assert result[0].rule == "R1_EXACT_REF"


def test_amount_customer_and_date_rule(make):
    """With no usable reference, an exact amount for one customer matches on R2."""
    inv = make.invoice(amount=500_00)
    pay = make.payment(amount=500_00, reference="BANK TRANSFER")
    result = match([inv], [pay])
    assert result[0].rule == "R2_AMOUNT_CUSTOMER_DATE"
    assert result[0].status == "auto"


def test_two_identical_candidates_are_not_auto_matched(make):
    """When two invoices fit equally well, R2 refuses to guess."""
    first = make.invoice(amount=500_00)
    second = make.invoice(amount=500_00)
    pay = make.payment(amount=500_00, reference="BANK TRANSFER")
    result = match([first, second], [pay])
    assert all(p.rule != "R2_AMOUNT_CUSTOMER_DATE" for p in result)


def test_tolerance_absolute_boundary_is_inclusive():
    """Exactly 0.02 is inside the flat tolerance and 0.03 is outside."""
    assert matching.within_tolerance(100_00, 2, EXACT_ONLY) is True
    assert matching.within_tolerance(100_00, -2, EXACT_ONLY) is True
    assert matching.within_tolerance(100_00, 3, EXACT_ONLY) is False


def test_tolerance_percentage_boundary_is_inclusive():
    """At 0.5% of 100.00 the allowance is exactly 0.50: 0.50 passes, 0.51 does not."""
    assert matching.tolerance_minor(100_00, DEFAULT_CONFIG) == 50
    assert matching.within_tolerance(100_00, 50, DEFAULT_CONFIG) is True
    assert matching.within_tolerance(100_00, 51, DEFAULT_CONFIG) is False


def test_tolerance_takes_the_larger_of_the_two_rules():
    """On a tiny invoice the flat 0.02 binds; on a large one the percentage does."""
    assert matching.tolerance_minor(1_00, DEFAULT_CONFIG) == 2
    assert matching.tolerance_minor(10_000_00, DEFAULT_CONFIG) == 5000


def test_within_tolerance_difference_matches_on_r3(make):
    """A rounding difference inside tolerance still settles, on rule R3."""
    inv = make.invoice(amount=500_00)
    pay = make.payment(amount=500_02, reference="TRANSFER")
    result = match([inv], [pay], EXACT_ONLY)
    assert result[0].rule == "R3_TOLERANCE_CUSTOMER"


def test_many_to_one_grouping(make):
    """One payment settling three invoices is found and recorded as an n:1 match."""
    invoices = [make.invoice(amount=amount) for amount in (100_00, 250_00, 375_00)]
    pay = make.payment(amount=725_00, reference="CONSOLIDATED PAYMENT")
    result = match(invoices, [pay])
    assert len(result) == 1
    assert result[0].rule == "R4_GROUP_SUM"
    assert result[0].shape == "n:1"
    assert len(result[0].invoice_ids) == 3
    assert result[0].delta_minor == 0


def test_one_to_many_grouping(make):
    """Two instalments settling one invoice are found and recorded as a 1:n match."""
    inv = make.invoice(amount=900_00)
    pays = [make.payment(amount=400_00, reference="INSTALMENT 1"),
            make.payment(amount=500_00, reference="INSTALMENT 2", days=20)]
    result = match([inv], pays)
    assert len(result) == 1
    assert result[0].rule == "R4_GROUP_SUM"
    assert result[0].shape == "1:n"
    assert len(result[0].payment_ids) == 2


def test_scored_breakdown_sums_to_the_confidence(make):
    """The confidence is exactly the sum of the component contributions, not a black box."""
    inv = make.invoice(no="INV-2026-0143", amount=1_000_00)
    pay = make.payment(reference="INV-2026-0148", amount=1_000_00)
    confidence, components, _ = score_pair(inv, pay)
    assert confidence == round(sum(c["contribution"] for c in components))
    assert {c["name"] for c in components} == {
        "Reference similarity", "Amount agreement", "Date proximity", "Customer identity"}


def test_component_weights_sum_to_one_hundred():
    """A perfect match on every component scores exactly 100."""
    assert DEFAULT_CONFIG.total_weight == 100


def test_single_character_typo_is_explained_in_words(make):
    """A one-character reference error is described as such, not as a raw ratio."""
    inv = make.invoice(no="INV-2026-0143")
    pay = make.payment(reference="INV-2026-0148")
    _, components, _ = score_pair(inv, pay)
    reference = next(c for c in components if c["name"] == "Reference similarity")
    assert reference["why"] == "reference differs by one character"


def test_amount_difference_is_explained_with_the_figure(make):
    """The explanation states the actual money difference and its direction."""
    inv = make.invoice(amount=1_000_00)
    pay = make.payment(amount=999_98, reference="x")
    _, components, _ = score_pair(inv, pay)
    amount = next(c for c in components if c["name"] == "Amount agreement")
    assert "0.02" in amount["why"] and "short" in amount["why"]


def test_early_payment_is_explained_as_before_the_invoice(make):
    """Cash arriving before the invoice date is described in plain language."""
    inv = make.invoice()
    pay = make.payment(reference="x", days=-3)
    _, components, _ = score_pair(inv, pay)
    when = next(c for c in components if c["name"] == "Date proximity")
    assert when["why"] == "paid 3 days before the invoice was issued"


def test_unusable_reference_cannot_reach_the_auto_threshold(make):
    """Structural guarantee: with no reference, amount, date and customer cap out below auto.

    This is why an amount coincidence can never post a payment on its own.
    """
    inv = make.invoice(amount=500_00)
    pay = make.payment(amount=500_00, reference="", days=0)
    confidence, _, _ = score_pair(inv, pay)
    ceiling = DEFAULT_CONFIG.w_amount + DEFAULT_CONFIG.w_date + DEFAULT_CONFIG.w_customer
    assert confidence == ceiling
    assert confidence < DEFAULT_CONFIG.auto_threshold


def test_wrong_customer_scores_zero_on_identity(make):
    """A payer unrelated to the customer contributes nothing to the score."""
    inv = make.invoice(customer="C001", name="Alazani LLC")
    pay = make.payment(customer="C999", payer="Totally Different Co", reference="x")
    _, components, _ = score_pair(inv, pay)
    identity = next(c for c in components if c["name"] == "Customer identity")
    assert identity["contribution"] == 0


def test_currency_mismatch_still_matches_on_reference_and_is_flagged(make):
    """A payment in the wrong currency still finds its invoice, and the note says so."""
    inv = make.invoice(no="INV-2026-0001", amount=100_00, currency="USD", gel=270_00)
    pay = make.payment(reference="INV-2026-0001", amount=100_00, currency="GEL", gel=100_00)
    result = match([inv], [pay])
    assert result[0].rule == "R1_EXACT_REF"
    assert any("currency differs" in line for line in result[0].explain)


def test_a_payment_is_never_assigned_twice(make):
    """No payment appears on more than one proposal, whatever the pass."""
    invoices = [make.invoice(no=f"INV-2026-{i:04d}", amount=100_00 + i) for i in range(1, 9)]
    payments = [make.payment(reference=f"INV-2026-{i:04d}", amount=100_00 + i)
                for i in range(1, 9)]
    payments.append(make.payment(reference="INV-2026-0001", amount=100_01))
    result = match(invoices, payments)
    used = [pid for p in result for pid in p.payment_ids]
    assert len(used) == len(set(used))


def test_an_invoice_is_never_assigned_twice(make):
    """The same guarantee holds on the invoice side."""
    invoices = [make.invoice(no=f"INV-2026-{i:04d}", amount=200_00) for i in range(1, 6)]
    payments = [make.payment(reference="TRANSFER", amount=200_00) for _ in range(5)]
    result = match(invoices, payments)
    used = [iid for p in result for iid in p.invoice_ids]
    assert len(used) == len(set(used))


def test_matching_is_deterministic(make):
    """The same inputs produce the same proposals, in the same order, every time."""
    invoices = [make.invoice(no=f"INV-2026-{i:04d}", amount=100_00 * i) for i in range(1, 15)]
    payments = [make.payment(reference=f"INV-2026-{i:04d}", amount=100_00 * i)
                for i in range(1, 12)]
    payments += [make.payment(reference="TRANSFER", amount=333_33) for _ in range(4)]

    first = match(invoices, payments)
    second = match(invoices, payments)

    assert [p.key() for p in first] == [p.key() for p in second]
    assert [p.confidence for p in first] == [p.confidence for p in second]
    assert [p.rule for p in first] == [p.rule for p in second]


def test_scores_below_the_review_floor_produce_no_proposal(make):
    """A pair with nothing in common is left alone rather than forced into a match."""
    inv = make.invoice(no="INV-2026-0001", amount=100_00, customer="C001")
    pay = make.payment(reference="ZZZZ", amount=9_999_00, customer="C777",
                       payer="Unrelated Co", days=200)
    assert match([inv], [pay]) == []


def test_proposed_band_is_between_the_two_thresholds(make):
    """A partial-signal pair lands in the review band, neither posted nor discarded."""
    inv = make.invoice(no="INV-2026-0143", amount=1_000_00)
    pay = make.payment(reference="PO-88421", amount=950_00)
    result = match([inv], [pay])
    assert len(result) == 1
    assert result[0].status == "proposed"
    assert DEFAULT_CONFIG.propose_threshold <= result[0].confidence < DEFAULT_CONFIG.auto_threshold


def test_config_is_overridable(make):
    """Tolerance is configuration, not a constant baked into the rules."""
    inv = make.invoice(amount=500_00)
    pay = make.payment(amount=450_00, reference="TRANSFER")
    generous = MatchConfig(tol_abs_minor=50_00, tol_pct_bp=0)
    assert match([inv], [pay], generous)[0].rule == "R3_TOLERANCE_CUSTOMER"


# ------------------------------------------------------------------- persistence

def seed_pair(conn):
    conn.execute(
        "INSERT INTO invoices (invoice_no, customer_code, customer_name, issue_date,"
        " due_date, amount_minor, currency, gel_minor) VALUES"
        " ('INV-2026-0001','C001','Alazani LLC','2026-06-01','2026-07-01',100000,'GEL',100000)")
    conn.execute(
        "INSERT INTO payments (payment_ref, reference, customer_code, payer_name, value_date,"
        " amount_minor, currency, gel_minor) VALUES"
        " ('BNK-1','INV-2026-0001','C001','Alazani LLC','2026-06-05',100000,'GEL',100000)")
    conn.commit()


def test_run_matching_persists_links_and_breakdown(conn):
    """Persisted matches carry their links and their stored explanation."""
    seed_pair(conn)
    stats = matching.run_matching(conn)
    assert stats["auto_matched_payments"] == 1
    row = conn.execute("SELECT * FROM matches").fetchone()
    assert row["rule"] == "R1_EXACT_REF"
    assert "explain" in row["breakdown"]
    links = conn.execute("SELECT COUNT(*) n FROM match_links").fetchone()["n"]
    assert links == 2


def test_run_matching_is_repeatable_without_duplicating(conn):
    """Re-running matching rebuilds rather than accumulating rows."""
    seed_pair(conn)
    matching.run_matching(conn)
    first = conn.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
    matching.run_matching(conn)
    second = conn.execute("SELECT COUNT(*) n FROM matches").fetchone()["n"]
    assert first == second == 1


def test_manual_confirmation_survives_a_rematch(conn):
    """Human decisions are preserved when the engine re-runs."""
    seed_pair(conn)
    matching.run_matching(conn)
    conn.execute("UPDATE matches SET method = 'manual', status = 'confirmed'")
    conn.commit()

    matching.run_matching(conn)

    rows = conn.execute("SELECT method, status FROM matches").fetchall()
    assert len(rows) == 1
    assert rows[0]["method"] == "manual" and rows[0]["status"] == "confirmed"


def test_matching_writes_an_audit_row(conn):
    """Running the engine is itself an auditable event."""
    seed_pair(conn)
    matching.run_matching(conn, actor="t.gogia", role="supervisor")
    row = conn.execute("SELECT * FROM audit_log WHERE action = 'MATCHING_RUN'").fetchone()
    assert row is not None
    assert row["actor"] == "t.gogia"


def test_auditor_cannot_run_matching(conn):
    """The read-only role cannot trigger a re-match."""
    from app.controls import ControlError
    seed_pair(conn)
    with pytest.raises(ControlError):
        matching.run_matching(conn, actor="audit.ext", role="auditor")
