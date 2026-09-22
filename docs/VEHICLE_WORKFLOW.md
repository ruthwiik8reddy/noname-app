# Verified vehicle continuity

This milestone connects the manual estimate workflow to bookings and jobs through explicit vehicle/customer IDs.

## Workflow

1. In New Estimate, look up a customer and explicitly select their vehicle by ID, description and plate/VIN. No first vehicle is silently selected. The server validates the customer/vehicle relationship and stores the vehicle ID and descriptive snapshot. These writes now require CSRF tokens.
2. For an approved estimate, open New Booking and load its estimate number. The server uses the verified vehicle and customer from that estimate rather than trusting edited autofill fields. Choose a studio-owned service, date, time and optional bay.
3. In Bookings, open **Vehicle / create job**. Select **Create or open job**. The job keeps the booking ID, estimate ID, vehicle ID and customer ID. Repeating or concurrently submitting this action opens the existing job rather than creating duplicates.
4. The vehicle timeline now includes estimates, bookings, jobs and inspections of linked jobs.

For bookings without an estimate, select a verified vehicle directly. A description-only booking is still possible, but cannot create a job until its vehicle is verified. It no longer silently creates or selects a vehicle by matching a make/model string.

Older unlinked records can be verified from the estimate detail or booking list. Link the source estimate first, then any existing booking, then convert to a job. Missing historical identities are never auto-backfilled. An already linked approved estimate cannot be switched to another vehicle; create a revision. Downstream linked vehicles must agree, and a job created from a booking cannot independently change its vehicle identity.

## Price and scheduling behavior

A job created from an approved estimate uses the estimate's **service subtotal excluding tax**, converting cents to dollars without rounding away fractional dollars. Without an estimate, it uses the booked service's current catalog price. Full invoicing, tax accounting and payment reconciliation remain separate work.

Bookings accept the application's existing AM/PM slot labels and normalize ISO times to those labels. The same bay/date/start-slot is checked in a write transaction. This prevents exact-slot conflicts through this creation path; it is not duration-aware bay scheduling and does not overhaul legacy update routes.

Repeated booking submissions use request keys plus a payload fingerprint. The same key/payload returns the existing booking. Reusing the key with different details is rejected. Booking-to-job conversion also has a database unique index and transaction protection.

## Access and migration

The existing staff estimate/booking creation roles remain in place. Explicit legacy identity correction and conversion actions are owner/manager-only, with CSRF protection. Every new cross-record lookup is studio-scoped and checked against the relevant customer.

Startup runs the additive `migrate_vehicle_workflow.py` migration after job records. Added columns: `estimates.vehicle_id`, `bookings.vehicle_id`, booking request metadata, `jobs.booking_id`, and `jobs.estimate_id`. No historical vehicle identity is inferred. Existing quick-quote/DVI estimate paths may still create estimates without a vehicle ID; those must be explicitly linked before booking through this workflow.

The existing public estimate approval route and broader legacy security issues are not redesigned in this milestone. Treat the local demo as development, not a production release.

## Validation

169 automated tests pass, including 13 new workflow tests for the full identity chain, fractional-dollar amounts, booking replay conflicts, concurrent job conversion, tenant isolation, approved-estimate rules, slot normalization/conflicts, unresolved legacy records, downstream identity drift, old-chain verification and route CSRF/role enforcement.

Live local verification created clearly labelled synthetic estimate #1 → booking #1 → job #9, all for demo vehicle #1. The resulting work price is $125.25 excluding tax. Repeating conversion returned job #9. Browser verification showed all records in the vehicle timeline. The synthetic approval was inserted as a labelled test fixture; no real customer signature or communication was involved.

Next milestone: approved customer follow-up drafts, consent/channel checks, and outcome tracking that links follow-ups to bookings. No customer messages were sent in this milestone.
