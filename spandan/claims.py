import logging
import re
import time
from typing import Literal

from pydantic import Field

from .models import NLI_REVISION, ModelError, Response
from .rules import digest, marker_anchors, value_anchors

VERSION = 'claims-v3'
MAX_REPAIRS = 2
NLI_THRESHOLD = 0.75
LOGGER = logging.getLogger('spandan.claims')
REPORTED = re.compile(r'\b(?:told (?:me|us)|said that|heard from|according to|reported that|'
                      r'informed (?:me|us)|claimed that)\b', re.I)
TOLD = re.compile(r'^\s*([^,.;!?]{1,110}?)\s+(?:told|informed)\s+(?:me|us)\b', re.I)
FIRSTHAND = re.compile(r'\bI\s+(?:did not\s+|didn.t\s+|could not\s+|couldn.t\s+)?'
                       r'(?:see|saw|find|found|notice|noticed|witness|witnessed|observe|observed)\b', re.I)
EXPLICIT_SUSPICION = re.compile(r'^\s*I\s+(?:suspect(?:ed)?|believe|think)\b', re.I)
LOCATIVE = re.compile(r'\b(?:near|outside|inside|at|on|behind|beside|under|above|from|into|in|by)\s+(?:the\s+)?', re.I)
SUSPICION = re.compile(r'\b(?:suspect(?:ed|s)?|believe|think|might have|may have)\b', re.I)
OPINION = re.compile(r'\b(?:in my opinion|in my view|i consider)\b', re.I)
UNIDENTIFIED = re.compile(r'^(?:someone|somebody|unknown(?: person| man| woman)?|(?:an? )?(?:unknown )?(?:man|woman|person))$', re.I)
PRONOUN = re.compile(r'^(?:he|she|they|him|her|them|it|his|hers|their)$', re.I)
SELF = {'i', 'me', 'myself', 'the complainant', 'the reporter'}
IRREGULAR = {'saw': 'see', 'seen': 'see', 'took': 'take', 'taken': 'take', 'told': 'tell', 'said': 'say',
             'heard': 'hear', 'went': 'go', 'was': 'be', 'were': 'be', 'had': 'have', 'did': 'do',
             'ran': 'run', 'left': 'leave', 'gave': 'give', 'found': 'find', 'stole': 'steal',
             'broken': 'break', 'broke': 'break', 'lost': 'lose', 'paid': 'pay', 'came': 'come',
             'identified': 'identify', 'recognized': 'recognize', 'called': 'call', 'parked': 'park',
             'reported': 'report', 'returned': 'return', 'carried': 'carry', 'threatened': 'threaten'}

Kind = Literal['event', 'state', 'identity', 'statement', 'suspicion', 'opinion', 'contact_detail']


class Candidate(Response):
    quote: str = Field(min_length=1, max_length=1200)
    text: str = Field(min_length=1, max_length=500)
    kind: Kind
    actor: str | None = Field(max_length=180)
    act: str = Field(min_length=1, max_length=80)
    object: str | None = Field(max_length=180)
    place: str | None = Field(max_length=180)
    time: str | None = Field(max_length=120)
    quantity: str | None = Field(max_length=100)
    knowledge: str | None = Field(max_length=180)
    evidentiality: Literal['direct', 'reported', 'inferred', 'unspecified']
    polarity: Literal['affirmed', 'negated']
    certainty: Literal['certain', 'approximate', 'uncertain', 'unspecified']


class Extraction(Response):
    id: str
    claims: list[Candidate] = Field(max_length=12)


class Batch(Response):
    passages: list[Extraction] = Field(max_length=5)


class Judgment(Response):
    id: str
    supported: bool
    atomic: bool
    fields_supported: bool
    reason: str = Field(min_length=1, max_length=250)


class Judgments(Response):
    verdicts: list[Judgment] = Field(max_length=20)


