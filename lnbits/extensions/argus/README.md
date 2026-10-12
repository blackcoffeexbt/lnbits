# Argus payment authorization extension

Argus lives in `lnbits/extensions/argus`, with pairing credentials, policy proposals,
review requests, and authenticated decisions in `ext_argus.sqlite3`. PostgreSQL
installations use the normal `argus` extension schema. The extension is discovered
through its `config.json` and uses the standard extension migration/start/stop
lifecycle. No funding-source adapter changes are required.

Open **Extensions → Argus**, or `/argus`, as the superuser. Enter the LNbits HTTPS
URL on the device, select Connect, click Pair device in the extension, compare the
six-digit codes and confirm on both screens. No QR code, copied credential or
payment PIN is needed. The existing firmware endpoint `/api/v1/argus/ws` is
preserved; a TLS reverse proxy must support WebSocket upgrades. Argus validates
server certificates.

Enable authorization after pairing. Requests expire after the configurable
30–3,600 seconds, bounded by invoice expiry. Propose the per-payment, rolling-hour
and rolling-day policy in the extension and confirm on the device. Automatic
approval requires three positive limits. Amount plus maximum fees counts against
the device allowance when approval is issued, even if execution later fails.

## Core contract

Core's `payment_hooks.py` is a small generic dispatcher and fail-closed boundary.
Reservation, approval verification, dispatch, fee policy and recovery orchestration
live in the extension's `payment_authorization.py`. Core invokes the registered
provider's `enqueue` callback before payment dispatch. Argus internally uses
`prepare(context)` for the immutable review payload and `authorize(envelope)` for
the durable device decision. Neither callback may wait for a
device, funding source or network request, or call core ledger services while
holding an extension transaction.

The mandatory provider is persisted in existing `system_settings` storage under
its own `payment_authorization` tag. No authorization table or core migration is
needed. Core checks both process-local registration and the durable extension
activation record, so another worker's disable/uninstall cannot bypass the gate. Outgoing payments enter the
gate before the internal/external split, including payments originating in other
extensions. The financial reservation and server-owned `extra.authorization`
envelope commit in the same core transaction. The envelope includes a unique
attempt ID, provider, stage, canonical review body/digest, expiry and fee snapshot.
Client-supplied authorization metadata is discarded; metadata updates cannot
inject the reserved namespace. Incoming payments are unaffected.

The extension worker projects committed envelopes into its database by request
ID. Core remains authoritative for reservations, dispatch claims and settlement;
the extension owns device decisions and protocol state. No transaction spans the
two files. The extension never holds its database lock while calling core ledger
operations. Projection reconciles current pending payments and outstanding audit
records without scanning all historical payments each second.

Approval is persisted in the extension first. Core revalidates the decision,
request binding, permissions, hard limits, expiry and selected provider before
claiming execution under a serialized core transaction. Internal transfers reuse
the reservation and settle atomically. External calls run after the core dispatch
claim commits, with no core database lock or payment lock held during network I/O.
Concurrent workers cannot dispatch the same request twice. Fees are credited from
the approved snapshot once. Pending APIs return promptly and never query a
funding source using a synthetic authorization reference.

BOLT12 offers work when enforcement is off. When enforcement is on, they fail
before reservation or dispatch because the current device protocol requires a
concrete BOLT11 invoice. On-chain PSBT signing remains independent. Node spending
credentials are still on LNbits; a compromised server can bypass this gate.

## Recovery and extension removal

- Core reservation committed, extension insert lost: reconstruct from the envelope.
- Extension approval committed, execution not started: resume the core transition.
- Core settlement committed, extension audit stale: reconcile from core.
- Dispatch claimed, node result unknown: retain the reservation; never resend
  automatically. A claim before the node call can remain ambiguous and require
  operator investigation. Reconcile known backend references through normal core
  status checks; synthetic references are never sent to a funding source.
- Extension disabled, uninstalled, or unavailable: preserve provider selection,
  block new protected payments and retain existing holds until expiry or explicit
  cancellation. Removing an extension never silently disables authorization.
- Extension database lost: reconstruct holds, not approvals or credentials. Pair
  and configure again, or restore the extension backup. Holds from a previous
  instance identity require cancellation and a fresh payment request. Reconstructed
  dispatched claims cannot execute again.

Core expiry cleanup remains active independently of extension workers. Expiry or
cancellation releases only undispatched reservations; a dispatch claim cannot be
recalled. Explicitly disabling enforcement cancels undispatched holds before new
payments are allowed through.

The superuser recovery API remains usable without the extension:

- `GET /admin/api/v1/payment-authorization`: selected provider and availability.
- `PUT /admin/api/v1/payment-authorization` with `{"provider":""}`: explicitly
  disable enforcement and cancel undispatched holds.

The extension's superuser REST endpoints remain under `/api/v1/argus`: settings,
pairing/confirmation/revocation, policy proposals, requests and cancellation.
HMAC-SHA256 sessions bind direction, fresh session nonces, frame counters and exact
JSON body bytes. Payment decisions bind the immutable payload digest. Pairing
requires both confirmations. Never log credentials or WebSocket frames.

## Upgrade and verification

This is a fresh development schema. All Argus schema creation belongs to the
extension migration; core has no Argus migrations or compatibility import. The
extension migration can be repeated safely without replacing existing pairing
credentials. Back up core and extension databases together; mismatched backups
fail conservatively and may require manual reconciliation of ambiguous claims.

`make test-unit` includes fault injection at reservation, projection, approval,
dispatch-claim and audit-update boundaries, concurrent execution, extension
removal and lost database recovery. `make test-api` covers pairing, authentication
and recovery controls. These tests use fake funding sources and simulated devices;
they do not broadcast real payments or verify physical-device interoperability.
