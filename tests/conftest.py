"""Shared fixtures, plus the hook that writes data/quality.json for the /quality page.

The report is produced by the run itself rather than maintained by hand, so the evidence
page cannot drift away from what the suite actually proves.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import db

ROOT = Path(__file__).resolve().parent.parent

_results: list[dict] = []
_started = time.time()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    doc = (getattr(item, "function", None).__doc__ or "") if hasattr(item, "function") else ""
    report.rf_label = doc.strip().splitlines()[0] if doc.strip() else item.name
    report.rf_module = item.nodeid.split("::")[0]


def pytest_runtest_logreport(report):
    if report.when != "call":
        return
    _results.append({
        "name": report.nodeid.split("::")[-1],
        "module": getattr(report, "rf_module", report.nodeid.split("::")[0]),
        "label": getattr(report, "rf_label", report.nodeid),
        "outcome": report.outcome,
        "duration": round(report.duration, 3),
    })


def pytest_sessionfinish(session, exitstatus):
    if not _results:
        return
    by_module: dict[str, list[dict]] = {}
    for case in _results:
        by_module.setdefault(case["module"], []).append(case)
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "duration": round(time.time() - _started, 2),
        "total": len(_results),
        "passed": sum(1 for c in _results if c["outcome"] == "passed"),
        "failed": sum(1 for c in _results if c["outcome"] != "passed"),
        "exit_status": int(exitstatus),
        "cases": _results,
        "by_module": by_module,
    }
    # Deliberately the real data directory, not db.DATA_DIR: the app fixture repoints
    # that at a temp folder, and the evidence page reads from the repo.
    target = ROOT / "data" / "quality.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def conn():
    """A fresh in-memory database with the full schema applied."""
    connection = db.fresh(":memory:")
    yield connection
    connection.close()


TODAY = date(2026, 6, 1)


@pytest.fixture
def make():
    """Builders for the plain dicts the pure engine functions consume."""
    state = {"inv": 0, "pay": 0}

    def invoice(amount=100_00, currency="GEL", customer="C001", issue=TODAY,
                name="Alazani LLC", no=None, gel=None):
        state["inv"] += 1
        issue_date = issue.isoformat() if isinstance(issue, date) else issue
        return {
            "id": state["inv"],
            "invoice_no": no or f"INV-2026-{state['inv']:04d}",
            "customer_code": customer,
            "customer_name": name,
            "issue_date": issue_date,
            "due_date": (date.fromisoformat(issue_date) + timedelta(days=30)).isoformat(),
            "amount_minor": amount,
            "currency": currency,
            "gel_minor": amount if gel is None else gel,
        }

    def payment(amount=100_00, currency="GEL", customer="C001", value=None,
                reference="", payer="Alazani LLC", ref_no=None, gel=None, days=5):
        state["pay"] += 1
        when = value or (TODAY + timedelta(days=days))
        return {
            "id": state["pay"],
            "payment_ref": ref_no or f"BNK-{state['pay']:06d}",
            "reference": reference,
            "customer_code": customer,
            "payer_name": payer,
            "value_date": when.isoformat() if isinstance(when, date) else when,
            "amount_minor": amount,
            "currency": currency,
            "gel_minor": amount if gel is None else gel,
        }

    return type("Make", (), {"invoice": staticmethod(invoice),
                             "payment": staticmethod(payment)})
