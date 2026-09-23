"""Bay reservation intervals in the studio's existing local wall-clock convention."""
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from .job_records import RecordError


def duration_minutes(hours):
    try:
        duration=Decimal(str(hours))*60
        if not duration.is_finite() or duration<=0 or duration>10080:
            raise ValueError()
        return int(duration.to_integral_value(rounding=ROUND_CEILING))
    except (ValueError,TypeError,InvalidOperation):
        raise RecordError('Service duration must be positive and no longer than seven days.')


def interval(day, slot, minutes):
    try:
        try:t=time.fromisoformat(slot)
        except ValueError:t=datetime.strptime(slot,'%I:%M %p').time()
        if t.tzinfo or t.second or t.microsecond:raise ValueError()
        start=datetime.combine(datetime.strptime(day,'%Y-%m-%d').date(),t)
        if not isinstance(minutes,int) or not 1<=minutes<=10080:raise ValueError()
        return start,start+timedelta(minutes=minutes)
    except (ValueError,TypeError,OverflowError):
        raise RecordError('A valid reservation date, time and duration are required.')


def ensure_available(conn,sid,bay_id,day,slot,minutes,exclude_id=None):
    start,end=interval(day,slot,minutes)
    if not bay_id:return
    if not conn.execute('SELECT id FROM bays WHERE studio_id=? AND id=?',(sid,bay_id)).fetchone():
        raise RecordError('Select a bay in this studio.')
    rows=conn.execute('''SELECT b.*,s.duration_hr FROM bookings b LEFT JOIN services s
        ON s.id=b.service_id AND s.studio_id=b.studio_id
        WHERE b.studio_id=? AND b.bay_id=? AND b.id!=?
        AND lower(b.status) NOT IN ('cancelled','canceled','rejected')''',(sid,bay_id,exclude_id or -1)).fetchall()
    for row in rows:
        try:
            duration=row['duration_minutes'] if row['duration_minutes'] is not None else duration_minutes(row['duration_hr'])
            other_start,other_end=interval(row['date'],row['time_slot'],duration)
        except RecordError:
            raise RecordError(f"Booking #{row['id']} has an unresolved reservation time or duration. Correct it before reserving this bay.")
        if start<other_end and other_start<end:
            raise RecordError(f"This reservation overlaps booking #{row['id']} in the selected bay.")
