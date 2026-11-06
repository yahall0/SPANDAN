'use strict';

const content = document.querySelector('#content');
const statusIndicator = document.querySelector('#system-status');
const toast = document.querySelector('#toast');
const labels = {pass: 'Checked', clarify: 'Needs review', unresolved: 'Left open', running: 'Checking', queued: 'Waiting'};
const layers = {L0: 'Text and format', L1: 'Numbers and key details', L2: 'Agreement in meaning', L3: 'Match with the original'};
const textCheckNames = {
  'Passage ID and output': 'Text is present', 'English and length': 'English text and length',
  'Protected values': 'Private details preserved', 'Values and roles': 'Numbers and dates',
  'Meaning markers': 'Denials, uncertainty and reported information',
  'Bidirectional equivalence': 'Same meaning both ways', 'English statement coverage': 'Statements included',
  'Source and English evidence coverage': 'Original wording covered', 'Factual ambiguity': 'Unclear wording',
  'Statement pairing': 'Matching statements', 'Bidirectional entailment': 'Same meaning both ways',
  'Statement count': 'Number of statements', 'Material slots': 'Key details match'
};
const reviewErrors = {
  'Record the answer and a corrected passage or confirmed language.': 'Add a short note and either choose the language or enter the corrected text.',
  'Record why the complainant is uncertain.': 'Tell us why you are leaving this open.',
  'The graph changed. Reload it before recording a decision.': 'The case has changed. Refresh it before saving your review.',
  'The passage changed. Reload it before recording a review.': 'This section has changed. Refresh it before saving your review.',
  'Only compatible entity types can be merged.': 'Choose two names for the same kind of thing, such as two names for one person.',
  'The quote, grounded fields and suspicion wording must remain faithful to the source.': 'Keep the detail consistent with the original text, including any uncertainty or suspicion.'
};
const languageNames = new Intl.DisplayNames(['en'], {type: 'language'});
const documentLanguages = ['en', 'hi', 'gu', 'bn', 'mr', 'pa', 'ur', 'ta', 'te', 'kn', 'ml', 'or', 'as', 'ne', 'mai', 'sa', 'sd', 'kok', 'mni', 'brx', 'doi', 'ks', 'sat', 'si', 'mul'];
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
    const message = (detail || 'The app could not finish. Try again.').replace(/^Value error, /, '');
    throw new Error(reviewErrors[message] || message);
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
    prepareReviewForms();
    content.setAttribute('aria-busy', 'false');
    document.querySelector('#breadcrumb').textContent = view === 'graph' ? 'Details and timeline' : key === 'new' ? 'New complaint' : key === 'home' ? 'Complaints' : 'Check complaint text';
    document.querySelector('#nav-cases').classList.toggle('active', key !== 'new');
    document.querySelector('#nav-new').classList.toggle('active', key === 'new');
  } catch (error) {
    if (state !== current) return;
    content.setAttribute('aria-busy', 'false');
    content.innerHTML = '<p class="notice" role="alert">' + safe(error.message) + '</p><a class="back" href="#">← Back to complaints</a>';
  }
}

function uploadCard() {
  return '<section class="card"><div class="card-title"><h2>New complaint</h2>' +
    '<p>Add a typed complaint to start checking it.</p></div><div class="card-body">' +
    '<form data-form="upload"><label class="field">Complaint document' +
    '<input name="file" type="file" accept=".pdf,.docx,.doc,.txt" required><small>PDF, DOCX, DOC or TXT · Up to 10 MB</small></label>' +
    '<label class="field">Case reference <input name="case_ref" maxlength="100" placeholder="Optional reference"></label>' +
    sourceLanguageControl() +
    '<div class="field-grid"><label class="field">Can you ask the complainant questions now?<select name="mode"><option value="live">Yes, they are here</option>' +
    '<option value="deferred">No, follow up later</option></select></label>' +
    '<label class="field">Language for follow-up questions<select name="officer_language"><option value="en">English</option>' +
    '<option value="hi">हिन्दी</option><option value="gu">ગુજરાતી</option></select></label></div>' +
    '<p class="form-error" role="alert" hidden></p><button class="button" type="submit">Check complaint <span aria-hidden="true">→</span></button>' +
    '</form></div></section>';
}

