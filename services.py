"""Booking + payment business logic. Functions here never commit; the caller does,
so each HTTP request is one atomic transaction."""
from datetime import timedelta
from flask import current_app
from sqlalchemy import func
from models import db, Event, Seat, Booking, BookingSeat, Payment, now


class BookingError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _hold_until():
    return now() + timedelta(minutes=current_app.config["HOLD_MINUTES"])


def _release_seats(booking):
    Seat.query.filter_by(booking_id=booking.id, status="RESERVED").update(
        {"status": "AVAILABLE", "booking_id": None, "held_until": None}, synchronize_session=False)
    db.session.expire_all()


def release_expired():
    for b in Booking.query.filter(Booking.status == "PENDING", Booking.expires_at < now()).all():
        _release_seats(b)
        b.status = "EXPIRED"
        for p in b.payments:
            if p.status == "CREATED":
                p.status = "EXPIRED"
    db.session.flush()


def _claim(seat_ids, booking, new_status, until):
    """Row-locked, all-or-nothing seat claim (ordered by id to avoid deadlocks)."""
    seats = Seat.query.filter(Seat.id.in_(seat_ids)).order_by(Seat.id).with_for_update().all()
    ok = len(seats) == len(seat_ids) and all(
        s.status == "AVAILABLE" or (s.status == "RESERVED" and s.booking_id == booking.id) for s in seats)
    if not ok:
        return False
    for s in seats:
        s.status, s.booking_id, s.held_until = new_status, booking.id, until
    return True


def new_attempt(booking):
    for p in booking.payments:
        if p.status == "CREATED":
            p.status = "CANCELLED"  # superseded order
    attempt = len(booking.payments) + 1
    order_id = current_app.gateway.create_order(booking.amount, f"bk{booking.id}-a{attempt}")
    p = Payment(booking_id=booking.id, user_id=booking.user_id, gateway_order_id=order_id,
                amount=booking.amount, attempt=attempt, status="CREATED")
    db.session.add(p)
    db.session.flush()
    db.session.refresh(booking)
    return p


def create_booking(user, event_id, labels):
    release_expired()
    event = db.session.get(Event, event_id)
    if not event:
        raise BookingError("Event not found", 404)
    labels = list(dict.fromkeys(labels or []))
    if not 1 <= len(labels) <= 6:
        raise BookingError("Select between 1 and 6 seats")
    seats = Seat.query.filter(Seat.event_id == event.id, Seat.label.in_(labels)).all()
    if len(seats) != len(labels):
        raise BookingError("Invalid seat selection")
    b = Booking(user_id=user.id, event_id=event.id, status="PENDING",
                amount=event.price * len(seats), expires_at=_hold_until())
    db.session.add(b)
    db.session.flush()
    if not _claim([s.id for s in seats], b, "RESERVED", b.expires_at):
        db.session.rollback()
        raise BookingError("One or more seats are no longer available", 409)
    for s in seats:
        db.session.add(BookingSeat(booking_id=b.id, seat_id=s.id))
    db.session.flush()
    db.session.refresh(b)
    new_attempt(b)
    return b


def retry_booking(booking):
    release_expired()
    db.session.refresh(booking)
    if booking.status == "PENDING":  # resume the live order
        return booking
    if booking.status not in ("FAILED", "CANCELLED", "EXPIRED"):
        raise BookingError(f"Booking is {booking.status}; cannot retry", 409)
    ids = [l.seat_id for l in booking.seat_links]
    until = _hold_until()
    if not _claim(ids, booking, "RESERVED", until):
        raise BookingError("Seats are no longer available. Please pick new seats.", 409)
    booking.status, booking.expires_at = "PENDING", until
    new_attempt(booking)
    return booking


def cancel_booking(booking):
    """User dismissed the payment window. Idempotent; no-op unless still PENDING."""
    if booking.status != "PENDING":
        return booking
    _release_seats(booking)
    booking.status = "CANCELLED"
    for p in booking.payments:
        if p.status == "CREATED":
            p.status = "CANCELLED"
    return booking


def mark_paid(order_id, gateway_payment_id):
    """Idempotent. Called from BOTH the verify endpoint and the webhook; whichever
    arrives first confirms, the other sees SUCCESS and returns without changes."""
    p = Payment.query.filter_by(gateway_order_id=order_id).with_for_update().first()
    if p is None:
        return None
    b = Booking.query.filter_by(id=p.booking_id).with_for_update().first()
    if p.status == "SUCCESS":
        return b
    p.status, p.gateway_payment_id = "SUCCESS", gateway_payment_id or p.gateway_payment_id
    p.failure_reason, p.paid_at = None, now()
    if b.status == "CONFIRMED":
        p.note = "Duplicate payment for an already confirmed booking - refund required"
        return b
    ids = [l.seat_id for l in b.seat_links]
    if _claim(ids, b, "BOOKED", None):  # also re-acquires seats if hold had expired/released
        b.status, b.confirmed_at = "CONFIRMED", now()
    else:
        b.status = "REFUND_REQUIRED"
        p.note = "Paid but seats were taken meanwhile - refund required"
    return b


def mark_failed(order_id, gateway_payment_id=None, reason=None):
    p = Payment.query.filter_by(gateway_order_id=order_id).with_for_update().first()
    if p is None or p.status == "SUCCESS":
        return None
    b = Booking.query.filter_by(id=p.booking_id).with_for_update().first()
    p.status, p.failure_reason = "FAILED", (reason or "Payment failed")[:250]
    if gateway_payment_id and not p.gateway_payment_id:
        p.gateway_payment_id = gateway_payment_id
    latest = max(x.attempt for x in b.payments)
    if b.status == "PENDING" and p.attempt == latest:
        _release_seats(b)
        b.status = "FAILED"
    return b


def event_seats(event_id):
    release_expired()
    db.session.commit()
    return Seat.query.filter_by(event_id=event_id).order_by(Seat.id).all()


def available_counts():
    rows = db.session.query(Seat.event_id, func.count(Seat.id)).filter(Seat.status == "AVAILABLE").group_by(Seat.event_id).all()
    return dict(rows)


def seed():
    from datetime import timedelta as td
    if Event.query.first():
        return
    data = [("Arijit Singh Live", "Jawaharlal Nehru Stadium, Delhi", 12, 149900),
            ("Stand-up Night: Zakir Khan", "Siri Fort Auditorium, Delhi", 20, 79900),
            ("Tech Summit 2026", "Pragati Maidan, Delhi", 30, 49900)]
    for title, venue, days, price in data:
        e = Event(title=title, venue=venue, starts_at=now() + td(days=days), price=price)
        db.session.add(e)
        db.session.flush()
        for row in "ABCDE":
            for n in range(1, 11):
                db.session.add(Seat(event_id=e.id, label=f"{row}{n}"))
    db.session.commit()
