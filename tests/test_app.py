"""Web layer: every route serves, and the controls hold when driven over HTTP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, exceptions as exc, generate, matching, seed

ROUTES = ["/", "/workspace", "/exceptions", "/approvals", "/audit", "/import", "/quality"]


@pytest.fixture(scope="session")
def app_env(tmp_path_factory):
    """A fully seeded application pointed at a temporary data directory."""
    data = tmp_path_factory.mktemp("reconflow-data")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db, "DATA_DIR", data)
        mp.setattr(db, "DB_PATH", data / "reconflow.db")
        mp.setattr(generate, "DATA_DIR", data)

        generate.write_csvs(data)
        conn = db.fresh()
        seed.load_customers(conn)
        seed.import_all(conn)
        matching.run_matching(conn)
        exc.rebuild(conn, generate.AS_OF)
        seed.plant_approvals(conn)
        conn.close()

        from app.main import app
        yield {"app": app, "data": data}


@pytest.fixture
def client(app_env):
    return TestClient(app_env["app"])


def as_role(app_env, actor):
    client = TestClient(app_env["app"])
    client.cookies.set("rf_actor", actor)
    return client


def db_conn(app_env):
    return db.connect(app_env["data"] / "reconflow.db")


# --------------------------------------------------------------------------- smoke

@pytest.mark.parametrize("route", ROUTES)
def test_every_navigation_route_serves(client, route):
    """Every page in the top navigation returns 200 with real content."""
    response = client.get(route)
    assert response.status_code == 200
    assert "ReconFLOW" in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize("route", ROUTES)
def test_every_page_has_a_heading_and_a_lede(client, route):
    """Each page states what it shows, per the shared UI bar."""
    text = client.get(route).text
    assert "<h1>" in text
    assert 'class="lede"' in text


@pytest.mark.parametrize("route", ROUTES)
def test_every_page_declares_a_mobile_viewport(client, route):
    """The responsive meta tag is present so the layout holds on a phone."""
    assert 'name="viewport"' in client.get(route).text


def test_match_detail_shows_a_confidence_breakdown(client, app_env):
    """The match detail page explains the decision rather than stating a number."""
    conn = db_conn(app_env)
    match_id = conn.execute("SELECT id FROM matches WHERE method = 'pass2' LIMIT 1").fetchone()["id"]
    conn.close()
    response = client.get(f"/workspace/match/{match_id}")
    assert response.status_code == 200
    assert "Why this match" in response.text
    assert "Reference similarity" in response.text


def test_rule_based_match_detail_explains_its_rule(client, app_env):
    """A first-pass match shows its rule and reasoning, not an empty breakdown panel."""
    conn = db_conn(app_env)
    match_id = conn.execute(
        "SELECT id FROM matches WHERE rule = 'R1_EXACT_REF' LIMIT 1").fetchone()["id"]
    conn.close()
    text = client.get(f"/workspace/match/{match_id}").text
    assert "Deterministic rule" in text
    assert "reference matches the invoice number" in text


def test_exception_detail_serves(client, app_env):
    """An exception opens with its guidance and underlying record."""
    conn = db_conn(app_env)
    exception_id = conn.execute("SELECT id FROM exceptions LIMIT 1").fetchone()["id"]
    conn.close()
    response = client.get(f"/exceptions/{exception_id}")
    assert response.status_code == 200
    assert "What to do" in response.text


def test_missing_records_redirect_with_a_message(client):
    """A bad id is handled, not turned into a stack trace."""
    for url in ("/workspace/match/999999", "/exceptions/999999"):
        response = client.get(url)
        assert response.status_code == 200
        assert "does not exist" in response.text


def test_workspace_views_and_filters_serve(client):
    """The workspace tabs and filters all render."""
    for url in ("/workspace?view=invoices", "/workspace?view=payments",
                "/workspace?status=proposed", "/workspace?q=INV-2026-0001"):
        assert client.get(url).status_code == 200


def test_exception_filters_serve(client):
    """Queue filters return a page rather than an error, even when empty."""
    for url in ("/exceptions?reason=SHORT_PAY", "/exceptions?severity=high",
                "/exceptions?bucket=60%2B", "/exceptions?status=resolved"):
        assert client.get(url).status_code == 200


def test_empty_filter_result_shows_an_empty_state(client):
    """A filter matching nothing shows a real empty state, never a blank box."""
    response = client.get("/exceptions?assignee=nobody.at.all")
    assert response.status_code == 200
    assert 'class="empty"' in response.text


def test_static_stylesheets_are_served(client):
    """Both stylesheets load, so the page is not unstyled."""
    for path, marker in (("/static/base.css", "--accent"), ("/static/app.css", "#14b8a6")):
        response = client.get(path)
        assert response.status_code == 200
        assert marker in response.text


def test_dashboard_reports_a_computed_match_rate(client, app_env):
    """The headline figure on the dashboard is the one the engine actually computed."""
    conn = db_conn(app_env)
    expected = matching.summarise(conn)["auto_match_rate"]
    conn.close()
    assert f"{expected}%" in client.get("/").text


def test_dashboard_labels_the_data_as_synthetic(client):
    """The honesty rule: synthetic data is declared in the interface itself."""
    text = client.get("/").text
    assert "Synthetic data" in text
    assert "note-warn" in text


def test_quality_page_reports_the_planted_scenarios(client):
    """The evidence page lists what the dataset deliberately contains."""
    text = client.get("/quality").text
    assert "CURRENCY_MISMATCH" in text
    assert "MALFORMED_ROWS" in text


# ------------------------------------------------------------ controls over HTTP

def test_role_switch_changes_the_acting_user(app_env):
    """The demo switcher sets the acting identity and the page reflects it."""
    client = TestClient(app_env["app"])
    response = client.get("/role/audit.ext?next=/exceptions")
    assert response.status_code == 200
    assert client.cookies.get("rf_actor") == "audit.ext"
    assert "audit.ext" in response.text


def test_an_unknown_demo_user_is_refused(client):
    """Only the defined demo identities can be assumed."""
    assert "Unknown demo user" in client.get("/role/root?next=/").text


def test_auditor_write_is_refused_over_http(app_env):
    """Driving the real form as the auditor is refused by the server, not the template."""
    conn = db_conn(app_env)
    row = conn.execute("SELECT id, status FROM exceptions WHERE status = 'open' LIMIT 1").fetchone()
    conn.close()

    client = as_role(app_env, "audit.ext")
    response = client.post(f"/exceptions/{row['id']}/action",
                           data={"action": "investigating", "note": "trying it"})

    assert response.status_code == 200
    assert "read-only" in response.text

    conn = db_conn(app_env)
    after = conn.execute("SELECT status FROM exceptions WHERE id = ?", (row["id"],)).fetchone()
    conn.close()
    assert after["status"] == row["status"]


def test_self_approval_is_refused_over_http(app_env):
    """The seeded supervisor-raised item cannot be approved by that same supervisor."""
    conn = db_conn(app_env)
    approval = conn.execute(
        "SELECT id, maker FROM approvals WHERE status = 'pending' AND maker = 't.gogia'"
    ).fetchone()
    conn.close()
    assert approval is not None, "seeding should raise one supervisor-made request"

    client = as_role(app_env, "t.gogia")
    response = client.post(f"/approvals/{approval['id']}/decide",
                           data={"decision": "approved", "note": "mine"})

    assert "Segregation of duties" in response.text

    conn = db_conn(app_env)
    status = conn.execute("SELECT status FROM approvals WHERE id = ?",
                          (approval["id"],)).fetchone()["status"]
    conn.close()
    assert status == "pending"


def test_analyst_approval_is_refused_over_http(app_env):
    """An analyst pressing approve is told why it will not work."""
    conn = db_conn(app_env)
    approval = conn.execute(
        "SELECT id FROM approvals WHERE status = 'pending' LIMIT 1").fetchone()
    conn.close()
    client = as_role(app_env, "a.beridze")
    response = client.post(f"/approvals/{approval['id']}/decide",
                           data={"decision": "approved"})
    assert "does not hold" in response.text or "cannot" in response.text


def test_supervisor_can_approve_an_analyst_request_over_http(app_env):
    """The intended path completes and the decision is recorded with the checker."""
    conn = db_conn(app_env)
    approval = conn.execute(
        "SELECT id FROM approvals WHERE status = 'pending' AND maker = 'n.kapanadze'"
    ).fetchone()
    conn.close()
    assert approval is not None

    client = as_role(app_env, "t.gogia")
    response = client.post(f"/approvals/{approval['id']}/decide",
                           data={"decision": "approved", "note": "verified"})
    assert response.status_code == 200

    conn = db_conn(app_env)
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval["id"],)).fetchone()
    audited = conn.execute(
        "SELECT COUNT(*) n FROM audit_log WHERE action = 'APPROVAL_APPROVED'"
        " AND entity_id = ?", (str(approval["id"]),)).fetchone()["n"]
    conn.close()
    assert row["status"] == "approved"
    assert row["checker"] == "t.gogia"
    assert audited == 1


# ----------------------------------------------------------------- import over HTTP

def test_uploading_a_known_file_posts_nothing(app_env):
    """Re-uploading a file already imported is refused, with the counts to prove it."""
    client = TestClient(app_env["app"])
    payload = (app_env["data"] / "payments_2026_04.csv").read_bytes()

    conn = db_conn(app_env)
    before = conn.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"]
    conn.close()

    response = client.post("/import/upload", data={"kind": "payments"},
                           files={"upload_file": ("payments_2026_04.csv", payload, "text/csv")})

    assert "already imported" in response.text
    conn = db_conn(app_env)
    after = conn.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"]
    conn.close()
    assert after == before


def test_uploading_a_new_file_with_a_bad_row_reports_both_counts(app_env):
    """A mixed file imports the good rows and reports the bad one."""
    client = TestClient(app_env["app"])
    content = (
        "payment_ref,reference,customer_code,payer_name,value_date,amount,currency\n"
        "BNK-UPLOAD-1,INV-2026-0001,C001,Alazani LLC,2026-07-02,100.00,GEL\n"
        "BNK-UPLOAD-2,INV-2026-0002,C002,Mtkvari JSC,31/02/2026,200.00,GEL\n"
    ).encode()
    response = client.post("/import/upload", data={"kind": "payments"},
                           files={"upload_file": ("uploaded.csv", content, "text/csv")})
    assert "1 accepted" in response.text
    assert "1 rejected" in response.text


def test_an_empty_upload_is_refused(app_env):
    """An empty file is rejected before it reaches the parser."""
    client = TestClient(app_env["app"])
    response = client.post("/import/upload", data={"kind": "payments"},
                           files={"upload_file": ("empty.csv", b"", "text/csv")})
    assert "empty" in response.text


def test_auditor_cannot_upload_over_http(app_env):
    """The read-only role is refused at the import route too."""
    client = as_role(app_env, "audit.ext")
    content = b"payment_ref,reference,customer_code,payer_name,value_date,amount,currency\n"
    response = client.post("/import/upload", data={"kind": "payments"},
                           files={"upload_file": ("x.csv", content, "text/csv")})
    assert "read-only" in response.text


def test_rematch_is_refused_for_the_auditor(app_env):
    """Re-running the engine is a write and is gated like one."""
    client = as_role(app_env, "audit.ext")
    assert "read-only" in client.post("/import/rematch").text


def test_audit_page_shows_the_recorded_history(client):
    """The audit page reflects the entries the seeding and tests actually wrote."""
    text = client.get("/audit").text
    assert "IMPORT_COMPLETED" in text
    assert "append-only" in text.lower()
    assert "MATCHING_RUN" in text
