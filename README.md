# CourtPro — booking platform for padel clubs (Django, multi-club SaaS)

One installation serves many padel clubs. Each club gets its own booking site
(`smash.yourplatform.pk` or its own domain), its own data, staff logins and settings, and pays you a
monthly subscription. Players book courts in under 30 seconds; owners run the day from a dashboard.

## The platform (multi-club)

- **Sales landing page** at the bare domain: live booking-sheet hero, the three money leaks and their fixes, a
  no-show cost calculator, pricing from `tenants/plans.py`, FAQ, a "demo club" link (`PLATFORM_DEMO_CLUB`) and a
  "Club login" finder that sends owners to their own club's login page. Fonts are self-hosted (`static/fonts`).
- **Self-service signup** at the bare domain: club name + subdomain + owner login → a live booking site with
  two starter courts, a 14-day Pro trial, and the owner logged straight into their dashboard
- **Full data isolation**: every club-owned row carries its club; queries are filtered to the club for
  the current web address automatically, and saving without an active club is refused
- **Subdomains or custom domains** per club (`book.smashpadel.pk`)
- **Plans** (`tenants/plans.py`): Starter (2 courts, no WhatsApp) and Pro (unlimited, WhatsApp) — edit prices/limits there
- **Subscription states**: trial → active (paid) → 7-day grace with a warning banner → expired (online booking
  paused, dashboard limited to the subscription page) or manually suspended
- **Platform admin** (`/platform/` on the bare domain, superusers): MRR, clubs in trial/overdue, bookings and money
  processed per club, one-click "+1 month paid", edit plan/domain/dates, suspend, and "Open" to jump into any
  club's dashboard
- **Roles** per club: *Owner* (everything) and *Staff/reception* (bookings, check-ins, payments — no prices,
  settings, exports, invoice voids or team management)
- Existing single-club databases migrate automatically: the data moves into a first club

## Features

**Public website**
- Modern, mobile-first landing page (courts, peak/off-peak prices, extras, contact, WhatsApp button)
- Live availability grid for every court; 60/90/120-minute bookings on a 30-minute grid
- Guest checkout (name + mobile) or optional account; extras (racket rental, balls, coach…) and promo codes
- Instant booking reference, email confirmation and a shareable printable invoice (Print → PDF)
- Self-service "Manage booking" (reference + phone) with cancellation-window policy

**Owner dashboard** (`/dashboard/`, staff only)
- Overview: cash collected today/month, bookings today, occupancy %, outstanding balance,
  14-day revenue chart, busiest hours, top regulars, pending approvals
- Schedule: courts × time grid; click any empty slot to add a walk-in/phone booking
- Bookings: search/filter, status flow (confirm → check in → complete / no-show / cancel), CSV export
- Invoices: auto-created per booking, manual invoices (coaching, events, memberships), partial payments
  (Cash, Card, Bank, JazzCash, Easypaisa), void, CSV export
- Customers: auto-built from bookings — games played, no-shows, total paid, one-click WhatsApp
- One-click "Send on WhatsApp" with booking details + invoice link (free click-to-chat, no API costs)
- Courts & prices, extras, promo codes, and all club settings editable without code

**Advance payments (stops no-shows)**
- Require a deposit (e.g. 50%) or full payment when players book online — Dashboard → Settings → Online payments
- The slot is held for 15 minutes (configurable) while the player pays; unpaid holds are released automatically
- JazzCash Hosted Checkout (wallet, card, voucher) with signed requests, signature-verified return + IPN,
  idempotent callbacks and amount-tamper checks; a demo gateway for development and sales demos
- Late payment after the hold expired: re-confirms if the slot is still free, otherwise flags a refund on the dashboard
- Shareable "Pay online" link for phone bookings and manual invoices; cash at the desk also confirms a hold

