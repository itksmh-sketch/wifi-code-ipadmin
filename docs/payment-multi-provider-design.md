# Payment multi-provider — design & build reference

**Status:** BUILD COMPLETE, uncommitted (2026-09-08). Steps 1–6 applied and
live-verified; Step 7 folded into Step 6. Migrations `026` + `027` run on the
production VM. Flutterwave code is live but **unreachable** — `is_available=false`
in the catalog — pending the §9 sandbox round-trip gate.
**Started:** 2026-09-07
**Scope:** operator-side payment provider selection — let an operator choose their
bring-your-own payment provider (Paystack *or* Flutterwave) and enter credentials
through a generic, catalog-schema-driven UI. Ship a real second provider
(Flutterwave), not just scaffolding.

Out of scope for this batch: SMS provider selection (separate follow-up), the
platform-provided SMS gateway, per-operator SMS metering/billing.

**§11 has the as-built summary** — read it first; §§2–10 are the design record
with as-built deltas flagged inline.

---

## 1. Why this doc exists

This is a sizeable change touching the DB schema, the payment provider
abstraction, the webhook surface, and both the operator-admin frontend and the
provider catalog. It spans multiple sessions. This doc is the durable reference —
decisions, rationale, the build order, the Flutterwave integration surface, and
the go-live gate — so the context isn't scattered across chat history.

---

## 2. Starting state (as of commit `7746e08`)

- **One payment provider, hardcoded.** `PaymentService` maps every `PaymentMethod`
  to a single `PaystackProvider`; `provider_for_transaction` hardcodes the
  `provider == "paystack"` credential lookup and constructs `PaystackProvider`
  directly.
- **`operator_payment_credentials`** stores one row per operator
  (`UNIQUE(isp_operator_id)`, enforced *twice* — a named constraint and a unique
  index), with three dedicated encrypted columns
  (`public_key_encrypted`, `secret_key_encrypted`, `webhook_secret_encrypted`).
- **Webhook route** is `POST /api/v1/webhooks/paystack/{operator_slug}` only —
  Paystack-specific, filters creds on `provider == "paystack"`, builds
  `PaystackProvider`, enqueues `process_webhook_event("paystack", …)`.
- **Provider catalog** (`provider_catalog` table, migration 021,
  `src/modules/platform/provider_catalog.py`) already exists and already carries
  a per-provider `credential_schema` (`{configured_by, fields:[{name,label,type,
  required,secret}]}`). Paystack is `is_integrated=true, is_available=true`;
  Flutterwave is a stub (`is_integrated=false`, empty field list). The catalog is
  read only by the platform-owner API today (`GET /api/v1/platform/providers`) —
  no operator-facing endpoint.
- **Frontend** `frontend/src/pages/PaymentCredentials.jsx` — a single hardcoded
  Paystack form (`<select>` with one `<option>`, three fixed fields).
- **`PaymentProvider` ABC** (`src/modules/payments/providers/base.py`) is already
  the right seam: `initiate`, `verify`, `handle_webhook`, plus optional
  `submit_otp/phone/pin/birthday/address`. Only `PaystackProvider` implements it.

### Live production credential rows (verified 2026-09-07)

Two rows, both **test keys**, both `is_active=true`, different operators:

| operator | slug | provider | webhook secret | notes |
|---|---|---|---|---|
| `b71a905f…` | `aftown-net` | paystack | none | `pk_test_/sk_test_` |
| `e71db0ba…` | `tenant-zero` | paystack | `"phase1…"` | confirmed testing artifact on a test operator — carry forward verbatim |

Both decrypt cleanly and round-trip through the new JSON shape
(`json.loads(decrypt(encrypt(json.dumps(blob)))) == blob` — verified out of band).

---

## 3. Confirmed decisions

1. **Seams + Flutterwave**, not seams alone. A real second provider is the point.
2. **Storage:** migrate Paystack's three encrypted columns to a single encrypted
   `TEXT` column, `credentials_encrypted`, holding `Fernet(json.dumps({field_name:
   value}, sort_keys=True))`. "JSONB shape" = the *decrypted* dict; the column is
   opaque ciphertext (same discipline as `routers.nas_secret`). No dual storage.
3. **Flutterwave card flow:** hosted **Standard checkout** (`POST /v3/payments` →
   redirect link → `OPEN_URL`). No direct card charge, no 3DES encryption-key /
   PCI surface. Mobile money stays a direct charge.
