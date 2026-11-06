import logging
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .claim_graph import GRAPH_VERSION, build as build_graph, validate_decision
from .claim_store import audit_runs, case_rows, record_decision, retry_run
from .claims import VERSION as CLAIMS_VERSION, claim_worker
from .intake import MAX_PASSAGE, MAX_UPLOAD, IntakeError, extract, normalize, segment
from .models import NLI_DIR, NLI_FILES, LocalModels
from .rules import Masker
from .store import Conflict, Store
from .validation import LANGUAGES, QUESTIONS, VALIDATOR_VERSION, Validator, passed_all_layers

WEB = Path(__file__).parent / 'web'
LOGGER = logging.getLogger('spandan')


@contextmanager
def _worker_lock(folder):
    with (folder / 'worker.lock').open('a+b') as lock:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _worker(store, validator, stopping, wake, claim_wake):
    while not stopping.is_set():
        wake.clear()
        job = store.claim()
        if job is None:
            wake.wait(1)
            continue
        passage, peers = job
        try:
            masker = Masker([p['effective_source'] for p in peers])
            context = [{'id': p['id'], 'source': masker.mask(p['effective_source'][:MAX_PASSAGE])}
                       for p in peers if abs(p['ordinal'] - passage['ordinal']) == 1]
            result = validator.run(passage, masker, context, passage['officer_language'])
            if result['verdict'] == 'pass' and not passed_all_layers(result):
                raise RuntimeError('A passing result is missing required verification.')
        except Exception as error:
            LOGGER.error('Passage validation failed internally (%s).', type(error).__name__)
            result = {'verdict': 'clarify', 'slot': 'service', 'validator_version': VALIDATOR_VERSION,
                      'reason': 'An internal error prevented verification. Recheck this passage.',
                      'question': QUESTIONS[passage['officer_language']]['service'], 'attempts': []}
        store.finish(passage, result)
        if result['verdict'] == 'pass':
            claim_wake.set()