**Automatic WhatsApp messages (club's own number)**
- Booking confirmation, 24-hour and 2-hour reminders via the Meta WhatsApp Cloud API
- Uses the club's own WhatsApp Business number + token, so Meta bills the club directly
- Every message is logged per booking (sent / failed + reason); failures are retried; never sent twice
- Email confirmations and reminders go out too when the player gave an email

**Club credit & packages**
- Sell packages (e.g. *Gold: pay Rs 45,000, get Rs 50,000*) or custom top-ups; the money is invoiced when sold
- Reception pays any booking from the customer's credit in one click (partial if the balance is short)
- Cancelled paid bookings can be refunded straight to credit (owner only); every change is logged with who did it
- Credit spend is never counted twice: dashboard revenue and the cash report count money when credit is sold

**Weekly recurring bookings**
- "Repeat weekly for 4/8/12/26 weeks" on the new-booking form: same court and time every week
- Weeks that are already taken are skipped and listed; each week has its own invoice (pay weekly or from credit)
- One click cancels all remaining weeks

**End-of-day cash report**
- Takings per business day by payment method and by staff member; a late-night 1 AM payment counts for that evening
- Reception counts the drawer and closes the day; shortages/surpluses are recorded with a note
- Owners see every close and difference, can reopen a day, and get a dashboard warning if yesterday's cash wasn't counted

**Business rules handled**
- Opening hours that run past midnight (e.g. 8 AM – 2 AM); a 1 AM game stays on the right club day
- Peak pricing window (can cross midnight) + optional all-day weekend peak; slots that straddle peak are split-priced
- Double-booking protection: row lock on the court + overlap check inside a transaction
- Gap-free yearly invoice numbers (`INV-2026-00001`), tax applied after discount
- Booking window, minimum notice, cancellation cutoff, auto-confirm vs. owner approval
- Cancelling an unpaid booking voids its invoice and gives the promo use back

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # platform admin + 2 demo clubs: bookings, deposits, credit, a weekly group, cash closes
python manage.py runserver
```

| What | Address | Login |
|---|---|---|
| Platform landing + signup | http://localhost:8000 | — |
| Platform admin | http://localhost:8000/platform/ | admin@courtpro.test / admin12345 |
| Club 1 site + dashboard | http://smash.localhost:8000 (`/dashboard/`) | owner@smash.test / owner12345 · staff@smash.test / staff12345 |
| Club 2 (Starter plan) | http://baseline.localhost:8000 | owner@baseline.test / owner12345 |

Browsers resolve `*.localhost` to your machine automatically. Demo promo codes: `WELCOME10`, `MORNING500`.
Change every demo password before going live. Emails print to the console until SMTP is configured.

## Background jobs

Holds are released the instant they lapse (availability checks ignore expired holds), but a small job
tidies records, sends reminders and retries failed messages:

```bash
python manage.py run_scheduled_jobs              # run once — e.g. cron every 5 minutes
python manage.py run_scheduled_jobs --loop 120   # run forever (the `scheduler` service in docker-compose)
```

## Turning on JazzCash

1. Register at the JazzCash sandbox portal and get a Merchant ID, Password and Integrity Salt.
2. Dashboard → Settings → Online payments: choose *Deposit to confirm*, gateway *JazzCash*, paste the
   credentials, keep *sandbox mode* ticked.
3. In the JazzCash portal set the Return URL to `https://<your-domain>/payments/return/jazzcash/` and the
   IPN URL to `https://<your-domain>/payments/jazzcash/ipn/` (both shown on the settings page).
4. Make a test booking and pay with the sandbox test wallet/card. Check the field names in your JazzCash
   merchant integration guide while testing — the request/response signing is implemented to the published
   HMAC-SHA256 scheme, but it hasn't been run against a live sandbox account yet.
5. Untick sandbox mode when Jazz gives you live credentials.

The demo gateway only works when `PAYMENTS_DEMO_ENABLED` is true (default: same as `DEBUG`), so it can't
be used to fake payments in production.

## Turning on WhatsApp

1. In Meta Business Suite create a WhatsApp Business account for the club and add its number to the Cloud API.
2. In WhatsApp Manager create two **Utility** templates (language `en`) with exactly these variables:

   `booking_confirmed`
   ```
   Hi {{1}}, your padel court at {{2}} is booked ✅
   Ref: {{3}}
   Court: {{4}}
   When: {{5}}
   Manage your booking: {{6}}
   ```
   `booking_reminder`
   ```
   Hi {{1}}, reminder: you're playing padel at {{2}} {{3}} — {{4}}.
   Ref: {{5}}. Can't make it? Manage your booking: {{6}}
   ```
3. After approval, paste the **Phone number ID** and a **permanent (system user) access token** in
   Dashboard → Settings → WhatsApp, tick *Send automatic WhatsApp messages*, and use **Send test**.

Meta charges per conversation (utility rates are low in Pakistan) — the bill goes to the club's Meta account.

## Tests

```bash
python manage.py test        # 114 tests: club isolation, roles, deposits, JazzCash, reminders, credit, weekly bookings, cash report
```

## Deploy (Docker + PostgreSQL)

```bash
cp .env.example .env         # PLATFORM_DOMAIN, secret key, SMTP, your bank details for club payments
docker compose up -d --build
docker compose exec web python manage.py createsuperuser   # your platform admin login
```

DNS and HTTPS for many clubs:

1. Point `yourplatform.pk` **and** `*.yourplatform.pk` (wildcard A record) at the server.
2. Get a wildcard certificate for `*.yourplatform.pk` (Let's Encrypt DNS challenge, e.g. Caddy or certbot with your
   DNS provider's plugin) and serve the app behind Nginx/Caddy.
3. Custom club domains: the club adds a CNAME `book.theirclub.pk → yourplatform.pk`, you set it on the club in
   platform admin, and issue its certificate (Caddy "on-demand TLS" does this automatically).
4. `DJANGO_ALLOWED_HOSTS=.yourplatform.pk,yourplatform.pk` — add custom domains, or use `*` behind a proxy:
   unknown hosts are already rejected by the tenant middleware with a 404.

Use PostgreSQL in production — SQLite is fine for development only. Club sites use separate login cookies
per host, so a session on one club never carries over to another.

## Project layout

```
config/      settings & URLs (env-driven); urls_platform.py = bare-domain URLs
tenants/     Club, Membership, plans, per-request club (context.py), auto-scoped managers, host middleware,
             signup, magic login links, platform admin
accounts/    custom User (email login, phone), signup, "My bookings"
core/        Club settings (singleton), home page, template filters, seed_demo command
courts/      Court model (type, features, off-peak/peak prices, active flag)
bookings/    Booking, Extra, PromoCode, Notification; services.py = slots, pricing, holds, create/cancel/expire
             notifications.py + whatsapp.py = confirmations & reminders (WhatsApp Cloud API + email)
billing/     Invoice, Payment, OnlinePayment, Wallet, CreditPackage, DayClose; services.py = numbering & payments
             wallets.py = club credit; reports.py = end-of-day cash report
             gateways.py = JazzCash + demo adapters; payments.py = checkout attempts & idempotent results
dashboard/   Owner dashboard views/forms
templates/   Public site, dashboard, invoice, emails
static/      site.css, dashboard.css
```

## Next ideas

- More gateways: Easypaisa / Safepay — add a class in `billing/gateways.py` (two methods: `checkout`, `parse_callback`).
- "Slot freed" alerts: WhatsApp a waitlist when a hold expires or a booking is cancelled.
- Memberships / prepaid packages, recurring weekly bookings, multi-club (tenant) support.