4. **ABC change:** add `client_ip: Optional[str] = None` to `initiate()` —
   additive; Paystack ignores it, Flutterwave MoMo uses it.
   **(as built)** also `expected_amount_ghs: Optional[Decimal] = None` on
   `verify()` — see decision 11.
5. **Flutterwave webhook trust:** mandatory `verify()` (server-to-server) on
   *every* webhook before treating a transaction as paid — never trust
   `data.status` from the payload. Non-negotiable: Flutterwave's `verif-hash` is
   a static shared secret compared for equality, a weaker guarantee than
   Paystack's HMAC-SHA512. (Paystack keeps trusting its verified `charge.success`
   as today.) **(as built)** `FlutterwaveProvider.handle_webhook` returns
   `status=PENDING` unconditionally; `webhooks/processor.py` has an
   `if provider == "flutterwave"` branch that calls
   `refresh_transaction_status(force=True)` and returns — `apply_webhook_update`
   (the trust-the-status path) is never reached for Flutterwave.
6. **Old `PUT /payment-credentials` contract is dropped** and the React page
   rewritten in the same batch (step 6). Operator-scoped admin API, no external
   callers, no compatibility surface.
7. **`PaymentTransaction.provider` enum** gains `flutterwave` (additive
   `ALTER TYPE`). **(as built)** done in migration `026`.
8. Vestigial `hubtel` value in the `operator_payment_provider` enum is left
   alone (removing an enum value is disproportionate).
9. **Flutterwave go-live gate:** a genuine Flutterwave **sandbox round-trip — a
   real test transaction through the live API**, per network (MTN / Vodafone /
   AirtelTigo), resolving the §7 response-shape uncertainties — before
   `is_available=true` is flipped for Flutterwave on `/platform/providers`. Same
   standard as Phase 3's real-Paystack-transaction proof. Unit tests against
   recorded fixtures are necessary but not sufficient.