EXTRACT = (
    'Extract every atomic claim in each checked English passage. Return one passage result with the exact input id. '
    'Copy a continuous verbatim quote, preserving case, punctuation and spaces. Each claim describes exactly one '
    'fact, allegation or explicitly attributed statement. The text field is one plain English sentence which the quote '
    'alone supports, preserving negation, uncertainty, dates, quantities, pronouns and the reporter’s perspective. '
    'For actor, object, place, time, quantity, and knowledge, copy a short exact phrase from the quote or use null. '
    'Use a single plain base verb for act. Keep an unnamed person unnamed. Keep the reported account as a statement '
    'with reported evidentiality and the person who told the reporter in knowledge. An explicitly expressed suspicion '
    'is kind suspicion and certainty uncertain. For ordinary assertions without a certainty marker use unspecified. '
    'Use direct for firsthand perception or action, reported for something the reporter was told, inferred for the '
    'reporter’s own stated inference and unspecified when the basis is absent. Never infer an identity, intention, '
    'actor, location, event connection or timeline order. Do not add a claim from another passage to this passage. '
    'Use JSON null, never the string "null". Use knowledge only for reported speech. Never put a reporter name '
    'in a quote field unless the name occurs in the quote. '
    'Example input passage id H12: "I found a green wallet." Example output: '
    '{"passages":[{"id":"H12","claims":[{"quote":"I found a green wallet.","text":"I found a green wallet.",'
    '"kind":"event","actor":"I","act":"find","object":"a green wallet","place":null,"time":null,"quantity":null,'
    '"knowledge":null,"evidentiality":"direct","polarity":"affirmed","certainty":"unspecified"}]}]}.'
)
VERIFY = (
    'Independently judge each claim using its exact quote and stated reporter. Return its id and a short reason. '
    'Supported means the quote entails the claim including all qualifiers and attribution. '
    'Atomic means it expresses a single fact or single attributed report. Fields supported means the structured '
    'actor, act, object, place, time, quantity, knowledge, polarity, certainty and evidentiality agree with the quote. '
    'If reference_context is supplied, also check every proposed pronoun referent against that exact context. '
    'Reject added intent, guessed pronoun referents, dropped doubt or negation, converted hearsay, invented specificity '
    'and omitted material facts within this claim. Judge what the reporter says, without speculating about whether '
    'the reporter is accurate or whether an observation is objectively provable. '
    'The pronoun I denotes the reporter in each claim. If unsure set the relevant flag to false. '
    'An indefinite article or singular noun does not require a numeric quantity. An actor described only as '
    '"someone" may remain unidentified. Exact wording with every qualifier present can support the same wording. '
    'An omitted optional field is fine when the quote does not supply that field. '
    'Example: for id C1, quote and text both "I found a green wallet.", actor "I", act "find", object '
    '"a green wallet", direct and affirmed, return '
    '{"verdicts":[{"id":"C1","supported":true,"atomic":true,"fields_supported":true,"reason":"Direct observation."}]}.'
)


def key(value):
    return re.sub(r'\s+', ' ', (value or '').casefold().strip(' \t\r\n.,!?;:"“”'))


def actor_key(value):
    return re.sub(r'^(?:the|an?|my)\s+', '', key(value))


def action_key(value):
    word = re.sub(r'^(?:to|not|never)\s+', '', key(value)).split(' ', 1)[0]
    if word in IRREGULAR:
        return IRREGULAR[word]
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    return word[:-1] if len(word) > 3 and word.endswith('s') else word


def _slot_agrees(slot, frame_value, candidate_value):
    left, right = actor_key(frame_value), actor_key(candidate_value)
    if not left:
        return True
    if not right:
        return False
    if slot == 'actor' and {key(frame_value), key(candidate_value)} <= SELF:
        return True
    if slot == 'quantity' and value_anchors(left):
        return value_anchors(left) == value_anchors(right)
    if slot == 'place':
        return actor_key(LOCATIVE.sub('', left, count=1)) == actor_key(LOCATIVE.sub('', right, count=1))
    if slot == 'time':
        return re.sub(r'^(?:on|at)\s+', '', left) == re.sub(r'^(?:on|at)\s+', '', right)
    return left == right


def _literal_in(value, quote):
    return bool(key(value) and re.search(rf'(?<!\w){re.escape(key(value))}(?!\w)', key(quote)))


