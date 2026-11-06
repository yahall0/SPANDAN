import re
from collections import defaultdict
from itertools import combinations

from .claim_time import disjoint, normalize_time
from .claims import (Candidate, PRONOUN, SELF, UNIDENTIFIED, action_key, actor_key, key, rule_checks)
from .rules import digest, value_anchors

GRAPH_VERSION = 'claim-graph-v1'
EVENT_KINDS = {'event', 'state', 'statement'}
ACTION_CUE = re.compile(r'^\s*(?:then\b|after (?:that|this|which)\b|subsequently\b|next\b)', re.I)
CLAUSE_CUE = re.compile(r'\b(?:(and then|and afterwards|then|after|before|because))\b', re.I)
ROLE_LINKS = {'actor': 'actor', 'object': 'object', 'place': 'place', 'knowledge': 'told by', 'time': 'time'}


def _id(*parts):
    return digest(parts)[:24]


def _issue(name, detail, claim_ids, **data):
    return {'id': _id(name, sorted(claim_ids), data), 'name': name, 'detail': detail,
            'claim_ids': list(claim_ids), **data}


def _supports_merge(left, right):
    return left['type'] == right['type'] and left['type'] != 'time'


def _decision_claim(claim, run, review):
    if not review:
        return claim
    if review['action'] == 'unresolved':
        return {**claim, 'status': 'escalated', 'decision': review}
    fields = {field: claim[field] for field in Candidate.model_fields}
    if review['action'] == 'correct':
        fields.update(review['overrides'])
    candidate = Candidate.model_validate(fields)
    if review['action'] == 'correct' and 'text' in review['overrides'] and key(candidate.text) != key(candidate.quote):
        raise ValueError('Use the exact checked quote when correcting claim wording. Add new testimony as a statement.')
    checked, _ = rule_checks(candidate, {'english': run['evidence']['english'], 'frames': run['frames']})
    if any(not check['ok'] for check in checked if check.get('hard')):
        raise ValueError('The quote, grounded fields and suspicion wording must remain faithful to the source.')
    references = {slot: link for slot, link in claim.get('references', {}).items() if fields[slot] == claim[slot]}
    return {**claim, **fields, 'status': 'officer_confirmed', 'decision': review, 'references': references,
            'officer_checks': checked, 'original_candidate': {k: claim[k] for k in fields}}


def _entities(snapshot, claims):
    records = {}
    edges = []
    merged = {}
    for d in snapshot['decisions']:
        if d['action'] == 'merge':
            merged[d['target']] = d['other']

    def root(identifier):
        seen = set()
        while identifier in merged and identifier not in seen:
            seen.add(identifier)
            identifier = merged[identifier]
        return identifier

    def add(claim, label, kind, identity, identity_key):
        if not label:
            return None
        identifier = _id(snapshot['case']['id'], identity_key)
        target = root(identifier)
        entry = records.setdefault(target, {'id': target, 'label': label, 'type': kind, 'identity': identity,
                                             'claim_ids': [], 'mentions': [], 'merged': []})
        if identifier != target and identifier not in entry['merged']:
            entry['merged'].append(identifier)
        if identifier == target:
            entry['label'], entry['identity'] = label, identity
        if claim['id'] not in entry['claim_ids']:
            entry['claim_ids'].append(claim['id'])
        mention = {'claim_id': claim['id'], 'text': label, 'passage_id': claim['passage_id'],
                   'document_id': claim['document_id']}
        if mention not in entry['mentions']:
            entry['mentions'].append(mention)
        return target

    for claim in claims:
        document = claim['document_id']
        reporter = add(claim, claim['reporter'], 'person', 'reporter', ('reporter', document))
        claim['reporter_id'] = reporter
        for slot, relation in ROLE_LINKS.items():
            label = claim.get(slot)
            reference = claim.get('references', {}).get(slot)
            if reference:
                label = reference['label']
            if not label:
                continue
            if slot in ('actor', 'knowledge') and key(label) in SELF:
                identifier = reporter
            else:
                kind = 'person' if slot in ('actor', 'knowledge') else slot
                if kind == 'object' and re.match(r'^(?:a |the |unknown )?(?:man|woman|person|neighbor|neighbour|child)\b', key(label)):
                    kind = 'person'
                identity = 'unidentified' if kind == 'person' and (
                    UNIDENTIFIED.fullmatch(key(label)) or PRONOUN.fullmatch(key(label))
                ) else 'stated'
                scope = (claim['document_id'], claim['passage_id'], reference['quote_start']) if (
                    identity == 'unidentified' and reference
                ) else (claim['document_id'], claim['passage_id'], claim['quote_start']) if identity == 'unidentified' else document
                identifier = add(claim, label, kind, identity, (scope, kind, actor_key(label)))
            claim[f'{slot}_id'] = identifier
            edges.append({'id': _id(claim['id'], relation, identifier), 'from': claim['id'], 'to': identifier,
                          'relation': relation, 'basis': 'context' if reference else 'reported' if slot == 'knowledge' else 'claim',
                          'claim_ids': [claim['id']], 'context_quote': reference['quote'] if reference else None})
        if claim.get('evidentiality') == 'direct' and not claim.get('knowledge_id'):
            claim['knowledge_id'] = reporter
    return list(records.values()), edges


