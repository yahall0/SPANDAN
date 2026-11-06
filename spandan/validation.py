import collections
import time
import unicodedata

import regex

from .intake import MAX_PASSAGE, script_spans
from .models import (NLI_REVISION, PROMPT_VERSION, Entailment, Frames, Language, ModelError,
                     Translation)
from .rules import RULES_VERSION, TOKEN, WORDS, check_anchors, digest, value_anchors

VALIDATOR_VERSION = 'typed-validator-v1'
REQUIRED_LAYERS = {'L0', 'L1', 'L2', 'L3'}
NLI_THRESHOLD = 0.8
CRITICAL = ('speaker', 'evidentiality', 'polarity', 'actor', 'act', 'object', 'quantity')
MAJOR = ('certainty', 'time', 'place', 'manner', 'tense')
SLOTS = CRITICAL + MAJOR
LANGUAGE_MINIMUM = 0.85
LANGUAGES = {'en', 'hi', 'gu', 'bn', 'mr', 'pa', 'ur', 'ta', 'te', 'kn', 'ml', 'or',
             'as', 'ks', 'kok', 'ne', 'sa', 'sd', 'mai', 'doi', 'brx', 'mni', 'sat', 'si', 'mul'}

QUESTIONS = {
    'en': {
        'language': 'Which language is this passage written in?',
        'speaker': 'Who made this statement?',
        'evidentiality': 'Did the complainant witness this directly, or did someone tell them? If told, by whom?',
        'polarity': 'Does this passage say the event happened, or that it did not happen?',
        'actor': 'Who performed the action described here?',
        'act': 'What action does the complainant say occurred?',
        'object': 'What person or item was affected?',
        'quantity': 'What is the exact number or amount, and what does it refer to?',
        'certainty': 'Was the complainant certain, estimating, or unsure?',
        'time': 'When did this happen, and was the time exact or approximate?',
        'place': 'Where exactly does the complainant say this happened?',
        'manner': 'How does the complainant say the action happened?',
        'tense': 'Did this already happen, is it happening now, or is it expected later?',
        'statement': 'What exactly did the complainant mean in this passage?',
        'service': 'Can the passage be processed again once the local models are available?',
        'length': 'Can you restate this passage concisely while keeping all material details?',
    },
    'hi': {
        'language': 'यह अंश किस भाषा में लिखा गया है?',
        'speaker': 'यह कथन किसने दिया?',
        'evidentiality': 'क्या शिकायतकर्ता ने यह स्वयं देखा या किसी ने बताया? किसने?',
        'polarity': 'क्या शिकायतकर्ता के अनुसार यह घटना हुई थी या नहीं?',
        'actor': 'यह काम किसने किया?',
        'act': 'शिकायतकर्ता के अनुसार क्या हुआ?',
        'object': 'किस व्यक्ति या वस्तु पर इसका असर पड़ा?',
        'quantity': 'सही संख्या या रकम क्या है, और वह किसकी है?',
        'certainty': 'क्या शिकायतकर्ता को यकीन है, या यह अंदाज़ा है?',
        'time': 'यह कब हुआ, और क्या समय निश्चित है या अनुमानित?',
        'place': 'शिकायतकर्ता के अनुसार यह कहाँ हुआ?',
        'manner': 'शिकायतकर्ता के अनुसार यह किस तरह हुआ?',
        'tense': 'क्या यह हो चुका है, अभी हो रहा है, या आगे होने की बात कही गई है?',
        'statement': 'इस अंश में शिकायतकर्ता का ठीक-ठीक क्या मतलब था?',
        'service': 'स्थानीय मॉडल उपलब्ध होने पर क्या इस अंश की फिर से जाँच की जा सकती है?',
        'length': 'क्या सभी ज़रूरी बातें रखते हुए इस अंश को कम शब्दों में बता सकते हैं?',
    },
    'gu': {
        'language': 'આ અંશ કઈ ભાષામાં લખાયો છે?',
        'speaker': 'આ નિવેદન કોણે આપ્યું?',
        'evidentiality': 'ફરિયાદીએ આ જાતે જોયું કે કોઈએ જણાવ્યું? કોણે?',
        'polarity': 'ફરિયાદી પ્રમાણે આ ઘટના બની હતી કે નહોતી બની?',
        'actor': 'આ કામ કોણે કર્યું?',
        'act': 'ફરિયાદી પ્રમાણે શું થયું?',
        'object': 'કઈ વ્યક્તિ કે વસ્તુ પર તેની અસર થઈ?',
        'quantity': 'ચોક્કસ સંખ્યા કે રકમ કેટલી છે અને તે શેની છે?',
        'certainty': 'ફરિયાદીને ખાતરી છે કે આ માત્ર અંદાજ છે?',
        'time': 'આ ક્યારે બન્યું, અને સમય ચોક્કસ છે કે અંદાજિત?',
        'place': 'ફરિયાદી પ્રમાણે આ ક્યાં બન્યું?',
        'manner': 'ફરિયાદી પ્રમાણે આ કેવી રીતે બન્યું?',
        'tense': 'શું આ બની ગયું છે, અત્યારે બની રહ્યું છે કે પછી બનવાની વાત છે?',
        'statement': 'આ અંશમાં ફરિયાદીનો ચોક્કસ અર્થ શું હતો?',
        'service': 'સ્થાનિક મોડલ ઉપલબ્ધ થાય ત્યારે આ અંશ ફરી ચકાસી શકાય?',
        'length': 'બધી જરૂરી વિગતો રાખીને આ અંશ ટૂંકામાં કહી શકો?',
    },
}

