'use strict';

const content = document.querySelector('#content');
const statusIndicator = document.querySelector('#system-status');
const toast = document.querySelector('#toast');
const labels = {pass: 'Verified', clarify: 'Review', unresolved: 'Unresolved', running: 'Checking', queued: 'Queued'};
const layers = {L0: 'Structure', L1: 'Meaning anchors', L2: 'English agreement', L3: 'Source verification'};
const languageNames = new Intl.DisplayNames(['en'], {type: 'language'});
let current = {key: '', signature: '', passage: '', draft: false};
let toastTimer;

const safe = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));
const timestamp = value => new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(value));
const badge = status => '<span class="status ' + safe(status) + '">' + safe(labels[status] || status) + '</span>';
const metric = (label, value) => '<div class="metric"><span>' + safe(label) + '</span><strong>' + safe(value) + '</strong></div>';
const passageUrl = (documentId, passageId) => '#case/' + safe(documentId) + '/' + safe(passageId);
const needsIntake = passage => ['clarify', 'unresolved'].includes(passage.status);
const excerpt = (value, limit = 105) => {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
};
const panel = (title, text, checked = false) => '<div class="text-panel ' + (checked ? 'checked' : '') +
  '"><h3>' + safe(title) + '</h3><p dir="auto">' + safe(text) + '</p></div>';

async function request(url, options) {
  const result = await fetch(url, options);
  if (!result.ok) {
    const error = await result.json().catch(() => ({}));
    const detail = typeof error.detail === 'string' ? error.detail : error.detail?.[0]?.msg;
    throw new Error(detail || 'The local request failed. Try again.');
  }
  return result.json();
}

function notify(message) {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
}

function route() {
  const hash = location.hash;
  const match = /^#(case|graph)\/([a-f0-9]{32})(?:\/(H\d+))?$/.exec(hash);
  return {key: match ? match[2] : hash === '#new' ? 'new' : 'home', view: match?.[1] || 'home', passage: match?.[3] || ''};
}

async function refresh(force = false) {
  const {key, view, passage} = route();
  if (key !== current.key || view !== current.view) current = {key, view, signature: '', passage, draft: false};
  if (current.routePassage !== passage) {
    current.routePassage = passage;
    current.passage = passage;
    current.passageFilter = 'all';
  }
  const state = current;
  if (!force && state.draft) return;
  try {
    const data = await request(key === 'home' || key === 'new' ? '/api/cases' : '/api/cases/' + key + (view === 'graph' ? '/graph' : ''));
    if (state !== current || key !== route().key || view !== route().view || passage !== route().passage || (!force && state.draft)) return;
    const signature = JSON.stringify(data);
    if (!force && signature === state.signature) return;
    // Preserve the officer's focus and expanded evidence while other passages finish.
    if (!force && (content.contains(document.activeElement) || content.querySelector('details[open]'))) return;
    state.signature = signature;
    state.data = data;
    content.innerHTML = Array.isArray(data) ? dashboard(data, key === 'new') : view === 'graph' ? graphView(data) : caseView(data);
    content.setAttribute('aria-busy', 'false');
    document.querySelector('#breadcrumb').textContent = view === 'graph' ? 'Claims and timeline' : key === 'new' ? 'New complaint' : key === 'home' ? 'Case intake' : 'Passage review';
    document.querySelector('#nav-cases').classList.toggle('active', key !== 'new');
    document.querySelector('#nav-new').classList.toggle('active', key === 'new');
  } catch (error) {
    if (state !== current) return;
    content.setAttribute('aria-busy', 'false');
    content.innerHTML = '<p class="notice" role="alert">' + safe(error.message) + '</p><a class="back" href="#">← Back to case intake</a>';
  }
}

function uploadCard() {
  return '<section class="card"><div class="card-title"><h2>New complaint</h2>' +
    '<p>Upload a typed document for local verification.</p></div><div class="card-body">' +
    '<form data-form="upload"><label class="field">Complaint document' +
    '<input name="file" type="file" accept=".pdf,.docx,.doc,.txt" required><small>PDF, DOCX, DOC or TXT · Up to 10 MB</small></label>' +
    '<label class="field">Case reference <input name="case_ref" maxlength="100" placeholder="Optional reference"></label>' +
    sourceLanguageControl() +
    '<div class="field-grid"><label class="field">Complainant<select name="mode"><option value="live">At the desk</option>' +
    '<option value="deferred">Call back later</option></select></label>' +
    '<label class="field">Officer language<select name="officer_language"><option value="en">English</option>' +
    '<option value="hi">हिन्दी</option><option value="gu">ગુજરાતી</option></select></label></div>' +
    '<p class="form-error" role="alert" hidden></p><button class="button" type="submit">Start verification <span aria-hidden="true">→</span></button>' +
    '</form></div></section>';
}