def _source_quote(proposed, source):
    if proposed in source:
        return proposed
    words = proposed.split()
    if not words:
        return proposed
    pattern = r'(?<!\w)' + r'\s+'.join(map(re.escape, words)) + r'(?!\w)'
    matches = list(re.finditer(pattern, source))
    return matches[0][0] if len(matches) == 1 else proposed


def _frame_slot(frame, slot):
    value = frame.get(slot)
    return None if key(value) == 'null' and not _literal_in(value, frame['evidence']) else value


def _reference_context(candidate, item):
    pronouns = [slot for slot in ('actor', 'object', 'knowledge')
                if PRONOUN.fullmatch(key(getattr(candidate, slot)))]
    if len(pronouns) != 1 or key(getattr(candidate, pronouns[0])) not in ('he', 'she', 'him', 'her'):
        return {}
    start = item['english'].find(candidate.quote)
    if start <= 0:
        return {}
    previous = [(item['english'].rfind(f['evidence'], 0, start), f) for f in item['frames']]
    if not any(index >= 0 for index, _ in previous):
        return {}
    index, frame = max(previous, key=lambda entry: entry[0])
    options = {}
    for value in (frame.get('actor'), frame.get('object')):
        normalized = actor_key(value)
        if not normalized or key(value) in SELF or PRONOUN.fullmatch(key(value)):
            continue
        mention = re.search(rf'(?<!\w){re.escape(normalized)}(?!\w)', frame['evidence'], re.I)
        role = re.fullmatch(r'(?:unknown )?(?:man|woman|person|neighbor|neighbour|clerk|guard|watchman|witness)', normalized)
        name = mention and re.fullmatch(r'[A-Z][a-z]{2,}(?: [A-Z][a-z]{2,})?', mention[0])
        if mention and (role or name):
            options[normalized] = mention[0]
    if len(options) != 1:
        return {}
    label = next(iter(options.values()))
    return {pronouns[0]: {'label': label, 'quote': frame['evidence'], 'quote_start': index}}


