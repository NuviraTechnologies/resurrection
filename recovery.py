"""Resurrection recovery engine.

State machine for one "patient" subscription:
    (none) -> FLATLINED (suspended, outstanding balance)
           -> DIAGNOSED -> INVOICED -> PAID -> REVIVED (captured + active)

Diagnosis is deterministic template logic over the real subscription data.
An optional LLM polish hook activates when DEEPSEEK_API_KEY or KIMI_API_KEY
is set (OpenAI-compatible chat endpoints); any failure falls back to the
template. The flow works fully with no key.
"""

import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

from paypal_client import PayPalClient, PayPalError
from sim_client import SimClient

EVENTS = []
_lock = threading.Lock()

# Every recovery invoice this app instance has created. The $ RECOVERED
# counter is tallied from these invoices' own payment records (GET invoice
# -> status + payments), never from local bookkeeping alone.
RECOVERY_INVOICE_IDS = []


class StageError(Exception):
    """A recovery step was requested out of order. The message is written
    for the dashboard user — plain, specific, no internals."""

PATIENT = {
    "seeded": False,
    "stage": "EMPTY",          # EMPTY|FLATLINED|DIAGNOSED|INVOICED|PAID|REVIVED
    "subscription_id": None,
    "member_name": None,
    "member_email": None,
    "plan_name": None,
    "amount": None,
    "currency": "USD",
    "status": None,
    "outstanding": "0.00",
    "failed_payments": 0,
    "diagnosis": None,
    "member_message": None,
    "invoice_id": None,
    "invoice_status": None,
    "invoice_link": None,
    "capture_id": None,
    "recovered_total": "0.00",
}


def log_event(ev):
    ev = dict(ev)
    ev["ts"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with _lock:
        EVENTS.append(ev)
        del EVENTS[:-200]


def system_event(note, sim):
    log_event({"method": "—", "endpoint": "resurrection", "status": 200,
               "sim": sim, "note": note, "ms": 0})


_HAS_ID = bool(os.environ.get("PAYPAL_CLIENT_ID"))
_HAS_SECRET = bool(os.environ.get("PAYPAL_CLIENT_SECRET"))
SIM_MODE = not (_HAS_ID and _HAS_SECRET)
CLIENT = SimClient(log_event) if SIM_MODE else PayPalClient(log_event)
if SIM_MODE and _HAS_ID != _HAS_SECRET:
    missing = "PAYPAL_CLIENT_SECRET" if _HAS_ID else "PAYPAL_CLIENT_ID"
    system_event(f"{missing} is not set, so live mode is off — running in "
                 "SIM mode. Set both PAYPAL_CLIENT_ID and "
                 "PAYPAL_CLIENT_SECRET to use the PayPal sandbox.", True)


def money(v):
    return f"{float(v):.2f}"


# ---------------------------------------------------------------- diagnosis
def diagnose(sub):
    """Deterministic diagnosis from subscription data + member message.

    Every amount in the output comes from the subscription record, and
    the member message describes each amount by its source. If PayPal
    reports an outstanding balance, that is the amount owed and the
    message calls it that. If PayPal reports no balance (the live
    sandbox case: buyer approval collected the first cycle, so no
    failed-payment balance ever posted), the amount owed is the missed
    cycle — the subscription's last payment amount — and the message
    presents it as the missed payment, never as a balance PayPal did
    not report.
    """
    billing = sub.get("billing_info", {}) or {}
    outstanding = (billing.get("outstanding_balance") or {}).get(
        "value", "0.00")
    last_payment = ((billing.get("last_payment") or {}).get("amount")
                    or {}).get("value", "0.00")
    failed = billing.get("failed_payments_count", 0)
    # Live payloads carry status_change_note as a plain string; the SIM
    # fixture modeled it as {"note": ...}. Handle both shapes.
    _raw_note = sub.get("status_change_note")
    if isinstance(_raw_note, dict):
        note = _raw_note.get("note", "")
    else:
        note = _raw_note or ""
    name = ((sub.get("subscriber") or {}).get("name") or {}).get(
        "given_name", "there")
    # Live payloads carry no plan name. The fallback is the bare noun
    # so the message reads "your membership" — the old fallback text
    # ("your membership") sat inside a template that already supplies
    # the possessive and produced "your your membership".
    plan = sub.get("_plan_name") or "membership"

    causes = []
    if failed:
        causes.append(
            f"the member's payment method failed {failed} times")
    if note:
        causes.append("PayPal's native policy note reads: " + note)
    if float(outstanding or 0) > 0:
        causes.append(
            f"an outstanding balance of ${money(outstanding)} is unpaid")
    elif float(last_payment or 0) > 0:
        causes.append(
            f"PayPal reports no outstanding balance; the missed amount "
            f"is ${money(last_payment)}, the subscription's last "
            "payment amount")
    if not causes:
        causes.append("the subscription is suspended with no payment "
                      "activity recorded")
    diagnosis = ("Diagnosis: " + "; ".join(causes) +
                 ". PayPal's native handling stops here — retry twice, "
                 "suspend, and the member is never told why or how to fix "
                 "it. Resurrection treats this as recoverable: one clear "
                 "invoice for exactly the missed amount, then capture and "
                 "reactivation the moment it is paid.")

    if float(outstanding or 0) > 0:
        opener = (f"your {plan} was paused because a payment didn't go "
                  f"through, and the outstanding balance of "
                  f"${money(outstanding)} is still open")
    elif float(last_payment or 0) > 0:
        opener = (f"your last payment of ${money(last_payment)} didn't "
                  f"go through, and your {plan} was paused")
    else:
        opener = (f"your {plan} was paused because a payment didn't "
                  "go through")
    message = (
        f"Hi {name} — {opener}. "
        "Nothing is cancelled and you haven't lost anything. The invoice "
        "with this message covers exactly that missed payment. Pay it and "
        "your membership switches straight back on — no rejoining, no new "
        "plan, no fees. — Resurrection recovery, on behalf of the business")
    return diagnosis, _llm_polish(message, diagnosis) or message


def _llm_polish(message, diagnosis):
    """Optional: polish the member message with DeepSeek or Kimi.

    Returns None on any problem — the template message stands.
    """
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("KIMI_API_KEY")
    if not key:
        return None
    if os.environ.get("DEEPSEEK_API_KEY"):
        url, model = ("https://api.deepseek.com/chat/completions",
                      "deepseek-chat")
    else:
        url, model = ("https://api.moonshot.cn/v1/chat/completions",
                      "moonshot-v1-8k")
    try:
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content":
                 "Rewrite the customer recovery message to be warm, plain, "
                 "and under 90 words. Keep every fact and the exact dollar "
                 "amount. Return only the message."},
                {"role": "user", "content": message}],
            "temperature": 0.4}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        text = data["choices"][0]["message"]["content"].strip()
        system_event("Diagnosis message polished by LLM hook", SIM_MODE)
        return text or None
    except Exception:
        return None


