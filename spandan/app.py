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
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

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


def _worker(store, validator, stopping, wake):
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


def create_app(data_dir=None, models_factory=LocalModels):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(data_dir or os.environ.get('SPANDAN_DATA_DIR', 'data'))
        with _worker_lock(store.folder):
            store.recover()
            models = models_factory()
            stopping, wake = threading.Event(), threading.Event()
            worker = threading.Thread(target=_worker, args=(store, Validator(models), stopping, wake), daemon=True)
            app.state.store, app.state.models, app.state.wake = store, models, wake
            worker.start()
            try:
                yield
            finally:
                stopping.set()
                wake.set()
                worker.join()
                models.close()

    app = FastAPI(title='SPANDAN Intake', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None)
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
                'nli': nli, 'independent': independent, 'validator_version': VALIDATOR_VERSION}

    @app.get('/api/cases')
    def cases(request: Request):
        return request.app.state.store.list()

    @app.post('/api/cases', status_code=202)
    def create_case(request: Request, file: UploadFile = File(), case_ref: str = Form('', max_length=100),
                    mode: Literal['live', 'deferred'] = Form('live'),
                    officer_language: Literal['en', 'hi', 'gu'] = Form('en')):
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
        key = request.app.state.store.create(filename, data, case_ref.strip(), mode, officer_language, blocks, passages)
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
