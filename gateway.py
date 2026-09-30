import hmac
import hashlib
import uuid
import requests


def _sign(secret, msg):
    if isinstance(msg, str):
        msg = msg.encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


class Gateway:
    """Razorpay wrapper. In mock mode, orders are generated locally but the
    signature/webhook verification logic is identical to production."""

    def __init__(self, key_id, key_secret, webhook_secret, mock):
        self.mock = mock
        self.key_id = key_id or "rzp_mock"
        self.key_secret = key_secret or "mock_secret"
        self.webhook_secret = webhook_secret or "mock_webhook_secret"

    def create_order(self, amount, receipt):
        if self.mock:
            return "order_mock_" + uuid.uuid4().hex[:14]
        r = requests.post(
            "https://api.razorpay.com/v1/orders",
            auth=(self.key_id, self.key_secret),
            json={"amount": amount, "currency": "INR", "receipt": receipt, "payment_capture": 1},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["id"]

    def verify_payment(self, order_id, payment_id, signature):
        expected = _sign(self.key_secret, f"{order_id}|{payment_id}")
        return hmac.compare_digest(expected, signature or "")

    def verify_webhook(self, raw_body, signature):
        expected = _sign(self.webhook_secret, raw_body)
        return hmac.compare_digest(expected, signature or "")

    def sign_payment(self, order_id, payment_id):  # mock mode only
        return _sign(self.key_secret, f"{order_id}|{payment_id}")


def get_gateway(cfg):
    return Gateway(cfg["RAZORPAY_KEY_ID"], cfg["RAZORPAY_KEY_SECRET"],
                   cfg["RAZORPAY_WEBHOOK_SECRET"], cfg["MOCK_PAYMENTS"])
