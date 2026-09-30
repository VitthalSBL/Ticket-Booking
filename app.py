import os
import re
import json
import hashlib
import uuid
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, request, jsonify, session
from sqlalchemy.exc import IntegrityError

load_dotenv()
from models import db, User, Event, Booking, Payment, WebhookEvent  # noqa: E402
import services as svc  # noqa: E402
from gateway import get_gateway  # noqa: E402


def iso(d):
    return d.isoformat() + "Z" if d else None


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")
    db_url = os.getenv("DATABASE_URL", "sqlite:///tickets.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    key, secret = os.getenv("RAZORPAY_KEY_ID", ""), os.getenv("RAZORPAY_KEY_SECRET", "")
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-change-me"),
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
        HOLD_MINUTES=int(os.getenv("HOLD_MINUTES", "10")),
        RAZORPAY_KEY_ID=key, RAZORPAY_KEY_SECRET=secret,
        RAZORPAY_WEBHOOK_SECRET=os.getenv("RAZORPAY_WEBHOOK_SECRET", ""),
        MOCK_PAYMENTS=os.getenv("MOCK_PAYMENTS") == "1" or not (key and secret),
    )
    db.init_app(app)
    app.gateway = get_gateway(app.config)
    with app.app_context():
        db.create_all()
        svc.seed()

    def login_required(f):
        @wraps(f)
        def w(*a, **k):
            if not session.get("uid"):
                return jsonify(error="Please log in"), 401
            return f(*a, **k)
        return w

    def uid():
        return session["uid"]

    def pay_dict(p):
        return dict(id=p.id, order_id=p.gateway_order_id, transaction_id=p.gateway_payment_id,
                    status=p.status, amount=p.amount, attempt=p.attempt, reason=p.failure_reason,
                    note=p.note, created_at=iso(p.created_at), paid_at=iso(p.paid_at))

    def booking_dict(b):
        return dict(id=b.id, status=b.status, amount=b.amount,
                    event=dict(id=b.event.id, title=b.event.title, venue=b.event.venue, starts_at=iso(b.event.starts_at)),
                    seats=sorted(l.seat.label for l in b.seat_links),
                    created_at=iso(b.created_at), expires_at=iso(b.expires_at), confirmed_at=iso(b.confirmed_at),
                    payments=[pay_dict(p) for p in sorted(b.payments, key=lambda x: x.attempt)])

    def checkout_payload(b):
        live = [p for p in b.payments if p.status == "CREATED"]
        if not live:
            return dict(booking=booking_dict(b), payment=None)
        p = max(live, key=lambda x: x.attempt)
        return dict(booking=booking_dict(b), payment=dict(
            order_id=p.gateway_order_id, amount=p.amount, currency="INR",
            key_id=app.gateway.key_id, mock=app.gateway.mock))

    def own_booking(bid):
        b = db.session.get(Booking, bid)
        if not b or b.user_id != uid():
            return None
        return b

    @app.errorhandler(svc.BookingError)
    def _be(e):
        return jsonify(error=str(e)), e.code

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/config")
    def config():
        return jsonify(key_id=app.gateway.key_id, mock=app.gateway.mock)

    # ---------- auth ----------
    @app.post("/api/auth/register")
    def register():
        d = request.get_json(silent=True) or {}
        name, email, pw = (d.get("name") or "").strip(), (d.get("email") or "").strip().lower(), d.get("password") or ""
        if not name or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) or len(pw) < 6:
            return jsonify(error="Valid name, email and a password of 6+ characters are required"), 400
        u = User(name=name, email=email)
        u.set_password(pw)
        db.session.add(u)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify(error="Email already registered"), 409
        session["uid"] = u.id
        return jsonify(user=dict(id=u.id, name=u.name, email=u.email)), 201

    @app.post("/api/auth/login")
    def login():
        d = request.get_json(silent=True) or {}
        u = User.query.filter_by(email=(d.get("email") or "").strip().lower()).first()
        if not u or not u.check_password(d.get("password") or ""):
            return jsonify(error="Invalid email or password"), 401
        session["uid"] = u.id
        return jsonify(user=dict(id=u.id, name=u.name, email=u.email))

    @app.post("/api/auth/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/me")
    def me():
        u = db.session.get(User, session.get("uid")) if session.get("uid") else None
        return jsonify(user=dict(id=u.id, name=u.name, email=u.email) if u else None)

    # ---------- events ----------
    @app.get("/api/events")
    def events():
        svc.release_expired()
        db.session.commit()
        counts = svc.available_counts()
        return jsonify(events=[dict(id=e.id, title=e.title, venue=e.venue, starts_at=iso(e.starts_at),
                                    price=e.price, available=counts.get(e.id, 0)) for e in Event.query.order_by(Event.starts_at)])

    @app.get("/api/events/<int:eid>/seats")
    def seats(eid):
        e = db.session.get(Event, eid)
        if not e:
            return jsonify(error="Event not found"), 404
        return jsonify(event=dict(id=e.id, title=e.title, venue=e.venue, price=e.price),
                       seats=[dict(label=s.label, status=s.status) for s in svc.event_seats(eid)])

    # ---------- bookings ----------
    @app.post("/api/bookings")
    @login_required
    def create_booking():
        d = request.get_json(silent=True) or {}
        user = db.session.get(User, uid())
        try:
            b = svc.create_booking(user, d.get("event_id"), d.get("seats"))
            db.session.commit()
        except svc.BookingError:
            raise
        except Exception:
            db.session.rollback()
            app.logger.exception("create booking failed")
            return jsonify(error="Could not start payment. Please try again."), 502
        return jsonify(checkout_payload(b)), 201

    @app.post("/api/bookings/<int:bid>/retry")
    @login_required
    def retry(bid):
        b = own_booking(bid)
        if not b:
            return jsonify(error="Booking not found"), 404
        try:
            svc.retry_booking(b)
            db.session.commit()
        except svc.BookingError:
            db.session.commit()
            raise
        except Exception:
            db.session.rollback()
            app.logger.exception("retry failed")
            return jsonify(error="Could not restart payment"), 502
        return jsonify(checkout_payload(b))

    @app.post("/api/bookings/<int:bid>/cancel")
    @login_required
    def cancel(bid):
        b = own_booking(bid)
        if not b:
            return jsonify(error="Booking not found"), 404
        svc.cancel_booking(b)
        db.session.commit()
        return jsonify(booking=booking_dict(b))

    @app.get("/api/bookings/<int:bid>")
    @login_required
    def get_booking(bid):
        svc.release_expired()
        db.session.commit()
        b = own_booking(bid)
        return (jsonify(booking=booking_dict(b)) if b else (jsonify(error="Booking not found"), 404))

    @app.get("/api/profile/history")
    @login_required
    def history():
        svc.release_expired()
        db.session.commit()
        u = db.session.get(User, uid())
        bookings = Booking.query.filter_by(user_id=u.id).order_by(Booking.id.desc()).all()
        payments = Payment.query.filter_by(user_id=u.id).all()
        return jsonify(
            user=dict(id=u.id, name=u.name, email=u.email),
            summary=dict(confirmed=sum(1 for b in bookings if b.status == "CONFIRMED"),
                         total_paid=sum(p.amount for p in payments if p.status == "SUCCESS")),
            bookings=[booking_dict(b) for b in bookings])

    # ---------- payments ----------
    @app.post("/api/payments/verify")
    @login_required
    def verify():
        d = request.get_json(silent=True) or {}
        oid, pid, sig = d.get("razorpay_order_id"), d.get("razorpay_payment_id"), d.get("razorpay_signature")
        p = Payment.query.filter_by(gateway_order_id=oid, user_id=uid()).first()
        if not p:
            return jsonify(error="Order not found"), 404
        if not app.gateway.verify_payment(oid, pid, sig):
            return jsonify(error="Signature verification failed"), 400
        b = svc.mark_paid(oid, pid)
        db.session.commit()
        return jsonify(booking=booking_dict(b))

    @app.post("/api/payments/failed")
    @login_required
    def failed():
        d = request.get_json(silent=True) or {}
        p = Payment.query.filter_by(gateway_order_id=d.get("order_id"), user_id=uid()).first()
        if not p:
            return jsonify(error="Order not found"), 404
        b = svc.mark_failed(p.gateway_order_id, d.get("payment_id"), d.get("reason"))
        db.session.commit()
        return jsonify(booking=booking_dict(b or p.booking))

    @app.post("/api/webhooks/razorpay")
    def webhook():
        raw = request.get_data()
        if not app.gateway.verify_webhook(raw, request.headers.get("X-Razorpay-Signature", "")):
            return jsonify(error="invalid signature"), 400
        try:
            payload = json.loads(raw)
        except ValueError:
            return jsonify(error="bad payload"), 400
        event_id = request.headers.get("X-Razorpay-Event-Id") or hashlib.sha256(raw).hexdigest()
        try:
            db.session.add(WebhookEvent(event_id=event_id, event=payload.get("event"), payload=raw.decode()))
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            return jsonify(status="duplicate ignored"), 200
        try:
            ev = payload.get("event")
            ents = payload.get("payload", {})
            pay = ents.get("payment", {}).get("entity", {})
            order_id = pay.get("order_id") or ents.get("order", {}).get("entity", {}).get("id")
            if order_id and ev in ("payment.captured", "order.paid"):
                svc.mark_paid(order_id, pay.get("id"))
            elif order_id and ev == "payment.failed":
                svc.mark_failed(order_id, pay.get("id"), pay.get("error_description"))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("webhook processing failed")
            return jsonify(error="processing error"), 500  # Razorpay will retry
        return jsonify(status="ok")

    # ---------- dev-only (mock mode) ----------
    @app.post("/api/dev/mock-pay")
    @login_required
    def mock_pay():
        if not app.gateway.mock:
            return jsonify(error="Not available"), 404
        oid = (request.get_json(silent=True) or {}).get("order_id")
        if not Payment.query.filter_by(gateway_order_id=oid, user_id=uid()).first():
            return jsonify(error="Order not found"), 404
        pid = "pay_mock_" + uuid.uuid4().hex[:14]
        return jsonify(razorpay_order_id=oid, razorpay_payment_id=pid,
                       razorpay_signature=app.gateway.sign_payment(oid, pid))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
