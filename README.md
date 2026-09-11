# telehealth-billing-service

Payments, invoices and patient notifications for a telehealth platform, with idempotent
Stripe webhook processing. Django 5, DRF, Celery, PostgreSQL 16, Redis 7.

This is the **billing half** of a two-service system. The other half is
**[telehealth-clinical-api](https://github.com/wasdevv/telehealth-clinical-api)** — a
Rails GraphQL service that owns appointments, patients and medical records, with its own
database. It calls the internal contract documented below; neither service touches the
other's tables.

---

## Architecture

```
   Stripe                                        telehealth-clinical-api (Rails)
     │                                                        │
     │ POST /api/v1/webhooks/stripe/                          │ Authorization: Token <shared>
     │ Stripe-Signature: t=…,v1=…                             │ Idempotency-Key: sha256(external_ref)
     │                                                        │
     ▼                                                        ▼
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │  telehealth-billing-service        Django 5 + DRF              THIS REPO         │
 │                                                                                  │
 │  POST /api/v1/invoices/            create or recognise an invoice   201 | 200    │
 │  POST /api/v1/invoices/void/       void by external_ref             200 | 404    │
 │  POST /api/v1/notifications/reminder/   queue email + SMS                202     │
 │  GET  /api/v1/health/              liveness, unauthenticated             200     │
 │  POST /api/v1/webhooks/stripe/     signature → record → queue            200     │
 │  /api/v1/auth/…                    Knox login, 2FA, Google                       │
 │  /api/docs/  /api/redoc/  /api/schema/   generated OpenAPI                       │
 │                                                                                  │
 │   ① verify signature  ②  record even unhandled types                             │
 │   ③ unique (provider, event_id)  ④ on_commit → Celery, answer 200 now            │
 └──────┬──────────────────────────────────────────────┬────────────────────────────┘
        │                                              │
        ▼                                              ▼
 ┌────────────────────┐                       ┌──────────────────────────┐
 │ PostgreSQL 16      │                       │ Redis 7                  │
 │                    │                       │                          │
 │ accounts_user      │                       │  Celery broker + results │
 │ billing_invoice    │ external_ref  UNIQUE   │                          │
 │ billing_payment    │ charge_id     UNIQUE   │  worker: process events  │
 │ billing_webhook_…  │ (provider,    UNIQUE   │  beat:   3 recurring     │
 │ billing_internal_… │  event_id)             │          safety nets     │
 └────────────────────┘                       └──────────────────────────┘
        the billing database — separate from the clinical one on purpose
                                              │
                                              ▼
                                   Twilio SMS  +  Mailgun email
```

---

## Setup

### Requirements

[uv](https://docs.astral.sh/uv/) and Docker. uv installs Python 3.12 itself; PostgreSQL 16
and Redis 7 come from `docker-compose.dev.yml`.

### Quick start

```bash
uv sync
cp .env.example .env

docker compose -f docker-compose.dev.yml up -d     # PostgreSQL 16 + Redis 7
uv run python manage.py migrate
uv run python manage.py createsuperuser            # optional, for /admin/

uv run python manage.py runserver                              # terminal 1
uv run celery -A telehealth_billing worker --loglevel=info     # terminal 2
uv run celery -A telehealth_billing beat  --loglevel=info      # terminal 3
```

Then open **http://localhost:8000/api/docs/** for the generated Swagger UI, or
**/api/redoc/** for Redoc.

**About the ports.** `docker-compose.dev.yml` publishes PostgreSQL on **55433** and Redis
on **56380**, not the usual 5432/6379 — those are usually taken, including by the sibling
`telehealth-clinical-api`, which uses 55432/56379. `POSTGRES_PORT` and `REDIS_PORT`
override them, and `.env.example` already matches the defaults.

Without a worker running, nothing processes webhooks. For a single-process try-out, set
`CELERY_TASK_ALWAYS_EAGER=true` in `.env` and tasks run inline.

### Tests and lint

```bash
uv run pytest                 # 87 tests, coverage gate at 85%
uv run ruff check .
uv run ruff format --check .
```

### The whole service in containers

```bash
cp .env.example .env          # fill in the secrets; compose refuses to start without them
docker compose -f docker-compose.standalone.yml up --build
```

Five containers with healthchecks: `billing`, `worker`, `beat`, `postgres:16`, `redis:7`.
Only `billing` migrates; `worker` and `beat` wait on its healthcheck, so nothing races.

This stack runs `config.settings.production` but sets `DJANGO_SECURE_SSL_REDIRECT=false`,
because there is no TLS terminator in front of it and the clinical service calls over
plain HTTP on the internal network. With the redirect on, every one of those POSTs comes
back `301` and no invoice is ever created. Put a terminating proxy in front — one that
sets `X-Forwarded-Proto` — and set it back to `true`.

---

## The internal contract

Called by `telehealth-clinical-api`, authenticated with
`Authorization: Token <INTERNAL_SERVICE_TOKEN>`. Paths and payload keys are fixed by that
contract and must not change on either side.

```bash
export T=shared-with-the-clinical-service
export API=http://localhost:8000

# Create an invoice. 201 the first time, 200 for the same external_ref after that.
curl -sX POST $API/api/v1/invoices/ \
  -H "Authorization: Token $T" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8' \
  -d '{"external_ref":"appointment:42","patient_email":"patient@example.com",
       "amount_cents":15000,"currency":"usd","description":"Telehealth consultation"}'

# Void it. 200 when voided or already void, 404 when there is nothing to void — which the
# clinical service treats as success, because the end state is the same either way.
curl -sX POST $API/api/v1/invoices/void/ \
  -H "Authorization: Token $T" -H 'Content-Type: application/json' \
  -d '{"external_ref":"appointment:42"}'

# Queue a reminder. 202: accepted, not delivered.
curl -sX POST $API/api/v1/notifications/reminder/ \
  -H "Authorization: Token $T" -H 'Content-Type: application/json' \
  -d '{"email":"patient@example.com","phone":"","starts_at":"2026-09-23T10:00:00Z",
       "doctor":"Dr. Ana Reyes"}'

# Health. No authentication — a load balancer has no token.
curl -s $API/api/v1/health/
```

A paid invoice is **not** voided by a cancellation. Money already moved; refunding it is a
different decision with different accounting, and not one another service gets to make
implicitly. The endpoint answers 200 with the invoice unchanged.

### Authentication API

```bash
# Log in. If the account has 2FA on, `otp_code` is required in this same request —
# there is no second endpoint where the factor could be skipped.
curl -sX POST $API/api/v1/auth/login/ -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…","otp_code":"123456"}'
# => {"expiry":"…","token":"…","user":{…}}

curl -sX POST $API/api/v1/auth/2fa/setup/  -H "Authorization: Bearer $TOKEN"
# => {"otpauth_uri":"otpauth://totp/…","qr_code_png_base64":"iVBORw0KGgo…"}

curl -sX POST $API/api/v1/auth/2fa/enable/ -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"code":"123456"}'
# => {"otp_enabled":true,"recovery_codes":[…]}   ← shown exactly once

curl -sX POST $API/api/v1/auth/logout/    -H "Authorization: Bearer $TOKEN"
curl -sX POST $API/api/v1/auth/logoutall/ -H "Authorization: Bearer $TOKEN"
```

### Environment variables

| Variable | Purpose |
|---|---|
| `DJANGO_SECRET_KEY` | Signing. Production refuses to boot without it. |
| `DJANGO_DEBUG` | `false` everywhere but a laptop |
| `DJANGO_ALLOWED_HOSTS` | Required in production |
| `DJANGO_SECURE_SSL_REDIRECT` | `true` behind a TLS terminator, `false` on a plain internal network |
| `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | Database |
| `REDIS_URL` | Celery broker, results and cache |
| `INTERNAL_SERVICE_TOKEN` | Shared with telehealth-clinical-api — the same value on both sides |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Payments and webhook verification |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | SMS; absent means "log instead of send" |
| `MAILGUN_API_KEY` / `MAILGUN_DOMAIN` / `MAILGUN_FROM` | Email; same |
| `GOOGLE_OAUTH_CLIENT_ID` | The audience a Google ID token must be issued for |

---

## The webhook

`POST /api/v1/webhooks/stripe/` does four things in this order, and the order is the design.

**1. Verify the signature before anything else.** Nothing is read out of the body, logged
or written to the database until `stripe.Webhook.construct_event` passes. Without it the
endpoint means "anyone on the internet can declare an invoice paid" — the payload is just
JSON and it claims whatever it likes. The same call rejects stale timestamps, which is what
stops a genuine past event being captured and replayed later.

**2. Record even the event types we do not handle.** An unhandled type is still evidence.
Dropping it silently makes "Stripe has been sending us `charge.dispute.created` for a
month and nobody noticed" a question nobody can answer. Those are stored as `ignored` and
never queued.

**3. Uniqueness in the database, not in an `if`.** `(provider, event_id)` is a unique
constraint, so a redelivery — Stripe retries for days after a non-2xx — loses the INSERT
and becomes a no-op. An `exists()` check in Python would read a snapshot taken before the
competing transaction committed, which is precisely the race that refunds a patient twice.

**4. Commit, then queue, then answer 200.** The task hand-off is inside
`transaction.on_commit`. A worker is a different process against the same database: queue
inside the transaction and it can pick the task up before the INSERT is visible — the task
then fails on a row that is about to exist — or, if the transaction rolls back, it runs for
an event that never happened. And the 200 goes out before any processing, because Stripe
times out at 20 seconds and retries on anything that is not a 2xx: doing the work inline
turns one slow refund into a redelivery storm.

`billing/tests/test_webhook_commit_ordering.py` is the only file in the suite that can tell
`delay(...)` from `on_commit(lambda: delay(...))`. Everything else passes either way.

### Idempotent handlers

One handler per type: `payment_intent.succeeded`, `payment_intent.payment_failed`,
`charge.refunded`, `charge.dispute.created`. Each is idempotent *on its own*, which is a
different guarantee from event de-duplication: the unique constraint stops the same
**event** being processed twice; the handlers stop the same **outcome** being applied twice
when two different events describe it — a redelivered charge under a new event id, a
reconciliation sweep, a manual replay of the dead-letter queue.

Concretely:

- a duplicate `Payment` is impossible because `provider_charge_id` is unique, and the
  `IntegrityError` is caught rather than prevented by a lookup;
- refunds store the provider's **absolute** refunded total, never a running sum of our own
  — adding on every delivery is exactly what over-refunds;
- a `payment_failed` arriving after a success cannot un-pay an invoice, because Stripe can
  deliver out of order;
- a dispute records the *first* timestamp, not the latest;
- every invoice update takes `select_for_update`, so two events for one invoice cannot
  read the same row and overwrite each other with stale data.

### The recurring safety nets

| Task | Schedule (UTC) | Why |
|---|---|---|
| `retry_failed_webhook_events` | every 10 min | Celery's own retries run out. Without a dead-letter sweeper, an event that failed while a dependency was down stays failed forever — silently, which is the worst way for a billing system to be wrong. |
| `reconcile_with_stripe` | 03:20 daily | Webhooks get lost: an endpoint down for an hour, retries exhausted, a deploy dropping a request mid-flight. Each one leaves a patient charged by Stripe and shown as unpaid by us. |
| `expire_stale_invoices` | 03:50 daily | So `open` keeps meaning something. Only untouched invoices; anything with a payment attempt is a person's problem, not a sweep target. |

---

## Security notes

- **Service-to-service auth is compared in constant time.** `IsInternalService` uses
  `hmac.compare_digest`, not `==`. Python's `==` on strings returns as soon as it finds a
  differing byte, so how long it takes leaks how many leading characters were right; an
  attacker who can time responses recovers the token one character at a time — a few
  thousand requests instead of brute-forcing a 256-bit space. An unset token closes the
  door rather than opening it.
- **The webhook trusts a signature, not a network.** No IP allowlist, no shared secret in
  a query string: `construct_event` or nothing.
- **Recovery codes are SHA-256 digests**, shown once, consumed atomically under a row lock
  so two simultaneous requests cannot spend the same one.
- **Google cannot bypass a second factor.** OAuth proves one factor exactly like a password
  does; an account with 2FA enabled is sent back to the password flow.
- **Google identity is matched on `sub`, never on email.** The subject is stable; an email
  address is not, and matching on a mutable field is how accounts get merged by accident.
  The ID token's audience is checked, so a valid Google token minted for a different
  application cannot be replayed here.
- **Contact details are masked in logs** (`pa***om`), and no Stripe payload body is logged.
- **Production refuses to boot** without `DJANGO_SECRET_KEY`, `INTERNAL_SERVICE_TOKEN`,
  `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` or `DJANGO_ALLOWED_HOSTS`, and rejects the
  development key by name. `manage.py check --deploy` passes with zero warnings and CI runs
  it at `--fail-level WARNING`.
- **HSTS for a year, secure and HttpOnly cookies, `X-Frame-Options: DENY`,
  `nosniff`** — all in `config/settings/production.py`, and all asserted in
  `config/tests_production_settings.py`, because security settings in a file nobody imports
  are settings that do not exist.

---

## Design decisions

### Why respond 200 before processing

Stripe gives a webhook endpoint about 20 seconds and treats anything that is not a 2xx as
a failure worth retrying — for days, with backoff. Process inline and the endpoint's
latency becomes the sum of everything a handler touches: a database write, an invoice
update, possibly a notification. One slow dependency then turns a single event into a
redelivery storm, and every redelivery is another event to de-duplicate while the system is
already under strain. Acknowledging first and working after inverts that: the provider sees
a fast, stable endpoint, and our own queue absorbs the slowness where it can be retried
with backoff and watched.

The cost is real and worth naming: a 200 now means "recorded", not "applied". Anything
reading an invoice in the seconds after a payment may see the old status. That is why the
event row is written *before* the 200 rather than after — the acknowledgement is a promise
we have already made durable.

### Why idempotency by unique constraint rather than a check in code

`if WebhookEvent.objects.filter(event_id=...).exists()` is the obvious version, and it is
wrong under concurrency. That SELECT reads a snapshot from before any competing transaction
committed, so two simultaneous redeliveries both see "no such event" and both proceed. The
window is small and the traffic that opens it — a provider retrying while the first
delivery is still in flight — is exactly the traffic this endpoint gets.

A unique constraint has no window. The second INSERT fails at the database, and catching
`IntegrityError` turns that into a plain 200. The same reasoning puts unique constraints on
`Invoice.external_ref` (the clinical service's retry cannot create a second invoice) and
`Payment.provider_charge_id` (reprocessing cannot create a second payment). Correctness
belongs where concurrency is actually arbitrated.

### Why reconcile daily when there is already a webhook

Because webhooks are a delivery mechanism, and every delivery mechanism loses messages. An
endpoint down for an hour, a retry budget exhausted, a deploy dropping a request
mid-flight, a signature secret rotated at the wrong moment — each leaves the same
end state: Stripe considers an invoice paid and we consider it open. The patient has been
charged and is shown as owing money.

No amount of care in the webhook removes that, because the failure is in getting the
message to us at all. The only fix is to ask the provider directly. `reconcile_with_stripe`
is the answer to "what if none of the above worked?", and the honest answer to that question
in a billing system is never "it can't happen". It corrects under `select_for_update`, logs
every correction at WARNING — a silent correction is a bug report nobody files — and is
itself idempotent, so running it twice changes nothing.

### Why a separate database from the Rails service

The clinical service owns PHI: diagnoses, notes, appointment histories. This one owns
payment state and talks to a PSP. Keeping the databases apart means a compromise on the
payment side does not hand over medical records, and the encryption keys, backup policy and
retention rules for PHI are not entangled with the ones for invoices. It also means either
service can be restored, migrated or redeployed without coordinating a schema change with
the other.

They integrate through an explicit HTTP contract with idempotency keys on both sides —
`Idempotency-Key` from the clinical service, `idempotency_key` onward to Stripe — which is
what makes the network's unreliability survivable. The cost is real: no join across the
boundary, and every call can time out. The clinical service is built for that, keeping the
appointment and retrying the invoice in the background.

### Why Knox rather than DRF's own TokenAuthentication

DRF's built-in token is a single, never-expiring row per user stored in plaintext. Knox
hashes the token, supports several per user (so logging out of a phone does not log out a
laptop), expires them, refreshes them on use, and gives `logoutall` for the "I lost the
device" case. For a service that authorises access to payment data, that is the difference
between a session and a permanent key.

### Why the second factor lives in the login serializer

It could live in a separate `/verify` endpoint with a challenge token. It does not, because
every extra endpoint is another path where credentials can validate and a token can be
issued — and the 2FA check has to be on *all* of them. Validating it in `LoginSerializer`
means the token is minted after the serializer passes, or not at all: there is no ordering
to get wrong. Google's callback is the one other way in, and it refuses accounts with a
second factor outright rather than reimplementing the check.

---

## CI/CD

`.github/workflows/ci.yml` — three jobs:

- **test**: PostgreSQL 16 and Redis 7 service containers, `astral-sh/setup-uv` with the
  cache keyed on `uv.lock`, `ruff check`, `ruff format --check`,
  `makemigrations --check --dry-run` (a model change without its migration is a deploy that
  fails at 3am), `check --deploy --fail-level WARNING`, then pytest with the 85% gate.
- **schema**: `spectacular --validate --fail-on-warn` and uploads `openapi.yaml` as an
  artifact. Generating it is also a check — it fails on any view drf-spectacular cannot
  describe.
- **image**: builds the production image with Buildx and the GitHub Actions cache.

`.github/workflows/mirror.yml` — mirrors `main` to GitLab with `git push --mirror`.
Configure before use:

- repository **secret** `GITLAB_TOKEN` — a GitLab PAT with `write_repository`
- repository **variable** `GITLAB_REPOSITORY` — e.g.
  `gitlab.com/your-namespace/telehealth-billing-service.git` (host and path only)

The job is skipped, not failed, until `GITLAB_REPOSITORY` is set, and the token is only
ever interpolated into the remote URL — never echoed.

---

## Repository checklist

The GitHub repository description should read exactly:

> Payment and notification service with idempotent Stripe webhook processing. Django 5, DRF, Celery.