function sourceLanguageControl() {
  return '<label class="field">Document language<select name="source_language"><option value="">Detect automatically</option>' +
    '<option value="en">English · I confirm this document is in English</option></select>' +
    '<small>Applies to the uploaded text. Officer language controls review questions.</small></label>';
}

function dashboard(cases, isNew) {
  const count = key => cases.reduce((sum, item) => sum + item[key], 0);
  const rows = cases.map(item => '<a class="case-row" href="#case/' + safe(item.id) + '"><div><strong>' +
    safe(item.case_ref || item.filename) + '</strong><small>' + safe(item.case_ref ? item.filename + ' · ' : '') +
    safe(timestamp(item.received_at)) + ' · ' + item.passed + '/' + item.total + ' verified</small></div>' +
    badge(item.review ? 'clarify' : item.pending ? 'running' : 'pass') + '</a>').join('');
  return '<div class="heading"><div><span class="eyebrow">Component 01 · Complaint intake</span><h1>' +
    (isNew ? 'Intake a complaint' : 'Complaint workspace') + '</h1>' +
    '<p>Review typed complaints and prepare verified English for the next stage.</p></div>' +
    (isNew ? '<a class="button secondary" href="#">View cases</a>' : '<a class="button" href="#new">＋ New complaint</a>') + '</div>' +
    '<div class="metric-grid">' + metric('Complaints', cases.length) + metric('Verified passages', count('passed')) +
    metric('Needs review', count('review')) + metric('In progress', count('pending')) + '</div>' +
    '<div class="layout"><section class="card"><div class="card-title"><h2>Complaint register</h2>' +
    '<p>Open a case to see passage evidence and review questions.</p></div>' +
    (rows || '<div class="empty"><span aria-hidden="true">▤</span><h3>No complaints yet</h3><p>Upload a typed document to start a case.</p></div>') +
    '</section>' + uploadCard() + '</div>';
}

function caseView(doc) {
  const passages = doc.passages;
  const passed = passages.filter(p => p.status === 'pass').length;
  const review = passages.filter(needsIntake).length;
  const visible = current.passageFilter === 'review' ? passages.filter(needsIntake) : passages;
  const selected = visible.find(p => p.id === current.passage) || visible.find(needsIntake) || visible[0];
  const base = '/api/cases/' + safe(doc.id);
  return '<a class="back" href="#">← Case intake</a><div class="heading"><div><span class="eyebrow">Passage verification</span><h1>' +
    safe(doc.case_ref || doc.filename) + '</h1><p>' + safe(doc.case_ref ? doc.filename + ' · ' : '') +
    safe(doc.mode === 'live' ? 'Complainant at the desk' : 'Deferred call-back') + '</p></div><div class="actions">' +
    '<a class="button secondary" href="#graph/' + safe(doc.parent_case || doc.id) + '">Claims and timeline</a>' +
    '<button type="button" class="button secondary" data-case-refresh>Refresh</button>' +
    '<a class="button secondary" href="' + base + '/original">Original file</a>' +
    (passed ? '<a class="button" download="spandan-checked.json" href="' + base + '/handoff' +
      (passed === passages.length ? '?finalize=true">Download checked English' : '">Download verified passages') + '</a>' : '') +
    '</div></div><div class="case-meta"><span>Received ' + safe(timestamp(doc.received_at)) + '</span><span>English output</span>' +
    '<span title="' + safe(doc.sha256) + '">SHA-256 ' + safe(doc.sha256.slice(0, 12)) + '…</span></div>' +
    '<div class="metric-grid">' + metric('Total passages', passages.length) + metric('Verified', passed) +
    metric('Needs intake review', review) + metric('In progress', passages.length - passed - review) + '</div>' +
    '<p class="notice info">' + (review ? safe(review + ' passages need intake review before claim extraction can use them. ') : '') +
    'Verified here means the passage cleared intake. Open <a href="#graph/' + safe(doc.parent_case || doc.id) +
    '">Claims and timeline</a> to see which claims cleared the next stage.</p>' +
    '<div class="review-layout"><section class="card"><div class="card-title"><h2>Passages</h2>' +
    '<div class="review-filters">' + [['all', 'All passages'], ['review', 'Needs review']].map(([filter, label]) =>
      '<button type="button" class="button secondary" data-passage-filter="' + filter + '" aria-pressed="' +
      ((current.passageFilter || 'all') === filter) + '">' + label + '</button>').join('') + '</div></div><div class="passage-list">' +
    visible.map(p => '<button type="button" class="passage-button ' + (p.id === selected?.id ? 'selected' : '') +
      '" data-passage="' + safe(p.id) + '" aria-pressed="' + (p.id === selected.id) + '"><span><strong>' + safe(p.id) +
      '</strong><small class="passage-excerpt">' + safe(excerpt(p.source, 85)) + '</small></span>' + badge(p.status) + '</button>').join('') +
    '</div></section>' + (selected ? passageView(doc, selected, base) : '<p class="notice info">No passages need intake review.</p>') + '</div>';
}

