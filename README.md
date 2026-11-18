# Men-Ions backend

The backend for Men-Ions, a WhatsApp-first boda delivery service for small businesses in
Kampala. Merchants book deliveries, customers share a location pin, and riders accept and
complete jobs, all on WhatsApp. This service owns all the logic. WhatsApp access goes through
Kapso using Meta's standard Cloud API format, and the provider can be swapped with settings only.

It has the data model, the order state machine, plans and drop accounting, hash-chained
receipts, the webhook receiver, the outbound WhatsApp client, and the WhatsApp bots that run the
service (milestone 2).

## The bots

Who answers is decided by the sender's phone: an active merchant, a pending merchant, a rider,
the customer of an open order, or anyone else.

| Who | What they can do |
| --- | --- |
| Anyone new | Register a shop: name, owner, what they sell, daily volume, pickup pin and landmark. Then choose a plan |
| Pending merchant | Pay for the plan by Mobile Money with a reference, tap *I've paid*, wait for approval |
| Merchant | Menu (opens by itself on approval): book a delivery, my orders, cancel an order, my plan, buy or renew, help. The customer is picked from recent customers, shared as a contact card (📎 → Contact), or typed |
| Customer | Shares the delivery location (that makes the order READY), adds directions, asks for status. Gets the 4-digit drop code and an arrival estimate (`app/eta.py`: straight-line distance, a road factor and boda speeds; a range, not live traffic) |
| Rider | Goes on and off shift, accepts job offers (first tap wins), sends a pickup photo, then a delivery photo and the customer's code; or reports a failed delivery |
| Admin (`ADMIN_PHONES`) | Commands starting with `/`: approve payments (with the EFRIS FDN and code), add riders, assign, reschedule, cancel, unlock, suspend. Send `/help` |

**We only deliver.** The bots never ask about, collect or pass on money for the goods: payment
is between the merchant and the customer. (The cash-on-delivery columns, cash handovers and payouts
from milestone 1 are still in the database and `app/receipts.py`, unused by the bots; receipts
show no cash lines when no cash was collected.)

**Reaching the customer.** A customer has usually never written to us, so WhatsApp only allows
an approved template. After booking, the merchant gets a click-to-chat link
(`https://wa.me/<WHATSAPP_DISPLAY_NUMBER>?text=MNI-0001`) to send the customer; when the
customer sends it, the 24-hour window opens and the bot asks for their location. If the
number isn't set, the merchant is told to have the customer send the order code.

**The 24-hour window.** Every message is queued while a webhook is handled and sent after the
commit (`app/messaging.py`). Outside the window it goes through `WHATSAPP_UPDATE_TEMPLATE` if
set; otherwise it is skipped and an `admin_alerts` row says what wasn't sent (courtesy notices
are skipped quietly).

**Testing with one phone.** The Kapso sandbox talks to one registered phone. Put that phone in
`ADMIN_PHONES` and use `/as merchant`, `/as rider`, `/as customer`, `/as unknown` and `/as auto`
to choose which bot answers your normal messages. `/as` is refused when `ENV=production`.
A full run on one phone:

1. Send `hi` and register a shop, choose a plan, tap *I've paid*.
2. `/pending`, then `/approve <REF> TEST-FDN TEST-CODE`.
3. `/rider add <your number> Your Name`, then `/as rider` and tap *Go on shift*.
4. `/as merchant`, `menu`, book a delivery with your own number as the customer.
5. `/as customer`, send anything, share a location. You get the drop code; the rider bot gets a job offer.
6. `/as rider`: accept, *Picked up*, photo, *Delivered*, photo, type the code.

## Setup

You need Docker with the Compose plugin.

```bash
cp .env.example .env                                # the defaults work for local development
docker compose up -d --build                        # starts db and api (http://localhost:8000)
docker compose run --rm api alembic upgrade head    # creates all tables
curl localhost:8000/healthz                         # {"status":"ok","database":"ok"}
```

