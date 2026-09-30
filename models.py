from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def now():
    return datetime.utcnow()


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=now)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    venue = db.Column(db.String(150), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    price = db.Column(db.Integer, nullable=False)  # paise per seat


class Seat(db.Model):
    """AVAILABLE -> RESERVED (temporary hold) -> BOOKED (only after verified payment)."""
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False, index=True)
    label = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(12), nullable=False, default="AVAILABLE")
    booking_id = db.Column(db.Integer, db.ForeignKey("booking.id"), nullable=True)
    held_until = db.Column(db.DateTime, nullable=True)
    __table_args__ = (db.UniqueConstraint("event_id", "label"),)


class Booking(db.Model):
    # PENDING, CONFIRMED, FAILED, CANCELLED, EXPIRED, REFUND_REQUIRED
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    event_id = db.Column(db.Integer, db.ForeignKey("event.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    amount = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=now)
    expires_at = db.Column(db.DateTime)
    confirmed_at = db.Column(db.DateTime)
    event = db.relationship("Event")
    user = db.relationship("User")
    seat_links = db.relationship("BookingSeat", backref="booking", lazy="joined")
    payments = db.relationship("Payment", backref="booking", lazy="joined")


class BookingSeat(db.Model):
    """Remembers which seats belong to a booking (needed for retries)."""
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("booking.id"), nullable=False, index=True)
    seat_id = db.Column(db.Integer, db.ForeignKey("seat.id"), nullable=False)
    seat = db.relationship("Seat", lazy="joined")


class Payment(db.Model):
    """One row per payment attempt. CREATED, SUCCESS, FAILED, CANCELLED, EXPIRED."""
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("booking.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    gateway_order_id = db.Column(db.String(64), unique=True, nullable=False)
    gateway_payment_id = db.Column(db.String(64), unique=True, nullable=True)  # transaction ID
    status = db.Column(db.String(12), nullable=False, default="CREATED")
    amount = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(3), default="INR")
    attempt = db.Column(db.Integer, default=1)
    failure_reason = db.Column(db.String(255))
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=now)
    paid_at = db.Column(db.DateTime)


class WebhookEvent(db.Model):
    """Every webhook stored once; event_id unique => replayed webhooks are ignored."""
    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.String(128), unique=True, nullable=False)
    event = db.Column(db.String(64))
    payload = db.Column(db.Text)
    received_at = db.Column(db.DateTime, default=now)