# ------------------------------------------------------------------- stages
def _require_stage(*stages, action):
    if PATIENT["stage"] not in stages:
        raise StageError(
            f"Can't {action} yet — the patient is at stage "
            f"{PATIENT['stage']}. Follow the numbered steps in order; "
            "the buttons light up when a step is ready.")


def seed():
    if PATIENT["seeded"] and PATIENT["stage"] not in ("EMPTY", "REVIVED"):
        raise StageError(
            "A recovery is already in progress for this patient. Finish "
            "it (or restart the app) before admitting a new one.")
    if SIM_MODE:
        sub = CLIENT.seed_patient()
        _load_patient(sub)
        PATIENT["stage"] = "FLATLINED"
        system_event("Patient flatlined: subscription SUSPENDED with "
                     "outstanding balance (seeded)", True)
        return dict(PATIENT)
    # LIVE seed: suspend an existing sandbox subscription, or create one.
    sub_id = os.environ.get("RESURRECTION_SUBSCRIPTION_ID")
    if not sub_id:
        raise RuntimeError(
            "Live seed needs RESURRECTION_SUBSCRIPTION_ID in .env — create "
            "a subscription in the PayPal sandbox (see README 'Seeding'), "
            "or unset PAYPAL_CLIENT_ID to run in SIM mode.")
    sub = CLIENT.get_subscription(sub_id)
    if sub.get("status") not in ("ACTIVE", "SUSPENDED"):
        # e.g. APPROVAL_PENDING: the sandbox buyer has not approved the
        # subscription yet. Never dress that up as a flatlined patient —
        # refuse plainly so a live demo can't fake its starting state.
        raise StageError(
            f"Live seed stopped: subscription {sub_id} is "
            f"{sub.get('status')}, not ACTIVE. It has to be approved by "
            "the sandbox buyer and reach ACTIVE before Resurrection can "
            "seed a recovery from it.")
    if sub.get("status") == "ACTIVE":
        CLIENT.suspend_subscription(
            sub_id, "Resurrection demo seed: simulated failed payment — "
                    "native retry policy exhausted.")
        sub = CLIENT.get_subscription(sub_id)
    _load_patient(sub)
    PATIENT["stage"] = "FLATLINED"
    system_event("Patient flatlined: subscription suspended (live seed)",
                 False)
    return dict(PATIENT)


