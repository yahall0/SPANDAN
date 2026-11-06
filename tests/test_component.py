import io
import tempfile
import time
import unittest
from zipfile import ZipFile

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from spandan.app import create_app
from spandan.intake import IntakeError, extract, segment
from spandan.models import ModelError
from spandan.rules import Masker, check_anchors
from spandan.validation import Validator, passed_all_layers


def docx(text):
    payload = io.BytesIO()
    with ZipFile(payload, 'w') as archive:
        archive.writestr('word/document.xml',
                         '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                         f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return payload.getvalue()


def pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if text:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})
        })
        contents = DecodedStreamObject()
        contents.set_data(f'BT /F1 14 Tf 30 720 Td ({text}) Tj ET'.encode('ascii'))
        page[NameObject('/Contents')] = contents
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class FixedModels:
    a, b, verifier = 'first', 'second', 'first'

    def __init__(self, a='I saw a red car.', b=None, fail=False):
        self.translations = {'Translation A': a, 'Translation B': b or a}
        self.fail = fail

    def inventory(self):
        return {self.a: 'a-version', self.b: 'b-version'}

    def close(self):
        pass

    def chat(self, model, role, instruction, data, schema, trace, inventory, budget):
        if self.fail:
            raise ModelError('Local model unavailable.')
        if role == 'Language identification':
            return schema(language='en', confidence=0.99)
        if role in self.translations:
            return schema(id=data['id'], english=self.translations[role])
        if role.startswith('Frame '):
            text = data['passage']
            object_ = 'blue car' if role == 'Frame source' and data['language'] == 'Hindi' else 'red car'
            return schema.model_validate({'ambiguous': False, 'statements': [
                {'evidence': text, 'speaker': 'I', 'evidentiality': 'direct', 'polarity': 'affirmed', 'tense': 'past',
                 'actor': 'I', 'act': 'see', 'object': object_, 'quantity': None, 'certainty': None,
                 'time': None, 'place': None, 'manner': None}
            ]})
        raise AssertionError(role)

    def entailment(self, pairs):
        return [{'entailment': 0.01, 'contradiction': 0.02, 'neutral': 0.97}
                if 'knife' in hypothesis else {'entailment': 0.99, 'contradiction': 0, 'neutral': 0.01}
                for _, hypothesis in pairs]


class ValidationTests(unittest.TestCase):
    def run_passage(self, models, source='मैंने नीली गाड़ी देखी।'):
        passage = {'id': 'H01', 'effective_source': source, 'start': 0, 'end': len(source),
                   'page': None, 'document_hash': 'synthetic', 'language_override': 'hi'}
        return Validator(models).run(passage, Masker([source]), [], 'en')

    def test_pdf_and_word_preserve_typed_text(self):
        complaint = 'I saw a red car.'
        self.assertEqual(segment(extract('report.pdf', pdf(complaint)))[0]['page'], 1)
        self.assertEqual(segment(extract('report.docx', docx(complaint)))[0]['source'], complaint)
        with self.assertRaisesRegex(IntakeError, 'no extractable typed text'):
            extract('scanned.pdf', pdf(''))

    def test_dependent_sentences_and_offsets(self):
        source = '  Dr. Rao came.\nBut he left.  \n\nI waited.'
        passages = segment([(1, source)])
        self.assertEqual(len(passages), 2)
        self.assertEqual(passages[0]['source'], 'Dr. Rao came.\nBut he left.')
        for passage in passages:
            self.assertEqual(source[passage['start']:passage['end']], passage['source'])

    def test_missing_protected_value_retries_then_holds(self):
        result = self.run_passage(FixedModels(), 'मैंने ९८२५० १२३४५ देखा।')
        self.assertEqual([attempt['verdict'] for attempt in result['attempts']], ['retry', 'clarify'])
        self.assertEqual(result['slot'], 'quantity')
        self.assertNotIn('checked_english', result)

    def test_numbers_and_polarity_have_hard_gates(self):
        self.assertFalse(check_anchors('मैंने दो लोगों को नहीं देखा।', 'I saw two people.')[1]['ok'])
        self.assertFalse(check_anchors('I paid ₹5,000. I received ₹500.', 'I paid ₹500. I received ₹5,000.')[0]['ok'])
        self.assertTrue(check_anchors('५ अक्टूबर २०२६ को रात करीब २ बजे', 'On 5 October 2026 at about 2 AM')[0]['ok'])

    def test_one_direction_is_insufficient_for_added_detail(self):
        result = self.run_passage(FixedModels(b='I saw a red car and a knife.'))
        self.assertEqual(result['verdict'], 'clarify')
        self.assertTrue(any(check['layer'] == 'L2' and not check['ok'] for check in result['attempts'][0]['checks']))

    def test_agreement_cannot_pass_a_shared_wrong_slot(self):
        result = self.run_passage(FixedModels())
        self.assertEqual(result['slot'], 'object')
        self.assertEqual(result['verdict'], 'clarify')
        self.assertTrue(any(check['layer'] == 'L2' and check['ok'] for check in result['attempts'][0]['checks']))
        self.assertNotIn('checked_english', result)

    def test_handoff_gate_requires_all_four_recorded_layers(self):
        result = {'verdict': 'pass', 'checked_english': 'I saw a red car.', 'attempts': [
            {'verdict': 'pass', 'checks': [{'ok': True, 'layer': layer} for layer in ('L0', 'L1', 'L2')]}
        ]}
        self.assertFalse(passed_all_layers(result))
        result['attempts'][0]['checks'].append({'ok': True, 'layer': 'L3'})
        self.assertTrue(passed_all_layers(result))


