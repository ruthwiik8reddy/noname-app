# Customer lifecycle, scheduling and approval milestone

Implemented on the existing `feature/autofiera-stabilization` branch. This is another development milestone, not completion of the full automotive ERP roadmap.

## Customer follow-up desk

Open `/followups/` as an owner or general manager. The desk suggests explicitly linked completed vehicle visits older than 90 days and open estimates older than seven days. Future bookings, active follow-ups and recent follow-ups are suppressed from candidates. These fixed rules are review aids, not service-due predictions.

Prepare a deterministic draft from a specific vehicle and source job/estimate. There is at most one active follow-up for a vehicle/purpose. Duplicate create requests are idempotent and changed payloads cannot reuse a request key.

Record the customer's actual permission for the intended channel and exact recipient, including source/date evidence. Email and international phone syntax are checked. Permission is separate for email, SMS and WhatsApp. Missing permission, opt-out, changed recipient, changed source identity and future bookings block approval/contact recording. Changing permission invalidates approved drafts. Editing a message clears approval. Optimistic versions reject stale page submissions.

No provider sends messages. A manager can record that contact already happened outside Autofiera, explicitly attesting to that fact and entering a reference. This is a team-reported contact log, not a verified delivery receipt. Follow-up status and outcome also appear in the verified vehicle timeline. Record an outcome only against an active same-customer, same-vehicle booking created after contact. A captured booking-ID floor prevents attributing a pre-existing booking created within the same second. A booking can be attributed only once. Cancellations remain visible beside the original outcome. Attribution is not proof of causation or incremental revenue.

Tables: `contact_permissions`, `followups`, `followup_audit`. Every new route is manager-only, studio-scoped and CSRF-protected. No AI-generated text is needed for this flow. The AI intelligence screen links to this review desk.

## Duration-aware bay reservations

New bookings snapshot the selected service's duration as integer minutes. In a write transaction, half-open intervals are compared against active reservations in the selected bay, including overnight overlap and different start times. Adjacent reservations are allowed. Legacy rows without a snapshot use their current catalog duration for conflict checks, and malformed legacy reservations block that bay until corrected. This fallback is explicit, not a reconstruction of historical durations.

The booking form's availability endpoint checks the entire selected service duration. Cancelled reservations can be reopened only after conflict validation, and status changes have CSRF checks, allowed transitions and an audit event. Concurrent overlapping creates cannot both commit through this path.

Limits: times retain the existing studio-local wall-clock convention. Timezone/DST management, staff-resource scheduling, opening hours, maintenance blocks, actual bay occupancy, rescheduling UI and distributed reservation storage remain separate work. A booking without a bay reserves no bay. Direct dispatch job assignments are not included in booking availability. Existing legacy reservation dates are not guessed or rewritten.

## Audited time corrections

Owners/managers can correct stopped timers from job Work records, using explicit UTC start/end values and a reason. Future or reversed intervals, durations over seven days, overlapping staff timers, running timers, foreign records and stale original timestamps are rejected. The audit preserves old and new timestamps. Labor cost recalculates using the existing rate snapshot, cost review clears, and the normal intelligence event trigger runs. Technicians cannot use this action.

## Secure customer quote approval

The old public numeric `/estimates/<id>/approve` endpoint now returns HTTP 410 without exposing the estimate. Managers create links under `/approvals/manage/<id>`. Links use random 256-bit bearer tokens, expire after seven days, can be revoked before response, and are stored in the application database only as SHA-256 hashes. The full link is displayed once. Creating another link revokes earlier unanswered links. No email/SMS is sent by creating a link.

Each link freezes customer-facing quote fields and ordered line items in `estimate_approval_versions`. A verified customer/vehicle and reconciled line/tax totals are required. Internal notes and contact details are excluded from the customer snapshot. Quote changes invalidate an outstanding link. Customer approval requires an explicit acknowledgment and typed name, plus session CSRF. Decision records are transactional, audited and idempotent for an exact replay. A conflicting second decision is rejected.

Approved secure-version fingerprints are rechecked before booking and job conversion, preventing later edits from silently changing the approved quote. Previously approved legacy estimates remain usable and are not fabricated into historical signatures. This is quote-version approval evidence, not an independently verified identity or a claim of legal signature compliance. Create a new revised estimate when terms change.

Approval responses use no-store, no-referrer, noindex and anti-framing headers. Application log handlers redact approval URL tokens. Production reverse proxies and infrastructure must also redact capability URLs. No comprehensive overhaul of legacy session secrets, other public links, customer media or authorization routes is claimed.

## Validation and release state

The full suite passes **214 tests**, including **45 new tests** in this milestone (latest local run: 7.504 seconds). New tests cover concurrency, customer/tenant identity, consent changes, stale approvals, changed destinations, duplicate requests, outcome chronology, booking overlaps, timer review invalidation, expired/revoked links, quote tampering, conflicting customer responses and route access/CSRF. Full-suite results and synthetic demo IDs are in the companion validation files.

The local demo backup was taken before migrations. A labelled simulation covers prior completed job, follow-up permission/review/contact record, secure quote approval, booking, idempotent job conversion and attributed outcome. No customer was contacted, and no real consent or signature is claimed by the demo fixture.

## Remaining roadmap

Next core batches are invoice/payment allocation and delivery, consistent status policy across remaining legacy routes, private media/public-link hardening, deployment configuration/secret rotation and a representative shop pilot. Payment and messaging adapters need a chosen launch market, currency, provider and test credentials. No assumptions about those providers were made here.

Deeper vehicle condition comparison, model accuracy evaluation, trained forecasting and measured utilization need representative records and validation. Production PostgreSQL/queue/storage migration, monitoring, backups, retention, multi-location/subscription management, warranty lifecycle, franchise support and a technician mobile product are not complete. This milestone does not claim that the earlier “everything” vision is shipped.