function sourceLanguageControl() {
  return '<label class="field">Document language<select name="source_language"><option value="">Detect automatically</option>' +
    '<option value="en">English</option></select>' +
    '<small>Choose English if the whole document is in English.</small></label>';
}

function dashboard(cases, isNew) {
  const count = key => cases.reduce((sum, item) => sum + item[key], 0);
  const rows = cases.map(item => '<a class="case-row" href="#case/' + safe(item.id) + '"><div><strong>' +
    safe(item.case_ref || item.filename) + '</strong><small>' + safe(item.case_ref ? item.filename + ' · ' : '') +
    safe(timestamp(item.received_at)) + ' · ' + item.passed + ' of ' + item.total + ' sections checked</small></div>' +
    badge(item.review ? 'clarify' : item.pending ? 'running' : 'pass') + '</a>').join('');
  return '<div class="heading"><div><h1>' +
    (isNew ? 'Add a complaint' : 'Complaints') + '</h1>' +
    '<p>See which complaints need your attention.</p></div>' +
    (isNew ? '<a class="button secondary" href="#">View cases</a>' : '<a class="button" href="#new">＋ New complaint</a>') + '</div>' +
    '<div class="metric-grid">' + metric('Complaints', cases.length) + metric('Sections checked', count('passed')) +
    metric('Sections to review', count('review')) + metric('Still being checked', count('pending')) + '</div>' +
    '<div class="layout"><section class="card"><div class="card-title"><h2>Complaint register</h2>' +
    '<p>Open a complaint to see the original text and any questions to answer.</p></div>' +
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
  return '<a class="back" href="#">← Complaints</a><div class="heading"><div><span class="eyebrow">1 of 2 · Check the text</span><h1>' +
    safe(doc.case_ref || doc.filename) + '</h1><p>' + safe(doc.case_ref ? doc.filename + ' · ' : '') +
    safe(doc.mode === 'live' ? 'Available for questions now' : 'Follow up later') + '</p></div><div class="actions">' +
    '<a class="button" href="#graph/' + safe(doc.parent_case || doc.id) + '">See details and timeline</a>' +
    '<button type="button" class="button secondary" data-case-refresh>Refresh</button>' +
    '<a class="button secondary" href="' + base + '/original">Original file</a>' +
    (passed ? '<a class="button secondary" download="spandan-checked.json" href="' + base + '/handoff' +
      (passed === passages.length ? '?finalize=true">Download checked text' : '">Download checked sections') + '</a>' : '') +
    '</div></div><div class="case-meta"><span>Received ' + safe(timestamp(doc.received_at)) + '</span></div>' +
    '<div class="metric-grid">' + metric('Text sections', passages.length) + metric('Text checked', passed) +
    metric('Sections to review', review) + metric('Still being checked', passages.length - passed - review) + '</div>' +
    '<p class="notice info">' + (review ? safe(review === 1 ? 'One section needs your help. ' : review + ' sections need your help. ') : '') +
    'The app looks for case details in the sections it has checked.</p>' +
    '<div class="review-layout"><section class="card"><div class="card-title"><h2>Complaint text</h2>' +
    '<div class="review-filters">' + [['all', 'All sections'], ['review', 'Needs review']].map(([filter, label]) =>
      '<button type="button" class="button secondary" data-passage-filter="' + filter + '" aria-pressed="' +
      ((current.passageFilter || 'all') === filter) + '">' + label + '</button>').join('') + '</div></div><div class="passage-list">' +
    visible.map(p => '<button type="button" class="passage-button ' + (p.id === selected?.id ? 'selected' : '') +
      '" data-passage="' + safe(p.id) + '" aria-pressed="' + (p.id === selected.id) + '"><span><strong>' + safe(p.id) +
      '</strong><small class="passage-excerpt">' + safe(excerpt(p.source, 85)) + '</small></span>' + badge(p.status) + '</button>').join('') +
    '</div></section>' + (selected ? passageView(doc, selected, base) : '<p class="notice info">No sections need your review.</p>') + '</div>';
}