def _load_patient(sub):
    billing = sub.get("billing_info", {}) or {}
    name = (sub.get("subscriber") or {}).get("name") or {}
    PATIENT.update({
        "seeded": True,
        "subscription_id": sub.get("id"),
        "member_name": " ".join(
            x for x in [name.get("given_name"), name.get("surname")] if x)
            or "Member",
        "member_email": (sub.get("subscriber") or {}).get("email_address"),
        "plan_name": sub.get("_plan_name", "Subscription plan"),
        "amount": ((billing.get("last_payment") or {}).get("amount") or {})
                  .get("value", "0.00"),
        "status": sub.get("status"),
        "outstanding": (billing.get("outstanding_balance") or {})
                       .get("value", "0.00"),
        "failed_payments": billing.get("failed_payments_count", 0),
    })
    if float(PATIENT["outstanding"]) == 0 and float(PATIENT["amount"]) > 0:
        PATIENT["outstanding"] = PATIENT["amount"]


def run_diagnosis():
    if PATIENT["stage"] == "DIAGNOSED":
        return dict(PATIENT)  # already diagnosed — repeat click is a no-op
    _require_stage("FLATLINED", action="run a diagnosis")
    sub = CLIENT.get_subscription(PATIENT["subscription_id"])
    PATIENT["diagnosis"], PATIENT["member_message"] = diagnose(sub)
    PATIENT["stage"] = "DIAGNOSED"
    system_event("AI diagnosis complete", SIM_MODE)
    return dict(PATIENT)


def send_recovery_invoice():
    if PATIENT["stage"] in ("INVOICED", "PAID", "REVIVED"):
        return dict(PATIENT)  # invoice already sent — don't send another
    _require_stage("DIAGNOSED", action="send the recovery invoice")
    amount = money(PATIENT["outstanding"])
    body = {
        "detail": {
            "invoice_number": "RES-{}-{}".format(
                PATIENT['subscription_id'][-6:],
                datetime.now(timezone.utc).strftime("%H%M%S")),
            "reference": f"Recovery for subscription "
                         f"{PATIENT['subscription_id']}",
            "invoice_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "currency_code": PATIENT["currency"],
            "note": PATIENT["member_message"],
            "term": {"due_date": (datetime.now(timezone.utc)
                                  + timedelta(days=7)).strftime("%Y-%m-%d")},
        },
        "invoicer": {"name": {"given_name": "Resurrection",
                              "surname": "Recovery"}},
        "primary_recipients": [{
            "billing_info": {
                "name": {"given_name": PATIENT["member_name"]},
                "email_address": PATIENT["member_email"]}}],
        "items": [{
            "name": f"Missed payment recovery — {PATIENT['plan_name']}",
            "quantity": "1",
            "unit_amount": {"currency_code": PATIENT["currency"],
                            "value": amount}}],
        "configuration": {"partial_payment": {"allow_partial_payment": False},
                          "tax_calculated_after_discount": True},
        # No top-level "amount": Invoicing v2 computes it from the items.
        # Sending one is a 400 INVALID_REQUEST / calculation_error
        # (verified live against the sandbox, 2026-11-10).
    }
    created = CLIENT.create_invoice(body)
    PATIENT["invoice_id"] = created["id"]
    if created["id"] not in RECOVERY_INVOICE_IDS:
        RECOVERY_INVOICE_IDS.append(created["id"])
    CLIENT.send_invoice(created["id"])
    inv = CLIENT.get_invoice(created["id"])
    PATIENT["invoice_status"] = inv.get("status")
    for link in inv.get("links", []):
        if link.get("rel") == "payer-view":
            PATIENT["invoice_link"] = link.get("href")
    PATIENT["stage"] = "INVOICED"
    return dict(PATIENT)


def member_pays():
    """SIM: the simulated member pays. LIVE: poll the real invoice —
    the member pays through the PayPal invoice link (sandbox buyer)."""
    if PATIENT["stage"] == "PAID":
        return dict(PATIENT)  # already paid — repeat click is a no-op
    _require_stage("INVOICED", action="check payment")
    if SIM_MODE:
        CLIENT.sim_member_pays(PATIENT["invoice_id"])
    inv = CLIENT.get_invoice(PATIENT["invoice_id"])
    PATIENT["invoice_status"] = inv.get("status")
    # Live statuses (verified against the sandbox 2026-11-10): a payer
    # checkout payment reports "PAID"; a payment recorded through the
    # Invoicing record-payment API reports "MARKED_AS_PAID". Both mean
    # the member's balance is settled.
    if PATIENT["invoice_status"] in ("PAID", "MARKED_AS_PAID"):
        PATIENT["stage"] = "PAID"
    return dict(PATIENT)