If port 5432 or 8000 is taken on your machine, set `DB_PORT` or `API_PORT` in your shell (or in
`.env`) before `docker compose up`.

## Tests

```bash
docker compose run --rm api pytest
docker compose run --rm api ruff check .
```

Tests run against a real Postgres. They create a separate database (`menions_test`), build it
from the migration at the start of every run, and roll each test back. They always use the fake
WhatsApp provider, so no test can send a message.

## Environment variables

Configuration comes only from the environment. `.env.example` lists everything; `.env` is
git-ignored.

| Variable | What it is |
| --- | --- |
| `ENV` | `development`, `test` or `production` |
| `DATABASE_URL` | PostgreSQL URL, `postgresql+psycopg://user:pass@host:5432/db` |
| `PUBLIC_BASE_URL` | Public address of this service, used in receipt links. On the sandbox, the tunnel address |
| `WHATSAPP_PROVIDER` | `fake` (default, sends nothing), `kapso` or `meta` |
| `WHATSAPP_API_BASE` | Override the provider's base URL. Empty uses the default for the provider |
| `WHATSAPP_PHONE_NUMBER_ID` | The WhatsApp phone number id to send from |
| `KAPSO_API_KEY` | Sent as `X-API-Key` when the provider is `kapso` |
| `META_ACCESS_TOKEN` | Sent as `Authorization: Bearer ...` when the provider is `meta` |
| `WHATSAPP_WEBHOOK_SIGNATURE` | `none` (development only), `kapso` or `meta` |
| `KAPSO_WEBHOOK_SECRET` | Secret for `kapso` signatures |
| `META_APP_SECRET` | Secret for `meta` signatures |
| `WHATSAPP_VERIFY_TOKEN` | The token for Meta's webhook verification handshake |
| `COMPANY_NAME`, `COMPANY_TIN`, `COMPANY_ADDRESS`, `COMPANY_PHONE` | Printed on every receipt |
| `ADMIN_PHONES` | Comma-separated phones that can use `/` commands and get alerts on WhatsApp |
| `WHATSAPP_DISPLAY_NUMBER` | The business number as digits (`2567...`), for click-to-chat links |
| `MOMO_PAY_NUMBER`, `MOMO_PAY_NAME` | Where merchants pay for plans |
| `WHATSAPP_UPDATE_TEMPLATE`, `WHATSAPP_TEMPLATE_LANGUAGE` | Approved template with one body parameter, for messages outside the 24-hour window |
| `MEDIA_DIR` | Where pickup and delivery photos are saved (default `var/media`) |

The app refuses to start when:

- `ENV=production` and `WHATSAPP_WEBHOOK_SIGNATURE=none`;
- a provider or signature mode is chosen without its secret, or without a phone number id.

## Switching WhatsApp provider

Only settings change. No code does.

| | Kapso | Meta directly |
| --- | --- | --- |
| `WHATSAPP_PROVIDER` | `kapso` | `meta` |
| Base URL (default) | `https://api.kapso.ai/meta/whatsapp/v24.0` | `https://graph.facebook.com/v24.0` |
| Credential | `KAPSO_API_KEY` | `META_ACCESS_TOKEN` |
| `WHATSAPP_WEBHOOK_SIGNATURE` | `kapso` + `KAPSO_WEBHOOK_SECRET` | `meta` + `META_APP_SECRET` |

Any other Cloud API-compatible provider (360dialog, for example) works by setting
`WHATSAPP_API_BASE` to its base URL, provided it uses one of the two auth headers above. A
provider with a different auth header needs one more branch in `app/whatsapp/__init__.py`.

## Exposing the webhook locally

Kapso has to reach your machine over HTTPS. Use either tunnel:

```bash
cloudflared tunnel --url http://localhost:8000     # prints https://<random>.trycloudflare.com
# or
ngrok http 8000                                    # prints https://<random>.ngrok-free.app
```

Then in the Kapso dashboard:

1. Set the webhook URL to `https://<tunnel-host>/webhooks/whatsapp`.
2. Choose the payload format. On a real number use the **Meta format**. The Kapso **sandbox
   refuses Meta-format webhooks**, so there use **Kapso's own format, payload v2**, with the
   `whatsapp.message.*` events. The backend reads both and tells them apart by shape.
3. Copy the webhook secret into `KAPSO_WEBHOOK_SECRET`, set `WHATSAPP_WEBHOOK_SIGNATURE=kapso`,
   and restart: `docker compose up -d`.
4. If the dashboard asks for a verify token, use the value of `WHATSAPP_VERIFY_TOKEN`.

Send a WhatsApp message to the number, then check it arrived:

```bash
docker compose exec db psql -U menions -d menions \
  -c "select id, provider_message_id, kind, processed, error from webhook_events order by id desc limit 5"
```

A 401 in `docker compose logs api` means the signature didn't verify; see the first item below.

## Unconfirmed Kapso details: `TODO(kapso-docs)`

Each is marked in the code and isolated to one place.

| Where | What to confirm |
| --- | --- |
| `app/whatsapp/signatures.py` | Which signature header and algorithm Kapso uses on **Meta-format forwarded** webhooks. For Kapso's own format, `X-Webhook-Signature` (hex HMAC-SHA256 of the raw body) is **confirmed** on the sandbox. If Meta-format forwarding relays Meta's `X-Hub-Signature-256` instead, set `WHATSAPP_WEBHOOK_SIGNATURE=meta` |
| `app/config.py` | The API version in the base URL (`v24.0`). Kapso's SDK docs also show `https://app.kapso.ai/api/meta/` as a base; set `WHATSAPP_API_BASE` if the default is wrong |
| `app/cloud_api_adapter.py` | That Meta-format forwarding delivers Meta's payload unwrapped. The fixtures in `tests/fixtures/` follow Meta's documented examples; replace them with payloads captured from the Kapso sandbox |
| `app/whatsapp/cloud_api.py` | That media download through the proxy works like Meta's: `GET /{media_id}` returns a URL, fetched with the same auth header |

Also open: `TODO(efris-api)` in `app/plans.py`. The dispatcher issues the EFRIS receipt on URA's
portal and types the FDN and verification code in when approving a plan purchase.

## How the pieces fit

| File | Job |
| --- | --- |
| `app/orders.py` | `create_order()` and `transition()`: the only ways to set an order's status |
| `app/plans.py` | Plan catalogue, drop accounting, plan purchase requests |
| `app/receipts.py` | `issue_receipt()`, the hash chain, cash handovers, payouts, the receipt page |
| `app/webhooks.py` | `POST /webhooks/whatsapp`: verify, store, de-duplicate, normalize, route |
| `app/cloud_api_adapter.py`, `app/events.py` | Cloud API payload to normalized events |
| `app/kapso_adapter.py` | Kapso's own webhook format (v2) to the same events. Inbound messages reuse the Cloud API message reader; `kapso.direction` decides message vs status, because Kapso marks inbound messages `status: delivered` |
| `app/router.py` | Phone to role to bot, admin commands, `/as`, and `in_free_window()` |
| `app/bots/` | The bots: `merchant.py` (onboarding, plans, booking), `customer.py`, `rider.py`, `admin.py`, and `effects.py`, which turns the state machine's side effects into messages |
| `app/messaging.py` | The outbox, sending after commit, the 24-hour window and templates, saving photos |
| `app/whatsapp/` | Provider protocol, the one Cloud API client, signature verifiers, the fake |

Three rules the bots follow:

1. **`transition()` never commits and never sends.** The caller commits, then performs the
   returned side effects (`notify_*`, `send_receipt`).
2. **After `DropCodeRejected`, commit; do not roll back.** The wrong attempt is recorded in the
   same transaction. Rolling back would erase the count and let a 4-digit code be guessed.
3. **Receipts are never edited.** A correction is `void_receipt()`, which issues a new receipt.

## Assumptions

Where the brief was ambiguous or silent, this is what was chosen.

