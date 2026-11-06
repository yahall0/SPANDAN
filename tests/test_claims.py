import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from spandan.app import create_app
from spandan.claim_graph import build, validate_decision
from spandan.claim_time import disjoint, normalize_time
from spandan.claims import Candidate, ClaimExtractor, _claim, _run, rule_checks
from spandan.evaluate_claims import DEFAULT_CORPUS, fixture_graph, fixture_inputs, graph_expectations, load_corpus
from spandan.models import ModelError
from test_component import FixedModels


def make_gold_runs(case, documents):
    runs = []
    for statement, document in zip(case['statements'], documents):
        for passage, item in zip(statement['passages'], document):
            run = _run(item)
            run['claims'] = [{**_claim(item, Candidate.model_validate(passage['gold_claim'])), 'status': 'verified'}]
            runs.append(run)
    return runs


class ClaimsTests(unittest.TestCase):
    def test_pdf_line_wrap_recovers_exact_evidence_without_changing_words(self):
        case = next(c for c in load_corpus(DEFAULT_CORPUS) if c['family'] == 'negation')
        item = fixture_inputs(case)[0][0]
        candidate = Candidate.model_validate(case['statements'][0]['passages'][0]['gold_claim'])
        item['english'] = item['english'].replace('see ', 'see\n')
        item['frames'][0]['act'] = 'not see'
        claim = _claim(item, candidate)
        self.assertEqual(claim['quote'], item['english'])
        self.assertEqual(item['english'][claim['quote_start']:claim['quote_end']], claim['quote'])
        self.assertTrue(next(c['ok'] for c in claim['checks'] if c['name'] == 'Intake agreement'))
        changed = _claim(item, candidate.model_copy(update={'quote': candidate.quote.replace('not ', '')}))
        self.assertFalse(next(c['ok'] for c in changed['checks'] if c['name'] == 'Exact quote'))

    def test_missing_batch_results_are_retried_in_isolation(self):
        class OmittedBatchModel(FixedModels):
            def __init__(self):
                super().__init__()
                self.requests = []

            def chat(self, model, role, instruction, data, schema, trace, inventory, budget):
                if role == 'Claim extraction':
                    ids = [item['id'] for item in data['passages']]
                    self.requests.append(ids)
                    return schema.model_validate({'passages': [
                        {'id': identifier, 'claims': []} for identifier in ids[:1]
                    ]})
                return super().chat(model, role, instruction, data, schema, trace, inventory, budget)

        case = next(c for c in load_corpus(DEFAULT_CORPUS) if c['family'] == 'stated_order')
        models = OmittedBatchModel()
        runs = ClaimExtractor(models).process(fixture_inputs(case)[0])
        self.assertEqual(models.requests[:3], [['H01', 'H02'], ['H01'], ['H02']])
        self.assertEqual([i['name'] for i in runs['H02']['attempts'][0]['issues']], ['Batch shape'])

    def test_fictional_corpus_graph_expectations(self):
        cases = load_corpus(DEFAULT_CORPUS)
        self.assertEqual(len({case['id'] for case in cases}), 50)
        for case in cases:
            with self.subTest(case=case['id']):
                documents = fixture_inputs(case)
                graph = fixture_graph(case, documents, make_gold_runs(case, documents))
                self.assertEqual(graph_expectations(case, documents, graph), [])

    def test_flipped_hearsay_and_added_intent_fail(self):
        cases = load_corpus(DEFAULT_CORPUS)
        hearsay = next(case for case in cases if case['family'] == 'hearsay')
        item = fixture_inputs(hearsay)[0][0]
        gold = Candidate.model_validate(hearsay['statements'][0]['passages'][0]['gold_claim'])
        checks, _ = rule_checks(gold.model_copy(update={'evidentiality': 'direct'}), item)
        self.assertFalse(next(check['ok'] for check in checks if check['name'] == 'Evidentiality'))
        suspect = next(case for case in cases if case['family'] == 'suspicion')
        item = fixture_inputs(suspect)[0][0]
        gold = Candidate.model_validate(suspect['statements'][0]['passages'][0]['gold_claim'])
        checks, _ = rule_checks(gold.model_copy(update={'kind': 'event', 'text': 'Ravi moved the red bicycle.'}), item)
        self.assertFalse(next(check['ok'] for check in checks if check['name'] == 'Suspicion and opinion'))

    def test_nli_blocks_added_intent_even_when_verifier_accepts(self):
        class LenientVerifier(FixedModels):
            def entailment(self, pairs):
                return [{'entailment': .01, 'neutral': .98, 'contradiction': .01} for _ in pairs]

            def chat(self, model, role, instruction, data, schema, trace, inventory, budget):
                return schema.model_validate({'verdicts': [{'id': 'C1', 'supported': True, 'atomic': True,
                                                           'fields_supported': True, 'reason': 'Model accepted.'}]})

        case = next(c for c in load_corpus(DEFAULT_CORPUS) if c['family'] == 'inferred_connection')
        item = fixture_inputs(case)[0][0]
        gold = Candidate.model_validate(case['statements'][0]['passages'][0]['gold_claim'])
        claim = _claim(item, gold.model_copy(update={'text': 'Someone stole the red bicycle.'}))
        models = LenientVerifier()
        ClaimExtractor(models)._verify([(item, claim)], models.inventory(), [])
        self.assertEqual(claim['status'], 'escalated')
        self.assertFalse(next(check['ok'] for check in claim['checks'] if check['name'] == 'One-way NLI'))

    def test_conflict_choice_preserves_both_accounts(self):
        case = next(c for c in load_corpus(DEFAULT_CORPUS) if c['family'] == 'possible_conflict')
        documents = fixture_inputs(case)
        runs = make_gold_runs(case, documents)
        graph = fixture_graph(case, documents, runs)
        self.assertFalse(any(c['handoff'] for c in graph['claims']))
        issue = next(i for i in graph['issues'] if i['name'] == 'Possible source conflict')
        chosen = issue['claim_ids'][0]
        decision = {'id': 'audit-1', 'target': issue['id'], 'action': 'prefer', 'other': chosen,
                    'officer': 'Test', 'reason': 'Officer reviewed both source accounts.'}
        validate_decision(graph, issue['id'], decision)
        revised = build({**graph, 'decisions': [decision]})
        self.assertEqual(len(revised['claims']), 2)
        self.assertEqual([c['id'] for c in revised['claims'] if c['handoff']], [chosen])
        self.assertEqual(len(revised['inferred_connections']), 1)

    def test_ambiguous_date_and_overlap(self):
        received = '2026-11-06T10:30:00+05:30'
        a = normalize_time('last night at about 2 AM', received)
        b = normalize_time('6 November 2026 at 2:15 AM', received)
        self.assertEqual(a['start'], '2026-11-06T01:30:00+05:30')
        self.assertFalse(disjoint(a, b))
        self.assertIsNone(normalize_time('night of 5 October 2026 at 2 AM', received)['start'])


