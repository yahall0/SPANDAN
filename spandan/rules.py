import collections
import hashlib
import json
import re
import unicodedata

import regex

RULES_VERSION = 'typed-anchors-v1'
TOKEN = re.compile(r'⟦(?:ID|EMAIL|PHONE|VEHICLE|ACCOUNT|DOB|TERM)\d+⟧')
GLOSSARY = {'चोरी': 'theft', 'धमकी': 'threat', 'पंचनामा': 'panchnama',
            'ચોરી': 'theft', 'ધમકી': 'threat', 'પંચનામું': 'panchnama'}
IDENTIFIERS = {
    'EMAIL': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
    'VEHICLE': r'\b[A-Za-z]{2}\s*\d{2}\s*[A-Za-z]{1,3}\s*\d{4}\b',
    'DOB': r'(?:\b(?:date of birth|DOB)\b|जन्म\s*तिथि|જન્મ\s*તારીખ)\s*[:\-]?\s*\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}',
    'ACCOUNT': r'(?:\b(?:account|acct)(?:\s*(?:number|no\.?))?|खाता(?:\s*नंबर)?|ખાતા(?:\s*નંબર)?)\s*[:#-]?\s*(?P<value>\d(?:[ -]?\d){5,19})(?![ -]?\d)',
    'ID': r'(?<!\d)\d(?:[ -]?\d){11}(?![ -]?\d)',
    'PHONE': r'(?<![\d+])(?:\+91[ -]?)?\d(?:[ -]?\d){9}(?![ -]?\d)',
}
MARKERS = {
    'polarity': ('नहीं|नही|मत|बिना|न', 'નથી|નહીં|નહિ|વગર|ન', "not|never|no|without|cannot|can't|couldn't|didn't|don't|doesn't|wasn't|weren't|neither|nor|unable"),
    'approximation': ('लगभग|करीब|तकरीबन|आसपास', 'લગભગ|આશરે|આસપાસ', 'approximately|about|around|roughly|or so'),
    'uncertainty': ('शायद|संभवतः|मुझे लगता है|मुझे शक है', 'કદાચ|સંભવતઃ|મને લાગે છે|મને શંકા છે', r"maybe|perhaps|possibly|might|may|I think|I suspect|I am unsure|I'm unsure|I am not sure"),
    'reported speech': ('बताया कि|कहा कि|बताया|कहा|सुना', 'જણાવ્યું કે|કહ્યું કે|જણાવ્યું|કહ્યું|સાંભળ્યું', 'told|said|reported|according to|heard'),
}
WORDS = {
    **dict(zip('शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस'.split(), range(21))),
    **dict(zip('શૂન્ય એક બે ત્રણ ચાર પાંચ છ સાત આઠ નવ દસ અગિયાર બાર તેર ચૌદ પંદર સોળ સત્તર અઢાર ઓગણીસ વીસ'.split(), range(21))),
    **dict(zip('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(), range(21))),
    **dict(zip('thirty forty fifty sixty seventy eighty ninety'.split(), range(30, 100, 10))),
    'पांच': 5, 'सौ': 100, 'સો': 100, 'hundred': 100, 'हजार': 1000, 'हज़ार': 1000, 'હજાર': 1000, 'thousand': 1000,
    'लाख': 100000, 'લાખ': 100000, 'lakh': 100000, 'करोड़': 10000000, 'કરોડ': 10000000, 'crore': 10000000,
    'तीस': 30, 'ચાળીશ': 40, 'ચાળીસ': 40, 'चालीस': 40, 'पचास': 50, 'પચાસ': 50,
}
MONTHS = dict(zip('january february march april may june july august september october november december'.split(), range(1, 13)))
MONTHS.update(zip('जनवरी फ़रवरी मार्च अप्रैल मई जून जुलाई अगस्त सितंबर अक्टूबर नवंबर दिसंबर'.split(), range(1, 13)))
MONTHS.update(zip('જાન્યુઆરી ફેબ્રુઆરી માર્ચ એપ્રિલ મે જૂન જુલાઈ ઑગસ્ટ સપ્ટેમ્બર ઑક્ટોબર નવેમ્બર ડિસેમ્બર'.split(), range(1, 13)))
MONTHS.update({'फरवरी': 2, 'सितम्बर': 9, 'अक्तूबर': 10, 'ઓગસ્ટ': 8, 'ઓક્ટોબર': 10, 'ડી​​સેમ્બર': 12})
MONTH_TOKEN = '|'.join(map(re.escape, MONTHS))
NUMBER_TOKEN = '|'.join(sorted(map(re.escape, WORDS), key=len, reverse=True))
CLOCK_SUFFIX = r'(?:a\.?m\.?|p\.?m\.?|o.clock|बजे|વાગ્યે|વાગ્યા)'
NUMBER = r'\d+(?:[,.]\d+)*(?:st|nd|rd|th)?'
BOUNDARY = r'[\p{L}\p{M}\p{N}]'
QUANTITY = regex.compile(fr'(?<!{BOUNDARY})(?:{NUMBER}|{NUMBER_TOKEN})(?!{BOUNDARY})', regex.I)
CURRENCIES = r'₹|Rs\.?|INR|rupees?|रुपये|रुपए|रुपया|रु\.?|રૂપિયા|રૂપિયો'


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    return hashlib.sha256(value).hexdigest()


class Masker:
    def __init__(self, sources):
        self.vault = {}
        self.values = {}
        self.counts = collections.Counter()
        self.glossary = {}
        for source in sources:
            self.mask(source)

    def _put(self, kind, value, render=None):
        key = (kind, value)
        if key not in self.values:
            self.counts[kind] += 1
            token = f'⟦{kind}{self.counts[kind]}⟧'
            self.values[key] = token
            self.vault[token] = value
            if render is not None:
                self.glossary[token] = render
        return self.values[key]

    def mask(self, text):
        for kind, pattern in IDENTIFIERS.items():
            def replace(match):
                value = match.groupdict().get('value') or match.group()
                return match.group().replace(value, self._put(kind, value), 1)
            text = re.sub(pattern, replace, text, flags=re.I)
        for term, translation in GLOSSARY.items():
            text = regex.sub(fr'(?<!{BOUNDARY}){term}(?!{BOUNDARY})',
                             lambda match: self._put('TERM', match.group(), translation), text)
        return text

    def restore(self, text):
        return TOKEN.sub(lambda m: self.glossary.get(m[0], self.vault.get(m[0], m[0])), text)


def _ascii_digits(text):
    return ''.join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in text)


def _number(text):
    text = re.sub(r'(?:st|nd|rd|th)$', '', text)
    text = text.replace(',', '')
    if '.' in text:
        return text.rstrip('0').rstrip('.')
    return str(int(text))


def _word_number(parts):
    total = current = 0
    for part in parts:
        if part == 100:
            current = max(1, current) * 100
        elif part >= 1000:
            total += max(1, current) * part
            current = 0
        else:
            current += part
    return str(total + current)


def value_anchors(text):
    text = _ascii_digits(TOKEN.sub(' ', text)).casefold()
    text = re.sub(r'(?<=\d)\.(?=\d{2}\s*(?:[ap]\.?m\.?))', ':', text)
    anchors = []
    # Recognize explicit roles first. Each matched span is consumed exactly once.
    date_word = fr'(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_TOKEN})(?:[\s,]+(\d{{4}}))?(?!{BOUNDARY})'
    month_first = fr'(?<!{BOUNDARY})({MONTH_TOKEN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:[\s,]+(\d{{4}}))?(?!{BOUNDARY})'
    clock = fr'(?<!\d)(\d{{1,2}})(?::(\d{{2}}))?\s*({CLOCK_SUFFIX})(?!{BOUNDARY})'
    clock24 = r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)'
    numeric_date = r'(?<!\d)(\d{1,4})[/\-](\d{1,2})[/\-](\d{2,4})(?!\d)'

    def named_date(match, day, month):
        return f'{match[3] or "?"}-{MONTHS[match[month]]:02}-{int(match[day]):02}'

    def raw_date(match):
        year, month, day = (match[1], match[2], match[3]) if len(match[1]) == 4 else (match[3], match[2], match[1])
        return f'{year}-{int(month):02}-{int(day):02}'

    def time_value(match):
        hour = int(match[1])
        suffix = match[3].replace('.', '')
        context = text[max(0, match.start() - 36):min(len(text), match.end() + 20)]
        if suffix in ('am', 'pm'):
            hour = hour % 12 + (12 if suffix == 'pm' else 0) if 1 <= hour <= 12 else -1
        elif 1 <= hour <= 12:
            if regex.search(r'शाम|संध्या|दोपहर|સાંજ|બપોર|in the evening|in the afternoon', context):
                hour = hour % 12 + 12
            elif regex.search(r'रात|રાત|at night', context):
                hour = hour % 12 + (12 if hour >= 6 and hour != 12 else 0)
            elif regex.search(r'सुबह|सवेरे|સવારે|in the morning', context):
                hour = hour % 12
            else:
                return f'{hour:02}:{match[2] or "00"} (period unspecified)'
        return f'{hour:02}:{match[2] or "00"}'

    used = [False] * len(text)
    for role, pattern, convert in (
        ('date', date_word, lambda m: named_date(m, 1, 2)),
        ('date', month_first, lambda m: named_date(m, 2, 1)),
        ('date', numeric_date, raw_date),
        ('time', clock, time_value),
        ('time', clock24, lambda m: f'{int(m[1]):02}:{m[2]}'),
        ('amount', fr'(?:{CURRENCIES})\s*({NUMBER})(?!{BOUNDARY})', lambda m: _number(m[1])),
        ('amount', fr'(?<!{BOUNDARY})({NUMBER})\s*(?:{CURRENCIES})(?!{BOUNDARY})', lambda m: _number(m[1])),
    ):
        for match in regex.finditer(pattern, text):
            start, end = match.span()
            if not any(used[start:end]):
                anchors.append((start, role, convert(match)))
                used[start:end] = [True] * (end - start)

    rest = ''.join(' ' if used[i] else character for i, character in enumerate(text))
    pending = []
    pending_end = 0
    for match in QUANTITY.finditer(rest):
        word = match[0]
        gap = text[pending_end:match.start()]
        continuation = pending and regex.fullmatch(r'[\s-]*(?:and\s+)?', gap) and (
            WORDS.get(word, 0) >= 100 or pending[-1][1] >= 20
        )
        if pending and not continuation:
            anchors.append((pending[0][0], 'quantity', _word_number([value for _, value in pending])))
            pending = []
        if word in WORDS:
            pending.append((match.start(), WORDS[word]))
            pending_end = match.end()
        else:
            anchors.append((match.start(), 'quantity', _number(word)))
    if pending:
        anchors.append((pending[0][0], 'quantity', _word_number([value for _, value in pending])))
    return [(role, value) for _, role, value in sorted(anchors)]