function passageView(doc, p, base) {
  const result = p.result;
  const attempt = result?.attempts?.at(-1);
  let section = '<section class="card"><div class="card-title"><h2>Passage ' + safe(p.id) + ' ' + badge(p.status) + '</h2>' +
    '<p>' + (p.page ? 'PDF page ' + safe(p.page) + ' · ' : '') + 'Source characters ' + safe(p.start) + '–' + safe(p.end) +
    ' · Revision ' + safe(p.revision) + (result?.language ? (p.language_override ? ' · Confirmed ' : ' · Detected ') +
      safe(languageNames.of(result.language.language)) : '') + '</p></div><div class="card-body">' +
    '<div class="comparison">' + panel('Original source', p.source) +
    panel(p.status === 'pass' ? 'Verified English' : 'English output',
      p.status === 'pass' ? result.checked_english : 'Available after verification passes.', p.status === 'pass') + '</div>';
  if (p.revision > 1 && p.effective_source !== p.source) section += panel('Source used for this revision', p.effective_source);
  if (p.status === 'queued' || p.status === 'running') {
    section += '<p class="notice info">The local worker will process this passage. Other verified passages are available as they finish.</p>';
  }
  if (p.status === 'clarify' || p.status === 'unresolved') {
    const next = result.slot === 'language' ? 'Select the confirmed language and record a short review note.' :
      result.slot === 'service' ? 'Retry after the local models are available.' :
      'Compare the original with the reason below. Record a clarification if the source can be made clearer.';
    section += '<div class="notice"><strong>' + safe(p.status === 'unresolved' ? 'Held as unresolved' : 'Officer review required') +
      '</strong><p>' + safe(result.reason) + '</p><p dir="auto">' + safe(result.question) + '</p><p>' + safe(next) +
      '</p></div>' + reviewForm(doc, p, base);
  }
  if (attempt) {
    section += '<div class="checks">' + Object.entries(layers).map(([layer, title]) => layerDetail(layer, title, attempt.checks)).join('') + '</div>';
    if (attempt.translations.A || attempt.translations.B) {
      section += '<details class="fold"><summary>Local model readings</summary><div class="comparison">' +
        panel('Path A', attempt.translations.A || 'Unavailable') + panel('Path B', attempt.translations.B || 'Unavailable') + '</div></details>';
    }
  }
  section += '<details class="fold" data-history="' + base + '/passages/' + safe(p.id) + '/history"><summary>Audit history</summary>' +
    '<pre>Open to load history.</pre></details>';
  if (result) section += feedbackForm(p, base);
  return section + '</div></section>';
}

function layerDetail(layer, title, allChecks) {
  const checks = allChecks.filter(check => check.layer === layer);
  const state = checks.length ? checks.every(check => check.ok) ? 'pass' : 'clarify' : 'Not run';
  return '<details class="check"><summary>' + layer + ' · ' + title + ' ' + badge(state) + '</summary>' +
    (checks.map(check => '<p><strong>' + safe(check.name) + '</strong> · ' + safe(check.ok ? 'Passed' : 'Failed') +
      '<br>' + safe(check.detail) + (check.expected === undefined ? '' : '<br>Expected ' + safe(JSON.stringify(check.expected)) +
        ' · Observed ' + safe(JSON.stringify(check.observed))) + '</p>').join('') || '<p>A preceding check stopped this passage.</p>') +
    '</details>';
}

function reviewForm(doc, p, base) {
  const action = p.result.slot === 'service' ? '<option value="retry">Recheck after fixing local inference</option>' : '';
  return '<form data-form="review" data-url="' + base + '/passages/' + safe(p.id) + '/review" data-revision="' + p.revision + '">' +
    '<div class="field-grid"><label class="field">Officer name or ID<input name="officer" maxlength="100" required></label>' +
    '<label class="field">Review action<select name="action"><option value="clarify">Record clarification and recheck</option>' +
    '<option value="unresolved">Leave passage unresolved</option>' + action + '</select></label></div>' +
    '<label class="field">Answer or review note<textarea name="answer" maxlength="750" rows="2"></textarea></label>' +
    '<label class="field">Corrected passage<textarea name="corrected_source" maxlength="750" rows="3" dir="auto" ' +
    'placeholder="Enter the full confirmed passage if the wording changed"></textarea><small>Include all material details from the complainant’s statement.</small></label>' +
    '<label class="field">Confirmed language<input name="language" list="intake-languages" maxlength="3" value="' + safe(p.language_override || '') +
    '" placeholder="Choose a language or enter its code"><datalist id="intake-languages">' +
    ['en', 'hi', 'gu', 'bn', 'mr', 'pa', 'ur', 'ta', 'te', 'kn', 'ml', 'or', 'as', 'ne', 'mul'].map(code =>
      '<option value="' + code + '">' + safe(languageNames.of(code)) + '</option>').join('') + '</datalist></label>' +
    '<p class="form-error" role="alert" hidden></p><div class="actions"><button class="button" type="submit">Record review</button>' +
    '<span class="subtle">' + safe(doc.mode === 'live' ? 'Ask at the desk' : 'Queue the question for a call-back') + '</span></div></form>';
}

