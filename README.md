# TicketHub – Ticket Booking with Complete Payment Workflow

Flask + SQLAlchemy + Razorpay (Postgres in production, SQLite locally). All payment
logic (signature verification, webhooks, seat locking, idempotency) runs on the server.

## How it works
| Situation | What the server does |
|---|---|
| User picks seats | Seats atomically locked `RESERVED` for `HOLD_MINUTES` (row locks, all-or-nothing); booking `PENDING`; Razorpay order + `Payment(CREATED)` created |
| Payment success | HMAC signature verified (`/api/payments/verify`) and/or `payment.captured` webhook -> seats `BOOKED`, booking `CONFIRMED` |
| Payment failed | `payment.failed` webhook or client report -> payment `FAILED` (with reason), seats auto-released, booking `FAILED` |
| Payment cancelled | User closes checkout -> booking `CANCELLED`, seats released |
| Retry | `/api/bookings/<id>/retry` re-locks the same seats (409 if taken) and creates a new order (`attempt+1`) |
| Hold expired | Seats auto-released lazily on every seat/booking request; a late successful payment re-acquires the seats, otherwise booking becomes `REFUND_REQUIRED` |
| Duplicates | `mark_paid` is idempotent (row lock + status check); `gateway_order_id` and `gateway_payment_id` are UNIQUE; webhook `event_id` is UNIQUE. Verify + webhook + replays => exactly one booking |
| Audit | Every attempt is a `Payment` row (order id, transaction id, status, amount, reason, times); raw webhooks stored in `WebhookEvent` |
| History | `GET /api/profile/history` (UI: "My Bookings") |

## Run locally
```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # keep Razorpay keys empty => MOCK mode
python app.py               # http://localhost:5000
pytest                      # 7 tests
```
Mock mode shows a fake checkout with Success / Failure / Close buttons, but verification uses the same HMAC code as production.

## Use real Razorpay (test mode)
1. Razorpay Dashboard -> Settings -> API Keys -> generate **Test** keys.
2. Put `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in `.env` (or Render env vars).
3. Dashboard -> Settings -> Webhooks -> Add: URL `https://<your-app>.onrender.com/api/webhooks/razorpay`,
   secret = any string (set same in `RAZORPAY_WEBHOOK_SECRET`), events: `payment.captured`, `payment.failed`, `order.paid`.
4. Test card: `4111 1111 1111 1111`, any future expiry, any CVV.

## Deploy on Render
Push to GitHub -> Render -> New -> **Blueprint** -> select repo (uses `render.yaml`: web service + free Postgres).
Then add the three Razorpay env vars in the service settings.
(Free Postgres on Render expires after a limited period; fine for internship demo.)

## API summary
`POST /api/auth/register|login|logout` · `GET /api/me` · `GET /api/events` · `GET /api/events/<id>/seats`
`POST /api/bookings` · `POST /api/bookings/<id>/retry|cancel` · `GET /api/bookings/<id>`
`POST /api/payments/verify` · `POST /api/payments/failed` · `POST /api/webhooks/razorpay` · `GET /api/profile/history`