def marker_anchors(text):
    markers = []
    text = text.casefold().replace('’', "'")
    for category, terms in MARKERS.items():
        for match in regex.finditer(fr'(?<!{BOUNDARY})(?:{"|".join(terms)})(?!{BOUNDARY})', text):
            markers.append((match.start(), category))
    return [category for _, category in sorted(markers)]


def check_anchors(source, candidate):
    expected_values, actual_values = value_anchors(source), value_anchors(candidate)
    expected_markers, actual_markers = marker_anchors(source), marker_anchors(candidate)
    return [
        {'name': 'Values and roles', 'ok': expected_values == actual_values,
         'detail': 'Numbers, amounts, dates and times retain their role and order.',
         'expected': expected_values, 'observed': actual_values, 'slot': 'quantity'},
        {'name': 'Meaning markers', 'ok': expected_markers == actual_markers,
         'detail': 'Negation, approximation, uncertainty and reported-speech markers match in order.',
         'expected': expected_markers, 'observed': actual_markers, 'slot': next(
             (slot for label, slot in [('polarity', 'polarity'), ('reported speech', 'evidentiality'),
                                       ('uncertainty', 'certainty'), ('approximation', 'certainty')]
              if expected_markers.count(label) != actual_markers.count(label)), 'statement')},
    ]
