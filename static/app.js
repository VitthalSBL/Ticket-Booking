const $ = s => document.querySelector(s);
const S = { user: null, cfg: {}, event: null, seats: [], sel: new Set() };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const rupee = p => '₹' + (p / 100).toLocaleString('en-IN');
const when = d => d ? new Date(d).toLocaleString('en-IN') : '-';

async function api(path, method = 'GET', body) {
  const r = await fetch('/api' + path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || 'Request failed');
  return d;
}
function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.style.background = bad ? 'var(--bad)' : 'var(--ok)'; t.style.display = 'block';
  clearTimeout(toast.t); toast.t = setTimeout(() => t.style.display = 'none', 4000);
}
function nav() {
  $('#nav').innerHTML = S.user
    ? `<a onclick="go('events')">Events</a><a onclick="go('profile')">My Bookings</a><span>${esc(S.user.name)}</span><a onclick="logout()">Logout</a>`
    : `<a onclick="go('auth')">Login / Register</a>`;
}
async function go(view, arg) {
  nav();
  try {
    if (view === 'events') return await eventsView();
    if (view === 'seats') return await seatsView(arg);
    if (view === 'profile') return await profileView();
    return authView();
  } catch (e) { toast(e.message, true); }
}

async function eventsView() {
  const { events } = await api('/events');
  $('#app').innerHTML = '<h2>Upcoming events</h2>' + events.map(e => `
    <div class="card row"><div><b>${esc(e.title)}</b><div class="mut">${esc(e.venue)} · ${when(e.starts_at)}</div>
    <div class="mut">${rupee(e.price)} / seat · ${e.available} seats left</div></div>
    <button onclick="go('seats',${e.id})">Select seats</button></div>`).join('');
}

async function seatsView(id) {
  if (!S.user) { toast('Please log in first', true); return authView(); }
  const d = await api(`/events/${id}/seats`); S.event = d.event; S.seats = d.seats; S.sel.clear(); drawSeats();
}
function drawSeats() {
  const rows = {}; S.seats.forEach(s => (rows[s.label[0]] ??= []).push(s));
  $('#app').innerHTML = `<button class="sec" onclick="go('events')">← Back</button><h2>${esc(S.event.title)}</h2>
    <div class="stage">STAGE</div><div class="grid">${Object.values(rows).map(r => `<div class="srow">${r.map(s =>
      `<button class="seat ${s.status} ${S.sel.has(s.label) ? 'sel' : ''}" ${s.status !== 'AVAILABLE' ? 'disabled' : ''} onclick="toggle('${s.label}')">${s.label}</button>`).join('')}</div>`).join('')}</div>
    <p class="mut" style="text-align:center">Yellow = held by someone paying · Grey = booked · Max 6 seats</p>
    <div class="card row"><div><b>${[...S.sel].join(', ') || 'No seats selected'}</b><div class="mut">Total ${rupee(S.event.price * S.sel.size)}</div></div>
    <button id="paybtn" ${S.sel.size ? '' : 'disabled'} onclick="startBooking()">Pay &amp; book</button></div>`;
}
function toggle(l) { S.sel.has(l) ? S.sel.delete(l) : (S.sel.size < 6 && S.sel.add(l)); drawSeats(); }

async function startBooking() {
  $('#paybtn').disabled = true;
  try { pay(await api('/bookings', 'POST', { event_id: S.event.id, seats: [...S.sel] })); }
  catch (e) { toast(e.message, true); seatsView(S.event.id); }
}

