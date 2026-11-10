"""SIM mode client — same interface as PayPalClient, zero network.

Used when PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are absent. Simulates the
subscription state machine so the full Resurrection flow is demoable and
testable without credentials. EVERY event is logged with sim=True and the
dashboard shows a permanent SIM MODE badge — simulated results are never
presented as real PayPal traffic.
"""

import time
import uuid


def _id(prefix):
    return prefix + uuid.uuid4().hex[:16].upper()


class SimClient:
    def __init__(self, log):
        self.log = log
        self.subscriptions = {}
        self.invoices = {}
        self.ledger = []  # completed captures, feeds the $ RECOVERED counter

    def _emit(self, method, endpoint, note):
        time.sleep(0.25)  # let the dashboard breathe, like a real call
        self.log({
            "method": method, "endpoint": endpoint, "status": 200,
            "sim": True, "note": "[SIM] " + note, "ms": 250,
        })

    # ---- seed ----------------------------------------------------------
    def seed_patient(self, name="Jordan Member", email="member@example.com",
                     plan_name="Kensho Pro — Monthly", price="9.99"):
        sub_id = _id("I-")
        given, _, surname = name.partition(" ")
        self.subscriptions[sub_id] = {
            "id": sub_id,
            "status": "SUSPENDED",
            "plan_id": _id("P-"),
            "subscriber": {"name": {"given_name": given,
                                    "surname": surname or "Member"},
                           "email_address": email},
            "billing_info": {
                "outstanding_balance": {"currency_code": "USD",
                                        "value": price},
                "cycle_executions": [
                    {"tenure_type": "REGULAR", "sequence": 1,
                     "cycles_completed": 3, "cycles_remaining": 0}],
                "last_payment": {
                    "amount": {"currency_code": "USD", "value": price},
                    "time": "2026-10-26T14:03:00Z"},
                "next_billing_time": "2026-11-26T14:03:00Z",
                "failed_payments_count": 2,
            },
            "status_change_note": {
                "note": "Payment failure threshold reached: 2 failed payment "
                        "attempts (5 days apart). Subscription suspended by "
                        "PayPal's native retry policy.",
                "create_time": "2026-11-05T14:03:00Z"},
            "_plan_name": plan_name,
            "_price": price,
        }
        self._emit("POST", "/v1/billing/subscriptions",
                   "Seeded subscription (already ACTIVE, 3 paid cycles)")
        self._emit("POST", f"/v1/billing/subscriptions/{sub_id}/suspend",
                   "Native policy: retry x2 over 10 days failed -> SUSPENDED")
        return self.subscriptions[sub_id]

    # ---- subscriptions ---------------------------------------------------
    def get_subscription(self, sub_id):
        self._emit("GET", f"/v1/billing/subscriptions/{sub_id}",
                   "Read patient subscription")
        return self.subscriptions[sub_id]

    def suspend_subscription(self, sub_id, reason):
        self.subscriptions[sub_id]["status"] = "SUSPENDED"
        self._emit("POST", f"/v1/billing/subscriptions/{sub_id}/suspend",
                   reason)
        return {}

    def activate_subscription(self, sub_id, reason):
        self.subscriptions[sub_id]["status"] = "ACTIVE"
        self._emit("POST", f"/v1/billing/subscriptions/{sub_id}/activate",
                   "Reactivate subscription — patient revived")
        return {}

    def capture_outstanding(self, sub_id, amount, currency="USD"):
        sub = self.subscriptions[sub_id]
        sub["billing_info"]["outstanding_balance"] = {
            "currency_code": currency, "value": "0.00"}
        capture_id = _id("CAP-")
        self.ledger.append({
            "transaction_id": capture_id,
            "transaction_amount": {"currency_code": currency,
                                   "value": amount},
            "transaction_event_code": "T0006",
            "transaction_subject": f"Subscription {sub_id} outstanding "
                                   "balance capture",
            "transaction_initiation_date": "2026-11-10T16:00:00Z",
        })
        self._emit("POST", f"/v1/billing/subscriptions/{sub_id}/capture",
                   f"Captured outstanding balance ${amount} "
                   f"(capture {capture_id})")
        return {"id": capture_id, "status": "COMPLETED"}

    # ---- invoicing ---------------------------------------------------------
    def create_invoice(self, invoice_body):
        inv_id = _id("INV2-")
        # Invoicing v2 carries no top-level amount — like the real API,
        # the total is computed from the items (recovery.py stopped
        # sending "amount" after the live API rejected it with a 400).
        amount = (invoice_body.get("amount") or {}).get("value")
        if amount is None:
            total = 0.0
            for item in invoice_body.get("items", []):
                total += (float(item["unit_amount"]["value"])
                          * float(item.get("quantity", "1")))
            amount = f"{total:.2f}"
        email = (invoice_body["primary_recipients"][0]
                 ["billing_info"]["email_address"])
        self.invoices[inv_id] = {
            "id": inv_id, "status": "DRAFT", "amount": amount,
            "email": email,
            "links": [{"rel": "payer-view",
                       "href": f"https://www.sandbox.paypal.com/invoice/"
                               f"payerView/details/{inv_id}"}],
        }
        self._emit("POST", "/v2/invoicing/invoices",
                   f"Recovery invoice drafted for ${amount}")
        return {"id": inv_id,
                "href": f"https://api-m.sandbox.paypal.com/v2/invoicing/"
                        f"invoices/{inv_id}"}

    def send_invoice(self, invoice_id):
        self.invoices[invoice_id]["status"] = "SENT"
        self._emit("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                   "Recovery invoice sent to member")
        return {}

    def get_invoice(self, invoice_id):
        self._emit("GET", f"/v2/invoicing/invoices/{invoice_id}",
                   "Check invoice status")
        return self.invoices[invoice_id]

    def sim_member_pays(self, invoice_id):
        self.invoices[invoice_id]["status"] = "PAID"
        self._emit("POST", f"/v2/invoicing/invoices/{invoice_id}/pay",
                   "Member paid the recovery invoice (simulated payer)")

    # ---- reporting ---------------------------------------------------------
    def transaction_search(self, start_iso, end_iso):
        self._emit("GET", "/v1/reporting/transactions",
                   "Transaction Search: verify recovered funds")
        return {"transaction_details": list(self.ledger)}
