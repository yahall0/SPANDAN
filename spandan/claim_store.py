import json
from uuid import uuid4

from .rules import digest
from .store import encode, now
from .validation import passed_all_layers

BATCH_SIZE = 5


def _input(row):
    item = dict(row)
    checked = json.loads(item.pop('result'))
    if not passed_all_layers(checked):
        return None
    item['english'] = checked['checked_english']
    attempt = checked['attempts'][-1]
    item['frames'] = attempt['frames'].get('A', {}).get('statements', [])
    item['evidence'] = checked['evidence']
    item['validator_version'] = checked['validator_version']
    item['checked_hash'] = digest(item['english'])
    # Frames are produced against masked English. Match evidence against the checked handoff.
    restore = {**checked.get('vault', {}), **checked.get('glossary', {})}
    for frame in item['frames']:
        for field, value in frame.items():
            if isinstance(value, str):
                for token, original in restore.items():
                    value = value.replace(token, original)
                frame[field] = value
    return item


def next_batch(store):
    with store.connection() as conn:
        rows = conn.execute('''
            WITH ready AS (
                SELECT p.rowid AS queue, p.document_id, p.id, p.page, p.start, p.end, p.source,
                    p.effective_source, p.revision, p.result, d.received_at, d.format, d.sha256,
                    COALESCE(w.case_id,d.id) AS case_id, COALESCE(w.reporter,'Complainant') AS reporter
                FROM passages p JOIN documents d ON d.id=p.document_id
                LEFT JOIN witness_statements w ON w.document_id=d.id
                LEFT JOIN claim_runs c ON c.document_id=p.document_id AND c.passage_id=p.id
                    AND c.revision=p.revision
                WHERE p.status='pass' AND c.document_id IS NULL
            )
            SELECT * FROM ready WHERE document_id=(SELECT document_id FROM ready ORDER BY queue LIMIT 1)
            ORDER BY queue LIMIT ?
        ''', (BATCH_SIZE,)).fetchall()
    return [item for row in rows if (item := _input(row))]


def save_batch(store, batch, results):
    with store.connection() as conn:
        for item in batch:
            conn.execute('''
                INSERT OR IGNORE INTO claim_runs (document_id, passage_id, revision, created_at, result)
                SELECT document_id, id, revision, ?, ? FROM passages
                WHERE document_id=? AND id=? AND revision=? AND status='pass'
            ''', (now(), encode(results[item['id']]), item['document_id'], item['id'], item['revision']))


def case_rows(conn, case_id):
    root = conn.execute('''
        SELECT d.id, d.filename, d.case_ref, d.received_at, 'Complainant' AS reporter, 'complainant' AS kind
        FROM documents d WHERE d.id=? AND NOT EXISTS (SELECT 1 FROM witness_statements WHERE document_id=d.id)
    ''', (case_id,)).fetchone()
    if root is None:
        return None
    witnesses = conn.execute('''
        SELECT d.id, d.filename, d.case_ref, d.received_at, w.reporter, 'witness' AS kind
        FROM witness_statements w JOIN documents d ON d.id=w.document_id WHERE w.case_id=? ORDER BY d.rowid
    ''', (case_id,)).fetchall()
    passages = conn.execute('''
        SELECT p.document_id, p.id, p.status, p.revision, p.result AS intake, c.result AS extracted
        FROM passages p LEFT JOIN claim_runs c ON c.document_id=p.document_id AND c.passage_id=p.id
            AND c.revision=p.revision
        WHERE p.document_id=? OR p.document_id IN (SELECT document_id FROM witness_statements WHERE case_id=?)
        ORDER BY p.rowid
    ''', (case_id, case_id)).fetchall()
    decisions = conn.execute('SELECT * FROM claim_decisions WHERE case_id=? ORDER BY rowid', (case_id,)).fetchall()
    entries = []
    pending = []
    for p in passages:
        if p['status'] == 'pass' and p['intake'] and passed_all_layers(json.loads(p['intake'])):
            if p['extracted']:
                entries.append(json.loads(p['extracted']))
                continue
            status = 'claim_queue'
        else:
            status = p['status']
        pending.append({'document_id': p['document_id'], 'passage_id': p['id'],
                        'revision': p['revision'], 'status': status})
    audit = [{**json.loads(row['detail']), 'id': row['id'], 'target': row['target'], 'recorded_at': row['created_at']}
             for row in decisions]
    version = digest([[e['id'] for e in entries], pending, [entry['id'] for entry in audit]])
    return {'case': dict(root), 'statements': [dict(row) for row in (root, *witnesses)], 'runs': entries,
            'pending': pending, 'decisions': audit, 'version': version}


def record_decision(conn, case_id, target, detail):
    key = uuid4().hex
    conn.execute('INSERT INTO claim_decisions VALUES (?, ?, ?, ?, ?)', (key, case_id, target, now(), encode(detail)))
    return key


def retry_run(conn, run, decision_id):
    parameters = (run['document_id'], run['passage_id'], run['revision'])
    conn.execute('''
        INSERT INTO claim_run_archive
        SELECT document_id, passage_id, revision, created_at, result, ? FROM claim_runs
        WHERE document_id=? AND passage_id=? AND revision=?
    ''', (decision_id, *parameters))
    conn.execute('DELETE FROM claim_runs WHERE document_id=? AND passage_id=? AND revision=?', parameters)


def audit_runs(conn, case_id):
    rows = conn.execute('''
        SELECT c.result, c.created_at, COALESCE(p.status='pass' AND p.revision=c.revision,0) AS current, NULL AS retry_decision
        FROM claim_runs c JOIN passages p ON p.document_id=c.document_id AND p.id=c.passage_id
        WHERE c.document_id=? OR c.document_id IN (SELECT document_id FROM witness_statements WHERE case_id=?)
        UNION ALL
        SELECT result, created_at, 0 AS current, decision_id AS retry_decision FROM claim_run_archive
        WHERE document_id=? OR document_id IN (SELECT document_id FROM witness_statements WHERE case_id=?)
        ORDER BY created_at
    ''', (case_id, case_id, case_id, case_id)).fetchall()
    return [{**json.loads(row['result']), 'created_at': row['created_at'], 'current': bool(row['current']),
             'retry_decision': row['retry_decision']}
            for row in rows]
