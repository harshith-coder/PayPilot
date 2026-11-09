import json
import os
import time
from datetime import date

import httpx

PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
API_KEY = os.getenv("GEMINI_API_KEY", "")
CURRENCY = os.getenv("DEFAULT_CURRENCY", "USD")

TOOLS = [{
    "function_declarations": [{
        "name": "draft_invoice",
        "description": (
            "Prepare an invoice draft only when the client name, email, description, "
            "amount, and due date are known. Never guess missing details."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "client_name": {"type": "STRING"},
                "client_email": {"type": "STRING"},
                "description": {"type": "STRING"},
                "amount": {"type": "NUMBER"},
                "currency": {"type": "STRING", "description": "ISO currency code"},
                "due_date": {"type": "STRING", "description": "YYYY-MM-DD"},
            },
            "required": ["client_name", "client_email", "description", "amount", "due_date"],
        },
    }]
}]


def _system_prompt(invoices):
    summary = [
        {k: i[k] for k in ("number", "client_name", "amount", "currency", "due_date", "display_status")}
        for i in invoices
    ]
    return (
        "You are PayPilot, an assistant helping small-business owners get paid through PayPal. "
        f"Today is {date.today().isoformat()}. Default currency is {CURRENCY}. "
        "Use draft_invoice only after all required fields are known; the user reviews and confirms "
        "the draft, and you never send anything yourself. Resolve relative dates such as 'in 7 days'. "
        "If details are missing, ask one short question. Answer money questions only from this invoice data:\n"
        + json.dumps(summary)
    )


def _contents(messages):
    return [
        {
            "role": "model" if message["role"] == "assistant" else "user",
            "parts": [{"text": message["content"]}],
        }
        for message in messages
    ]


def _generate(messages, system, max_tokens, structured=False):
    if PROVIDER != "gemini":
        raise RuntimeError("AI_PROVIDER must be 'gemini'")
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    generation_config = {"maxOutputTokens": max_tokens}
    if structured:
        generation_config.update({
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "reply": {"type": "STRING"},
                    "draft": {
                        "type": "OBJECT",
                        "properties": {
                            "client_name": {"type": "STRING"},
                            "client_email": {"type": "STRING"},
                            "description": {"type": "STRING"},
                            "amount": {"type": "NUMBER"},
                            "currency": {"type": "STRING"},
                            "due_date": {"type": "STRING"},
                        },
                    },
                },
                "required": ["reply"],
            },
        })
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": _contents(messages),
        "generationConfig": generation_config,
    }
    response = None
    last_error = None
    models = list(dict.fromkeys((MODEL, FALLBACK_MODEL)))
    for model in models:
        for attempt in range(2):
            try:
                response = httpx.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": API_KEY},
                    json=payload,
                    timeout=30,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                continue
            if response.status_code < 500:
                break
            last_error = RuntimeError(
                f"Gemini {response.status_code}: {response.text[:400]}"
            )
            time.sleep(1)
        if response is not None and response.status_code < 500:
            break
    if response is None:
        raise RuntimeError("Gemini request failed") from last_error
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini {response.status_code}: {response.text[:400]}")
    candidates = response.json().get("candidates", [])
    if not candidates or "content" not in candidates[0]:
        raise RuntimeError("Gemini returned no response content")
    return candidates[0]["content"].get("parts", [])


def chat(messages, invoices):
    parts = _generate(messages, _system_prompt(invoices), 700, structured=True)
    text = "".join(part.get("text", "") for part in parts).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini returned invalid invoice JSON") from exc
    draft = result.get("draft")
    if draft:
        draft.setdefault("currency", CURRENCY)
    return {"reply": result.get("reply", "").strip(), "draft": draft}


def write_reminder(inv):
    fallback = (
        f"Reminder: invoice {inv['number']} is due",
        f"Hi {inv['client_name']}, a friendly reminder that invoice {inv['number']} for "
        f"{inv['amount']:.2f} {inv['currency']} was due on {inv['due_date']}. "
        "Please let me know if you have any questions. Thank you!",
    )
    try:
        parts = _generate([{
            "role": "user",
            "content": (
                "Write a short, polite payment reminder from a small business owner. "
                f"Client: {inv['client_name']}. Invoice {inv['number']} for {inv['amount']:.2f} "
                f"{inv['currency']} ({inv['description']}), due {inv['due_date']}, "
                f"reminder number {inv['reminders'] + 1}. Under 70 words, no placeholders. "
                "Output only the message body."
            ),
        }], "Write the requested reminder exactly as instructed.", 300)
        note = "".join(part.get("text", "") for part in parts).strip()
        return (f"Reminder: invoice {inv['number']}", note) if note else fallback
    except Exception:
        return fallback