// ---- checkout (real Razorpay or built-in mock) ----
function loadRzp() {
  return new Promise((ok, no) => {
    if (window.Razorpay) return ok();
    const s = document.createElement('script'); s.src = 'https://checkout.razorpay.com/v1/checkout.js'; s.onload = ok; s.onerror = () => no(new Error('Could not load Razorpay')); document.head.appendChild(s);
  });
}
async function verifyAndFinish(payload) {
  try { await api('/payments/verify', 'POST', payload); toast('Payment verified — booking confirmed!'); }
  catch (e) { toast(e.message, true); }
  go('profile');
}
async function reportFailure(orderId, paymentId, reason) {
  await api('/payments/failed', 'POST', { order_id: orderId, payment_id: paymentId, reason }).catch(() => { });
  toast('Payment failed — seats released. Retry from My Bookings.', true); go('profile');
}
async function cancelPending(bookingId) {
  await api(`/bookings/${bookingId}/cancel`, 'POST').catch(() => { });
  toast('Payment cancelled — seats released', true); go('profile');
}
async function pay({ booking, payment: p }) {
  if (!p) return go('profile');
  if (p.mock) return mockCheckout(booking, p);
  try { await loadRzp(); } catch (e) { return toast(e.message, true); }
  let settled = false;
  const rz = new Razorpay({
    key: p.key_id, amount: p.amount, currency: p.currency, order_id: p.order_id, name: 'TicketHub',
    description: `${booking.event.title} (${booking.seats.join(', ')})`, prefill: { name: S.user.name, email: S.user.email },
    handler: r => { settled = true; verifyAndFinish(r); },
    modal: { ondismiss: () => { if (!settled) { settled = true; cancelPending(booking.id); } } }
  });
  rz.on('payment.failed', r => { if (settled) return; settled = true; rz.close(); reportFailure(p.order_id, r.error?.metadata?.payment_id, r.error?.description); });
  rz.open();
}
function mockCheckout(booking, p) {
  $('#mbox').innerHTML = `<h3>Mock Razorpay checkout</h3><p class="mut">${esc(booking.event.title)} · ${esc(booking.seats.join(', '))}</p>
    <h2>${rupee(p.amount)}</h2><p class="mut">Test mode: no real gateway is used.</p>
    <div style="display:grid;gap:8px"><button class="ok" id="m1">Simulate SUCCESS</button><button class="bad" id="m2">Simulate FAILURE</button><button class="sec" id="m3">Close (cancel)</button></div>`;
  $('#modal').style.display = 'flex';
  const done = () => $('#modal').style.display = 'none';
  $('#m1').onclick = async () => { done(); const t = await api('/dev/mock-pay', 'POST', { order_id: p.order_id }); verifyAndFinish(t); };
  $('#m2').onclick = () => { done(); reportFailure(p.order_id, null, 'Card declined (simulated)'); };
  $('#m3').onclick = () => { done(); cancelPending(booking.id); };
}

// ---- profile ----
async function profileView() {
  if (!S.user) return authView();
  const h = await api('/profile/history');
  $('#app').innerHTML = `<h2>${esc(h.user.name)}'s profile</h2><p class="mut">${esc(h.user.email)} · ${h.summary.confirmed} confirmed booking(s) · ${rupee(h.summary.total_paid)} paid</p>` +
    (h.bookings.map(b => `<div class="card"><div class="row"><div><b>${esc(b.event.title)}</b> <span class="badge ${b.status}">${b.status}</span>
      <div class="mut">Booking #${b.id} · Seats ${esc(b.seats.join(', '))} · ${rupee(b.amount)} · ${when(b.created_at)}</div></div>
      <div>${['FAILED', 'CANCELLED', 'EXPIRED'].includes(b.status) ? `<button onclick="retry(${b.id})">Retry payment</button>` : ''}
      ${b.status === 'PENDING' ? `<button onclick="retry(${b.id})">Pay now</button> <button class="sec" onclick="cancelPending(${b.id})">Cancel</button>` : ''}</div></div>
      <table><tr><th>#</th><th>Order ID</th><th>Transaction ID</th><th>Status</th><th>Amount</th><th>Time</th></tr>${b.payments.map(p => `
      <tr><td>${p.attempt}</td><td><code>${esc(p.order_id)}</code></td><td><code>${esc(p.transaction_id || '-')}</code></td>
      <td><span class="badge ${p.status}">${p.status}</span>${p.reason ? `<div class="mut">${esc(p.reason)}</div>` : ''}${p.note ? `<div class="mut">${esc(p.note)}</div>` : ''}</td>
      <td>${rupee(p.amount)}</td><td>${when(p.paid_at || p.created_at)}</td></tr>`).join('')}</table></div>`).join('') || '<div class="card">No bookings yet.</div>');
}
async function retry(id) { try { pay(await api(`/bookings/${id}/retry`, 'POST')); } catch (e) { toast(e.message, true); profileView(); } }

// ---- auth ----
function authView(mode = 'login') {
  const reg = mode === 'register';
  $('#app').innerHTML = `<div class="card" style="max-width:380px;margin:30px auto"><h3>${reg ? 'Create account' : 'Login'}</h3>
    ${reg ? '<input id="n" placeholder="Name">' : ''}<input id="e" placeholder="Email" type="email"><input id="p" placeholder="Password (6+ chars)" type="password">
    <button onclick="submitAuth('${mode}')" style="width:100%">${reg ? 'Register' : 'Login'}</button>
    <p class="mut" style="text-align:center"><a style="cursor:pointer;color:var(--pri)" onclick="authView('${reg ? 'login' : 'register'}')">${reg ? 'Have an account? Login' : 'New here? Register'}</a></p></div>`;
}
async function submitAuth(mode) {
  try {
    const body = { email: $('#e').value, password: $('#p').value }; if (mode === 'register') body.name = $('#n').value;
    S.user = (await api('/auth/' + mode, 'POST', body)).user; go('events');
  } catch (e) { toast(e.message, true); }
}
async function logout() { await api('/auth/logout', 'POST'); S.user = null; go('events'); }

(async () => { S.cfg = await api('/config'); S.user = (await api('/me')).user; go('events'); })();