class WorkflowModels(FixedModels):
    def chat(self, model, role, instruction, data, schema, trace, inventory, budget):
        if role == 'Claim extraction':
            def fields(item):
                quote = item['english']
                return {'quote': quote, 'text': quote, 'kind': 'event', 'actor': 'I', 'act': 'see',
                        'object': 'a red car', 'place': None, 'time': None, 'quantity': None, 'knowledge': None,
                        'evidentiality': 'direct', 'polarity': 'affirmed', 'certainty': 'unspecified'}
            return schema.model_validate({'passages': [{'id': i['id'], 'claims': [fields(i)]} for i in data['passages']]})
        if role == 'Independent claim verification':
            return schema.model_validate({'verdicts': [{'id': item['id'], 'supported': True, 'atomic': True,
                                                       'fields_supported': False, 'reason': 'Needs officer review.'}
                                                      for item in data['claims']]})
        return super().chat(model, role, instruction, data, schema, trace, inventory, budget)


class ClaimWorkflowTests(unittest.TestCase):
    def test_retry_archives_the_failed_run(self):
        class RetryModels(WorkflowModels):
            blocked = True

            def chat(self, model, role, instruction, data, schema, trace, inventory, budget):
                if role == 'Claim extraction' and self.blocked:
                    raise ModelError('Local test service unavailable.')
                return super().chat(model, role, instruction, data, schema, trace, inventory, budget)

        models = RetryModels()
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(folder, lambda: models)) as client:
            created = client.post('/api/cases', files={'file': ('report.txt', b'I saw a red car.')}).json()
            base = f'/api/cases/{created["id"]}/graph'

            def completed_graph():
                for _ in range(150):
                    result = client.get(base).json()
                    if not result['pending']:
                        return result
                    time.sleep(.03)
                self.fail('The claim queue did not finish.')

            failed = completed_graph()
            issue = next(i for i in failed['issues'] if i['name'] == 'Local model')
            models.blocked = False
            response = client.post(base + '/decisions', json={'version': failed['version'], 'target': issue['id'],
                                   'officer': 'Test', 'reason': 'Local service restored.', 'action': 'retry'})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(len(completed_graph()['claims']), 1)
            runs = client.get(base + '/audit').json()['runs']
            self.assertEqual(len(runs), 2)
            self.assertTrue(any(run['retry_decision'] == response.json()['id'] and not run['current'] for run in runs))

    def test_witness_officer_confirmation_and_intake_revocation(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(folder, WorkflowModels)) as client:
            raw = ('report.txt', b'I saw a red car.')
            response = client.post('/api/cases', files={'file': raw})
            case_id = response.json()['id']
            response = client.post('/api/cases', files={'file': raw}, data={'parent_case': case_id, 'reporter': 'Witness A'})
            self.assertEqual(response.status_code, 202)
            witness = response.json()['id']
            self.assertEqual(len(client.get('/api/cases').json()), 1)
            self.assertEqual(client.get(f'/api/cases/{case_id}').json()['parent_case'], None)
            self.assertEqual(client.get(f'/api/cases/{witness}').json()['parent_case'], case_id)
            base = f'/api/cases/{case_id}/graph'
            for _ in range(150):
                graph = client.get(base).json()
                if not graph['pending']:
                    break
                time.sleep(.03)
            self.assertEqual(len(graph['statements']), 2)
            self.assertEqual(len(graph['claims']), 2)
            self.assertEqual(client.get(base + '/handoff').json()['claims'], [])
            self.assertEqual(client.get(base + '/handoff?finalize=true').status_code, 409)
            target = next(c for c in graph['claims'] if c['reporter'] == 'Witness A')
            decision = {'version': graph['version'], 'target': target['id'], 'officer': 'Test',
                        'action': 'confirm', 'reason': 'Officer inspected the exact quote.'}
            self.assertEqual(client.post(base + '/decisions', json=decision).status_code, 201)
            self.assertEqual(client.post(base + '/decisions', json=decision).status_code, 409)
            cleared = client.get(base + '/handoff').json()
            self.assertEqual([c['status'] for c in cleared['claims']], ['officer_confirmed'])
            self.assertEqual(cleared['claims'][0]['reporter'], 'Witness A')
            feedback = {'revision': 1, 'officer': 'Test', 'kind': 'possible_escape', 'detail': 'Review source.'}
            self.assertEqual(client.post(f'/api/cases/{witness}/passages/H01/feedback', json=feedback).status_code, 201)
            self.assertEqual(client.get(base + '/handoff').json()['claims'], [])
            audit = client.get(base + '/audit').json()
            self.assertEqual(len(audit['decisions']), 1)
            self.assertTrue(any(run['evidence']['document_id'] == witness and not run['current'] for run in audit['runs']))


if __name__ == '__main__':
    unittest.main()
