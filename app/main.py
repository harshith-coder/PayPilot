import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field, field_validator  # noqa: E402

from . import agent, db, paypal  # noqa: E402

logger = logging.getLogger("paypilot")
app = FastAPI(title="PayPilot")
db.init()
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() == "true"
bearer = HTTPBearer(auto_error=False)


class Msg(BaseModel):
    role: str
    content: str


class ChatIn(BaseModel):
    messages: list[Msg]


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=12)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
):
    if not credentials:
        if AUTH_REQUIRED:
            raise HTTPException(401, "Authentication required")
        return None
    user = db.session_user(credentials.credentials)
    if not user:
        raise HTTPException(401, "Invalid or expired session")
    return user


@app.post("/api/auth/register", status_code=201)
def register(body: Credentials):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Invalid email")
    user = db.create_user(email, body.password)
    if not user:
        raise HTTPException(409, "Email is already registered")
    return {"id": user["id"], "email": user["email"]}


@app.post("/api/auth/login")
def login(body: Credentials):
    user = db.authenticate(body.email.strip().lower(), body.password)
    if not user:
        raise HTTPException(401, "Invalid email or password")
    db.claim_unowned_invoices(user["id"])
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    return {
        "token": db.create_session(user["id"], expires.isoformat()),
        "expires_at": expires.isoformat(),
        "user": user,
    }


@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


class Draft(BaseModel):
    client_name: str = Field(min_length=1)
    client_email: str
    description: str = Field(min_length=1)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = "USD"
    due_date: str

    @field_validator("client_email")
    @classmethod
    def _email(cls, v):
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("invalid email")
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v):
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("currency must be a 3-letter uppercase ISO code")
        return v

    @field_validator("due_date")
    @classmethod
    def _date(cls, v):
        date.fromisoformat(v)
        return v


@app.get("/api/config")
def config():
    return {
        "mock": paypal.MOCK,
        "currency": agent.CURRENCY,
        "ai_provider": agent.PROVIDER,
        "ai_model": agent.MODEL,
        "auth_required": AUTH_REQUIRED,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/invoices")
def list_invoices(user=Depends(current_user)):
    return db.all_invoices(user["id"] if user else None)


@app.post("/api/chat")
def chat(body: ChatIn, user=Depends(current_user)):
    msgs = [m.model_dump() for m in body.messages if m.content.strip()]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "Last message must be from the user")
    try:
        return agent.chat(msgs, db.all_invoices(user["id"] if user else None))
    except Exception as e:
        logger.exception("%s request failed", agent.PROVIDER)
        raise HTTPException(502, f"{agent.PROVIDER.title()} request failed: {e}")


@app.post("/api/invoices")
def create_invoice(d: Draft, user=Depends(current_user)):
    data = d.model_dump()
    data["amount"] = float(data["amount"])
    try:
        pid, number = paypal.create_and_send(data)
    except Exception as e:
        logger.exception("PayPal invoice creation failed")
        raise HTTPException(502, str(e))
    inv_id = db.add(
        paypal_id=pid,
        number=number,
        user_id=user["id"] if user else None,
        **data,
    )
    return db.get(inv_id)


def _sync(inv):
    status = paypal.get_status(inv["paypal_id"])
    if status:
        db.set_status(inv["id"], status)
    return db.get(inv["id"])


@app.post("/api/invoices/{inv_id}/sync")
def sync(inv_id: int, user=Depends(current_user)):
    inv = db.get(inv_id, user["id"] if user else None)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    try:
        return _sync(inv)
    except Exception as e:
        logger.exception("Invoice sync failed for invoice %s", inv_id)
        raise HTTPException(502, str(e))


@app.post("/api/invoices/{inv_id}/mark-paid")
def mark_paid_mock(inv_id: int, user=Depends(current_user)):
    """Mock mode only: simulates a payment so the dashboard flow can be demoed offline."""
    if not paypal.MOCK:
        raise HTTPException(400, "Only available in mock mode")
    inv = db.get(inv_id, user["id"] if user else None)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    db.set_status(inv_id, "paid")
    return db.get(inv_id, user["id"] if user else None)


@app.post("/api/invoices/{inv_id}/remind")
def remind(inv_id: int, user=Depends(current_user)):
    inv = db.get(inv_id, user["id"] if user else None)
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv["display_status"] == "paid":
        raise HTTPException(400, "Invoice is already paid")
    subject, note = agent.write_reminder(inv)
    try:
        paypal.remind(inv["paypal_id"], subject, note)
    except Exception as e:
        logger.exception("Invoice reminder failed for invoice %s", inv_id)
        raise HTTPException(502, str(e))
    db.bump_reminder(inv_id)
    return {
        "invoice": db.get(inv_id, user["id"] if user else None),
        "subject": subject,
        "note": note,
    }


@app.post("/api/webhooks/paypal")
async def webhook(request: Request):
    """Point a PayPal sandbox webhook (INVOICING.INVOICE.*) here.
    The payload is not trusted: we re-fetch the invoice status from PayPal."""
    event = await request.json()
    if not paypal.MOCK and not paypal.verify_webhook_signature(request.headers, event):
        raise HTTPException(400, "Invalid PayPal webhook signature")
    event_id = event.get("id")
    if event_id and not db.record_webhook_event(event_id):
        return {"ok": True, "duplicate": True}
    res = event.get("resource", {})
    pid = res.get("invoice", {}).get("id") or res.get("id")
    inv = db.by_paypal_id(pid) if pid else None
    if inv:
        try:
            _sync(inv)
        except Exception:
            logger.exception("Webhook invoice sync failed for PayPal invoice %s", pid)
            raise HTTPException(502, "Invoice status synchronization failed")
    return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True))