class WorkflowTests(unittest.TestCase):
    def wait_for(self, client, key, status):
        for _ in range(150):
            passage = client.get('/api/cases/' + key).json()['passages'][0]
            if passage['status'] == status:
                return passage
            time.sleep(0.02)
        self.fail(f'Passage did not reach {status}: {passage["status"]}')

    def test_original_handoff_review_and_history(self):
        raw = docx('I saw a red car.')
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(folder, FixedModels)) as client:
            response = client.post('/api/cases', files={'file': ('complaint.docx', raw)})
            self.assertEqual(response.status_code, 202)
            key = response.json()['id']
            base = '/api/cases/' + key
            passage = self.wait_for(client, key, 'pass')
            self.assertEqual(client.get(base + '/original').content, raw)
            self.assertTrue(client.get(base + '/handoff?finalize=true').json()['complete'])
            target = base + '/passages/H01'
            response = client.post(target + '/feedback', json={
                'revision': passage['revision'], 'officer': 'Test', 'kind': 'possible_escape', 'detail': 'Recheck.'
            })
            self.assertEqual(response.status_code, 201)
            self.assertEqual(client.get(base + '/handoff').json()['passages'], [])
            self.assertEqual(client.get(base + '/handoff?finalize=true').status_code, 409)
            review = {'revision': 1, 'officer': 'Test', 'action': 'clarify', 'answer': 'It was red.',
                      'corrected_source': 'I saw a red car.'}
            self.assertEqual(client.post(target + '/review', json=review).status_code, 202)
            self.assertEqual(client.post(target + '/review', json=review).status_code, 409)
            self.wait_for(client, key, 'pass')
            history = client.get(target + '/history').json()
            self.assertEqual([run['revision'] for run in history['runs']], [1, 2])
            self.assertEqual(history['reviews'][0]['detail']['answer'], 'It was red.')

    def test_missing_model_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder, TestClient(create_app(folder, lambda: FixedModels(fail=True))) as client:
            response = client.post('/api/cases', files={'file': ('complaint.pdf', pdf('I saw a red car.'))})
            key = response.json()['id']
            passage = self.wait_for(client, key, 'clarify')
            self.assertEqual(passage['result']['slot'], 'service')
            self.assertEqual(client.get('/api/cases/' + key + '/handoff').json()['passages'], [])


if __name__ == '__main__':
    unittest.main()