class Review(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    revision: int = Field(ge=1)
    officer: str = Field(min_length=1, max_length=100)
    action: Literal['clarify', 'unresolved', 'retry']
    answer: str = Field(default='', max_length=MAX_PASSAGE)
    corrected_source: str = Field(default='', max_length=MAX_PASSAGE)
    language: str = Field(default='', max_length=3)

    @model_validator(mode='after')
    def validate_review(self):
        if self.language and self.language not in LANGUAGES:
            raise ValueError('Choose a supported language code.')
        if self.action == 'clarify' and (not self.answer or not (self.corrected_source or self.language)):
            raise ValueError('Record the answer and a corrected passage or confirmed language.')
        if self.action == 'unresolved' and not self.answer:
            raise ValueError('Record why the complainant is uncertain.')
        if self.action != 'clarify' and (self.corrected_source or self.language):
            raise ValueError('Only a clarification can change the effective source or language.')
        self.corrected_source = normalize(self.corrected_source)
        return self


class Feedback(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    revision: int = Field(ge=1)
    officer: str = Field(min_length=1, max_length=100)
    kind: Literal['possible_escape', 'false_alarm', 'readback_conflict', 'downstream_conflict', 'audit_note']
    detail: str = Field(min_length=1, max_length=2000)


class ClaimDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    version: str = Field(min_length=64, max_length=64)
    target: str = Field(min_length=24, max_length=24)
    officer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1500)
    action: Literal['confirm', 'correct', 'unresolved', 'prefer', 'merge', 'retry']
    other: str | None = Field(default=None, max_length=24)
    field: Literal['text', 'kind', 'actor', 'act', 'object', 'place', 'time', 'quantity', 'knowledge',
                   'evidentiality', 'polarity', 'certainty'] | None = None
    value: str | None = Field(default=None, max_length=500)

    @model_validator(mode='after')
    def check_action(self):
        if self.action == 'correct' and not self.field:
            raise ValueError('Select which field to correct.')
        if self.action in ('prefer', 'merge') and not self.other:
            raise ValueError('Select the claim or entity for this decision.')
        return self


def create_app(data_dir=None, models_factory=LocalModels):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(data_dir or os.environ.get('SPANDAN_DATA_DIR', 'data'))
        with _worker_lock(store.folder):
            store.recover()
            models = models_factory()
            stopping, wake, claim_wake = threading.Event(), threading.Event(), threading.Event()
            worker = threading.Thread(target=_worker, args=(store, Validator(models), stopping, wake, claim_wake), daemon=True)
            claim_thread = threading.Thread(target=claim_worker, args=(store, models, stopping, claim_wake), daemon=True)
            app.state.store, app.state.models, app.state.wake, app.state.claim_wake = store, models, wake, claim_wake
            worker.start()
            claim_thread.start()
            try:
                yield
            finally:
                stopping.set()
                wake.set()
                claim_wake.set()
                worker.join()
                claim_thread.join()
                models.close()

    app = FastAPI(title='SPANDAN', version='0.2.0', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    app.mount('/assets', StaticFiles(directory=WEB, check_dir=False), name='assets')

    @app.middleware('http')
    async def local_request(request, call_next):
        origin = request.headers.get('origin')
        fetch_site = request.headers.get('sec-fetch-site')
        if fetch_site == 'cross-site' or (origin and origin != f'{request.url.scheme}://{request.headers.get("host")}'):
            return JSONResponse({'detail': 'Open the app from its local address.'}, status_code=403)
        if request.method == 'POST':
            length = request.headers.get('content-length')
            if length is None or not length.isdecimal():
                return JSONResponse({'detail': 'A valid request length is required.'}, status_code=411)
            if int(length) > MAX_UPLOAD + 64 * 1024:
                return JSONResponse({'detail': 'The upload limit is 10 MB.'}, status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(Conflict)
    def conflict_handler(_, error):
        return JSONResponse({'detail': str(error)}, status_code=409)

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(WEB / 'index.html')

    @app.get('/api/status')
    def status(request: Request):
        models = request.app.state.models
        available = models.inventory()
        required = list(dict.fromkeys((models.a, models.b, models.verifier)))
        nli = all((NLI_DIR / file).is_file() for file in NLI_FILES)
        independent = bool(available.get(models.a) and available.get(models.b) and available[models.a] != available[models.b])
        return {'ready': nli and independent and all(model in available for model in required),
                'models': [{'name': model, 'ready': model in available} for model in required],
                'nli': nli, 'independent': independent, 'validator_version': VALIDATOR_VERSION,
                'claim_version': CLAIMS_VERSION, 'graph_version': GRAPH_VERSION}

    @app.get('/api/cases')
    def cases(request: Request):
        return request.app.state.store.list()

    @app.post('/api/cases', status_code=202)
    def create_case(request: Request, file: UploadFile = File(), case_ref: str = Form('', max_length=100),
                    mode: Literal['live', 'deferred'] = Form('live'),
                    officer_language: Literal['en', 'hi', 'gu'] = Form('en'),
                    parent_case: str = Form('', max_length=32), reporter: str = Form('', max_length=100)):
        store = request.app.state.store
        if parent_case:
            if not reporter.strip():
                raise HTTPException(422, 'Enter the witness name or reference.')
            with store.connection() as conn:
                root = conn.execute('''
                    SELECT 1 FROM documents d WHERE d.id=? AND NOT EXISTS (
                        SELECT 1 FROM witness_statements WHERE document_id=d.id
                    )
                ''', (parent_case,)).fetchone()
                if root is None:
                    raise HTTPException(404, 'Parent complaint not found.')
        data = file.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, 'The upload limit is 10 MB.')
        filename = Path((file.filename or '').replace('\\', '/')).name
        if not filename or len(filename) > 255:
            raise HTTPException(422, 'Supply a valid document filename.')
        try:
            blocks = extract(filename, data)
            passages = segment(blocks)
            if not passages:
                raise IntakeError('No passages could be extracted.')
            if len(passages) > 250:
                raise IntakeError('Split documents longer than 250 passages into smaller files.')
        except IntakeError as error:
            raise HTTPException(422, str(error)) from error
        key = store.create(filename, data, case_ref.strip(), mode, officer_language, blocks, passages,
                           parent_case or None, reporter.strip())
        request.app.state.wake.set()
        return {'id': key, 'passages': len(passages)}

    def case_or_404(request, case_id):
        case = request.app.state.store.document(case_id)
        if case is None:
            raise HTTPException(404, 'Case not found.')
        return case

    @app.get('/api/cases/{case_id}')
    def case_detail(request: Request, case_id: str):
        return case_or_404(request, case_id)

    @app.get('/api/cases/{case_id}/original')
    def original(request: Request, case_id: str):
        row = request.app.state.store.original(case_id)
        if row is None:
            raise HTTPException(404, 'Case not found.')
        return Response(row['original'], media_type='application/octet-stream',
                        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(row['filename'])}"})

    @app.get('/api/cases/{case_id}/handoff')
    def handoff(request: Request, case_id: str, finalize: bool = False):
        case = case_or_404(request, case_id)
        checked, pending = [], []
        for passage in case['passages']:
            result = passage['result']
            if passage['status'] == 'pass' and result and passed_all_layers(result):
                checked.append({'id': passage['id'], 'english': result['checked_english'], 'page': passage['page'],
                                'start': passage['start'], 'end': passage['end'], 'revision': passage['revision'],
                                'source_hash': result['evidence']['source_hash'],
                                'validator_version': result['validator_version']})
            else:
                pending.append({'id': passage['id'], 'status': passage['status']})
        if finalize and pending:
            raise HTTPException(409, 'Finalization is blocked until every passage passes verification.')
        return {'document_id': case_id, 'document_sha256': case['sha256'], 'output_language': 'en',
                'complete': not pending, 'passages': checked, 'pending': pending}

    def graph_or_404(conn, case_id):
        snapshot = case_rows(conn, case_id)
        if snapshot is None:
            raise HTTPException(404, 'Complaint not found.')
        return build_graph(snapshot)

    @app.get('/api/cases/{case_id}/graph')
    def graph(request: Request, case_id: str):
        with request.app.state.store.connection() as conn:
            conn.execute('BEGIN')
            result = graph_or_404(conn, case_id)
        result.pop('runs')
        return result

    @app.get('/api/cases/{case_id}/graph/audit')
    def graph_audit(request: Request, case_id: str):
        with request.app.state.store.connection() as conn:
            conn.execute('BEGIN')
            result = graph_or_404(conn, case_id)
            runs = audit_runs(conn, case_id)
        return {'runs': runs, 'decisions': result['decisions'], 'graph_version': result['graph_version']}

    @app.get('/api/cases/{case_id}/graph/handoff')
    def graph_handoff(request: Request, case_id: str, finalize: bool = False):
        with request.app.state.store.connection() as conn:
            conn.execute('BEGIN')
            result = graph_or_404(conn, case_id)
        checked = [c for c in result['claims'] if c['handoff']]
        ids = {c['id'] for c in checked}
        links = [link for link in result['order_links'] if set(link['claim_ids']) <= ids]
        ordered = {endpoint for link in links for endpoint in (link['from'], link['to'])}
        events = [{**{k: v for k, v in event.items() if k != 'english'}, 'unordered': event['id'] not in ordered}
                  for event in result['events'] if event['claim_id'] in ids]
        lane_ids = {event['lane_id'] for event in events}
        entities = [{**entity, 'claim_ids': [i for i in entity['claim_ids'] if i in ids],
                     'mentions': [m for m in entity['mentions'] if m['claim_id'] in ids]}
                    for entity in result['entities'] if set(entity['claim_ids']) & ids]
        pending = result['pending']
        review = [c['id'] for c in result['claims'] if not c['handoff'] and c.get('disposition') != 'other_account_preserved']
        issue_ids = [i['id'] for i in result['issues'] if not i['decision'] or i['decision']['action'] == 'unresolved']
        complete = not (pending or review or issue_ids)
        if finalize and not complete:
            raise HTTPException(409, 'Pending passages, claims or issues need review.')
        return {'case_id': case_id, 'complete': complete, 'version': result['version'], 'claims': checked,
                'entities': entities, 'events': events, 'order_links': links,
                'lanes': [lane for lane in result['lanes'] if lane['id'] in lane_ids],
                'entity_links': [link for link in result['entity_links'] if set(link['claim_ids']) <= ids],
                'pending': pending, 'review': review, 'issues': issue_ids}

    @app.post('/api/cases/{case_id}/graph/decisions', status_code=201)
    def graph_decision(request: Request, case_id: str, body: ClaimDecision):
        with request.app.state.store.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = graph_or_404(conn, case_id)
            if result['version'] != body.version:
                raise Conflict('The graph changed. Reload it before recording a decision.')
            detail = body.model_dump(exclude={'version', 'target'})
            try:
                validate_decision(result, body.target, detail)
            except (ValueError, ValidationError) as error:
                raise HTTPException(422, str(error)) from error
            key = record_decision(conn, case_id, body.target, detail)
            if body.action == 'retry':
                retry_run(conn, detail['run'], key)
        if body.action == 'retry':
            request.app.state.claim_wake.set()
        return {'id': key}

    @app.get('/api/cases/{case_id}/passages/{passage_id}/history')
    def history(request: Request, case_id: str, passage_id: str):
        history = request.app.state.store.history(case_id, passage_id)
        if history is None:
            raise HTTPException(404, 'Passage not found.')
        return history

    @app.post('/api/cases/{case_id}/passages/{passage_id}/review', status_code=202)
    def review(request: Request, case_id: str, passage_id: str, body: Review):
        update = request.app.state.store.review(case_id, passage_id, body.model_dump())
        if update is None:
            raise HTTPException(404, 'Passage not found.')
        request.app.state.wake.set()
        return update

    @app.post('/api/cases/{case_id}/passages/{passage_id}/feedback', status_code=201)
    def feedback(request: Request, case_id: str, passage_id: str, body: Feedback):
        key = request.app.state.store.add_feedback(case_id, passage_id, **body.model_dump())
        if key is None:
            raise HTTPException(404, 'Passage not found.')
        return {'id': key}

    return app


app = create_app()
