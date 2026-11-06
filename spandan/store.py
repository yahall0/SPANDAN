import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .rules import digest


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _decoded(row, *fields):
    result = dict(row)
    for field in fields:
        if result[field] is not None:
            result[field] = json.loads(result[field])
    return result


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.database = self.folder / 'complaints.sqlite3'
        with self.connection() as conn:
            conn.executescript('''
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, case_ref TEXT NOT NULL, filename TEXT NOT NULL,
                    format TEXT NOT NULL, original BLOB NOT NULL, sha256 TEXT NOT NULL,
                    received_at TEXT NOT NULL, source_text TEXT NOT NULL,
                    mode TEXT NOT NULL, officer_language TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS passages (
                    document_id TEXT NOT NULL REFERENCES documents(id), id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL, page INTEGER, start INTEGER NOT NULL, end INTEGER NOT NULL,
                    source TEXT NOT NULL, effective_source TEXT NOT NULL, language_override TEXT,
                    revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'queued',
                    result TEXT, PRIMARY KEY(document_id, id)
                );
                CREATE INDEX IF NOT EXISTS passage_queue ON passages(status);
                CREATE TABLE IF NOT EXISTS runs (
                    document_id TEXT NOT NULL, passage_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, input TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(document_id, passage_id, revision),
                    FOREIGN KEY(document_id, passage_id) REFERENCES passages(document_id, id)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    document_id TEXT NOT NULL, passage_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, officer TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
                    PRIMARY KEY(document_id, passage_id, revision),
                    FOREIGN KEY(document_id, passage_id) REFERENCES passages(document_id, id)
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, passage_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL, officer TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL,
                    FOREIGN KEY(document_id, passage_id) REFERENCES passages(document_id, id)
                );
                CREATE TABLE IF NOT EXISTS witness_statements (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id),
                    case_id TEXT NOT NULL REFERENCES documents(id), reporter TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS witness_case ON witness_statements(case_id);
                CREATE TABLE IF NOT EXISTS claim_runs (
                    document_id TEXT NOT NULL, passage_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(document_id, passage_id, revision),
                    FOREIGN KEY(document_id, passage_id) REFERENCES passages(document_id, id)
                );
                CREATE TABLE IF NOT EXISTS claim_run_archive (
                    document_id TEXT NOT NULL, passage_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, result TEXT NOT NULL, decision_id TEXT NOT NULL,
                    FOREIGN KEY(document_id, passage_id) REFERENCES passages(document_id, id)
                );
                CREATE TABLE IF NOT EXISTS claim_decisions (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES documents(id),
                    target TEXT NOT NULL, created_at TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS claim_decision_case ON claim_decisions(case_id);
            ''')

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def recover(self):
        with self.connection() as conn:
            conn.execute("UPDATE passages SET status = 'queued' WHERE status = 'running'")

    def create(self, filename, data, case_ref, mode, officer_language, blocks, passages, parent=None, reporter=None,
               source_language=None):
        doc_id = uuid4().hex
        with self.connection() as conn:
            conn.execute('INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                         (doc_id, case_ref, filename, Path(filename).suffix.lstrip('.').lower(), data,
                          digest(data), now(), '\n\n'.join(block for _, block in blocks), mode, officer_language))
            if parent:
                conn.execute('INSERT INTO witness_statements VALUES (?, ?, ?)', (doc_id, parent, reporter))
            conn.executemany('''
                INSERT INTO passages
                (document_id, id, ordinal, page, start, end, source, effective_source, language_override)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', [(doc_id, p['id'], index, p['page'], p['start'], p['end'], p['source'], p['normalized'], source_language)
                  for index, p in enumerate(passages, 1)])
        return doc_id

    def claim(self):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''
                SELECT p.*, d.sha256 AS document_hash, d.officer_language
                FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE p.status = 'queued' ORDER BY p.rowid LIMIT 1
            ''').fetchone()
            if row is None:
                return None
            conn.execute("UPDATE passages SET status='running' WHERE document_id=? AND id=?",
                         (row['document_id'], row['id']))
            peers = conn.execute('''
                SELECT id, ordinal, effective_source FROM passages WHERE document_id=? ORDER BY ordinal
            ''', (row['document_id'],)).fetchall()
            return dict(row), [dict(peer) for peer in peers]

    def finish(self, passage, result):
        key = (passage['document_id'], passage['id'], passage['revision'])
        with self.connection() as conn:
            updated = conn.execute('''
                UPDATE passages SET status=?, result=?
                WHERE document_id=? AND id=? AND revision=? AND status='running'
            ''', (result['verdict'], encode(result), *key)).rowcount
            if updated:
                conn.execute('INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)',
                             (*key, now(), passage['effective_source'], encode(result)))

    def list(self):
        with self.connection() as conn:
            rows = conn.execute('''
                SELECT d.id, d.case_ref, d.filename, d.format, d.received_at, d.mode,
                    COUNT(*) AS total, SUM(p.status='pass') AS passed,
                    SUM(p.status IN ('queued','running')) AS pending,
                    SUM(p.status IN ('clarify','unresolved')) AS review
                FROM documents d
                LEFT JOIN witness_statements own ON own.document_id=d.id
                JOIN passages p ON p.document_id=d.id OR p.document_id IN (
                    SELECT document_id FROM witness_statements WHERE case_id=d.id
                )
                WHERE own.document_id IS NULL
                GROUP BY d.id ORDER BY d.rowid DESC
            ''').fetchall()
            return [dict(row) for row in rows]

    def document(self, doc_id):
        with self.connection() as conn:
            conn.execute('BEGIN')
            document = conn.execute('''
                SELECT d.id, d.case_ref, d.filename, d.format, d.sha256, d.received_at, d.mode,
                    d.officer_language, w.case_id AS parent_case
                FROM documents d LEFT JOIN witness_statements w ON w.document_id=d.id WHERE d.id=?
            ''', (doc_id,)).fetchone()
            if document is None:
                return None
            passages = conn.execute('SELECT * FROM passages WHERE document_id=? ORDER BY ordinal', (doc_id,))
            return {**document, 'output_language': 'en', 'passages': [_decoded(p, 'result') for p in passages]}

    def original(self, doc_id):
        with self.connection() as conn:
            return conn.execute('SELECT filename, original FROM documents WHERE id=?', (doc_id,)).fetchone()

    def review(self, doc_id, passage_id, review):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('''
                SELECT p.*, d.mode FROM passages p JOIN documents d ON d.id=p.document_id
                WHERE p.document_id=? AND p.id=?
            ''', (doc_id, passage_id)).fetchone()
            if row is None:
                return None
            if row['status'] not in ('clarify', 'unresolved') or row['revision'] != review['revision']:
                raise Conflict('The passage changed. Reload it before recording a review.')
            result = json.loads(row['result'])
            if review['action'] == 'retry' and result['slot'] != 'service':
                raise Conflict('Only a local inference failure can be retried without a clarification.')
            revision = row['revision'] + 1
            reviewed = now()
            detail = {**review, 'question': result['question'], 'mode': row['mode']}
            conn.execute('INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (doc_id, passage_id, revision, reviewed, review['officer'], review['action'], encode(detail)))
            status = 'unresolved' if review['action'] == 'unresolved' else 'queued'
            effective = review['corrected_source'] or row['effective_source']
            language = review['language'] or row['language_override']
            conn.execute('''
                UPDATE passages SET status=?, revision=?, effective_source=?, language_override=?, result=?
                WHERE document_id=? AND id=?
            ''', (status, revision, effective, language, row['result'] if status == 'unresolved' else None,
                  doc_id, passage_id))
            return {'status': status, 'revision': revision}

    def history(self, doc_id, passage_id):
        key = (doc_id, passage_id)
        with self.connection() as conn:
            conn.execute('BEGIN')
            row = conn.execute('SELECT 1 FROM passages WHERE document_id=? AND id=?', key).fetchone()
            if row is None:
                return None
            runs = conn.execute('SELECT * FROM runs WHERE document_id=? AND passage_id=? ORDER BY revision', key)
            reviews = conn.execute('SELECT * FROM reviews WHERE document_id=? AND passage_id=? ORDER BY revision', key)
            feedback = conn.execute('SELECT * FROM feedback WHERE document_id=? AND passage_id=? ORDER BY rowid', key)
            return {'runs': [_decoded(r, 'result') for r in runs], 'reviews': [_decoded(r, 'detail') for r in reviews],
                    'feedback': [dict(f) for f in feedback]}

    def add_feedback(self, doc_id, passage_id, revision, officer, kind, detail):
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT revision FROM passages WHERE document_id=? AND id=?',
                               (doc_id, passage_id)).fetchone()
            if row is None:
                return None
            if row['revision'] != revision:
                raise Conflict('The passage changed. Reload it before recording feedback.')
            key = uuid4().hex
            conn.execute('INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (key, doc_id, passage_id, revision, now(), officer, kind, detail))
            if kind in ('possible_escape', 'readback_conflict', 'downstream_conflict'):
                current = conn.execute('SELECT status, result FROM passages WHERE document_id=? AND id=?',
                                       (doc_id, passage_id)).fetchone()
                if current['status'] == 'pass':
                    held = json.loads(current['result'])
                    held.update(verdict='clarify', slot='statement',
                                reason='An officer recorded a possible accuracy problem.',
                                question='What does the original complaint say about the flagged detail?')
                    held.pop('checked_english', None)
                    conn.execute("UPDATE passages SET status='clarify', result=? WHERE document_id=? AND id=?",
                                 (encode(held), doc_id, passage_id))
            return key
