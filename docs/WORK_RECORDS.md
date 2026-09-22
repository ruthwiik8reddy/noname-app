# Vehicle history and job cost foundation

This milestone adds a usable first version of vehicle links, labor timers, material tracking, and reviewed direct job contribution. It does not implement the full follow-up, scheduling optimization or vision roadmap.

## Use it

Open a job from Dispatch, then select **Work records**. Owners and general managers can link a verified vehicle, set staff cost rates, record quoted labor/material budgets, start or stop timers, consume/return materials and review costs. Technicians can record work and usage for assigned jobs; they can stop their own existing timer after reassignment. Financial fields, rates, budgets, corrections and vehicle timelines are restricted to owners/managers.

1. Create the customer and vehicle through the existing Customers workflow, then explicitly select the vehicle on the job. A matching make/model description is never used to guess identity. A job with an existing customer can only link to that customer's vehicle in the same studio. Linking an unowned job also attaches the vehicle's verified customer. Changes are audited.
2. Set the business's hourly labor cost before starting timers. A staff member can have one active timer per studio. Pausing means stopping the timer and starting a new segment when work resumes. Completion/cancellation stops active timers through a database trigger, including when an older status route is used.
3. Record material use in the inventory item's unit, with up to three decimal places. Stock, inventory log and cost entry are written atomically. Replayed submissions cannot deduct stock twice; insufficient stock rejects the entire operation. Owners can return unused quantities against a specific original consumption entry.
4. Review the job's labor/material records after completion and explicitly confirm that they are complete. New work entries, cost corrections, budget changes or changes to job price/status clear the review.
5. Open **vehicle history** to see explicitly linked jobs and their inspections. This is visit continuity, not automated image comparison or a claim that AI inspection findings are accurate.

The existing inventory scanner now uses the same protected material workflow. It requires exactly one in-progress job assigned by staff ID; ambiguous jobs direct the technician to a specific work-record page instead of silently selecting one. It no longer relies on the missing session username or permits negative stock through that endpoint.

## Accounting definitions

Existing job selling prices are whole USD and are converted to integer cents once. Inventory unit costs are already cents. Material quantities in the new ledger use integer thousandths of a unit. Labor duration uses UTC epoch seconds. Costs round half-up to cents per entry. Rate snapshots preserve historical costs when the current staff rate or inventory price changes. Partial returns preserve the original unit price and cannot refund more than the original rounded consumption cost.

Job contribution = selling price − recorded labor cost − recorded material cost.

It is **not net profit, cash received, or a complete accounting margin**. Overhead, payment fees, taxes and unrecorded work are excluded. Unknown labor rates and missing/zero catalog material costs are shown as unknown, not silently priced at zero. An explicit manager correction can establish a legitimate zero cost. Existing legacy materials are not silently imported with today's prices.

Managers can explicitly correct a timer's hourly cost or a consumption entry's unit cost with an audit reason. Material cost corrections recalculate linked returns without moving stock again. The corrected job needs a new review. Historical rate changes are not applied automatically.

The intelligence snapshot now includes **reviewed job contribution** for completed jobs in the selected period. It excludes unreviewed or incomplete costs. Negative reviewed contribution can generate a business-agent finding. The natural-language assistant selects this verified fact through the existing evidence validation.

## Storage and operation

Startup runs `migrate_job_records.py` after the existing intelligence migration. It adds `jobs.vehicle_id`, staff cost rates, labor segments, material entries, budgets/review flags and an audit table. Migration is repeatable and does not guess or backfill historical vehicle identity.

Writes use SQLite transactions; time starts and material movements use `BEGIN IMMEDIATE` to serialize concurrent validation/writes. Unique request keys protect duplicate start/stock submissions. New cost records/reviews queue business analysis through the existing durable event mechanism.

Routes:

- `GET /work-records/jobs/<job_id>`
- `GET /work-records/vehicles/<vehicle_id>`
- `POST /work-records/jobs/<job_id>/<operation>` where operation is link, rate, plan, start, stop, consume, return, correct-cost or review.

Every new write requires a session-bound CSRF token, role checks and tenant-scoped record validation. The browser submits tokens automatically. Cost corrections and review are manager-only. The existing scanner uses the same CSRF token via a header.

## Validation and limits

The expanded suite contains 156 passing tests: the previous 129 plus 27 new tests. New coverage includes explicit identity, customer/tenant mismatch, immutable rate snapshots unless deliberately corrected, timer deduplication, automatic stopping, unknown costs, atomic stock writes, overspending rejection, numeric validation, repeat submissions, partial returns and cent-rounding boundaries, correction audit behavior, concurrent stock races, migration repeatability, review invalidation, owner/technician access, CSRF and inclusion of reviewed costs in intelligence.

The local demo contains a labelled synthetic example: job #8 / vehicle #1, selling price $200, 30 recorded sample minutes at $30/hour ($15), 2.5 sample material units at $2.50 ($6.25), reviewed contribution $178.75. These are demonstration records, not measurements of a real customer's job. The prior demo database was backed up before migration.

Next work: propagate verified vehicle identity through bookings and estimates; add reviewed time corrections for forgotten timers; improve standardized photo capture; build approved customer follow-up with outcome tracking. Production still requires the previously identified security/workflow fixes, backup/retention controls and representative shop pilots. Existing general inventory adjustment routes outside the new work-record/scanner path were not overhauled in this milestone.
