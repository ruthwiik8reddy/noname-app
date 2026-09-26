"""Recorded labor, reviewed direct contribution and a backtested naive scenario."""
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from ..repositories.job_records import JobRecords,rounded


def add_operations(conn,sid,start,end,today,fact):
    repo=JobRecords(connection=conn)
    jobs=conn.execute("SELECT id,service FROM jobs WHERE studio_id=? AND status='Completed' AND date(completed_at)>=? AND date(completed_at)<? ORDER BY id",(sid,start.isoformat(),end.isoformat())).fetchall()
    reviewed=[];services=defaultdict(list);labor=defaultdict(list);budgets=[]
    for job in jobs:
        record=repo.detail(sid,job['id'])
        if not record['reviewed']:continue
        evidence={'job_id':job['id'],'revenue_cents':record['revenue_cents'],'contribution_cents':record['contribution_cents'],'labor_seconds':record['seconds']}
        reviewed.append(evidence);services[job['service'] or 'Unspecified'].append(evidence)
        for segment in record['labor']:
            labor[segment['staff_id']].append({'job_id':job['id'],'timer_id':segment['id'],'seconds':segment['seconds']})
        budget=record['plan'].get('planned_minutes')
        if budget is not None:budgets.append({'job_id':job['id'],'planned_minutes':budget,'actual_minutes':round(record['seconds']/60,2)})
    coverage=round(len(reviewed)*100/len(jobs),1) if jobs else None
    fact('cost_review_coverage','quality','Completed jobs with reviewed costs',coverage,'percent',
         f'{len(reviewed)} of {len(jobs)} completed jobs have reviewed direct costs in this period.',
         'Unreviewed jobs are excluded from contribution and completed-job labor analysis.',reviewed)
    for index,(service,records) in enumerate(sorted(services.items())):
        amount=sum(r['contribution_cents'] for r in records);revenue=sum(r['revenue_cents'] for r in records)
        ratio=round(100*amount/revenue,1) if revenue>0 else None
        fact(f'service_contribution_{index}','revenue',f'Reviewed contribution: {service}',amount,'cents',
             f'{service}: ${amount/100:,.2f} direct contribution on {len(records)} reviewed jobs'+(f' ({ratio}% of their selling value).' if ratio is not None else '.'),
             'Selling price less recorded labor/materials on reviewed completed jobs, grouped by recorded service label. Excludes overhead, fees and tax. Not net profit.',records)
    for staff_id,segments in sorted(labor.items()):
        hours=round(sum(r['seconds'] for r in segments)/3600,2);count=len({r['job_id'] for r in segments})
        fact(f'staff_recorded_hours_{staff_id}','operations',f'Recorded labor: staff #{staff_id}',hours,'hours',
             f'Staff #{staff_id} recorded {hours} hours across {count} reviewed completed jobs.',
             'All recorded labor on jobs completed in the selected period, even if the timer started earlier. Does not measure attendance, utilization, speed or quality and is not a staff ranking.',segments)
    variance=round(sum(r['actual_minutes']-r['planned_minutes'] for r in budgets),2) if budgets else None
    fact('labor_budget_variance','operations','Reviewed labor against budget',variance,'minutes',
         f'Recorded labor differs from budget by {variance:+.2f} minutes across {len(budgets)} reviewed jobs.' if budgets else 'No reviewed completed jobs have labor budgets for comparison.',
         'Actual recorded staff minutes minus manager-entered planned minutes. Multiple staff hours accumulate. Negative means fewer minutes than budget, not higher quality.',budgets)
    # Time-period totals clip stopped timers to UTC window boundaries. Active timers are excluded.
    lo=int(datetime.combine(start,datetime.min.time(),timezone.utc).timestamp());hi=int(datetime.combine(end,datetime.min.time(),timezone.utc).timestamp())
    stopped=[dict(r) for r in conn.execute('''SELECT l.id,l.job_id,l.staff_id,l.started_at,l.ended_at FROM job_labor l
       JOIN jobs j ON j.id=l.job_id AND j.studio_id=l.studio_id
       WHERE l.studio_id=? AND l.ended_at IS NOT NULL AND l.started_at<? AND l.ended_at>?''',(sid,hi,lo))]
    hours=round(sum(max(0,min(r['ended_at'],hi)-max(r['started_at'],lo)) for r in stopped)/3600,2)
    fact('period_recorded_labor','operations','Stopped timer hours in reporting window',hours,'hours',
         f'{hours} stopped-timer hours fall inside the selected UTC reporting window.',
         'Clips stopped segments to the window. Excludes active timers and unrecorded work. This is recorded labor, not paid attendance.',stopped)
    monday=today-timedelta(days=today.weekday());begin=monday-timedelta(weeks=10)
    history=[dict(r) for r in conn.execute("SELECT id,price,completed_at FROM jobs WHERE studio_id=? AND status='Completed' AND date(completed_at)>=? AND date(completed_at)<?",(sid,begin.isoformat(),monday.isoformat()))]
    weeks=[0]*10
    for row in history:
        day=datetime.fromisoformat(row['completed_at'].replace('Z','+00:00')).date()
        idx=(day-begin).days//7
        if 0<=idx<10:weeks[idx]+=rounded(Decimal(str(row['price'] or 0))*100)
    occupied=sum(v>0 for v in weeks)
    if len(history)>=20 and occupied>=8:
        prediction=rounded(Decimal(sum(weeks[-4:]))/4)
        errors=[abs(rounded(Decimal(sum(weeks[i-4:i]))/4)-weeks[i]) for i in range(4,10)]
        mae=rounded(Decimal(sum(errors))/len(errors))
        statement=f'If the last four complete weeks repeat, the next full week has ${prediction/100:,.2f} of completed job value. Historical one-week mean absolute error was ${mae/100:,.2f} over six checks.'
        value=prediction
    else:
        value=None;statement=f'Weekly projection withheld: {len(history)} jobs across {occupied} nonzero weeks. At least 20 jobs and eight nonzero weeks in the last ten complete weeks are required.'
    fact('weekly_value_scenario','revenue','Weekly completed-value planning scenario',value,'cents',statement,
         'Four-week moving-average baseline, not a trained model, cash forecast or guarantee. Uses ten complete UTC Monday–Sunday weeks and six rolling historical checks. Missing logs, seasonality and promotions can invalidate it. Mean absolute error is not a confidence interval.',history)
