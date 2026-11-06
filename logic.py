from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re


def parse_hm(s):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',s):
        raise ValueError('Heure HH:MM requise')
    a,b=map(int,s.split(':'))
    return a*60+b


def windows(now, rows, tz='Europe/Paris'):
    """Generate windows for local calendar days; supports crossing midnight and DST."""
    zone=ZoneInfo(tz)
    day=now.astimezone(zone).date()
    out=[]
    for offset in range(-2,3):
        d=day+timedelta(days=offset)
        for start,end in rows:
            sh,sm=divmod(parse_hm(start),60)
            eh,em=divmod(parse_hm(end),60)
            a=datetime(d.year,d.month,d.day,sh,sm,tzinfo=zone)
            b=datetime(d.year,d.month,d.day,eh,em,tzinfo=zone)
            if b<=a:b+=timedelta(days=1)
            out.append((a,b))
    return sorted(out)


def automatic_open(now, rows, tz='Europe/Paris'):
    return any(a<=now.astimezone(ZoneInfo(tz))<b for a,b in windows(now,rows,tz))


def next_boundary(now, rows, kind, tz='Europe/Paris'):
    current=now.astimezone(ZoneInfo(tz))
    candidates=[]
    for a,b in windows(now,rows,tz):
        target=a if kind=='open' else b
        if target>current:
            # If closing of overlapping windows, only announce effective closing.
            probe=target+timedelta(seconds=1)
            was=target-timedelta(seconds=1)
            if kind=='close' and automatic_open(probe,rows,tz):continue
            if kind=='open' and automatic_open(was,rows,tz):continue
            candidates.append(target)
    return min(candidates) if candidates else None


def countdown_label(seconds):
    mins=round(seconds/60)
    if mins>=60 and mins%60==0:return f'{mins//60}h'
    return f'{mins} min'


def countdown_due(seconds):
    mins=round(seconds/60)
    return (mins in (1,2,3,5,15,30)) or (mins>0 and mins%60==0)


def safe_name(name):
    """False for non-Latin scripts; Latin accents, digits and common punctuation are fine."""
    import unicodedata
    for ch in name:
        if ch.isalpha() and 'LATIN' not in unicodedata.name(ch,''):
            return False
    return True