def _links(events):
    by_doc = defaultdict(list)
    links = {}

    def add(left, right, relation, basis, cue=None):
        if left['id'] == right['id']:
            return
        link = {'id': _id(left['id'], right['id'], relation, basis), 'from': left['id'], 'to': right['id'],
                'relation': relation, 'basis': basis, 'claim_ids': [left['claim_id'], right['claim_id']], 'cue': cue}
        links[link['id']] = link

    for event in events:
        if event['polarity'] == 'affirmed':
            by_doc[event['document_id']].append(event)
    for lane in by_doc.values():
        lane.sort(key=lambda e: (e['source_start'], e['quote_start'], e['id']))
        for left, right in zip(lane, lane[1:]):
            if left['passage_id'] == right['passage_id']:
                if left['quote_end'] <= right['quote_start']:
                    between = right['english'][left['quote_end']:right['quote_start']]
                    following = between + ' ' + right['quote']
                    cue = CLAUSE_CUE.match(following.strip(' .,;'))
                    if cue:
                        if cue[1].casefold() in {'after', 'because'}:
                            add(right, left, 'triggered' if cue[1].casefold() == 'because' else 'before', 'stated', cue[1])
                        else:
                            add(left, right, 'before', 'stated', cue[1])
                    elif ',' in between:
                        lead = CLAUSE_CUE.match(left['english'][:left['quote_end']].strip())
                        if lead and lead[1].casefold() in {'after', 'before', 'because'}:
                            if lead[1].casefold() == 'before':
                                add(right, left, 'before', 'stated', lead[1])
                            else:
                                add(left, right, 'triggered' if lead[1].casefold() == 'because' else 'before',
                                    'stated', lead[1])
            elif int(right['passage_id'][1:]) == int(left['passage_id'][1:]) + 1:
                cue = ACTION_CUE.search(right['english'])
                if cue and right['quote_start'] <= cue.end() + 2 and (
                    sum(e['passage_id'] == left['passage_id'] for e in lane) == 1
                ):
                    add(left, right, 'before', 'stated', cue[0].strip())

    timed = sorted((e for e in events if e['polarity'] == 'affirmed' and e['time'] and e['time']['start']),
                   key=lambda e: (e['time']['end'], e['id']))
    for right in timed:
        # The latest time frontier is enough to show direct ordering while keeping the graph sparse.
        predecessor = next((left for left in reversed(timed) if left['time']['end'] < right['time']['start']), None)
        if predecessor:
            add(predecessor, right, 'before', 'from_times')
    return list(links.values())


