"""Run the claim pipeline on the fictional, pre-checked development corpus."""

import argparse
import json
import math
import time
from collections import Counter
from pathlib import Path

from .claim_graph import build
from .claim_store import BATCH_SIZE
from .claims import ClaimExtractor, action_key, actor_key
from .models import LocalModels
from .rules import digest

DEFAULT_CORPUS = Path('fixtures/component2_cases.jsonl')
CRITICAL_FIELDS = ('actor', 'knowledge', 'polarity', 'evidentiality', 'kind')


def load_corpus(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def fixture_inputs(case):
    documents = []
    for index, statement in enumerate(case['statements']):
        document_id = digest([case['id'], index])[:32]
        items = []
        start = 0
        for passage in statement['passages']:
            gold = passage['gold_claim']
            frame = {'evidence': gold['quote'], 'speaker': 'I', 'evidentiality': (
                None if gold['evidentiality'] == 'unspecified' else gold['evidentiality']
            ), 'polarity': gold['polarity'], 'tense': 'past', 'actor': gold['actor'], 'act': gold['act'],
                'object': gold['object'], 'quantity': gold['quantity'], 'certainty': gold['certainty'],
                'time': gold['time'], 'place': gold['place'], 'manner': None}
            source = passage['source']
            items.append({'id': passage['id'], 'document_id': document_id, 'source': source,
                          'effective_source': source, 'english': passage['english'], 'frames': [frame],
                          'revision': 1, 'page': 1 if statement['format'] == 'pdf' else None,
                          'start': start, 'end': start + len(source), 'reporter': statement['reporter'],
                          'received_at': statement['received_at'], 'validator_version': 'authored-development-fixture',
                          'checked_hash': digest(passage['english']), 'sha256': digest(statement['passages']),
                          'format': statement['format']})
            start += len(source) + 2
        documents.append(items)
    return documents


def fixture_graph(case, documents, runs):
    statements = [{'id': document[0]['document_id'], 'reporter': document[0]['reporter']}
                  for document in documents]
    return build({'case': {**statements[0], 'case_ref': case['title']}, 'statements': statements, 'runs': runs,
                  'pending': [], 'decisions': [], 'version': 'development-fixture'})


def graph_expectations(case, documents, graph):
    expected = case['expected']
    failures = []
    issue_names = {i['name'] for i in graph['issues']}
    hypotheses = {e['relation'] for e in graph['inferred_connections']}
    if expected.get('issue') and expected['issue'] not in issue_names:
        failures.append('Missing issue: ' + expected['issue'])
    if expected.get('no_issue') in issue_names:
        failures.append('Unexpected issue: ' + expected['no_issue'])
    if expected.get('hypothesis') and expected['hypothesis'] not in hypotheses:
        failures.append('Missing hypothesis: ' + expected['hypothesis'])
    if 'order' in expected:
        events = {e['id']: (e['document_id'], e['passage_id']) for e in graph['events']}
        links = {(events[e['from']], events[e['to']], e['basis']) for e in graph['order_links']}

        def location(address):
            return documents[address[0]][0]['document_id'], address[1]

        wanted = {(location(link['from']), location(link['to']), link['basis']) for link in expected['order']}
        if (not wanted and links) or not wanted <= links:
            failures.append('Timeline order differs from the authored expectation.')
    return failures


def evaluate(cases, models):
    extractor = ClaimExtractor(models)
    reports, totals = [], Counter()
    for case in cases:
        start = time.monotonic()
        documents = fixture_inputs(case)
        runs = []
        for document in documents:
            for offset in range(0, len(document), BATCH_SIZE):
                runs.extend(extractor.process(document[offset:offset + BATCH_SIZE]).values())
        graph = fixture_graph(case, documents, runs)
        problems = graph_expectations(case, documents, graph)
        for document, original in zip(documents, case['statements']):
            for item, passage in zip(document, original['passages']):
                gold = passage['gold_claim']
                actual = [c for c in graph['claims'] if c['document_id'] == item['document_id'] and
                          c['passage_id'] == item['id'] and c['status'] == 'verified']
                matching = [c for c in actual if c['quote'] == gold['quote'] and action_key(c['act']) == action_key(gold['act'])]
                totals['gold_claims'] += 1
                totals['verified_claims'] += len(actual)
                if not matching:
                    problems.append(f'{item["reporter"]}/{item["id"]}: expected claim not verified.')
                else:
                    totals['matched'] += 1
                    for field in CRITICAL_FIELDS:
                        if actor_key(matching[0][field]) != actor_key(gold[field]):
                            totals['critical_mismatches'] += 1
                            problems.append(f'{item["reporter"]}/{item["id"]}: {field} differs.')
                if len(actual) > len(matching):
                    totals['unexpected_verified'] += len(actual) - len(matching)
                    problems.append(f'{item["reporter"]}/{item["id"]}: unexpected verified claim.')
        reports.append({'id': case['id'], 'family': case['family'],
                        'sources': [{'language': s['language'], 'format': s['format']} for s in case['statements']],
                        'machine_ms': round((time.monotonic() - start) * 1000), 'differences': problems})
    times = sorted(report['machine_ms'] for report in reports)
    return {'development_only': True, 'totals': dict(totals), 'claim_stage_p95_ms': (
        times[math.ceil(len(times) * .95) - 1] if times else None
    ), 'cases': reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', nargs='?', type=Path, default=DEFAULT_CORPUS)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    cases = load_corpus(args.corpus)
    models = LocalModels()
    try:
        report = evaluate(cases[:args.limit], models)
    finally:
        models.close()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    raise SystemExit(int(any(case['differences'] for case in report['cases'])))


if __name__ == '__main__':
    main()