def rule_checks(candidate, item, references=None):
    references = references or {}
    quote = candidate.quote
    markers = marker_anchors(quote)
    stated = [(index, f) for index, f in enumerate(item['frames'])
              if key(f['evidence']) and (key(quote) in key(f['evidence']) or key(f['evidence']) in key(quote))]
    matching = [(index, f) for index, f in stated
                if action_key(candidate.act) == action_key(f['act']) or
                (candidate.kind == 'statement' and candidate.evidentiality == 'reported' and
                 action_key(candidate.act) in {'say', 'tell', 'hear', 'report'})]
    if candidate.kind in {'contact_detail', 'suspicion', 'opinion'} and not matching and len(stated) == 1:
        matching = stated
    supported_frame = len(matching) == 1
    frame = matching[0][1] if supported_frame else {}
    quote_anchors = value_anchors(quote)
    candidate_anchors = value_anchors(candidate.text)
    required_evidentiality = frame.get('evidentiality')
    referenced = [getattr(candidate, slot) for slot in ('actor', 'object', 'place', 'time', 'quantity', 'knowledge')]
    status = []

    def check(name, ok, reason, hard=False):
        status.append({'name': name, 'ok': bool(ok), 'reason': reason, 'hard': hard})

    check('Exact quote', quote in item['english'], 'Evidence must occur verbatim in the checked passage.', True)
    check('Grounded fields', all(value is None or _literal_in(value, quote) for value in referenced),
          'Actor, object, place, time, quantity and knowledge must be grounded in the quote.', True)
    check('Polarity', ('polarity' in markers) == (candidate.polarity == 'negated') and
          (not frame or frame['polarity'] == candidate.polarity), 'Negation must agree with the quote and intake.')
    check('Evidentiality', (not REPORTED.search(quote) or candidate.evidentiality == 'reported') and
          (not required_evidentiality or required_evidentiality == candidate.evidentiality),
          'Hearsay and firsthand evidence must stay distinct.')
    check('Knowledge source', candidate.evidentiality != 'reported' or bool(candidate.knowledge),
          'Identify who supplied the reported information.')
    check('Suspicion and opinion', bool(SUSPICION.search(quote)) == (candidate.kind == 'suspicion') and
          (candidate.kind != 'suspicion' or (candidate.certainty == 'uncertain' and SUSPICION.search(candidate.text))) and
          (not OPINION.search(quote) or (candidate.kind == 'opinion' and OPINION.search(candidate.text))),
          'Keep an explicitly expressed suspicion or opinion attributed in its wording.', True)
    check('Precision and values', quote_anchors == candidate_anchors and
          ('approximation' not in markers or ('approximation' in marker_anchors(candidate.text) and
           candidate.certainty == 'approximate')) and
          ('uncertainty' not in markers or 'uncertainty' in marker_anchors(candidate.text)),
          'Numbers, dates, amounts and stated uncertainty must retain their precision.')
    differences = []
    if supported_frame:
        for slot in ('actor', 'object', 'place', 'time', 'quantity'):
            if candidate.kind == 'statement' and slot in ('actor', 'object'):
                continue
            expected = _frame_slot(frame, slot)
            observed = references[slot]['label'] if slot in references else getattr(candidate, slot)
            if not _slot_agrees(slot, expected, getattr(candidate, slot)) and not _slot_agrees(slot, expected, observed):
                differences.append({'field': slot, 'intake': expected, 'claim': observed})
    if not supported_frame:
        reason = ('The quote did not match a checked intake statement.' if not stated else
                  'The claim could not be matched to one intake statement with the same action.')
    elif differences:
        reason = 'Intake and claim extraction read these fields differently: ' + ', '.join(d['field'] for d in differences) + '.'
    else:
        reason = 'The claim matches one intake statement and its material fields.'
    check('Intake agreement', supported_frame and not differences, reason)
    if differences:
        status[-1]['differences'] = differences
    check('Reference ambiguity', not any(PRONOUN.fullmatch(key(getattr(candidate, slot))) and slot not in references
                                        for slot in ('actor', 'object', 'knowledge')),
          'A pronoun needs an explicit referent or an officer decision.')
    check('One fact', len(stated) <= 1, 'The quote must not cover multiple independent intake statements.')
    return status, matching[0][0] if supported_frame else None


def _claim(item, candidate):
    raw = candidate
    quote = _source_quote(raw.quote, item['english'])
    fixes = {slot: None for slot in ('actor', 'object', 'place', 'time', 'quantity', 'knowledge')
             if key(getattr(raw, slot)) == 'null' and not _literal_in('null', quote)}
    if quote != raw.quote:
        fixes['quote'] = quote
    # Restore exact quoted qualifiers if the model returned a literal but shortened wording.
    if raw.text != quote and _literal_in(raw.text, quote):
        fixes['text'] = quote
    markers = marker_anchors(quote)
    if raw.time and 'approximation' in markers and 'approximation' in marker_anchors(raw.time):
        fixes['certainty'] = 'approximate'
    if EXPLICIT_SUSPICION.search(quote):
        fixes.update(kind='suspicion', evidentiality='inferred', certainty='uncertain')
        if raw.place and not any(_literal_in(raw.place, quote[m.end():]) for m in LOCATIVE.finditer(quote)):
            fixes['place'] = None
    else:
        told = TOLD.search(quote)
        if told and not PRONOUN.fullmatch(key(told[1])):
            fixes.update(kind='statement', evidentiality='reported', knowledge=told[1])
        elif FIRSTHAND.search(quote) and not REPORTED.search(quote):
            if raw.evidentiality == 'unspecified':
                fixes['evidentiality'] = 'direct'
            if raw.actor is None or not _literal_in(raw.actor, quote):
                fixes['actor'] = 'I'
        elif UNIDENTIFIED.fullmatch(key(raw.actor)) and not REPORTED.search(quote):
            fixes['evidentiality'] = 'unspecified'
    if fixes.get('evidentiality', raw.evidentiality) != 'reported' and raw.knowledge and not _literal_in(raw.knowledge, quote):
        fixes['knowledge'] = None
    candidate = raw.model_copy(update=fixes)
    references = _reference_context(candidate, item)
    checks, frame = rule_checks(candidate, item, references)
    start = item['english'].find(candidate.quote)
    identifier = digest([item['document_id'], item['id'], item['revision'], candidate.model_dump()])[:24]
    normalized = {
        'id': identifier, **candidate.model_dump(), 'checks': checks, 'frame_index': frame, 'references': references,
        'quote_start': start if start >= 0 else None,
        'quote_end': start + len(candidate.quote) if start >= 0 else None,
        'allegation': 'reported_account', 'status': 'escalated',
    }
    if candidate != raw:
        normalized['raw_candidate'] = raw.model_dump()
    return normalized