function feedbackForm(p, base) {
  return '<details class="fold"><summary>Record verification feedback</summary><form data-form="feedback" data-url="' + base +
    '/passages/' + safe(p.id) + '/feedback" data-revision="' + p.revision + '"><div class="field-grid">' +
    '<label class="field">Officer name or ID<input name="officer" maxlength="100" required></label>' +
    '<label class="field">Signal<select name="kind"><option value="possible_escape">Possible accuracy problem</option>' +
    '<option value="false_alarm">Possible false alarm</option><option value="readback_conflict">Read-back conflict</option>' +
    '<option value="downstream_conflict">Downstream conflict</option><option value="audit_note">Audit note</option></select></label></div>' +
    '<label class="field">Evidence<textarea name="detail" maxlength="2000" required></textarea></label>' +
    '<p class="form-error" role="alert" hidden></p><div class="actions"><button class="button secondary" type="submit">Save feedback</button></div></form></details>';
}

content.addEventListener('input', event => {
  if (event.target.closest('form')) current.draft = true;
});

content.addEventListener('click', event => {
  const button = event.target.closest('[data-passage], [data-passage-filter], [data-case-refresh]');
  if (!button) return;
  if (current.draft && !window.confirm('Leave this passage and discard your unsaved review?')) return;
  current.draft = false;
  if (button.dataset.passageFilter) current.passageFilter = button.dataset.passageFilter;
  else if (button.dataset.passage) current.passage = button.dataset.passage;
  refresh(true);
});

content.addEventListener('toggle', async event => {
  const details = event.target;
  if (!details.open || !details.dataset.history || details.dataset.loaded) return;
  const output = details.querySelector('pre');
  try {
    output.textContent = JSON.stringify(await request(details.dataset.history), null, 2);
    details.dataset.loaded = 'true';
  } catch (error) {
    output.textContent = error.message;
  }
}, true);

content.addEventListener('submit', async event => {
  const form = event.target.closest('[data-form]');
  if (!form) return;
  event.preventDefault();
  const submit = form.querySelector('button[type=submit]');
  const errorOutput = form.querySelector('.form-error');
  const type = form.dataset.form;
  const data = new FormData(form);
  errorOutput.hidden = true;
  submit.disabled = true;
  current.draft = true;
  try {
    if (type === 'upload' || type === 'witness') {
      const created = await request('/api/cases', {method: 'POST', body: data});
      current.draft = false;
      if (type === 'witness') {
        await refresh(true);
        notify('Witness statement queued for verification.');
      } else {
        location.hash = 'case/' + created.id;
        notify('Complaint queued for verification.');
      }
    } else {
      const body = {...Object.fromEntries(data), revision: Number(form.dataset.revision)};
      if (type === 'review' && body.action !== 'clarify') {
        body.corrected_source = '';
        body.language = '';
      }
      if (type === 'decision') {
        delete body.revision;
        body.version = form.dataset.version;
        body.target = form.dataset.target;
        for (const field of ['field', 'value', 'other']) body[field] = body[field] || null;
      }
      await request(form.dataset.url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      current.draft = false;
      await refresh(true);
      notify(type === 'review' ? 'Officer review saved.' : type === 'decision' ? 'Graph decision recorded.' : 'Feedback saved.');
    }
  } catch (error) {
    errorOutput.textContent = error.message;
    errorOutput.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

async function updateStatus() {
  try {
    const status = await request('/api/status');
    statusIndicator.classList.toggle('ready', status.ready);
    statusIndicator.lastChild.textContent = status.ready ? ' Models ready' : ' Model setup needed';
    statusIndicator.title = status.ready ? status.models.map(model => model.name).join(', ') :
      'Run python -m spandan.setup_models and start Ollama.';
  } catch {
    statusIndicator.classList.remove('ready');
    statusIndicator.lastChild.textContent = ' App offline';
  }
}

window.addEventListener('hashchange', () => refresh(true));
refresh(true);
updateStatus();
setInterval(refresh, 1800);
setInterval(updateStatus, 12000);