function passageView(doc, p, base) {
  const result = p.result;
  const attempt = result?.attempts?.at(-1);
  const meta = [p.page ? 'PDF page ' + p.page : '', result?.language && result.slot !== 'language' ?
    (p.language_override ? 'Language confirmed: ' : 'Language detected: ') + languageNames.of(result.language.language) : ''].filter(Boolean);
  let section = '<section class="card"><div class="card-title"><h2>Section ' + safe(p.id) + ' ' + badge(p.status) + '</h2>' +
    (meta.length ? '<p>' + safe(meta.join(' · ')) + '</p>' : '') + '</div><div class="card-body">' +
    '<div class="comparison">' + panel('Original text', p.source) +
    (p.status === 'pass' && result.checked_english !== p.source ? panel('Checked English text', result.checked_english, true) : '') + '</div>';
  if (p.revision > 1 && p.effective_source !== p.source) section += panel('Text after your clarification', p.effective_source);
  if (p.status === 'queued' || p.status === 'running') {
    section += '<p class="notice info">The app is checking the complaint. You can read the sections it has already checked.</p>';
  }
  if (p.status === 'clarify' || p.status === 'unresolved') {
    const next = result.slot === 'language' ? 'The app could not tell which language this section is in. Choose the language below.' :
      result.slot === 'service' ? 'The app could not finish checking this section. Use Try again to check it once more.' :
      'Read the original text above. If you can clarify what it says, record the answer below.';
    section += '<div class="notice"><strong>' + safe(p.status === 'unresolved' ? 'You left this open' : 'This section needs your help') +
      '</strong><p>' + safe(next) + '</p>' + (!['language', 'service'].includes(result.slot) && result.question ?
        '<p dir="auto">' + safe(result.question) + '</p>' : '') + '</div>' + reviewForm(doc, p, base);
  }
  if (attempt) {
    section += '<details class="fold"><summary>What the app checked</summary><div class="checks">' +
      Object.entries(layers).map(([layer, title]) => layerDetail(layer, title, attempt.checks)).join('') + '</div></details>';
    if (attempt.translations.A || attempt.translations.B) {
      section += '<details class="fold"><summary>English text used for the checks</summary><div class="comparison">' +
        panel('First version', attempt.translations.A || 'Unavailable') + panel('Second version', attempt.translations.B || 'Unavailable') + '</div></details>';
    }
  }
  section += '<details class="fold" data-history="' + base + '/passages/' + safe(p.id) + '/history"><summary>Technical history</summary>' +
    '<pre>Loading the record…</pre></details>';
  if (result) section += feedbackForm(p, base);
  return section + '</div></section>';
}

function layerDetail(layer, title, allChecks) {
  const checks = allChecks.filter(check => check.layer === layer);
  const state = checks.length ? checks.every(check => check.ok) ? 'pass' : 'clarify' : 'Not run';
  return '<details class="check"><summary>' + title + ' ' + badge(state) + '</summary>' +
    (checks.map(check => {
      const name = check.name.replace(/^Path [AB] · /, '');
      return '<p><strong>' + safe(textCheckNames[name] || name) + '</strong> · ' +
        (check.ok ? 'Passed' : 'Needs review') + '</p>';
    }).join('') || '<p>The app stopped at an earlier check.</p>') +
    '</details>';
}