**Additions to the brief's schema**

- **`admin_alerts` table.** The brief asks for `admin_alert` records for a failed message status
  and for a cash difference. Neither belongs to an order, so they can't live in `order_events`.
  All alerts go in this one table, including the drop-code lock.
- **`order_events.kind`.** `transition` rows are status changes. `drop_code_wrong` and
  `drop_code_reset` rows count wrong codes; they are not status changes.
- **`webhook_events.kind`** (`message`, `status`, `unknown`).
- **Admin identities are text** (`approved_by`, `paid_by`, `dispatcher_id`, admin `actor_id`).
  There is no admin table: admins are the phones in `ADMIN_PHONES`.
- **`order_events` is append-only by trigger**, like `receipts`.
- **`cod_payouts.momo_reference` is unique**, so one transfer can't be recorded twice.

**Receipts**

- **Receipt numbers do not come from a Postgres sequence.** A sequence leaves a gap whenever a
  transaction rolls back, and the brief requires no gaps. The next number is read from the last
  receipt while holding the same advisory lock that protects the hash chain. Order codes
  (`MNI-0001`) and MoMo references do use sequences; gaps there are harmless.
- The stored snapshot includes the company details, recipient, linked record and EFRIS numbers,
  so the hash covers all of them.
- A `void` receipt carries the same link as the receipt it voids. A receipt can be voided once;
  a void can't be voided.
- `amount_ugx` is empty on booking confirmations and failed-delivery notices, and on delivery
  receipts with no cash.
- Receipts are issued inside `transition()`, in the same transaction as the status change, so a
  delivery can't exist without its receipts. Sending them is a returned side effect.
- Rider pay has no table of its own: the receipt is the record.

**Orders**

- Only the listed actors may make each move. A customer sets `READY`; an admin can't do it for
  them yet.
- A rider can pick up, deliver or fail only an order assigned to them.
- The third wrong drop code locks the order. Even the right code is then refused until an admin
  calls `reset_drop_code_attempts()`.
- Cash collected is taken to be the order's `cod_ugx`; there is no separate "amount collected".
- A cancelled order's drop goes back only to the plan it came from. If that plan has ended or
  been replaced, the drop expired with it.
- Rescheduling a failed order clears its rider, so any rider can take it.
- Booking also requires `merchants.active` to be true.

**Plans**

- Every plan runs a full period from the moment it is approved, never to midnight: a daily plan
  for 24 hours (bought 18:30, it ends 18:30 the next day, so evening drops can be booked next
  morning), a weekly plan for 7 days, a monthly plan to the same time next month.
- "1 calendar month" means the same day and time next month, clamped to the month's last day
  (bought 31 January, ends 28 February).
- A plan starts when the admin approves the request, not when the merchant claims payment.
- Approving a request does not un-suspend a `SUSPENDED` merchant.

**Cash handovers**

- The dispatcher records the amount first, then the rider confirms. The dispatcher can re-record
  to fix a typo until the rider confirms. After that, corrections are made by voiding.
- Expected cash is fixed when the dispatcher records it. A delivery completed later that day is
  not added.

**Webhook**

- One `webhook_events` row is stored per message or status in a request, each holding the full
  request body. A status update is keyed as `<wamid>:<status>`, because `sent`, `delivered` and
  `read` share the message's id.
- A repeated message is not stored again.
- A sender who is not a Ugandan mobile number is kept as `+<digits>` and routed as `unknown`.
- A `SUSPENDED` merchant resolves to `unknown`, following the lookup order in the brief.
  Milestone 2 must handle this before `handle_unknown` becomes self-onboarding.
- `send_flow` takes an optional `body`. The Cloud API requires body text; it defaults to the
  button label.
- A timeout after a send request has left is not retried, because the message may have gone
  out. Failures to connect, 429 and 5xx are retried, up to three attempts.

**Libraries beyond the stack:** `psycopg` (the PostgreSQL driver SQLAlchemy needs) and `ruff`
(lint, development only).
