# Deferred provider launch gates

Three providers are **built and tested but not yet offered to operators**:

| category | provider_key | state |
|----------|--------------|-------|
| payment  | `flutterwave`    | `is_integrated=true`, `is_available=false` |
| sms      | `hubtel`         | `is_integrated=true`, `is_available=false` |
| sms      | `africastalking` | `is_integrated=true`, `is_available=false` |

Each is waiting on one deliberate manual step — "Phase 8" in the SMS build, "§9"
in the payment build. This is that step, for all three.

## What the flip is

As the platform owner, set `is_available = true` on the catalog row:

```
PUT /api/v1/platform/providers/{entry_id}
{ "is_available": true }
```

(or the toggle on the `/platform/providers` page). `entry_id` is the
`provider_catalog` row id — `GET /api/v1/platform/providers` lists them.

The server rejects the flip with **409** unless `is_integrated` is already true,
so a dead provider can never be exposed. Nothing else changes: `is_available`
only controls whether operators are *offered* the provider in
`GET /api/v1/providers?category=…` and whether they can **save new credentials**
for it.

## What must be true before you flip it

**A real operator has completed a real end-to-end transaction with real
credentials** — not just "Test connection".

Test connection only proves the credentials *authenticate*:

* Africa's Talking — `GET /version1/user` (account/balance lookup).
* Paystack / Flutterwave — a read-only `GET /transaction` (list, page 1).
* Hubtel — **no test at all** (no documented no-cost check; the button is
  hidden, `/test` returns "not available").

None of that proves a message actually *delivers* or a charge actually
*settles*. Those depend on things auth checks can't see:

* **SMS** — the sender ID ("From") is registered and approved with the gateway,
  the account has balance, and the route to the destination network works. A
  perfectly valid API key still yields `InvalidSenderId`, `InsufficientBalance`,
  or `UserInBlacklist` at send time.
* **Flutterwave** — the charge completes *and* the webhook fires back to
  `/api/v1/webhooks/flutterwave/{operator_slug}` with a valid signature, so the
  voucher is actually issued.

### Pre-flight checklist

1. Pick a pilot/throwaway operator (or the operator who will launch first).
2. They enter their **own** real credentials via
   `/admin/{payment,sms}-credentials` and activate that provider.
3. Run **Test connection** where available (Africa's Talking, Paystack,
   Flutterwave) — a green result is necessary, not sufficient.
4. Do the real thing:
   * **SMS** — a real voucher purchase (or any flow that hits
     `resolve_successful_payment` with a `phone_number`). Confirm the SMS
     arrives on a real handset. Check `docker compose logs backend` for
     `voucher_sms_sent` vs `voucher_sms_send_failed provider=… error=…`.
   * **Flutterwave** — a real card/MoMo charge end to end; confirm the
     transaction reaches `success` and a voucher is issued (i.e. the webhook
     was received and verified), not just that the redirect came back.
5. Only then flip `is_available=true` for that provider.

Flip providers **one at a time**, each behind its own real transaction.

## Reversibility

Flipping `is_available` back to `false` is safe and immediate: operators can no
longer *pick* the provider or *save new* credentials for it. It does **not**
disable operators who already have an active credential row — their charges and
voucher SMS keep resolving (the resolvers read the credential row, not the
catalog). To stop an already-live operator you'd deactivate or delete their row,
or suspend the operator.

The catalog sync in migrations never touches `is_available` (or
`platform_rate_per_message`) — a redeploy or `sync_provider_catalog` re-run will
not undo or apply a flip.