def _run(item):
    evidence = {k: item[k] for k in ('document_id', 'page', 'start', 'end', 'source', 'effective_source',
                                    'english', 'revision', 'received_at', 'reporter', 'validator_version',
                                    'checked_hash', 'sha256', 'format')}
    return {'id': digest([item['document_id'], item['id'], item['revision'], item['checked_hash'], VERSION])[:24],
            'passage_id': item['id'], 'evidence': evidence, 'frames': item['frames'], 'version': VERSION,
            'nli_version': NLI_REVISION, 'claims': [], 'escalations': [], 'attempts': []}


def _issue(run, name, detail, **fields):
    return {'id': digest([run['id'], name, fields])[:24], 'name': name, 'detail': detail, **fields}


class ClaimExtractor:
    def __init__(self, models):
        self.models = models

    def process(self, batch):
        start = time.monotonic()
        runs = {item['id']: _run(item) for item in batch}
        by_id = {item['id']: item for item in batch}
        todo = list(by_id)
        feedback = {}
        isolated = set()
        inventory = self.models.inventory()
        for number in range(1, MAX_REPAIRS + 2):
            if not todo:
                break
            results, issues, traces = {}, {}, {}
            together = [i for i in todo if i not in isolated]
            groups = ([together] if together else []) + [[i] for i in todo if i in isolated]
            for group in groups:
                trace = []
                try:
                    extracted, problems = self._attempt([by_id[i] for i in group], feedback, inventory, trace)
                except ModelError as error:
                    extracted = {i: [] for i in group}
                    problems = {i: [('Local model', str(error), {})] for i in group}
                results.update(extracted)
                issues.update(problems)
                traces.update({i: trace for i in group})
            repair = []
            for i in todo:
                run = runs[i]
                previous = {c['frame_index']: c for c in run['claims'] if c['status'] == 'verified'}
                incoming = [c for c in results[i] if c['frame_index'] not in previous]
                claims = [*previous.values(), *incoming]
                covered = {c['frame_index'] for c in claims if c['status'] == 'verified'}
                problems = issues[i]
                if any(name == 'Batch shape' for name, _, _ in problems):
                    isolated.add(i)
                missing = set(range(len(by_id[i]['frames']))) - covered
                if not problems:
                    for frame in sorted(missing - {c['frame_index'] for c in incoming}):
                        problems.append(('Frame coverage', 'An intake statement has no verified claim.',
                                         {'frame_index': frame}))
                if not claims and not problems:
                    problems.append(('Frame coverage', 'This passage has no checked claim.', {}))
                run['claims'] = claims
                run['escalations'] = [_issue(run, name, detail, **fields) for name, detail, fields in problems]
                run['attempts'].append({'round': number - 1, 'calls': traces[i], 'claims': results[i],
                                        'issues': run['escalations']})
                failures = [c for c in incoming if c['status'] != 'verified']
                if problems or failures:
                    feedback[i] = [{'quote': c['quote'], 'failures': [x['reason'] for x in c['checks'] if not x['ok']]}
                                   for c in failures] + [{'issue': detail} for _, detail, _ in problems]
                    repair.append(i)
            todo = repair
        elapsed = round((time.monotonic() - start) * 1000)
        for run in runs.values():
            run['batch_machine_ms'] = elapsed
            run['batch_size'] = len(batch)
        return runs

    def _attempt(self, items, feedback, inventory, trace):
        if not inventory.get(self.models.a) or not inventory.get(self.models.b) or (
            inventory[self.models.a] == inventory[self.models.b]
        ):
            raise ModelError('Claim extraction requires two distinct installed local models.')
        raw = self.models.chat(self.models.a, 'Claim extraction', EXTRACT, {'passages': [
            {'id': item['id'], 'english': item['english'],
             'repair_feedback': feedback.get(item['id'], [])} for item in items
        ]}, Batch, trace, inventory, 2700)
        results, issues = {}, {}
        ready = []
        for item in items:
            identifier = item['id']
            parts = [part for part in raw.passages if part.id == identifier]
            results[identifier] = []
            issues[identifier] = []
            if len(parts) != 1:
                issues[identifier].append(('Batch shape', 'The claim model did not return one usable result for this passage.', {}))
                continue
            claims = [_claim(item, candidate) for candidate in parts[0].claims]
            results[identifier] = list({claim['id']: claim for claim in claims}.values())
            ready.extend((item, claim) for claim in results[identifier] if all(check['ok'] for check in claim['checks']))
        for offset in range(0, len(ready), 6):
            self._verify(ready[offset:offset + 6], inventory, trace)
        for claims in results.values():
            by_frame = {}
            for claim in claims:
                if claim['status'] != 'verified':
                    continue
                frame = claim['frame_index']
                if frame in by_frame:
                    claim['checks'].append({'name': 'Duplicate frame', 'ok': False,
                                            'reason': 'Another verified claim covers the same intake frame.'})
                    claim['status'] = 'escalated'
                else:
                    by_frame[frame] = claim['id']
        return results, issues

    def _verify(self, candidates, inventory, trace):
        prepared = [{'id': f'C{i}',
                     **({'reference_context': claim['references']} if claim['references'] else {}),
                     **{k: claim[k] for k in Candidate.model_fields}} for i, (item, claim) in enumerate(candidates, 1)]
        try:
            scores = self.models.entailment([(claim['quote'], claim['text']) for claim in prepared])
            verified = self.models.chat(self.models.b, 'Independent claim verification', VERIFY,
                                        {'claims': prepared}, Judgments, trace, inventory, 1400)
        except ModelError as error:
            for _, claim in candidates:
                claim['checks'].append({'name': 'Local verification', 'ok': False, 'reason': str(error)})
            return
        for index, (_, claim) in enumerate(candidates):
            matches = [v for v in verified.verdicts if v.id == prepared[index]['id']]
            verdict = matches[0] if len(matches) == 1 else None
            score = scores[index]
            identical = key(claim['quote']) == key(claim['text'])
            claim['checks'].extend([
                {'name': 'One-way NLI', 'ok': identical or score['entailment'] >= NLI_THRESHOLD,
                 'reason': 'The quote must entail the claim.', 'scores': score, 'threshold': NLI_THRESHOLD},
                {'name': 'Independent verifier', 'ok': bool(verdict and verdict.supported and verdict.atomic
                                                           and verdict.fields_supported),
                 'reason': verdict.reason if verdict else 'The verifier did not return one usable verdict.'},
            ])
            if all(check['ok'] for check in claim['checks']):
                claim['status'] = 'verified'


def claim_worker(store, models, stopping, wake):
    from .claim_store import BATCH_SIZE, next_batch, save_batch

    extractor = ClaimExtractor(models)
    while not stopping.is_set():
        wake.clear()
        try:
            batch = next_batch(store)
            if batch:
                if len(batch) < BATCH_SIZE and not stopping.is_set():
                    stopping.wait(0.8)
                    batch = next_batch(store)
                if batch and not stopping.is_set():
                    try:
                        results = extractor.process(batch)
                    except Exception as error:
                        LOGGER.error('Claim batch failed internally (%s).', type(error).__name__)
                        results = {}
                        for item in batch:
                            run = _run(item)
                            run['escalations'] = [_issue(run, 'Internal error', 'Claim processing could not complete.')]
                            results[item['id']] = run
                    save_batch(store, batch, results)
            else:
                wake.wait(1)
        except Exception as error:
            LOGGER.error('Claim queue temporarily unavailable (%s).', type(error).__name__)
            stopping.wait(1)