def _checks(claims, events, links):
    issues, suggestions = [], []
    by_event = {event['id']: event for event in events}
    by_signature = defaultdict(list)
    by_document = defaultdict(list)
    for event in events:
        if event['time'] and event['time']['question']:
            issues.append(_issue('Time needs clarification', event['time']['question'], [event['claim_id']]))
        if not (event['actor_id'] or event['object_id'] or event['time']):
            issues.append(_issue('Disconnected event', 'Who or what was involved, and when?', [event['claim_id']]))
        if event['object']:
            by_signature[(action_key(event['act']), actor_key(event['object']))].append(event)
        by_document[event['document_id']].append(event)

    for group in by_signature.values():
        for left, right in combinations(group, 2):
            ids = [left['claim_id'], right['claim_id']]
            if left['document_id'] == right['document_id']:
                if left['actor_id'] == right['actor_id'] and left['polarity'] != right['polarity']:
                    issues.append(_issue('Conflicting polarity', 'One account affirms and negates the same possible action.', ids))
                continue
            if left['polarity'] != 'affirmed' or right['polarity'] != 'affirmed':
                continue
            suggestions.append({'id': _id('possible_same_event', *ids), 'from': left['id'], 'to': right['id'],
                                'relation': 'possible_same_event', 'basis': 'inferred', 'claim_ids': ids,
                                'reason': 'The two accounts describe the same action and object wording.'})
            different_time = disjoint(left['time'], right['time'])
            different_place = _places_conflict(left['place'], right['place'])
            different_amount = left['amount'] and right['amount'] and left['amount'] != right['amount']
            if different_time or different_place or different_amount:
                fields = [name for name, found in [('time', different_time), ('place', different_place),
                                                  ('amount', different_amount)] if found]
                issues.append(_issue('Possible source conflict',
                                     'If these are the same event, the accounts differ on ' + ', '.join(fields) + '.',
                                     ids, fields=fields))
    for lane in by_document.values():
        lane.sort(key=lambda e: (e['source_start'], e['quote_start']))
        for left, right in zip(lane, lane[1:]):
            if left['polarity'] != 'affirmed' or right['polarity'] != 'affirmed':
                continue
            if int(right['passage_id'][1:]) - int(left['passage_id'][1:]) > 1:
                continue
            for action, observer in [(left, right), (right, left)]:
                if (UNIDENTIFIED.fullmatch(key(action['actor'])) and action_key(observer['act']) == 'see' and
                        UNIDENTIFIED.fullmatch(key(observer['object']))):
                    ids = [action['claim_id'], observer['claim_id']]
                    suggestions.append({'id': _id('possible_actor', *ids), 'from': observer['id'], 'to': action['id'],
                                        'relation': 'possible_actor', 'basis': 'inferred', 'claim_ids': ids,
                                        'reason': 'An unidentified actor and an observed person appear close in the account.'})

    adjacency = defaultdict(set)
    for link in links:
        adjacency[link['from']].add(link['to'])
        left, right = by_event[link['from']], by_event[link['to']]
        if link['basis'] == 'stated' and disjoint(left['time'], right['time']) and (
            right['time']['end'] < left['time']['start']
        ):
            issues.append(_issue('Time contradicts stated order', 'The timestamps run opposite to the stated order.',
                                 link['claim_ids'], link_id=link['id']))
    for link in links:
        visited, stack = set(), [link['to']]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            stack.extend(adjacency[node] - visited)
        if link['from'] in visited:
            issues.append(_issue('Order cycle', 'This link participates in contradictory event order.',
                                 link['claim_ids'], link_id=link['id']))
    return issues, suggestions


def _places_conflict(left, right):
    left, right = actor_key(left), actor_key(right)
    if not left or not right or left == right:
        return False
    opposites = (('inside', 'outside'), ('north', 'south'), ('east', 'west'))
    for first, second in opposites:
        for a, b in ((first, second), (second, first)):
            if left.startswith(a + ' ') and right.startswith(b + ' ') and actor_key(
                left[len(a):].strip()
            ) == actor_key(right[len(b):].strip()):
                return True
    return False