function reviewForm(doc, p, base) {
  const retry = p.result.slot === 'service';
  return '<form data-form="review" data-url="' + base + '/passages/' + safe(p.id) + '/review" data-revision="' + p.revision + '">' +
    '<div class="field-grid"><label class="field">Officer name or ID<input name="officer" maxlength="100" required></label>' +
    '<label class="field">What do you want to do?<select name="action">' +
    (retry ? '<option value="retry">Try again</option>' : '') + '<option value="clarify">Clarify the text</option>' +
    '<option value="unresolved">Leave open for now</option></select></label></div>' +
    '<label class="field" data-actions="clarify unresolved">What did you find out, or why are you leaving this open?' +
    '<textarea name="answer" maxlength="750" rows="2"></textarea></label>' +
    '<label class="field" data-actions="clarify">What should the section say?<textarea name="corrected_source" maxlength="750" rows="3" dir="auto" ' +
    'placeholder="Enter the full section if the wording changed"></textarea><small>You can leave this blank if you are only confirming the language.</small></label>' +
    '<label class="field" data-actions="clarify">Language of this section<select name="language"><option value="">Keep the current language</option>' +
    documentLanguages.map(code => '<option value="' + code + '"' + (code === p.language_override ? ' selected' : '') +
      '>' + safe(languageNames.of(code)) + '</option>').join('') + '</select></label>' +
    '<p class="form-error" role="alert" hidden></p><div class="actions"><button class="button" type="submit">Save review</button>' +
    '<span class="subtle">' + (doc.mode === 'live' ? 'Ask the person now' : 'Follow up with the person later') + '</span></div></form>';
}

function feedbackForm(p, base) {
  return '<details class="fold"><summary>Flag a problem or add a note</summary><form data-form="feedback" data-url="' + base +
    '/passages/' + safe(p.id) + '/feedback" data-revision="' + p.revision + '"><div class="field-grid">' +
    '<label class="field">Officer name or ID<input name="officer" maxlength="100" required></label>' +
    '<label class="field">What happened?<select name="kind"><option value="possible_escape">The app may have checked this incorrectly</option>' +
    '<option value="false_alarm">This may have been flagged by mistake</option><option value="readback_conflict">The person disagrees with the text</option>' +
    '<option value="downstream_conflict">A case detail disagrees with the text</option><option value="audit_note">I want to leave a note</option></select></label></div>' +
    '<label class="field">Tell us what you noticed<textarea name="detail" maxlength="2000" required></textarea></label>' +
    '<p class="form-error" role="alert" hidden></p><div class="actions"><button class="button secondary" type="submit">Save feedback</button></div></form></details>';
}

content.addEventListener('input', event => {
  if (event.target.closest('form')) current.draft = true;
});

function syncReviewForm(form) {
  const action = form.elements.action.value;
  for (const group of form.querySelectorAll('[data-actions]')) {
    group.hidden = !group.dataset.actions.split(' ').includes(action);
    for (const control of group.querySelectorAll('input, select, textarea')) control.disabled = group.hidden;
  }
  form.querySelector('button[type=submit]').textContent = action === 'retry' ? 'Try again' :
    action === 'clarify' ? 'Save and check again' : 'Save review';
}

function prepareReviewForms() {
  content.querySelectorAll('form[data-form="review"], form[data-form="decision"]').forEach(syncReviewForm);
}

content.addEventListener('change', event => {
  const form = event.target.closest('form[data-form="review"], form[data-form="decision"]');
  if (!form) return;
  if (event.target.name === 'field') updateCorrectionValue(form, event.target.value);
  if (event.target.name === 'action' || event.target.name === 'field') syncReviewForm(form);
});

content.addEventListener('click', event => {
  if (event.target.closest('[data-review-jump]')) {
    const queue = document.querySelector('#review-queue');
    queue?.focus({preventScroll: true});
    queue?.scrollIntoView({block: 'start'});
    return;
  }
  const button = event.target.closest('[data-passage], [data-passage-filter], [data-case-refresh]');
  if (!button) return;
  if (current.draft && !window.confirm('Discard your unsaved changes?')) return;
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
        notify('Witness statement added. The app will check it next.');
      } else {
        location.hash = 'case/' + created.id;
        notify('Complaint added. The app will start checking it.');
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
        if (body.action === 'retry') body.reason = 'Asked the app to check this section again.';
      }
      await request(form.dataset.url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      current.draft = false;
      await refresh(true);
      notify(body.action === 'retry' ? 'The app will check this section again.' :
        type === 'review' || type === 'decision' ? 'Review saved.' : 'Note saved.');
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
    statusIndicator.lastChild.textContent = status.ready ? ' Ready to check' : ' Setup needed';
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
