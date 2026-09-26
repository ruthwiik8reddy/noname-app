"""A consistent, tenant-scoped read snapshot and explicit business definitions.

Legacy jobs use whole dollars; estimates use cents. Convert at this boundary.
UTC calendar windows include today, with an adjacent equal-length comparison.
Stock coverage is a historical-rate scenario, never a demand prediction.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from ..repositories.base import BaseRepository


def cents(value):
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def money(value):
    return f"${value / 100:,.2f}"


class IntelligenceFacts(BaseRepository):
    def snapshot(self, studio_id, days=30, today=None):
        if type(days) is not int or not 7 <= days <= 90:
            raise ValueError('Choose a period between 7 and 90 days.')
        today = today or datetime.now(timezone.utc).date()
        end = today + timedelta(days=1)
        start = end - timedelta(days=days)
        previous = start - timedelta(days=days)
        facts, actions = [], []

        def fact(key, category, title, value, unit, statement, definition, rows, severity='info'):
            item = dict(id=key, category=category, title=title, value=value, unit=unit,
                        statement=statement, definition=definition, severity=severity,
                        evidence=rows[:100], evidence_count=len(rows))
            facts.append(item)
            return item

        def action(key, title, detail, evidence, url, severity='opportunity'):
            actions.append(dict(id=key, title=title, detail=detail, fact_ids=evidence,
                                url=url, severity=severity))

        # One read transaction prevents metrics in the same report seeing different commits.
        with self._conn() as conn:
            conn.execute('SAVEPOINT intelligence_read')
            try:
                def rows(sql, params=()):
                    return [dict(r) for r in conn.execute(sql, (studio_id, *params)).fetchall()]

                completed = rows("SELECT id, price, service, completed_at FROM jobs WHERE studio_id=? "
                                 "AND lower(status)='completed' AND date(completed_at)>=? "
                                 "AND date(completed_at)<? ORDER BY id", (previous.isoformat(), end.isoformat()))
                current = [r for r in completed if r['completed_at'][:10] >= start.isoformat()]
                prior = [r for r in completed if r['completed_at'][:10] < start.isoformat()]
                value, old = sum(cents(r['price']) for r in current), sum(cents(r['price']) for r in prior)
                fact('job_value', 'revenue', 'Completed job value', value, 'cents',
                     f"Completed jobs total {money(value)} in the current {days}-day period.",
                     'Sum of completed job prices; booked work value, not cash receipts or profit.', current)
                change = round((value - old) * 100 / old, 1) if old > 0 else None
                fact('value_change', 'revenue', 'Change against previous period', change, 'percent',
                     (f"Completed job value changed {change:+.1f}% versus the preceding {days} days ({money(old)})."
                      if change is not None else 'Percentage change is unavailable because the previous period has no positive job value.'),
                     'Adjacent equal-length UTC calendar periods. No percentage is inferred from a zero baseline.', completed,
                     'warning' if change is not None and change <= -15 else 'info')
                fact('completed_jobs', 'revenue', 'Completed jobs', len(current), 'jobs',
                     f"{len(current)} jobs completed in the current period.", 'Completion date, not creation date.', current)
                average = int((Decimal(value) / Decimal(len(current))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if current else None
                fact('average_ticket', 'revenue', 'Average completed job value', average, 'cents',
                     f"Average completed job value is {money(average)}." if current else 'No completed jobs are available for an average.',
                     'Completed job value divided by completed job count.', current)
                if change is not None and change <= -15:
                    action('review_revenue', 'Review the drop in completed work',
                           'Compare service mix and job volume before changing pricing.', ['job_value','value_change','completed_jobs'], '/jobs', 'warning')

                mix = {}
                for row in current:
                    key = row['service'] or 'Unspecified'
                    mix.setdefault(key, []).append(row)
                for index, (service, records) in enumerate(sorted(mix.items(), key=lambda p: sum(cents(r['price']) for r in p[1]), reverse=True)[:8]):
                    amount = sum(cents(r['price']) for r in records)
                    fact(f'service_{index}', 'revenue', str(service)[:160], amount, 'cents',
                         f"{str(service)[:160]} accounts for {money(amount)} across {len(records)} completed jobs.",
                         'Grouped by recorded service label; bundled jobs are not split into individual services.', records)

                estimates = rows("SELECT id, status, total, created_at FROM estimates WHERE studio_id=? "
                                 "AND lower(status) IN ('draft','sent') ORDER BY created_at")
                overdue = [r for r in estimates if r['created_at'] and r['created_at'][:10] <= (today-timedelta(days=7)).isoformat()]
                pipeline = sum(int(r['total'] or 0) for r in estimates)
                fact('estimate_pipeline', 'estimates', 'Open estimate value', pipeline, 'cents',
                     f"{len(estimates)} open estimates total {money(pipeline)}.",
                     'Current Draft and Sent estimates. Potential work, not earned revenue.', estimates)
                fact('stale_estimates', 'estimates', 'Estimates awaiting follow-up', len(overdue), 'estimates',
                     f"{len(overdue)} open estimates were created at least 7 days ago.",
                     'Age since creation; contact history may be incomplete.', overdue, 'warning' if overdue else 'info')
                if overdue:
                    action('follow_estimates', 'Follow up on older estimates', 'Review customer contact history before reaching out.', ['stale_estimates','estimate_pipeline'], '/estimates')

                leads = rows("SELECT id, status, source, created_at FROM leads WHERE studio_id=? "
                             "AND date(created_at)>=? AND date(created_at)<? AND coalesce(source,'')!='booking_form'",
                             (start.isoformat(), end.isoformat()))
                won = sum(r['status'] == 'won' for r in leads)
                conversion = round(won * 100 / len(leads), 1) if leads else None
                fact('lead_conversion', 'leads', 'Lead cohort won rate', conversion, 'percent',
                     f"{won} of {len(leads)} leads created in this period are now won." if leads else 'There are no eligible leads in this period.',
                     'Current won status of leads created in the period; excludes booking_form backfills. Recent cohorts are still maturing.', leads)

                waiting = rows("SELECT id, service, status, assigned_staff_id FROM jobs WHERE studio_id=? "
                               "AND lower(status) NOT IN ('completed','cancelled','canceled') AND assigned_staff_id IS NULL")
                fact('unassigned', 'operations', 'Jobs without an assigned technician', len(waiting), 'jobs',
                     f"{len(waiting)} active jobs have no staff assignment.",
                     'Uses assigned_staff_id; legacy technician names alone do not establish a staff link.', waiting,
                     'warning' if waiting else 'info')
                if waiting:
                    action('assign_work', 'Review unassigned work', 'Assign staff after checking skills and workload.', ['unassigned'], '/dispatch/', 'warning')

                returning = rows("""SELECT c.id, MAX(date(j.completed_at)) AS last_visit, COUNT(*) AS visits
                    FROM customers c JOIN jobs j ON j.customer_id=c.id AND j.studio_id=c.studio_id
                    WHERE c.studio_id=? AND lower(j.status)='completed' AND date(j.completed_at)<=?
                    GROUP BY c.id HAVING last_visit<=? AND NOT EXISTS
                    (SELECT 1 FROM bookings b WHERE b.customer_id=c.id AND b.studio_id=c.studio_id
                     AND date(b.date)>=? AND lower(b.status) NOT IN ('cancelled','canceled','rejected'))
                    ORDER BY last_visit""", (today.isoformat(), (today-timedelta(days=90)).isoformat(), today.isoformat()))
                fact('rebooking', 'customers', 'Customers to review for rebooking', len(returning), 'customers',
                     f"{len(returning)} linked customers last visited at least 90 days ago and have no future booking.",
                     'A fixed 90-day review rule, not a service-specific due date or a prediction of churn.', returning)
                if returning:
                    action('review_rebooking', 'Review rebooking candidates', 'Check service intervals and customer consent before contacting anyone.', ['rebooking'], '/customers')

                # Parameter order is explicit here because the tenant predicate follows JOIN predicates.
                stock = [dict(r) for r in conn.execute("""SELECT i.id, i.name, i.quantity, i.unit, i.reorder_level,
                    COALESCE(SUM(CASE WHEN l.change_qty<0 THEN -l.change_qty ELSE 0 END),0) AS consumed,
                    COUNT(DISTINCT CASE WHEN l.change_qty<0 THEN date(l.created_at) END) AS usage_days
                    FROM inventory_items i LEFT JOIN inventory_logs l ON l.item_id=i.id AND l.studio_id=i.studio_id
                    AND date(l.created_at)>=? AND date(l.created_at)<? WHERE i.studio_id=? GROUP BY i.id""",
                    ((end-timedelta(days=30)).isoformat(), end.isoformat(), studio_id)).fetchall()]
                low = [r for r in stock if r['quantity'] <= r['reorder_level']]
                fact('low_stock', 'inventory', 'Items at reorder level', len(low), 'items',
                     f"{len(low)} stock items are at or below their reorder level.", 'Current quantity compared with configured reorder level.', low,
                     'warning' if low else 'info')
                for item in stock:
                    # At least three distinct logged consumption dates for an indicative coverage scenario.
                    if item['consumed'] > 0 and item['usage_days'] >= 3:
                        coverage = round(max(0, item['quantity']) / (item['consumed']/30), 1)
                        if coverage <= 14:
                            fact(f"stock_{item['id']}", 'inventory', f"Stock coverage: {item['name']}", coverage, 'days',
                                 f"{item['name']} has about {coverage} days of stock if the last 30-day usage rate continues.",
                                 'Quantity / (logged withdrawals / 30). Assumes complete logs and unchanged usage; excludes supplier lead time.', [item], 'warning')
                stock_ids = [f['id'] for f in facts if f['id'].startswith('stock_')]
                if low or stock_ids:
                    action('review_stock', 'Review stock and supplier lead times', 'Verify physical stock and recent usage before ordering.', ['low_stock', *stock_ids], '/inventory', 'warning')

                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_cost_plans'").fetchone():
                    from ..repositories.job_records import JobRecords
                    reviewed_jobs=rows("""SELECT j.id FROM jobs j JOIN job_cost_plans p ON p.job_id=j.id AND p.studio_id=j.studio_id
                        WHERE j.studio_id=? AND lower(j.status)='completed' AND p.reviewed_at IS NOT NULL
                        AND date(j.completed_at)>=? AND date(j.completed_at)<?""",(start.isoformat(),end.isoformat()))
                    cost_records=[]
                    for job in reviewed_jobs:
                        record=JobRecords(connection=conn).detail(studio_id,job['id'])
                        if record['reviewed']:
                            cost_records.append(dict(job_id=job['id'],revenue_cents=record['revenue_cents'],
                                labor_cents=record['labor_cents'],material_cents=record['material_cents'],
                                contribution_cents=record['contribution_cents']))
                    amount=sum(r['contribution_cents'] for r in cost_records)
                    fact('reviewed_contribution','revenue','Reviewed job contribution',amount if cost_records else None,'cents',
                         (f"{len(cost_records)} completed jobs with reviewed direct costs contributed {money(amount)} before overhead, fees and tax."
                          if cost_records else 'No completed jobs in this period have reviewed direct costs.'),
                         'Only explicitly reviewed completed jobs are included. Selling price less recorded labor and materials; not net profit or cash collected.',cost_records)
                    negative=[r for r in cost_records if r['contribution_cents']<0]
                    if negative:
                        action('review_job_costs','Review jobs whose recorded costs exceed their price',
                               'Check time, materials, pricing and any rework before changing quotes.',
                               ['reviewed_contribution'],'/work-records/jobs/'+str(negative[0]['job_id']),'warning')

                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='job_labor'").fetchone():
                    from .operations import add_operations
                    add_operations(conn,studio_id,start,end,today,fact)

                missing = rows("SELECT id, status, completed_at, customer_id FROM jobs WHERE studio_id=? AND "
                               "(NOT EXISTS (SELECT 1 FROM customers c WHERE c.id=jobs.customer_id AND c.studio_id=jobs.studio_id) OR (lower(status)='completed' AND date(completed_at) IS NULL))")
                fact('data_quality', 'quality', 'Jobs with incomplete reporting data', len(missing), 'jobs',
                     f"{len(missing)} jobs lack a customer link or a valid completion date needed for reporting.",
                     'Completion dates are required for trend metrics; customer IDs are required for rebooking analysis.', missing,
                     'warning' if missing else 'info')
                if missing:
                    action('repair_records', 'Complete the underlying job records', 'Link customers and check completion dates so reports include the right work.', ['data_quality'], '/jobs', 'warning')
            finally:
                conn.execute('RELEASE intelligence_read')

        actions.sort(key=lambda a: 0 if a['severity']=='warning' else 1)
        return dict(version=1, studio_id=studio_id, generated_at=datetime.now(timezone.utc).isoformat(),
                    period=dict(days=days, start=start.isoformat(), end_exclusive=end.isoformat(),
                                previous_start=previous.isoformat(), timezone='UTC'),
                    currency='USD', facts=facts, actions=actions,
                    limitations=[
                        'Job prices are treated as whole USD and estimate totals as cents, matching the existing application.',
                        'Cash collection, profit and payment reconciliation require a transaction ledger.',
                        'Recorded hours and budget variances are not attendance, actual bay utilization, technician speed or quality.',
                        'Vehicle condition comparisons require stable vehicle links across visits and reviewed inspection evidence.',
                        'Stock coverage, rebooking rules and weekly moving-average scenarios are indicative, not trained forecasts.',
                    ])