LANGUAGE_PROMPT = (
    'Classify the language of the quoted sentence. Use en for English, hi for Hindi, gu for Gujarati, '
    'mr for Marathi, ne for Nepali, bn for Bengali, or another listed code. '
    'Examples: "I saw it" => en; "मैंने उसे देखा" and "maine usko dekha" => hi; '
    '"મેં તેને જોયો" => gu; "मी त्याला पाहिले" => mr; "मैले उसलाई देखेँ" => ne. '
    'मैंने and को indicate Hindi; मैले and लाई indicate Nepali. Use mul only for distinct languages '
    'mixed in the same passage and und if uncertain. '
    'Use confidence 0.95 when clear or 0.5 when uncertain.'
)
TRANSLATION_PROMPT = (
    'Translate only the source into faithful English. Return the same passage id. '
    'Keep each ⟦TAG1⟧-style token exactly as written, at each occurrence. Do not expand it. '
    'Transliterate proper names. Preserve speaker, tense, roles, amounts, dates, negation, '
    'reported speech, manner, doubt and ambiguity. Translate quantities into Arabic digits where possible. '
    'Use any neighbor text only to resolve references already clear in the source. Never copy neighbor facts, '
    'interpret what the law says, add explanations or complete gaps. For an ambiguous expression keep it ambiguous. '
    'Gujarati vocabulary examples: જોયા means saw, લઈ ગયા means took away, સાંભળ્યું means heard. '
    'Hindi vocabulary examples: देखा means saw, सुना means heard, ले गया means took away.'
)
FRAME_PROMPT = (
    'Extract every atomic material statement from this one passage. Evidence must be an exact continuous '
    'quote from the input passage for each statement. Preserve all clauses, including who said or heard what. '
    'Keep only the evidence quote in the original language. ALL other text fields must be in English, '
    'even for Hindi or Gujarati evidence. Do not include original-script words or parenthetical glosses in slots. '
    'Use short plain English, without extra articles. Never guess a value. '
    'Use null for every slot that is not explicitly stated. Time is a stated date or time, never verb tense. '
    'Place is a stated location. Manner is an explicit description of how, such as by force. '
    'Certainty is null unless a word expressly signals certainty, approximation or doubt. '
    'Evidentiality is direct for firsthand perception or action (saw, देखा, જોયા), '
    'reported for a fact attributed to someone else (was told, बताया, કહ્યું), '
    'inferred for a conclusion drawn from other facts, or null if the basis is absent. '
    'Use I for the first-person speaker, the base verb for act, and digits for quantities. '
    'In object leave out quantities already in the quantity field. Keep ⟦TAG1⟧-style tokens unchanged. '
    'Example for "I saw three bicycles": {"statements":[{"evidence":"I saw three bicycles",'
    '"speaker":"I","evidentiality":"direct","polarity":"affirmed","tense":"past","actor":"I","act":"see","object":"bicycles",'
    '"quantity":"3","certainty":null,"time":null,"place":null,"manner":null}],"ambiguous":false}. '
    'For Hindi "मैंने लाल गाड़ी देखी" or Gujarati "મેં લાલ ગાડી જોઈ", quote the original text in evidence. '
    'In both cases speaker and actor are I, evidentiality is direct, act is see, object is red car, '
    'tense is past and quantity is null. The Hindi दो and Gujarati બે are quantity "2". '
    'Mark ambiguous true for unresolved factual ambiguity or unreadable wording. A stated uncertainty alone is not ambiguity.'
)