def recover():
    if PATIENT["stage"] == "REVIVED":
        return dict(PATIENT)  # already recovered — a second capture would
        # double-count the recovered revenue, so repeats are a no-op
    _require_stage("PAID", action="capture and reactivate")
    # Read the balance PayPal itself reports, right now. The capture is
    # gated on a genuine outstanding balance: if the API says there is
    # none (e.g. the sandbox collected the first cycle at approval, so no
    # failed-payment balance ever posted), there is nothing to capture
    # and the recovery rides on the recovery invoice the member already
    # paid. A ZERO_OUTSTANDING_BALANCE answer must never kill the flow.
    sub = CLIENT.get_subscription(PATIENT["subscription_id"])
    api_outstanding = ((sub.get("billing_info") or {})
                       .get("outstanding_balance") or {}) \
                      .get("value", "0.00")
    PATIENT["outstanding"] = money(api_outstanding)
    if float(api_outstanding) > 0:
        try:
            capture = CLIENT.capture_outstanding(
                PATIENT["subscription_id"], money(api_outstanding),
                PATIENT["currency"])
            if isinstance(capture, dict):
                PATIENT["capture_id"] = capture.get("id")
        except PayPalError as e:
            if not (e.status == 422
                    and "ZERO_OUTSTANDING_BALANCE" in (e.body or "")):
                raise
            system_event(
                "PayPal reports no outstanding balance on this "
                "subscription — recovery rides on the paid recovery "
                "invoice", SIM_MODE)
    else:
        system_event(
            "PayPal reports no outstanding balance on this subscription "
            "— recovery rides on the paid recovery invoice", SIM_MODE)
    CLIENT.activate_subscription(
        PATIENT["subscription_id"],
        "Member recovered via Resurrection: recovery invoice paid, "
        "reactivating.")
    sub = CLIENT.get_subscription(PATIENT["subscription_id"])
    PATIENT["status"] = sub.get("status", "ACTIVE")
    PATIENT["outstanding"] = ((sub.get("billing_info") or {})
                              .get("outstanding_balance") or {}) \
                             .get("value", PATIENT["outstanding"])
    PATIENT["stage"] = "REVIVED"
    refresh_recovered_total()
    return dict(PATIENT)


def _invoice_paid_amount(inv):
    """Amount actually paid on one invoice, from PayPal's own record.

    Live GET-invoice shape: payments.transactions[] each carry an amount;
    the invoice also carries a top-level amount {value}. The SIM fixture
    carries amount as a plain string. Returns 0.0 unless the invoice is
    in a paid state."""
    if inv.get("status") not in ("PAID", "MARKED_AS_PAID"):
        return 0.0
    txns = ((inv.get("payments") or {}).get("transactions")) or []
    if txns:
        total = 0.0
        for txn in txns:
            value = (txn.get("amount") or {}).get("value")
            if value:
                total += float(value)
        return total
    amount = inv.get("amount")
    if isinstance(amount, dict):
        return float(amount.get("value") or 0)
    if amount:
        return float(amount)
    return 0.0


def refresh_recovered_total():
    """$ RECOVERED counter — sourced from Resurrection's own recovery
    invoices: for every invoice this app created, GET it back from PayPal
    and sum the recorded payments on the paid ones. One opportunistic
    Transaction Search call per refresh acts as an independent
    verification; if PayPal refuses it (the sandbox app has returned 403
    NOT_AUTHORIZED throughout testing), the counter still stands on the
    invoice records and the refusal is logged, not hidden."""
    total = 0.0
    for inv_id in RECOVERY_INVOICE_IDS:
        try:
            inv = CLIENT.get_invoice(inv_id)
        except Exception:
            continue  # an unreadable invoice contributes nothing
        total += _invoice_paid_amount(inv)
    PATIENT["recovered_total"] = money(total)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        CLIENT.transaction_search(start.strftime(fmt), end.strftime(fmt))
        system_event("Transaction Search verification call succeeded",
                     SIM_MODE)
    except PayPalError as e:
        if e.status == 403:
            system_event(
                "Transaction Search unavailable (403) — counter sourced "
                "from invoice payment records", SIM_MODE)
        else:
            system_event(
                f"Transaction Search verification call failed "
                f"(status {e.status}) — counter sourced from invoice "
                "payment records", SIM_MODE)
    return PATIENT["recovered_total"]


def state():
    with _lock:
        events = list(EVENTS)
    return {"mode": "SIM" if SIM_MODE else "LIVE",
            "patient": dict(PATIENT), "events": events}
