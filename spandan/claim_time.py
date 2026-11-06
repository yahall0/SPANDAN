import re
from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MONTHS = {name: index for index, name in enumerate(
    'january february march april may june july august september october november december'.split(), 1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
MONTH = '|'.join(sorted(MONTHS, key=len, reverse=True))
WORD_DATE = re.compile(rf'\b(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH})|'
                       rf'({MONTH})\s+(\d{{1,2}})(?:st|nd|rd|th)?)(?:[,\s]+(\d{{4}}))?\b', re.I)
CLOCK = re.compile(r'(?<![\d:])(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b|'
                   r'(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?!\d)', re.I)


def normalize_time(raw, received_at):
    if not raw:
        return None
    record = {'text': raw, 'start': None, 'end': None, 'precision': 'unresolved', 'question': None}
    text = raw.casefold()
    received = datetime.fromisoformat(received_at).astimezone(IST)
    iso = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
    numeric = re.search(r'\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b', text)
    named = WORD_DATE.search(text)
    clock = CLOCK.search(text)
    relative = re.search(r'\b(today|yesterday|last night|this morning)\b', text)
    if len(list(CLOCK.finditer(text))) > 1 or re.search(r'\b(?:from|between)\b.+\b(?:to|and)\b', text):
        return {**record, 'question': 'What date and time range should this event use?'}
    if re.search(r'\b(?:utc|gmt|pst|est)\b|[+-]\d{2}:\d{2}\b', text):
        return {**record, 'question': 'Which local date and time should this event use?'}
    try:
        if iso:
            day = datetime(*map(int, iso.groups())).date()
        elif numeric:
            first, second, year = map(int, numeric.groups())
            if year < 1000 or (first <= 12 and second <= 12 and first != second):
                return {**record, 'question': 'Which calendar date did the reporter mean?'}
            month, date = (first, second) if second > 12 else (second, first)
            day = datetime(year, month, date).date()
        elif named:
            if not named[5]:
                return {**record, 'question': 'Which year did the reporter mean?'}
            day = datetime(int(named[5]), MONTHS[named[2] or named[3]], int(named[1] or named[4])).date()
        elif relative:
            day = received.date() - timedelta(days=relative[1] in ('yesterday', 'last night'))
        else:
            return {**record, 'precision': 'undated' if clock else 'unresolved',
                    'question': 'Which date did this happen on?' if clock else 'When did this happen?'}
    except ValueError:
        return {**record, 'question': 'What is the correct calendar date?'}

    if clock:
        if clock[3]:
            if not 1 <= int(clock[1]) <= 12:
                return {**record, 'question': 'What is the correct time of day?'}
            hour = int(clock[1]) % 12 + (12 if clock[3] == 'p' else 0)
            minute = int(clock[2] or 0)
        else:
            hour, minute = int(clock[4]), int(clock[5])
        if hour < 6 and re.search(r'\bnight\b', text):
            if relative and relative[1] == 'last night':
                day += timedelta(days=1)
            else:
                return {**record, 'question': 'Was the after-midnight event on the named date or the next date?'}
        center = datetime.combine(day, time(hour, minute), tzinfo=IST)
        approximate = bool(re.search(r'\b(?:about|around|approximately|roughly)\b', text))
        radius = timedelta(minutes=30 if approximate else 0)
        return {**record, 'start': (center - radius).isoformat(), 'end': (center + radius).isoformat(),
                'precision': 'approximate' if approximate else 'exact'}

    if re.search(r'\b\d{1,2}\s*(?:o.clock|in the evening|at night|in the morning)\b', text):
        return {**record, 'question': 'What time of day did the reporter mean?'}
    start = datetime.combine(day, time.min, tzinfo=IST)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    precision = 'day'
    if relative and relative[1] == 'last night':
        start += timedelta(hours=18)
        end = start + timedelta(hours=12)
        precision = 'approximate'
    elif relative and relative[1] == 'this morning':
        end = start + timedelta(hours=12)
        precision = 'approximate'
    return {**record, 'start': start.isoformat(), 'end': end.isoformat(), 'precision': precision}


def disjoint(left, right):
    return bool(left and right and left['start'] and right['start'] and (
        left['end'] < right['start'] or right['end'] < left['start']
    ))
