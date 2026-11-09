"""Thin PayPal Invoicing v2 client. Runs in MOCK mode when no credentials are set."""
import os
import time
import uuid
from datetime import date

import httpx

CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
MODE = os.getenv("PAYPAL_MODE", "sandbox").lower()
if MODE not in {"sandbox", "live"}:
    raise RuntimeError("PAYPAL_MODE must be 'sandbox' or 'live'")
BASE = (
    "https://api-m.paypal.com"
    if MODE == "live"
    else "https://api-m.sandbox.paypal.com"
)
MOCK = not (CLIENT_ID and SECRET)
_tok = {"v": None, "exp": 0.0}


def _token():
    if _tok["v"] and time.time() < _tok["exp"] - 60:
        return _tok["v"]
    r = httpx.post(
        f"{BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, SECRET),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    _tok.update(v=j["access_token"], exp=time.time() + j["expires_in"])
    return _tok["v"]


def _call(method, path, body=None):
    r = httpx.request(
        method,
        f"{BASE}{path}",
        json=body,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PayPal {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def create_and_send(d):
    """d: client_name, client_email, description, amount, currency, due_date -> (paypal_id, number)"""
    if MOCK:
        n = uuid.uuid4().hex[:6].upper()
        return f"MOCK-{n}", f"MOCK-{n}"
    number = _call("POST", "/v2/invoicing/generate-next-invoice-number")["invoice_number"]
    parts = d["client_name"].split(" ", 1)
    name = {"given_name": parts[0]}
    if len(parts) > 1:
        name["surname"] = parts[1]
    body = {
        "detail": {
            "invoice_number": number,
            "invoice_date": date.today().isoformat(),
            "currency_code": d["currency"],
            "note": d["description"],
            "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": d["due_date"]},
        },
        "primary_recipients": [
            {"billing_info": {"name": name, "email_address": d["client_email"]}}
        ],
        "items": [
            {
                "name": d["description"][:200],
                "quantity": "1",
                "unit_amount": {"currency_code": d["currency"], "value": f"{d['amount']:.2f}"},
            }
        ],
    }
    created = _call("POST", "/v2/invoicing/invoices", body)
    pid = created.get("id") or created["href"].rstrip("/").split("/")[-1]
    _call("POST", f"/v2/invoicing/invoices/{pid}/send", {"send_to_recipient": True})
    return pid, number


def get_status(pid):
    if MOCK:
        return None
    return _call("GET", f"/v2/invoicing/invoices/{pid}").get("status", "SENT")


def remind(pid, subject, note):
    if MOCK:
        return
    _call(
        "POST",
        f"/v2/invoicing/invoices/{pid}/remind",
        {"subject": subject, "note": note, "send_to_recipient": True},
    )


def verify_webhook_signature(headers, event):
    """Ask PayPal to verify a webhook transmission signature."""
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID", "")
    required = (
        "PAYPAL-TRANSMISSION-ID",
        "PAYPAL-TRANSMISSION-TIME",
        "PAYPAL-CERT-URL",
        "PAYPAL-AUTH-ALGO",
        "PAYPAL-TRANSMISSION-SIG",
    )
    if not webhook_id or any(not headers.get(name) for name in required):
        return False
    response = httpx.post(
        f"{BASE}/v1/notifications/verify-webhook-signature",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
        json={
            "transmission_id": headers["PAYPAL-TRANSMISSION-ID"],
            "transmission_time": headers["PAYPAL-TRANSMISSION-TIME"],
            "cert_url": headers["PAYPAL-CERT-URL"],
            "auth_algo": headers["PAYPAL-AUTH-ALGO"],
            "transmission_sig": headers["PAYPAL-TRANSMISSION-SIG"],
            "webhook_id": webhook_id,
            "webhook_event": event,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("verification_status") == "SUCCESS"
