# Resurrection — AI Subscription Recovery

**PayPal AI Hackathon entry.** Built with the PayPal developer platform (sandbox) + AI.

## The problem

When a subscription payment fails, PayPal's native handling is: retry every
5 days, twice — then suspend the subscription. The member is never told why
it happened or how to fix it, and the business silently loses the revenue
and the customer. Failed payments ("involuntary churn") are one of the
largest sources of subscription revenue loss, and PayPal gives sellers no
recovery tooling at all.

## What Resurrection does

Resurrection is the recovery ward for flatlined subscriptions:

1. **Detect** — reads the suspended subscription (`GET /v1/billing/subscriptions/{id}`).
2. **Diagnose (AI)** — an AI diagnosis step turns the failure data (failed
   payment count, suspension note, outstanding balance) into a
   plain-language diagnosis and a warm, concrete recovery message for the
   member. Deterministic template logic by default; if `DEEPSEEK_API_KEY`
   or `KIMI_API_KEY` is set, an LLM polish hook rewrites the message
   (facts and amounts locked; any failure falls back to the template).
3. **Invoice** — creates and sends a PayPal recovery invoice for exactly
   the missed amount (Invoicing v2: `POST /v2/invoicing/invoices`, `.../send`).
   The diagnosis message rides on the invoice.
4. **Recover** — the moment the invoice is PAID, Resurrection reads the
   outstanding balance PayPal itself reports on the subscription. If
   PayPal reports a balance, it captures it
   (`POST /v1/billing/subscriptions/{id}/capture`, `OUTSTANDING_BALANCE`);
   if PayPal reports none, there is nothing to capture and the recovery
   rides on the invoice the member just paid. Either way it then
   reactivates the subscription (`POST .../activate`).
5. **Verify** — the **$ RECOVERED** counter is tallied from the recovery
   invoices' own payment records (each invoice read back from PayPal),
   with a PayPal Transaction Search (`GET /v1/reporting/transactions`)
   call per recovery as independent verification when the app has that
   scope.

The dashboard shows the patient subscription as a live EKG: flatline on
failure, revival on recovery — plus a complete event log of every real API
call (method, endpoint, HTTP status).

## Quick start

Zero dependencies — Python 3 standard library only.

```bash
git clone <this repo>
cd resurrection
python3 app.py            # http://localhost:8080
```

With no PayPal credentials, the app runs in **SIM MODE**: the full flow is
demoable end-to-end against a simulated state machine, and every simulated
call is permanently labeled `SIM` in the UI and event log. Simulated results
are never presented as real PayPal traffic.

Health check: `GET /api/health` returns `{"status": "ok", "mode": ...}`.

### Deploy (optional, free)

A `render.yaml` Blueprint is included for Render's free web service tier:
point Render at this repo and set `PAYPAL_CLIENT_ID` /
`PAYPAL_CLIENT_SECRET` in the dashboard (declared `sync: false` — no
values are stored in the repo). Render sets `PORT`, and the app binds
`0.0.0.0:$PORT` automatically when `PORT` is present; local runs without
`PORT` bind `127.0.0.1` only. With no secrets set on the host, the
deployed app runs in SIM MODE.

## Live sandbox mode

1. Create a free account at https://developer.paypal.com and a sandbox
   REST app; copy `.env.example` to `.env` and fill in
   `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` (`PAYPAL_ENV=sandbox`).
2. Export the variables (or use a shell env file) and run `python3 app.py`.
   Note: never commit `.env` — it is git-ignored.

### Seeding a failed subscription

The sandbox cannot force a real payment to fail on a schedule, so the
failed state is seeded, and the **recovery sequence itself is 100% real API
calls**:

- Create a subscription in the sandbox (PayPal dashboard test buyer/seller
  accounts, or `paypal_client.py`'s `create_product` / `create_plan` /
  `create_subscription` helpers), let it activate, then put its ID in
  `.env` as `RESURRECTION_SUBSCRIPTION_ID`.
- On "Admit patient", Resurrection reads it and suspends it with the reason
  *"simulated failed payment — native retry policy exhausted"* — the exact
  end state PayPal's retry-twice-then-suspend policy produces.
- From there everything is live: diagnosis, a real invoice to the member's
  sandbox email, the payment step, capture of the outstanding balance when
  PayPal reports one, reactivation, and Transaction Search verification.
  One disclosure on the payment step: the app's built-in "member pays"
  action records the payment through PayPal's Invoicing record-payment
  API (`POST /v2/invoicing/invoices/{id}/payments`) — a real API call
  recording an external payment, **not** a payer checkout click. A sandbox
  buyer can alternatively pay the invoice for real through its payer-view
  link; both routes land on the same invoice payment record the counter
  reads back.

## Layout

| File | Purpose |
|---|---|
| `app.py` | stdlib HTTP server + JSON API |
| `paypal_client.py` | PayPal REST client (OAuth client-credentials, raw REST, full call logging) |
| `sim_client.py` | SIM mode state machine — same interface, no network |
| `recovery.py` | Recovery state machine, AI diagnosis, orchestration |
| `static/index.html` | Single-page dashboard: patient card, EKG, $ RECOVERED counter, event log |

## Rules of the house

- $0 build: free tiers and open source only.
- The event log never lies: live calls are labeled LIVE, simulated calls
  are labeled SIM, and API errors are shown, not hidden.

## License

MIT — see `LICENSE`.