def build(snapshot):
    decisions = {d['target']: d for d in snapshot['decisions'] if d['action'] not in ('merge', 'retry')}
    claims, issues, events = [], [], []
    for run in snapshot['runs']:
        for issue in run['escalations']:
            issues.append({**issue, 'claim_ids': [], 'passage_id': run['passage_id'], 'evidence': run['evidence']})
        for candidate in run['claims']:
            choice = decisions.get(candidate['id'])
            claim = _decision_claim(candidate, run, choice)
            claims.append({**claim, 'evidence': run['evidence'], 'passage_id': run['passage_id'],
                           'document_id': run['evidence']['document_id'], 'reporter': run['evidence']['reporter'],
                           'allegation': {'suspicion': 'reported_suspicion', 'opinion': 'reported_opinion',
                                          'contact_detail': 'contact_detail'}.get(claim['kind'], 'reported_account'),
                           'amounts': [{'currency': 'INR', 'value': v}
                                       for role, v in value_anchors(claim['quote']) if role == 'amount']})

    active = [c for c in claims if c['status'] in ('verified', 'officer_confirmed')]
    entities, entity_links = _entities(snapshot, active)
    lanes = {s['id']: {'id': s['id'], 'document_id': s['id'], 'label': s['reporter'], 'kind': s.get('kind', 'source')}
             for s in snapshot['statements']}
    for claim in active:
        if claim['kind'] not in EVENT_KINDS:
            continue
        evidence = claim['evidence']
        lane_id = claim['document_id']
        if claim['evidentiality'] == 'reported':
            lane_id = _id('reported', claim['document_id'], claim.get('knowledge_id'))
            label = claim.get('references', {}).get('knowledge', {}).get('label') or claim['knowledge']
            lanes[lane_id] = {'id': lane_id, 'document_id': claim['document_id'], 'kind': 'reported',
                              'label': f'Reported · {label or "source unspecified"}'}
        events.append({'id': _id('event', claim['id']), 'claim_id': claim['id'], 'description': claim['text'],
                       'passage_id': claim['passage_id'], 'document_id': claim['document_id'], 'lane_id': lane_id, 'act': claim['act'],
                       'actor': claim['actor'], 'actor_id': claim.get('actor_id'),
                       'object': claim['object'], 'object_id': claim.get('object_id'), 'place': claim['place'],
                       'polarity': claim['polarity'], 'evidentiality': claim['evidentiality'],
                       'amount': claim['amounts'],
                       'time': normalize_time(claim['time'], evidence['received_at']),
                       'english': evidence['english'], 'source_start': evidence['start'],
                       'quote_start': claim['quote_start'], 'quote_end': claim['quote_end']})
    links = _links(events)
    graph_issues, hypotheses = _checks(active, events, links)
    issues.extend(graph_issues)
    held, preserved = set(), set()
    for issue in issues:
        choice = decisions.get(issue['id'])
        issue['decision'] = choice
        if choice and choice['action'] == 'prefer':
            preserved.update(set(issue['claim_ids']) - {choice['other']})
        elif len(issue['claim_ids']) > 1 or issue['name'] in ('Time needs clarification', 'Disconnected event'):
            held.update(issue['claim_ids'])
    for claim in claims:
        claim['handoff'] = claim['status'] in ('verified', 'officer_confirmed') and claim['id'] not in held | preserved
        if claim['id'] in preserved - held:
            claim['disposition'] = 'other_account_preserved'
        if claim['status'] == 'escalated':
            claim['decision'] = decisions.get(claim['id'])
    ordered = {endpoint for link in links for endpoint in (link['from'], link['to'])}
    for event in events:
        event['unordered'] = event['id'] not in ordered
    return {**snapshot, 'graph_version': GRAPH_VERSION, 'claims': claims, 'entities': entities, 'events': events,
            'lanes': list(lanes.values()),
            'entity_links': entity_links, 'order_links': links, 'inferred_connections': hypotheses, 'issues': issues}


def validate_decision(graph, target, detail):
    action = detail['action']
    if action == 'merge':
        entities = {e['id']: e for e in graph['entities']}
        if target == detail['other'] or target not in entities or detail['other'] not in entities:
            raise ValueError('Select two different current entities.')
        if not _supports_merge(entities[target], entities[detail['other']]):
            raise ValueError('Only compatible entity types can be merged.')
        return
    issues = {issue['id']: issue for issue in graph['issues']}
    claim = next((c for c in graph['claims'] if c['id'] == target), None)
    if action == 'retry':
        issue = issues.get(target)
        retryable = (issue and issue['name'] in ('Local model', 'Internal error')) or (
            claim and claim['status'] == 'escalated' and any(c['name'] == 'Local verification' for c in claim['checks'])
        )
        if not retryable:
            raise ValueError('Only a local processing failure can be rerun here.')
        item = issue if issue else claim
        detail['run'] = {'document_id': item['evidence']['document_id'], 'passage_id': item['passage_id'],
                         'revision': item['evidence']['revision']}
        return
    if target in issues:
        if action == 'prefer' and len(issues[target]['claim_ids']) > 1 and detail['other'] in issues[target]['claim_ids']:
            return
        if action == 'unresolved':
            return
        raise ValueError('Choose a conflicting claim or record that this issue is unresolved.')
    if not claim or action not in ('confirm', 'correct', 'unresolved'):
        raise ValueError('The selected claim is no longer available for this action.')
    if action == 'confirm' and claim['status'] != 'escalated':
        raise ValueError('Only an escalated claim needs confirmation.')
    if action in ('confirm', 'correct'):
        original = next(r for r in graph['runs'] if r['passage_id'] == claim['passage_id'] and
                        r['evidence']['document_id'] == claim['document_id'])
        raw = next(c for c in original['claims'] if c['id'] == target)
        if action == 'correct':
            detail['overrides'] = {field: claim[field] for field in Candidate.model_fields if claim[field] != raw[field]}
            detail['overrides'][detail['field']] = detail['value']
        _decision_claim(raw, original, detail)