def _compact(text):
    return regex.sub(r'\s+', ' ', text.strip())


def _key(value):
    if value is None:
        return ''
    value = unicodedata.normalize('NFKC', value).casefold().replace('’', "'")
    value = regex.sub(r'^(?:the|a|an)\s+', '', value.strip())
    return _compact(value.strip('.,;:!?'))


def _check(name, ok, detail, slot='statement', **evidence):
    return {'name': name, 'ok': ok, 'detail': detail, 'slot': slot, **evidence}


def _layer(record, layer, *checks):
    record['checks'].extend({'layer': layer, **check} for check in checks)
    return all(check['ok'] for check in checks)


def passed_all_layers(result):
    if result.get('verdict') != 'pass' or not result.get('checked_english') or not result.get('attempts'):
        return False
    last = result['attempts'][-1]
    checks = last['checks']
    return last['verdict'] == 'pass' and all(check['ok'] for check in checks) and REQUIRED_LAYERS.issubset(
        {check['layer'] for check in checks}
    )


def _match(left, right, accepts):
    """Find a one-to-one matching, even when several statements look similar."""
    if len(left) != len(right):
        return None
    owners = {}

    def assign(index, seen):
        for other, item in enumerate(right):
            if other in seen or not accepts(left[index], item):
                continue
            seen.add(other)
            if other not in owners or assign(owners[other], seen):
                owners[other] = index
                return True
        return False

    return sorted((i, j) for j, i in owners.items()) if all(
        assign(i, set()) for i in range(len(left))
    ) else None


def _grounded(text, frames):
    compact = _compact(text)
    covered = set()
    for statement in frames.statements:
        quote = _compact(statement.evidence)
        start = compact.find(quote)
        if not quote or start < 0 or not _key(statement.act) or _key(statement.polarity) not in ('affirmed', 'negated'):
            return False
        covered.update(range(start, start + len(quote)))
    return all(index in covered for index, char in enumerate(compact)
               if unicodedata.category(char)[0] in 'LMN')


def _slot_keys(statement):
    values = {slot: _key(getattr(statement, slot)) for slot in SLOTS}
    quantity = value_anchors(values['quantity'])
    if len(quantity) == 1 and regex.fullmatch(r'[\p{L}\d,.\s-]+', values['quantity']):
        values['quantity'] = quantity[0][1]
    # Move an explicit leading object count into the quantity field.
    embedded = regex.fullmatch(r'(\d+|[a-z]+) (.+)', values['object'])
    if embedded and (embedded[1].isdigit() or embedded[1] in WORDS):
        count = value_anchors(embedded[1])
        if len(count) == 1 and values['quantity'] in ('', count[0][1]):
            values['quantity'], values['object'] = count[0][1], embedded[2]
    evidence_counts = value_anchors(statement.evidence)
    if not values['quantity'] and len(evidence_counts) == 1 and evidence_counts[0][0] == 'quantity':
        values['quantity'] = evidence_counts[0][1]
    return values