10. Flipping `is_available=true` is a deliberate **platform-admin action** on
    `/platform/providers` *after* deploy and *after* the sandbox proof — never
    part of a code deploy. (Matches how Paystack's own availability was set.)
11. **(as built) Amount cross-check — Option B.** `verify()` takes an additive
    `expected_amount_ghs: Optional[Decimal] = None`. Paystack accepts and ignores
    it. `FlutterwaveProvider.verify` downgrades a `successful` charge to `FAILED`
    when `charged < expected − GHS 0.01`, or when `currency != "GHS"`. Threaded
    from the two call sites (`refresh_transaction_status`,
    `payment_reconciliation`) as `tx.amount_ghs`. Rationale: the customer can't
    alter a server-set charge amount (unlike the Phase-3 platform-billing case),
    but a provider-side quirk or an unresolved §5.8 Ghana-MoMo response shape
    could still settle a mismatched amount — cheap additive guard, same standard
    as every other money-path change.
12. **(as built) Webhook route: two explicit routes, not a catch-all.** The
    original design was a generic `/{provider}/{operator_slug}` + a `/paystack/…`
    alias. A generic catch-all also matches `/api/v1/webhooks/platform-billing/
    paystack` (`provider="platform-billing"`) and, being registered first,
    shadows that handler. Resolved with one explicit route per supported
    provider — `/paystack/{operator_slug}` (path & decorator unchanged) and
    `/flutterwave/{operator_slug}` — both delegating to `_process_webhook`. An
    unknown provider path is a native Starlette 404. See §4.3.
13. **(as built) Webhook credential lookup keeps `is_active == True`.** The
    design floated dropping it (for a webhook arriving just after a provider
    switch). Kept it: it's the current behaviour so keeping it is provably
    identical, and no code path can deactivate a provider until Step 6 ships —
    which it now has. **Revisit resolved:** when provider-switching is actually
    used, key the webhook's cred lookup on the in-flight transaction's stamped
    `tx.provider` rather than the operator's currently-active provider. Noted in
    §4.3; not needed until an operator runs two providers.
14. **(as built) Step 7 folded into Step 6.** The generic `POST /{provider}/test`
    endpoint needs a method to call, so `verify_credentials()` landed on the ABC
    + `PaystackProvider` + `FlutterwaveProvider` in Step 6. Step 7 is empty.
15. **(as built) `pk_/sk_` prefix validation dropped.** It was Paystack-specific
    and brittle; Test Connection validates keys against the live API properly.

---

## 4. Target architecture

### 4.1 Credential storage

```
operator_payment_credentials
  id                     uuid  pk
  isp_operator_id        uuid  fk isp_operators(id) on delete cascade
  provider               enum  operator_payment_provider  (paystack | flutterwave | hubtel)
  credentials_encrypted  text  NOT NULL   Fernet(json.dumps({field: value}, sort_keys=True))
  is_active              bool  NOT NULL default true
  last_validated_at      timestamptz
  last_validation_error  text
  created_at / updated_at timestamptz

  UNIQUE (isp_operator_id, provider)                  -- one row per provider per operator
  UNIQUE (isp_operator_id) WHERE is_active            -- ≤1 active provider per operator (partial)
  INDEX  (isp_operator_id)                            -- non-unique lookup (replaces the old unique index)
  INDEX  (provider), INDEX (is_active)                -- unchanged
```

Decrypted blob is keyed by `provider_catalog.credential_schema.fields[].name`:
- Paystack: `{"public_key", "secret_key", "webhook_secret"?}`
- Flutterwave: `{"public_key", "secret_key", "webhook_secret"?}`

### 4.2 Provider resolution

```
resolve_active_payment_provider(db, operator_id) -> (provider_key, creds: dict)
    SELECT … WHERE isp_operator_id = :op AND is_active = true      -- partial unique ⇒ ≤1 row
    raise ValueError("Operator has not configured a payment provider") if none
    return row.provider, json.loads(decrypt_secret(row.credentials_encrypted))

build_payment_provider(provider_key, creds, *, callback_url) -> PaymentProvider   # the registry
    "paystack"    -> PaystackProvider(secret_key=…, public_key=…, webhook_secret=creds.get("webhook_secret"), callback_url=…)
    "flutterwave" -> FlutterwaveProvider(secret_key=…, public_key=…, webhook_secret=creds.get("webhook_secret"), callback_url=…)
    else          -> raise ValueError
```

Consumed in exactly three places: `PaymentService.provider_for_transaction`, the
webhook route, and Test Connection.

`PaymentService.__init__` loses the four `PaymentMethod → provider` maps,
`_provider_names`, and the mock MTN/Vodafone/AirtelTigo providers (files stay,
just unreferenced in `dependencies.py`). `create_pending_transaction` resolves
the operator's active provider and stamps `tx.provider` with the real key.
`payment_method` still selects *how* a provider charges (mobile money vs card).

### 4.3 Webhook surface — **as built (two explicit routes)**

```
POST /api/v1/webhooks/paystack/{operator_slug}       -- path & decorator UNCHANGED from before this build
POST /api/v1/webhooks/flutterwave/{operator_slug}    -- new
```

No catch-all (decision 12). Both routes are one-liners delegating to a shared
`_process_webhook(provider_key, operator_slug, request, db)`:

1. `enforce_rate_limit(client_ip, f"webhook:{provider_key}", 60/60s)`
2. resolve operator by slug + `status == "approved"` → **404** if none
3. load `OperatorPaymentCredential` for `(operator, provider_key)` **AND
   `is_active == True`** (decision 13) → **404** if none
4. `build_payment_provider(provider_key, load_credentials(creds), callback_url=None)`
5. `provider.handle_webhook(dict(request.headers), raw_body)` → `ValueError` → **401**
6. enqueue `process_webhook_event(provider_key, internal_reference, status.value,
   provider_reference, raw_body.decode())`

For `provider_key="paystack"` every step is byte-identical to the pre-build
dedicated route — including the `webhook_secret → secret_key` fallback, which
`PaystackProvider.__init__` does internally when `credentials.get("webhook_secret")`
is `None`. Verified live: §8.

`processor.process_webhook_event` branches on the first arg — `"flutterwave"` →
mandatory `refresh_transaction_status(force=True)`; `"paystack"` →
`apply_webhook_update` as before.

**Future (decision 13):** once an operator runs two providers, change step 3 to
look up the in-flight transaction by the payload reference and use its stamped
`tx.provider`, so a webhook that lands just after a provider switch still
resolves. Not needed until then.

**Paystack transparency** (verified — see §8): `/api/v1/webhooks/paystack/{slug}`
keeps its exact path and semantics, so the two operators with externally-
configured Paystack dashboard webhook URLs need zero action.

### 4.4 Operator-facing catalog endpoint

```
GET /api/v1/providers?category=payment        -- operator-JWT scoped (get_admin_tenant_context)
→ [ {provider_key, display_name, description, credential_schema, is_platform_provided} ]
  WHERE category = :category AND is_available = true   ORDER BY sort_order
```

Exposes only what the config UI needs — not `is_integrated`, not
`platform_rate_per_message`, not ids. New module `src/modules/catalog/`. Reused
for `category=sms` by the later SMS feature.

### 4.5 Generic credentials API — **as built**

```
GET    /payment-credentials
   -> { active_provider: "paystack" | null,
        configured: [ { provider, is_active,
                        field_hints: {field_name: "••••1234" | null},
                        last_validated_at, last_validation_error } ] }
PUT    /payment-credentials/{provider}           body { values: {field: str}, activate: bool | null }
POST   /payment-credentials/{provider}/activate
POST   /payment-credentials/{provider}/test
DELETE /payment-credentials/{provider}
```

- `PUT` validation, in order: provider must be `category='payment' AND
  is_available=true` in the catalog → **404**; `credential_schema.configured_by`
  must be `"operator"` (not `"platform_admin"`) → **400**; every `required` field
  present and non-empty → **400**; no key outside the schema → **400**. Then
  `dump_credentials({k: v for k in schema_fields})` and upsert on
  `(isp_operator_id, provider)`.
- `activate`: `true` → deactivate siblings then activate (one tx, `UPDATE … WHERE
  id != this` first so the partial unique index never conflicts); `false` →
  inactive; `null` → activate only if the operator has no active provider (or
  this row already is it).
- **Masking is uniform `••••{last4}` on every stored field** (as built — the
  design floated "full value for `secret:false`"; uniform matches the old
  endpoint and avoids echoing a full key). The `secret` flag drives the *input
  type* on the write form only.
- `mark_checklist(…, "payment_configured")` fires whenever a write leaves the
  operator with an active provider.
- `DELETE` on the active provider → **409**.
- `POST /{provider}/test` → `build_payment_provider(...).verify_credentials()`;
  success stamps `last_validated_at`, any exception stamps `last_validation_error`
  + **400** with the provider's message.
- `schemas.py`: `PaymentCredentialProvider` / `PaymentCredentialUpdate` /
  `PaymentCredentialResponse` removed; `PaymentCredentialUpsert`,
  `PaymentCredentialsView`, `ConfiguredProviderView` added.

### 4.6 Frontend — **as built**

`PaymentCredentials.jsx` rewritten: on load, `GET /providers?category=payment` +
`GET /payment-credentials`. One `<ProviderCard>` per available provider; the form
is built from `credential_schema.fields` (`secret` → `type=password`, `required`
→ `required`, stored value shown as a `stored: ••••1234` placeholder). Per-card
actions: Save / Make active / Test connection / Remove. `configured_by ===
"platform_admin"` → "managed by the platform" note, no form. `npm run build` →
`backend/static/admin/` (tracked build output); verified clean and the new bundle
is served at `/admin/`.

---

## 5. Flutterwave integration surface

Flutterwave **v3 API**, base `https://api.flutterwave.com/v3`, auth
`Authorization: Bearer <FLWSECK-…>`.

### 5.1 Amount units — differs from Paystack

Paystack uses **pesewas** (`amount × 100`). Flutterwave uses **major units**
(`GHS 5.00` → `"amount": 5`). Applies to charge *and* verify. Every amount
crossing the boundary is `Decimal` GHS as-is, `.quantize("0.01")`.

### 5.2 `initiate(amount_ghs, phone, plan_id, site_id, internal_reference, payment_method, client_ip=None)`

**Mobile money** (`payment_method ∈ {mtn_momo, vodafone_cash, airteltigo}`):
`POST /v3/charges?type=mobile_money_ghana`
```json
{ "tx_ref": "<internal_reference>", "amount": 5, "currency": "GHS",
  "network": "MTN" | "VODAFONE" | "AIRTELTIGO",
  "email": "<synthesized, as Paystack does>", "phone_number": "<0XX…>",
  "fullname": "Hotspot Customer", "client_ip": "<client_ip>",
  "redirect_url": "<callback_url>" }
```
Expected: HTTP 200, `{"status":"success","data":{"status":"pending","flw_ref":"…","id":…}}`,
possibly with `meta.authorization` (`mode: "redirect"` + URL, or `mode: "otp"`).
Store `data.flw_ref` as `tx.provider_reference` (needed for OTP validation).

Mapping:

| Flutterwave initiate result | our `next_action` |
|---|---|
| `data.status == "pending"`, no `meta.authorization` | `WAIT` (customer approves on phone / USSD) |
| `meta.authorization.mode == "redirect"` | `OPEN_URL` (`authorization_url` = the redirect URL) |
| `meta.authorization.mode == "otp"` | `ENTER_OTP` |
| `data.status == "failed"` | `FAILED` |

**Card:** hosted Standard checkout — `POST /v3/payments`
```json
{ "tx_ref": "<internal_reference>", "amount": 5, "currency": "GHS",
  "redirect_url": "<callback_url>", "customer": {"email": "<synthesized>"},
  "payment_options": "card", "meta": {"plan_id": "…", "site_id": "…"} }
```
→ `{"status":"success","data":{"link":"https://checkout.flutterwave.com/v3/hosted/pay/…"}}`
→ `PaymentNextAction.OPEN_URL`, `authorization_url = data.link`. Mirrors
Paystack's `transaction/initialize` + `authorization_url` path.

### 5.3 `verify(provider_reference, expected_amount_ghs=None)`

`GET /v3/transactions/verify_by_reference?tx_ref=<internal_reference>` — verifies
by *our* reference, so Flutterwave's numeric `id` never needs persisting.
`data.status`: **`"successful"`** (not `"success"`) | `"failed"` | `"pending"`.

| verify result | our `PaymentStatus` |
|---|---|
| `successful` + `currency == "GHS"` + (no expected, or `amount ≥ expected − 0.01`) | `SUCCESS` |
| `successful` + `currency != "GHS"` | `FAILED` (`unexpected currency …`) |
| `successful` + `amount < expected − 0.01` | `FAILED` (`underpaid: …`) |
| `failed` | `FAILED` |
| anything else / HTTP 400 "no transaction" | `PENDING` (not raised — tx may not have reached Flutterwave yet) |

`expected_amount_ghs` is threaded from `refresh_transaction_status` and
`payment_reconciliation` as `tx.amount_ghs` (decision 11).

### 5.4 `handle_webhook(headers, raw_body)`

Header **`verif-hash`** = the operator's configured webhook secret hash, compared
for **equality** (`hmac.compare_digest`, not an HMAC digest):
```python
expected = self.webhook_secret            # creds["webhook_secret"]
got = headers.get("verif-hash") or headers.get("Verif-Hash")
if not expected or not got or not hmac.compare_digest(got, expected):
    raise ValueError("Invalid flutterwave webhook signature")
```
Payload: `{"event":"charge.completed","data":{"status":"successful"|"failed",
"tx_ref":"<internal_reference>","id":…,"amount":…,"currency":"GHS", ...}}`.

**Per decision 5: the webhook handler returns `status=PENDING` regardless of
`data.status`, and the processing path performs a mandatory `verify()` before
settling.** The webhook is a *trigger to re-verify*, never a source of truth.
Return `PaymentWebhookResult(internal_reference=data.tx_ref, status=PENDING,
provider_reference=data.flw_ref or data.tx_ref, provider_state=data.status,
provider_payload=payload, payment_channel=data.payment_type)`.

Implementation note: the enqueued `process_webhook_event` for provider
`flutterwave` must call `service.refresh_transaction_status(force=True)` (which
calls `provider.verify`) rather than `apply_webhook_update` with a trusted
status. Paystack's path is unchanged.

### 5.5 Follow-ups

`submit_otp(reference, otp)` → `POST /v3/validate-charge`
`{"type":"mobile_money_ghana","flw_ref":"<flw_ref>","otp":"<otp>"}`. The other
`submit_*` methods → `NotImplementedError` (base default); Ghana MoMo doesn't use
them.

### 5.6 `verify_credentials()` (Test Connection) — **as built**

On the ABC (`base.py`) as an optional method (default `raise
NotImplementedError`); implemented by both real providers, each opening and
closing its own client:
- Paystack: `GET /transaction?perPage=1` with the secret key.
- Flutterwave: `GET /v3/transactions?page=1` with the secret key.
200 → returns; non-2xx → `raise ValueError(<provider message>)`.

### 5.7 Catalog entry (replace the stub)

```python
{ "category": "payment", "provider_key": "flutterwave",
  "display_name": "Flutterwave", "is_integrated": True, "is_available": False,
  "is_platform_provided": False, "sort_order": 20,
  "credential_schema": {"configured_by": "operator", "fields": [
      {"name":"public_key","label":"Public key (FLWPUBK-…)","type":"string","required":True,"secret":False},
      {"name":"secret_key","label":"Secret key (FLWSECK-…)","type":"string","required":True,"secret":True},
      {"name":"webhook_secret","label":"Webhook secret hash","type":"string","required":False,"secret":True}]}}
```
`is_integrated` flips to `True` with this build; `is_available` stays `False`
until the §9 gate is met. The catalog upsert (`sync_provider_catalog`) refreshes
`is_integrated` and `credential_schema` on deploy but never touches
`is_available`.

### 5.8 Known unknowns — resolved only by the §9 sandbox round-trip

- Exact Ghana MoMo charge response per network (MTN vs Vodafone vs AirtelTigo):
  `pending` + phone prompt vs `redirect` vs `otp`. The §5.2 mapping is built
  defensively (default `WAIT`); the real branch taken per network must be
  observed.
- Whether the test Flutterwave account is provisioned for GHS mobile-money
  collections (account-level; Test Connection won't catch it — a real charge
  will).
- `redirect_url` return parameters and whether the portal success page polls
  correctly after an `OPEN_URL` return (Paystack card relies on webhook + poll
  today; Flutterwave should behave the same).

---

## 6. Migrations

**Both run on the production VM (2026-09-08). DB at `027_flutterwave_catalog_schema`.**

- `026_operator_payment_credentials_jsonb` — storage shape + constraints +
  `payment_provider` enum `+ flutterwave`. (design below)
- `027_flutterwave_catalog_schema` — `sync_provider_catalog(op.get_bind())`, an
  idempotent upsert that gives the Flutterwave payment row its real
  `credential_schema` and `is_integrated=true`. Never touches `is_available`.
  `downgrade()` reverts that one row to the stub.

### `026_operator_payment_credentials_jsonb`

Precedent for in-migration Python + `src.utils.encryption`:
`016_reencrypt_nas_secret.py`.

**`upgrade()` — ordered, destructive step gated on verification:**

1. `ADD COLUMN credentials_encrypted TEXT` (nullable).
2. Data backfill (Python over `op.get_bind()`): for each row, build
   `{"public_key": decrypt(public_key_encrypted), "secret_key":
   decrypt(secret_key_encrypted)}`, add `"webhook_secret"` iff
   `webhook_secret_encrypted` is non-null, write
   `encrypt(json.dumps(blob, sort_keys=True, separators=(",",":")))`.
3. **Verification gate:** re-select every row, `decrypt` → `json.loads`, assert
   `public_key` and `secret_key` present and non-empty. Any failure ⇒ `raise`
   (transaction rolls back, old columns intact, nothing lost).
4. Past the gate only: `ALTER COLUMN credentials_encrypted SET NOT NULL`, then
   `DROP COLUMN public_key_encrypted, secret_key_encrypted,
   webhook_secret_encrypted`.
5. Constraint swap: drop `uq_operator_payment_credentials_operator` and the
   unique `ix_operator_payment_credentials_isp_operator_id`; create
   `UNIQUE (isp_operator_id, provider)`,
   `UNIQUE (isp_operator_id) WHERE is_active`, and a plain
   `INDEX (isp_operator_id)`.

Separately (same migration or a sibling): `ALTER TYPE payment_provider ADD VALUE
'flutterwave'`.

**`downgrade()`:** recreate the three columns nullable, Python loop decrypts the
JSON back into them, drop `credentials_encrypted`, restore the old unique
constraint + index. Reversible.

**Safety:** 2 rows, both proven to convert, destructive step is conditional on
the in-migration gate. Not a blind bulk rewrite. Run by the operator (itksmh) on
the production VM, not by the agent.

---

## 7. Build order — **all steps done**

Each step was independently shippable; the system stayed working (Paystack-only)
throughout. Reported before applying; verified live against the running system.

| # | Step | Status |
|---|---|---|
| 1 | Migration 026 + `payment_provider` enum `+flutterwave` + `models.py` | ✅ migration run on VM; data-gate passed for both live rows |
| 2 | `providers/registry.py` + `provider_resolver.py` + generalize `provider_for_transaction` + `dependencies.py` cleanup | ✅ Paystack via new path; 43 tests green |
| 3 | `FlutterwaveProvider` (initiate / verify / webhook / OTP) + `client_ip` + `expected_amount_ghs` on the ABC + 16 fixture tests + catalog stub → real schema (migration 027) | ✅ code live, unreachable |
| 4 | **Two explicit webhook routes** (decision 12) — `/paystack/{slug}` unchanged + new `/flutterwave/{slug}` + `_process_webhook` helper + processor already branches | ✅ probe sweep + no-shadow proof passed (§8) |
| 5 | `GET /api/v1/providers` operator catalog endpoint (`src/modules/catalog/`) | ✅ returns Paystack only; Flutterwave absent (verified) |
| 6 | Generic credentials API (5 endpoints) + React UI rewrite + **`verify_credentials()` on ABC + both providers (Step 7 folded, decision 14)** + prefix validation dropped (decision 15) | ✅ probe sweep + snapshot/restore of tenant-zero verified byte-for-byte |
| 7 | — folded into Step 6 — | ✅ |

**Remaining before Flutterwave goes live:** the §9 sandbox round-trip, then the
platform admin flips `is_available=true` for Flutterwave on `/platform/providers`.

---

## 8. Paystack webhook transparency + no-shadow proof — **verified (Step 4)**

**Claim:** the two operators (`aftown-net`, `tenant-zero`) with Paystack webhook
URLs in their own Paystack dashboards need **zero action** — `/api/v1/webhooks/
paystack/{operator_slug}` keeps its exact path and semantics.

**Probe sweep against the running system (2026-09-08):**

| probe | result |
|---|---|
| `paystack/aftown-net` — bad signature | **401** |
| `paystack/tenant-zero` — bad signature | **401** |
| `paystack/aftown-net` — **valid signature** (`secret_key` fallback), valid JSON + reference | **200**, worker got `process_webhook_event provider=paystack` |
| `paystack/tenant-zero` — **valid signature** (its own stored `webhook_secret`) | **200**, job enqueued |
| `paystack/aftown-net` — no-sig / empty / malformed-JSON / non-UTF8 / 5 KB junk / valid-sig+missing-reference | **401** each |
| `paystack/nonexistent-xyz` | **404** |
| path-traversal slug | **404** |
| `GET` instead of `POST` | **405** |

**No-shadow proof (the reason for decision 12):**

| probe | result | meaning |
|---|---|---|
| `POST /api/v1/webhooks/platform-billing/paystack` (no sig / bad sig) | **401 `{"detail":"Invalid webhook signature"}`** | the platform-billing handler's *own* response — not "Unknown operator" / "Unknown provider". Not shadowed. |
| `POST /api/v1/webhooks/mtn/aftown-net` | **404** | no route matches (native Starlette) — a catch-all would have hit `_process_webhook` |
| `GET /api/v1/webhooks/mtn/aftown-net` | **404** | no route |

`webhook_base_url = http://34.122.11.114`, so the URL our code ever gave
operators is `http://34.122.11.114/api/v1/webhooks/paystack/<slug>` — unchanged.

**Why transparent:** `/paystack/{operator_slug}` is not an alias — it is the
canonical Paystack route, path and decorator untouched; only its body now calls
`_process_webhook("paystack", …)`. Every step is byte-identical for Paystack,
including the `webhook_secret → secret_key` fallback (done inside
`PaystackProvider.__init__` when `credentials.get("webhook_secret")` is `None`).
The only internal change is creds coming from the `credentials_encrypted` blob
instead of three columns — same decrypted values (round-trip verified in §2).

---

## 9. Flutterwave go-live gate

Before `is_available=true` is set for Flutterwave on `/platform/providers`:

- [x] Steps 1–6 shipped and live-verified (Step 7 folded in). **2026-09-08.**
- [x] Unit tests: `FlutterwaveProvider` against recorded fixtures — initiate
      pending / redirect / failed, card checkout, verify successful / wrong-
      currency / underpaid / failed / pending / unknown-ref, webhook valid-hash-
      returns-PENDING / bad-hash / missing-tx_ref, amount floor, HTTP error, OTP
      2-step, registry build, `verify_credentials` ok/bad. **27 provider tests.**
- [ ] **Genuine Flutterwave sandbox round-trip — real test transactions through
      the live v3 API:**
  - [ ] MTN MoMo — full flow to a settled voucher; record the actual
        initiate/verify/webhook shapes.
  - [ ] Vodafone Cash — same.
  - [ ] AirtelTigo Money — same.
  - [ ] Card via hosted checkout — redirect out, return, poll to settled.
  - [ ] Confirm the §5.2 `next_action` mapping matches observed reality per
        network; adjust `FlutterwaveProvider` if a network behaves differently.
  - [ ] Confirm mandatory webhook re-verify path settles the transaction and the
        payload `data.status` is never trusted.
- [ ] `docs/` updated with the observed per-network response shapes (fills §5.8).

Same standard as Phase 3's real-Paystack-transaction proof — fixtures are not
enough.

---

## 10. Open items / notes for future sessions

- **Flutterwave `initiate` / `verify` / `submit_otp` / `handle_webhook` details
  in §5 are asserted from Flutterwave v3 knowledge, not from a doc in-repo.** The
  §9 sandbox round-trip is what confirms them. §5.8 lists the specific unknowns.
- The mock `MTNMoMoProvider` / `VodafoneCashMockProvider` / `AirtelTigoMockProvider`
  files are unreferenced by any resolver now (only their own unit tests call them
  directly). They still carry the ABC's new `client_ip` / `expected_amount_ghs`
  params for consistency. Delete in a later cleanup if still unused.
- `PaymentProviderName` enum in `types.py` is now only cosmetic for the
  non-Paystack values. Not touched by this build.
- **`tenant-zero`'s `operator_payment_credentials` row is test-suite-managed** —
  `test_multi_tenancy.py` (as `admin@isp.com`) rewrites it on every integration
  run. Not a pristine production row. `aftown-net`'s row is the real one and was
  never mutated by this build.
- SMS provider selection is the sibling follow-up. It reuses `GET /api/v1/
  providers?category=sms` (works today, returns `[]`) and the generic-form
  pattern, but needs a new per-operator SMS credential store and a per-operator
  SMS service resolver — the global `@lru_cache` singleton stays for platform
  notifications only. Bring-your-own only; the platform gateway stays hidden
  behind `is_available=false`.
- Hubtel / Africa's Talking were found `is_available=true` in this environment
  (pre-existing, not from this build) and **flipped back to `false`** via
  `PUT /api/v1/platform/providers/{id}` during Step 5, so `?category=sms` returns
  `[]`. Making an SMS provider available is a deliberate platform-admin action
  when the SMS feature ships.

---

## 11. As-built summary

**What an operator can do now:** open Payment Provider Settings, see one card per
available provider (Paystack only today), fill the schema-driven form, save,
activate, test the connection. Payments resolve through the operator's active
provider.

**Files — new:**
`src/modules/payments/provider_resolver.py`,
`src/modules/payments/providers/registry.py`,
`src/modules/payments/providers/flutterwave.py`,
`src/modules/catalog/{__init__,routes.py}`,
`src/db/migrations/versions/026_*`, `027_*`.

**Files — changed:** `db/models.py` (`OperatorPaymentCredential` shape +
`__table_args__`), `schemas.py`, `modules/payments/{service,dependencies,
credentials_routes}.py`, `modules/payments/providers/{base,paystack,mtn,vodafone,
airteltigo}.py`, `modules/webhooks/{routes,processor}.py`,
`jobs/payment_reconciliation.py`, `modules/platform/provider_catalog.py`,
`app.py`, `frontend/src/pages/PaymentCredentials.jsx`,
`backend/static/admin/*` (rebuilt bundle), 3 test files.

**Endpoints — net new:** `GET /api/v1/providers`, `PUT|DELETE
/api/v1/payment-credentials/{provider}`, `POST /api/v1/payment-credentials/
{provider}/{activate,test}`, `POST /api/v1/webhooks/flutterwave/{operator_slug}`.
**Removed:** `PUT /api/v1/payment-credentials`, `POST /api/v1/payment-credentials/
test` (the no-provider forms), `GET` reshaped.

**DB:** `operator_payment_credentials` — 3 encrypted columns → 1
`credentials_encrypted`; `UNIQUE(isp_operator_id)` → `UNIQUE(isp_operator_id,
provider)` + partial `UNIQUE(isp_operator_id) WHERE is_active`. `payment_provider`
enum `+ flutterwave`. `provider_catalog` flutterwave row: real schema,
`is_integrated=true`, `is_available=false`.

**Tests:** `test_payment_providers.py` 25 → 27 (Flutterwave + `verify_credentials`
suites); `test_multi_tenancy.py` payment tests rewritten to the new contract;
`test_payment_service.py` updated. Full payment/webhook/provider/catalog sweep:
46 pass. Pre-existing unrelated failures (unchanged by this build):
`test_billing.py::test_invoice_payment_uses_platform_billing_keys`,
`::test_platform_billing_webhook_marks_paid_and_reactivates`,
`test_multi_tenancy.py::test_sessions_are_isolated_by_operator_id`.

**Not done / next:** §9 Flutterwave sandbox round-trip, then flip
`is_available=true`. The SMS sibling feature. Optional: delete the unused mock
providers; `platformApiCall` cleanup (unrelated).
