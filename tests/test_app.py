import os
import tempfile
from fastapi.testclient import TestClient

_test_db = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = os.path.join(_test_db.name, "test.db")
os.environ["DATABASE_URL"] = ""
os.environ["MONGODB_URI"] = ""
os.environ["AUTH_REQUIRED"] = "false"
os.environ["PAYPAL_CLIENT_ID"] = ""
os.environ["PAYPAL_CLIENT_SECRET"] = ""

from app.main import app
from app import paypal
from app import db as app_db


client = TestClient(app)


def test_health():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_register_login_and_current_user():
    email = "owner@example.com"
    password = "correct horse battery staple"
    register = client.post(
        "/api/auth/register", json={"email": email, "password": password}
    )
    login = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )

    assert register.status_code == 201
    assert login.status_code == 200
    token = login.json()["token"]
    current = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert current.status_code == 200
    assert current.json()["email"] == email


def test_login_claims_legacy_invoices():
    email = "legacy-owner@example.com"
    password = "correct horse battery staple"
    invoice = client.post(
        "/api/invoices",
        json={
            "client_name": "Legacy Client",
            "client_email": "legacy@example.com",
            "description": "Legacy work",
            "amount": "10.00",
            "currency": "USD",
            "due_date": "2026-12-01",
        },
    )
    assert invoice.status_code == 200
    assert client.post(
        "/api/auth/register", json={"email": email, "password": password}
    ).status_code == 201
    login = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    token = login.json()["token"]
    invoices = client.get(
        "/api/invoices", headers={"Authorization": f"Bearer {token}"}
    )
    assert any(item["description"] == "Legacy work" for item in invoices.json())


def test_rejects_invalid_currency():
    response = client.post(
        "/api/invoices",
        json={
            "client_name": "Test Client",
            "client_email": "client@example.com",
            "description": "Consulting",
            "amount": "10.00",
            "currency": "usd",
            "due_date": "2026-12-01",
        },
    )

    assert response.status_code == 422


def test_creates_mock_invoice():
    response = client.post(
        "/api/invoices",
        json={
            "client_name": "Test Client",
            "client_email": "client@example.com",
            "description": "Consulting",
            "amount": "10.00",
            "currency": "USD",
            "due_date": "2026-12-01",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["client_name"] == "Test Client"
    assert body["amount"] == 10.0
    assert body["display_status"] == "sent"


def test_postgres_invoice_insert_uses_bound_parameters(monkeypatch):
    class Cursor:
        def fetchone(self):
            return {"id": 42}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, params):
            assert "%s" in query
            assert ":paypal_id" not in query
            assert params[-1] == 7
            return Cursor()

    monkeypatch.setattr("app.db.DATABASE_URL", "postgresql://test")
    monkeypatch.setattr("app.db.conn", lambda: Connection())
    assert app_db.add(
        paypal_id="paypal-id",
        number="0001",
        client_name="Client",
        client_email="client@example.com",
        description="Work",
        amount=10,
        currency="USD",
        due_date="2026-12-01",
        user_id=7,
    ) == 42


def test_paypal_call_uses_bearer_token(monkeypatch):
    seen = {}

    class Response:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(paypal, "_token", lambda: "test-token")

    def request(method, url, json, headers, timeout):
        seen.update(method=method, url=url, json=json, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(paypal.httpx, "request", request)
    paypal._call("GET", "/test")

    assert seen["headers"]["Authorization"] == "Bearer test-token"


def test_webhook_event_is_idempotent():
    event = {"id": "test-event-1", "resource": {}}

    first = client.post("/api/webhooks/paypal", json=event)
    second = client.post("/api/webhooks/paypal", json=event)

    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert second.status_code == 200
    assert second.json() == {"ok": True, "duplicate": True}