def _structure(source, candidate, passage_id):
    expected = collections.Counter(TOKEN.findall(source))
    actual = collections.Counter(TOKEN.findall(candidate.english))
    candidate_text = TOKEN.sub('', candidate.english)
    input_length = max(1, len(TOKEN.sub('', source).strip()))
    ratio = len(candidate_text.strip()) / input_length
    return (
        _check('Passage ID and output', candidate.id == passage_id and bool(candidate.english.strip()),
               'The expected passage has a non-empty result.'),
        _check('English and length', not regex.search(r'[^\p{Script=Latin}\p{Common}\p{Inherited}]', candidate_text)
               and (input_length < 8 or 0.25 <= ratio <= 5),
               'The candidate uses English script and has a plausible length.', length_ratio=round(ratio, 2)),
        _check('Protected values', expected == actual, 'Every protected value returns unchanged at each occurrence.',
               'quantity', expected=dict(expected), observed=dict(actual)),
    )


class Validator:
    def __init__(self, models):
        self.models = models

    def run(self, passage, masker, context, officer_language):
        original = passage['effective_source']
        source = masker.mask(original)
        record = {
            'validator_version': VALIDATOR_VERSION, 'rules_version': RULES_VERSION,
            'prompt_version': PROMPT_VERSION, 'evidence': {'passage': passage['id'],
                'start': passage['start'], 'end': passage['end'], 'page': passage['page'],
                'source_hash': digest(original), 'document_hash': passage['document_hash'],
                'masked_hash': digest(source), 'context_ids': [item['id'] for item in context]},
            'masked_source': source, 'context': context,
            'glossary': {token: masker.glossary[token] for token in TOKEN.findall(source) if token in masker.glossary},
            'vault': {token: masker.vault[token] for token in TOKEN.findall(source)
                                               if token in masker.vault},
            'script_spans': script_spans(original), 'language': None, 'attempts': [],
        }
        started = time.monotonic()
        feedback = None
        for attempt in range(1, 3):
            item = {'number': attempt, 'checks': [], 'calls': [], 'translations': {}, 'frames': {}}
            record['attempts'].append(item)
            try:
                verdict, slot, reason = self._attempt(passage, source, context, masker.glossary, item, record, feedback)
            except ModelError as error:
                verdict, slot, reason = 'retry', 'service', str(error)
                _layer(item, 'Runtime', _check('Local inference', False, reason, slot))
            if verdict == 'retry' and attempt == 2:
                verdict = 'clarify'
            item.update(verdict=verdict, reason=reason)
            if verdict != 'retry':
                break
            feedback = reason
        record.update(verdict=verdict, reason=reason, slot=slot,
                      machine_ms=round(1000 * (time.monotonic() - started)),
                      question=QUESTIONS[officer_language][slot] if verdict == 'clarify' else None)
        if verdict == 'pass':
            record['checked_english'] = masker.restore(item['translations']['A'])
        return record

    def _attempt(self, passage, source, context, glossary, item, record, feedback):
        if len(passage['effective_source']) > MAX_PASSAGE:
            return 'clarify', 'length', 'The passage exceeds the supported verification window.'
        if TOKEN.search(passage['effective_source']):
            return 'clarify', 'statement', 'The original contains a reserved placeholder pattern.'
        models = self.models.inventory()
        trace = item['calls']

        def chat(model, role, instruction, data, schema, budget=700):
            return self.models.chat(model or self.models.verifier, role, instruction, data, schema, trace, models, budget)

        if passage['language_override']:
            override = passage['language_override']
            language = Language(language=override, confidence=1.0)
        else:
            language = chat(self.models.verifier, 'Language identification', LANGUAGE_PROMPT,
                            {'passage': source, 'scripts': sorted({span['script'] for span in record['script_spans']
                                                                   if span['script'] != 'Common'})}, Language, 100)
        record['language'] = {**language.model_dump(), 'confirmed': bool(passage['language_override'])}
        valid_language = language.language in LANGUAGES and language.confidence >= LANGUAGE_MINIMUM
        if language.language == 'en' and regex.search(r'[^\p{Script=Latin}\p{Common}\p{Inherited}]', TOKEN.sub('', source)):
            valid_language = False
        if not _layer(item, 'Intake', _check('Source language', valid_language,
                                             'The language is identified with sufficient confidence.', 'language')):
            return 'clarify', 'language', 'The source language needs confirmation.'

        if language.language == 'en':
            translations = {path: Translation(id=passage['id'], english=source) for path in ('A', 'B')}
            item['translation_method'] = 'English source carried through verbatim'
        else:
            # Verify model IDs as well as names so a local alias cannot impersonate independence.
            if not models.get(self.models.a) or not models.get(self.models.b):
                raise ModelError('Both local translation models must be available.')
            if models[self.models.a] == models[self.models.b]:
                raise ModelError('Translation paths must use two different local model versions.')
            label = {'gu': 'Gujarati', 'hi': 'Hindi', 'mul': 'Code mixed'}.get(language.language, language.language)
            data = {'id': passage['id'], 'source': source, 'language': label, 'language_code': language.language,
                    'retry_feedback': feedback}
            translations = {}
            for path, model in [('A', self.models.a), ('B', self.models.b)]:
                request = {**data, 'context_only': context} if path == 'A' else data
                translations[path] = chat(model, f'Translation {path}', TRANSLATION_PROMPT, request, Translation, 900)
            item['translation_method'] = 'Two independent local models'

        item['translations'] = {path: result.english for path, result in translations.items()}
        l0 = [_check(f'Path {path} · {check["name"]}', check['ok'], check['detail'], check['slot'],
                     **{k: v for k, v in check.items() if k not in ('name', 'ok', 'detail', 'slot')})
              for path, candidate in translations.items() for check in _structure(source, candidate, passage['id'])]
        if not _layer(item, 'L0', *l0):
            failure = next(check for check in l0 if not check['ok'])
            return 'retry', failure['slot'], f'{failure["name"]}: {failure["detail"]}'

        l1 = [{**check, 'name': f'Path {path} · {check["name"]}'}
              for path, result in translations.items() for check in check_anchors(source, result.english)]
        if not _layer(item, 'L1', *l1):
            failure = next(check for check in l1 if not check['ok'])
            return 'clarify', failure['slot'], f'{failure["name"]}: {failure["detail"]}'

        a, b = translations['A'].english, translations['B'].english
        if a == b:
            _layer(item, 'L2', _check('Bidirectional equivalence', True, 'Both English strings are identical.'))
            a_frames = None
        else:
            a_frames = self._frames(chat, 'A', a, 'en', glossary, item)
            b_frames = self._frames(chat, 'B', b, 'en', glossary, item)
            if not _layer(item, 'L2', _check('English statement coverage',
                                             _grounded(a, a_frames) and _grounded(b, b_frames),
                                             'Exact evidence quotes cover both English candidates.')):
                return 'retry', 'statement', 'The verifier did not account for all English statement text.'
            if not self._compare_english(chat, item, a_frames, b_frames):
                return 'clarify', 'statement', 'The English translations do not support each other in both directions.'

        source_frames = self._frames(chat, 'source', source, language.language, glossary, item)
        # English is passed through exactly. The same source frame applies by identity.
        if language.language == 'en':
            a_frames = source_frames
            item['frames']['A'] = source_frames.model_dump()
        elif a_frames is None:
            a_frames = self._frames(chat, 'A', a, 'en', glossary, item)
        coverage_note = ('The source frame covers the unchanged English text.' if language.language == 'en' else
                         'Separately extracted frames account for the source and checked English.')
        if not _layer(item, 'L3', _check('Source and English evidence coverage',
                                         _grounded(source, source_frames) and _grounded(a, a_frames), coverage_note)):
            return 'retry', 'statement', 'The verifier did not account for all source or English statement text.'
        if not _layer(item, 'L3', _check('Factual ambiguity', not (source_frames.ambiguous or a_frames.ambiguous),
                                         'The verifier found no unresolved ambiguity.')):
            return 'clarify', 'statement', 'The verifier found an unresolved ambiguity.'
        failure = self._compare_source(item, source_frames, a_frames)
        if failure:
            return 'clarify', failure, f'The source and English disagree on the {failure} slot.'
        return 'pass', 'statement', 'Every required layer passed.'

    @staticmethod
    def _frames(chat, label, text, language, glossary, item):
        language_name = {'hi': 'Hindi', 'gu': 'Gujarati', 'en': 'English', 'mul': 'Code mixed'}.get(language, language)
        frames = chat(None, f'Frame {label}', FRAME_PROMPT,
                      {'passage': text, 'language': language_name,
                       'glossary': {t: glossary[t] for t in TOKEN.findall(text) if t in glossary}}, Frames, 1350)
        item['frames'][label] = frames.model_dump()
        return frames

    def _compare_english(self, chat, item, a_frames, b_frames):
        a = [s.evidence for s in a_frames.statements]
        b = [s.evidence for s in b_frames.statements]
        if len(a) != len(b):
            return _layer(item, 'L2', _check('Statement pairing', False,
                                             'One candidate has unmatched statements.', counts=[len(a), len(b)]))
        pairs = [(x, y) for x in a for y in b]
        nonidentical = list(dict.fromkeys((x, y) for x, y in pairs if _compact(x) != _compact(y)))
        scores = self.models.entailment([direction for x, y in nonidentical for direction in ((x, y), (y, x))]) if nonidentical else []
        report = {}
        for index, (x, y) in enumerate(nonidentical):
            forward, reverse = scores[2 * index:2 * index + 2]
            passed = all(s['entailment'] >= NLI_THRESHOLD for s in (forward, reverse))
            borderline = all(s['contradiction'] < 0.15 and s['entailment'] >= 0.45 for s in (forward, reverse))
            fallback = None
            if borderline and not passed and len(pairs) <= 9:
                result = chat(None, 'Borderline entailment',
                              'Decide whether each statement entails the other in both directions. '
                              'Every factual detail, role, uncertainty and negation must agree. Unknown means false.',
                              {'A': x, 'B': y}, Entailment, 100)
                fallback = result.model_dump()
                passed = result.forward and result.reverse
            report[x, y] = {'A': x, 'B': y, 'forward': forward, 'reverse': reverse,
                            'fallback': fallback, 'ok': passed}
        alignment = _match(a, b, lambda x, y: _compact(x) == _compact(y) or report[x, y]['ok'])
        return _layer(item, 'L2', _check('Bidirectional entailment', alignment is not None,
                                         'Every English statement must have a mutually entailed counterpart.',
                                         nli_version=NLI_REVISION, threshold=NLI_THRESHOLD,
                                         scores=list(report.values()), alignment=alignment))

    @staticmethod
    def _compare_source(item, source_frames, english_frames):
        source = [_slot_keys(statement) for statement in source_frames.statements]
        english = [_slot_keys(statement) for statement in english_frames.statements]
        if len(source) != len(english):
            _layer(item, 'L3', _check('Statement count', False, 'The source and English have different statement counts.',
                                      counts=[len(source), len(english)]))
            return 'statement'
        alignment = _match(source, english, lambda a, b: a == b)
        if alignment is None:
            closest = min(((a, b) for a in source for b in english if a != b),
                          key=lambda pair: sum(pair[0][s] != pair[1][s] for s in SLOTS),
                          default=None)
            mismatches = [s for s in SLOTS if closest and closest[0][s] != closest[1][s]]
            slot = mismatches[0] if mismatches else 'statement'
            _layer(item, 'L3', _check('Material slots', False, 'No one-to-one slot match exists.',
                                      slot, differing_slots=mismatches))
            return slot
        _layer(item, 'L3', _check('Material slots', True, 'All critical and major slots match by statement.',
                                  alignment=alignment, compared_slots=SLOTS))
        return None
