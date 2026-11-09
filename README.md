# PayPilot

AI agent that turns a chat message into a PayPal invoice, tracks payment status, and writes polite reminders.
Built for the PayPal AI Hackathon with FastAPI, React, the Gemini API (tool calling) and the PayPal Invoicing API.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys
uvicorn app.main:app --reload
```
Open http://localhost:8000

- `GEMINI_API_KEY`: from aistudio.google.com
- `GEMINI_MODEL`: Gemini model name (default: `gemini-3.8-flash`)
- `AI_PROVIDER`: currently `gemini`
- `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`: developer.paypal.com > Apps & Credentials > Sandbox > your app.
  Leave both blank to run in mock mode (no real PayPal calls).
- `DATABASE_URL`: optional PostgreSQL/Supabase connection URL; leave blank when using MongoDB or SQLite.
- `MONGODB_URI`: optional MongoDB Atlas connection URI; leave blank when using PostgreSQL or SQLite.
- `MONGODB_DB`: MongoDB database name, defaulting to `paypilot`.
- `AUTH_REQUIRED`: set to `true` before public deployment to require authenticated API access.

## How it works
1. Chat message goes to `/api/chat`; Claude uses the `draft_invoice` tool to extract structured data (or asks a follow-up question).
2. The user reviews the draft and presses **Confirm and send**. Nothing is sent without approval.
3. `/api/invoices` creates and sends the invoice via PayPal Invoicing v2 and stores it in SQLite, PostgreSQL, or MongoDB.
4. Status updates through the PayPal webhook (`POST /api/webhooks/paypal`) or the **Refresh** button. The webhook payload is never trusted: the invoice is re-fetched from PayPal.
5. **Send reminder** has Gemini write the message and sends it through PayPal's remind endpoint.

Authentication endpoints are available at `/api/auth/register`, `/api/auth/login`, and
`/api/auth/me`. Sessions use hashed passwords and expiring bearer tokens.

## Testing the full flow in the PayPal sandbox
1. Create a sandbox Business and Personal account at developer.paypal.com > Testing Tools > Sandbox Accounts.
2. Use your **Business** account's REST app credentials in `.env`.
3. Invoice a client using the **Personal** sandbox account's email, open the invoice link and pay it, then press **Refresh**.
4. For automatic updates, expose the app with a tunnel (e.g. `ngrok http 8000`) and add a sandbox webhook
   `https://<tunnel>/api/webhooks/paypal` for `INVOICING.INVOICE.PAID`.

## Docker
```bash
docker build -t paypilot . && docker run --env-file .env -p 8000:8000 paypilot
```

## Layout
```
app/main.py     API routes and validation
app/agent.py    Claude tool-calling (invoice drafting, Q&A, reminders)
app/paypal.py   PayPal Invoicing v2 client (mock mode when no keys)
app/db.py       SQLite storage
app/static/     React chat + dashboard (no build step)
```
