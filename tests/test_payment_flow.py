import os, sys, json, hmac, hashlib, tempfile
from datetime import timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["MOCK_PAYMENTS"] = "1"
import pytest
from app import app
from models import db, Booking, Payment, now
import services as svc

GW = app.gateway


@pytest.fixture(autouse=True)
def fresh():
    with app.app_context():
        db.drop_all(); db.create_all(); svc.seed()


def user(email):
    c = app.test_client()
    r = c.post("/api/auth/register", json={"name": "T", "email": email, "password": "secret1"})
    assert r.status_code == 201
    return c


def book(c, seats=("A1", "A2")):
    return c.post("/api/bookings", json={"event_id": 1, "seats": list(seats)})


def seat_status(c, label):
    return {s["label"]: s["status"] for s in c.get("/api/events/1/seats").get_json()["seats"]}[label]


def verify(c, order_id):
    t = c.post("/api/dev/mock-pay", json={"order_id": order_id}).get_json()
    return c.post("/api/payments/verify", json=t), t


def webhook(c, event, order_id, pay_id, event_id, sig=None):
    body = json.dumps({"event": event, "payload": {"payment": {"entity": {
        "id": pay_id, "order_id": order_id, "error_description": "card declined"}}}}).encode()
    sig = sig or hmac.new(GW.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return c.post("/api/webhooks/razorpay", data=body, headers={
        "X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": event_id, "Content-Type": "application/json"})


def test_success_confirms_booking_and_records_txn():
    c = user("a@x.com"); d = book(c).get_json()
    assert d["booking"]["status"] == "PENDING" and seat_status(c, "A1") == "RESERVED"
    r, t = verify(c, d["payment"]["order_id"])
    b = r.get_json()["booking"]
    assert b["status"] == "CONFIRMED" and seat_status(c, "A1") == "BOOKED"
    assert b["payments"][0]["status"] == "SUCCESS" and b["payments"][0]["transaction_id"] == t["razorpay_payment_id"]


def test_duplicate_confirmations_never_duplicate_bookings():
    c = user("a@x.com"); d = book(c).get_json(); oid = d["payment"]["order_id"]
    _, t = verify(c, oid)
    assert c.post("/api/payments/verify", json=t).status_code == 200
    for eid in ("e1", "e2"):
        assert webhook(c, "payment.captured", oid, t["razorpay_payment_id"], eid).status_code == 200
    assert webhook(c, "payment.captured", oid, t["razorpay_payment_id"], "e1").get_json()["status"] == "duplicate ignored"
    with app.app_context():
        assert Booking.query.count() == 1 and Payment.query.count() == 1
    h = c.get("/api/profile/history").get_json()
    assert len(h["bookings"]) == 1 and h["summary"]["total_paid"] == 2 * 149900


def test_failed_payment_releases_seats_then_retry_succeeds():
    c = user("a@x.com"); d = book(c).get_json(); oid = d["payment"]["order_id"]
    assert webhook(c, "payment.failed", oid, "pay_f1", "f1").status_code == 200
    assert seat_status(c, "A1") == "AVAILABLE"
    b = c.get(f"/api/bookings/{d['booking']['id']}").get_json()["booking"]
    assert b["status"] == "FAILED" and b["payments"][0]["status"] == "FAILED" and b["payments"][0]["reason"]
    r = c.post(f"/api/bookings/{b['id']}/retry").get_json()
    assert r["booking"]["status"] == "PENDING" and r["payment"]["order_id"] != oid
    b2 = verify(c, r["payment"]["order_id"])[0].get_json()["booking"]
    assert b2["status"] == "CONFIRMED" and [p["status"] for p in b2["payments"]] == ["FAILED", "SUCCESS"]


def test_cancel_releases_seats_for_others_and_is_idempotent():
    a, b = user("a@x.com"), user("b@x.com"); d = book(a).get_json()
    assert book(b).status_code == 409
    for _ in range(2):
        assert a.post(f"/api/bookings/{d['booking']['id']}/cancel").get_json()["booking"]["status"] == "CANCELLED"
    assert book(b).status_code == 201


def test_bad_signatures_rejected():
    c = user("a@x.com"); d = book(c).get_json(); oid = d["payment"]["order_id"]
    r = c.post("/api/payments/verify", json={"razorpay_order_id": oid, "razorpay_payment_id": "pay_x", "razorpay_signature": "bad"})
    assert r.status_code == 400
    assert webhook(c, "payment.captured", oid, "pay_x", "z1", sig="bad").status_code == 400
    assert c.get(f"/api/bookings/{d['booking']['id']}").get_json()["booking"]["status"] == "PENDING"


def test_user_cannot_touch_other_users_booking():
    a, b = user("a@x.com"), user("b@x.com"); d = book(a).get_json()
    assert b.post(f"/api/bookings/{d['booking']['id']}/cancel").status_code == 404


def test_expiry_releases_and_late_capture_reacquires_or_flags_refund():
    a, b = user("a@x.com"), user("b@x.com"); d = book(a).get_json(); oid = d["payment"]["order_id"]
    with app.app_context():
        bk = db.session.get(Booking, d["booking"]["id"]); bk.expires_at = now() - timedelta(minutes=1); db.session.commit()
    assert seat_status(a, "A1") == "AVAILABLE"
    assert webhook(a, "payment.captured", oid, "pay_late", "l1").status_code == 200
    assert a.get(f"/api/bookings/{d['booking']['id']}").get_json()["booking"]["status"] == "CONFIRMED"
    d2 = book(a, ("B1",)).get_json(); oid2 = d2["payment"]["order_id"]
    with app.app_context():
        bk = db.session.get(Booking, d2["booking"]["id"]); bk.expires_at = now() - timedelta(minutes=1); db.session.commit()
    assert book(b, ("B1",)).status_code == 201
    webhook(a, "payment.captured", oid2, "pay_late2", "l2")
    assert a.get(f"/api/bookings/{d2['booking']['id']}").get_json()["booking"]["status"] == "REFUND_REQUIRED"
