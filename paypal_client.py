"""PayPal REST client — raw HTTP via stdlib urllib, OAuth client-credentials.

Every call is reported to an event-log callback as:
    {method, endpoint, status, sim: False, note}
so the dashboard can show judges the real API traffic.

Base URLs:
  sandbox: https://api-m.sandbox.paypal.com
  live:    https://api-m.paypal.com
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


class PayPalError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"PayPal API {status}: {body}")


class PayPalClient:
    """Minimal PayPal client covering exactly the Resurrection flow."""

    def __init__(self, log):
        self.client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
        self.client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "PayPalClient needs PAYPAL_CLIENT_ID and "
                "PAYPAL_CLIENT_SECRET set (see .env.example) — or unset "
                "both to run in SIM mode.")
        env = os.environ.get("PAYPAL_ENV", "sandbox").lower()
        self.base = BASE_URLS.get(env, BASE_URLS["sandbox"])
        self.log = log  # callable(event_dict)
        self._token = None
        self._token_exp = 0

    # ---- low level -----------------------------------------------------
    def _request(self, method, path, body=None, auth=True, note=""):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self._token_value()}")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                raw = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read().decode(errors="replace")
            self.log({
                "method": method, "endpoint": path, "status": status,
                "sim": False, "note": note or raw[:200],
                "ms": int((time.time() - started) * 1000),
            })
            raise PayPalError(status, raw)
        except urllib.error.URLError as e:
            self.log({
                "method": method, "endpoint": path, "status": 0,
                "sim": False,
                "note": f"Network error reaching PayPal: {e.reason}",
                "ms": int((time.time() - started) * 1000),
            })
            raise PayPalError(0, f"Network error: {e.reason}")
        self.log({
            "method": method, "endpoint": path, "status": status,
            "sim": False, "note": note,
            "ms": int((time.time() - started) * 1000),
        })
        return json.loads(raw) if raw else {}

    def _token_value(self):
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        url = self.base + "/v1/oauth2/token"
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = urllib.request.Request(
            url, data=b"grant_type=client_credentials", method="POST")
        req.add_header("Authorization", f"Basic {creds}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode(errors="replace"))
        except urllib.error.HTTPError as e:
            # Wrong credentials land here (401 invalid_client).
            raise PayPalError(e.code, e.read().decode(errors="replace"))
        except urllib.error.URLError as e:
            raise PayPalError(0, f"Network error: {e.reason}")
        self._token = data["access_token"]
        self._token_exp = time.time() + data.get("expires_in", 3600)
        self.log({
            "method": "POST", "endpoint": "/v1/oauth2/token", "status": 200,
            "sim": False, "note": "OAuth client-credentials", "ms": 0,
        })
        return self._token

    # ---- subscriptions -------------------------------------------------
    def get_subscription(self, sub_id):
        return self._request("GET", f"/v1/billing/subscriptions/{sub_id}",
                             note="Read patient subscription")

    def suspend_subscription(self, sub_id, reason):
        return self._request(
            "POST", f"/v1/billing/subscriptions/{sub_id}/suspend",
            {"reason": reason}, note="Seed: suspend (simulated failure)")

    def activate_subscription(self, sub_id, reason):
        return self._request(
            "POST", f"/v1/billing/subscriptions/{sub_id}/activate",
            {"reason": reason}, note="Reactivate subscription")

    def capture_outstanding(self, sub_id, amount, currency="USD"):
        return self._request(
            "POST", f"/v1/billing/subscriptions/{sub_id}/capture",
            {"note": "Resurrection recovery: outstanding balance captured "
                     "after member paid recovery invoice.",
             "capture_type": "OUTSTANDING_BALANCE",
             "amount": {"currency_code": currency, "value": amount}},
            note="Capture outstanding balance")

    def create_product(self, name, description):
        return self._request(
            "POST", "/v1/catalogs/products",
            {"name": name, "description": description, "type": "SERVICE",
             "category": "SOFTWARE"},
            note="Seed: create product")

    def create_plan(self, product_id, name, price, currency="USD"):
        return self._request(
            "POST", "/v1/billing/plans",
            {"product_id": product_id, "name": name,
             "billing_cycles": [{
                 "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                 "tenure_type": "REGULAR", "sequence": 1,
                 "total_cycles": 0,
                 "pricing_scheme": {"fixed_price": {
                     "value": price, "currency_code": currency}}}],
             "payment_preferences": {
                 "auto_bill_outstanding": True,
                 "payment_failure_threshold": 2},
             "taxes": {"percentage": "0", "inclusive": False}},
            note="Seed: create plan (PayPal native failure threshold = 2)")

    def create_subscription(self, plan_id, email, given_name, surname):
        return self._request(
            "POST", "/v1/billing/subscriptions",
            {"plan_id": plan_id,
             "subscriber": {"name": {"given_name": given_name,
                                     "surname": surname},
                            "email_address": email},
             "application_context": {"user_action": "SUBSCRIBE_NOW",
                                     "return_url": "https://example.com/ok",
                                     "cancel_url": "https://example.com/cancel"}},
            note="Seed: create subscription")

    # ---- invoicing ------------------------------------------------------
    def create_invoice(self, invoice_body):
        created = self._request("POST", "/v2/invoicing/invoices",
                                invoice_body,
                                note="Create recovery invoice (draft)")
        # Live API quirk (verified 2026-11-10): the 201 response is a
        # single HATEOAS link object {href, rel, method} — there is no
        # "id" field. Recover the invoice ID from the self href so
        # callers can rely on created["id"].
        if isinstance(created, dict) and not created.get("id"):
            href = created.get("href", "")
            if not href:
                for link in created.get("links", []):
                    if link.get("rel") == "self":
                        href = link.get("href", "")
                        break
            if href:
                created = dict(created)
                created["id"] = href.rstrip("/").split("/")[-1]
        return created

    def send_invoice(self, invoice_id):
        return self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
            {"send_to_invoicer": True}, note="Send recovery invoice to member")

    def get_invoice(self, invoice_id):
        return self._request("GET", f"/v2/invoicing/invoices/{invoice_id}",
                             note="Check invoice status")

    # ---- reporting ------------------------------------------------------
    def transaction_search(self, start_iso, end_iso):
        path = ("/v1/reporting/transactions"
                f"?start_date={start_iso}&end_date={end_iso}"
                "&fields=all&page_size=100&page=1")
        return self._request("GET", path,
                             note="Transaction Search: verify recovered funds")
