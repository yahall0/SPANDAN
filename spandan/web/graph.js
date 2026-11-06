'use strict';

const relationLabels = {before: 'Before', triggered: 'Led to', possible_same_event: 'Possibly the same event',
  possible_actor: 'Possibly the same person', from_times: 'Based on times', stated: 'Stated in the account',
  inferred: 'Possible link'};
const graphLabel = value => relationLabels[value] || String(value || '').replaceAll('_', ' ');
const graphShort = (text, limit) => text.length <= limit ? text : text.slice(0, limit - 1) + '…';
const issueCopy = {
  'Batch shape': ['The app missed this section', 'Try again to look for details in this section.'],
  'Frame coverage': ['A detail is missing', 'The app found this in the text but could not add a checked detail. Review the original words below.'],
  'Local model': ['The app could not finish', 'Try again to check this section.'],
  'Internal error': ['The app could not finish', 'Try again to check this section.'],
  'Disconnected event': ['An event is missing details', 'Check the original text for who or what was involved, or when it happened.'],
  'Order cycle': ['The order of events disagrees', 'The links put the same events before and after each other. Compare the accounts below.'],
  'Conflicting polarity': ['Statements disagree on what happened', 'One statement says the action happened. Another says it did not.']
};
const issueTitle = issue => issueCopy[issue.name]?.[0] || issue.name;
const issueHelp = issue => issueCopy[issue.name]?.[1] || issue.detail;
const detailChecks = {
  'Exact quote': ['Quote matches the text', 'The quote does not match the text the app checked.'],
  'Grounded fields': ['Details are supported', 'One or more details could not be found in the quote.'],
  'Polarity': ['What happened or did not happen', 'The app is unsure whether the text says something happened or did not happen.'],
  'Evidentiality': ['How the person knows', 'The app is unsure whether the person saw this themselves or heard it from someone else.'],
  'Knowledge source': ['Who provided the information', 'The account says someone told the person, but the app could not identify who.'],
  'Suspicion and opinion': ['Suspicion or opinion', 'Check whether the person is describing something they know, sharing an opinion, or expressing a suspicion.'],
  'Precision and values': ['Numbers, dates and uncertainty', 'A number, date or uncertain detail may have changed.'],
  'Intake agreement': ['Match with the checked text', 'The app read the same words differently in its two checks. Compare this detail with the original text.'],
  'Reference ambiguity': ['Who or what is meant', 'The app is unsure who or what a word such as “he” or “it” refers to.'],
  'One fact': ['One event or statement at a time', 'The app may have combined separate things the person said.'],
  'Duplicate frame': ['Repeated detail', 'The app found another detail for the same words.'],
  'Local verification': ['Check finished', 'The app could not finish checking this detail. Try again.'],
  'One-way NLI': ['Quote supports the detail', 'The app could not confirm that the quote supports the whole detail.'],
  'Independent verifier': ['Second check agrees', 'The app’s second check did not confirm this detail. Compare it with the quote.']
};
const checkName = check => detailChecks[check.name]?.[0] || 'Detail check';
const checkHelp = check => detailChecks[check.name]?.[1] || 'Compare this detail with the original text.';
const detailField = {text: 'Wording', kind: 'Type of detail', actor: 'Who did it', act: 'What they did',
  object: 'Person or thing involved', place: 'Where', time: 'When', quantity: 'How many or how much',
  knowledge: 'Who gave the information', evidentiality: 'How the person knows',
  polarity: 'Whether it happened', certainty: 'How sure they were'};
const kinds = {event: 'Event', state: 'Situation', identity: 'Identity', statement: 'Reported statement',
  suspicion: 'Suspicion', opinion: 'Opinion', contact_detail: 'Contact information'};
const sourcesOfKnowledge = {direct: 'Seen or experienced', reported: 'Heard from someone', inferred: 'Based on a conclusion',
  unspecified: 'How they know is unclear'};
const polarityLabels = {affirmed: 'Says it happened', negated: 'Says it did not happen'};
const certaintyLabels = {certain: 'Certain', approximate: 'Approximate', uncertain: 'Uncertain', unspecified: 'Unclear'};
const claimState = claim => claim.handoff ?
  (claim.status === 'officer_confirmed' ? 'Confirmed by an officer' : 'Checked by the app') :
  claim.disposition === 'other_account_preserved' ? 'Kept as another account' : 'Needs review';

function graphView(graph) {
  const base = '/api/cases/' + safe(graph.case.id) + '/graph';
  const cleared = graph.claims.filter(c => c.handoff);
  const selected = current.graphSelection || (cleared[0] ? {type: 'claim', id: cleared[0].id} : null);
  const checked = cleared.length;
  const blocked = graph.pending.filter(needsIntake);
  const processing = graph.pending.length - blocked.length;
  const queue = reviewGroups(graph);
  const focus = selected && graph.events.find(e => e.id === selected.id && selected.type === 'event');
  const visible = graph.events.length > 18 && !current.fullGraph
    ? neighborhood(graph, focus || graph.events[0]) : graph.events;
  return '<a class="back" href="#case/' + safe(graph.case.id) + '">← Check complaint text</a>' +
    '<div class="heading"><div><span class="eyebrow">2 of 2 · Details and timeline</span><h1>' +
    safe(graph.case.case_ref || graph.case.filename) + '</h1>' +
    '<p>See what people said, when things happened, and which details need your help.</p></div><div class="actions">' +
    (checked ? '<a class="button secondary" download="spandan-claims.json" href="' + base + '/handoff">Download checked details</a>' : '') +
    '<button type="button" class="button secondary" data-case-refresh>Refresh</button>' +
    '<a class="button secondary" download="spandan-claim-audit.json" href="' + base + '/audit">Download review record</a></div></div>' +
    '<div class="metric-grid">' + metric('Details checked', checked) + metric('Groups to review', queue.length) +
    metric('Sections to check first', blocked.length) + metric('Still being checked', processing) + '</div>' +
    '<section class="case-next notice info" aria-label="What to do next"><strong>' +
    (blocked.length || queue.length || processing ? 'Some details are still open' : 'Case review complete') + '</strong><p>' +
    (blocked.length ? 'Start with the sections below. The app needs your help with the wording before it can use these sections in the case. ' : '') +
    (queue.length ? 'Open an item under Needs your review to see what the app was unsure about. ' : '') +
    (processing ? 'The app is still checking ' + processing + (processing === 1 ? ' section.' : ' sections.') : '') +
    (!blocked.length && !queue.length && !processing ? 'Read or download the checked details below.' : '') + '</p>' +
    (blocked.length || queue.length ? '<div class="actions">' + blocked.map(p => {
      const source = graph.statements.find(doc => doc.id === p.document_id);
      return '<a class="button secondary" href="' + passageUrl(p.document_id, p.passage_id) + '">' +
        safe('Check section ' + p.passage_id + (source?.kind === 'witness' ? ' · ' + source.reporter : '')) + '</a>';
    }).join('') + (queue.length ? '<button type="button" class="button secondary" data-review-jump>Go to items to review</button>' : '') +
    '</div>' : '') + '</section>' +
    '<div class="graph-layout"><div class="graph-stack"><section class="card"><div class="card-title"><h2>Checked details</h2>' +
    '<p>Choose a detail to see the words it came from.</p></div>' +
    (cleared.length ? cleared.map(c => graphRow('claim', c.id, c.text, c.reporter + ' · ' + c.passage_id + ' · ' + claimState(c))).join('') :
      '<p class="card-body subtle">Details appear here once the app checks them or an officer confirms them.</p>') +
    '</section><section class="card"><div class="card-title graph-header">' +
    '<div><h2>Timeline</h2><p>' + (graph.events.length === 1 ? 'One event has been checked so far.' :
      'Each row shows one account. Events with no known order appear on the right.') + '</p></div>' +
    (graph.events.length > 18 ? '<button type="button" class="button secondary" data-graph-toggle="true">' +
      (current.fullGraph ? 'Show nearby events' : 'Show all events') + '</button>' : '') + '</div>' +
    '<div class="graph-legend"><span><i></i> Order shown by words or times</span><span class="hyp"><i></i> Possible link</span>' +
    '<span class="hearsay"><i></i> Heard from someone</span><span class="conflict"><i></i> Accounts disagree</span></div>' +
    (visible.length ? timeline(graph, visible) : '<div class="empty"><h3>No checked events yet</h3><p>Events appear as their details are checked.</p></div>') +
    '</section><section class="card"><div class="card-title"><h2>People, places and things</h2><p>Choose a name to see where it appears.</p></div>' +
    '<div class="card-body graph-entities">' +
    (graph.entities.map(e => graphButton('entity', e.id, e.label + (e.identity === 'unidentified' ? ' · unidentified' : ''), 'entity-chip')).join('') ||
      '<p class="subtle">People, objects, places and times will appear here.</p>') +
    '</div></section>' + sources(graph) + '</div><div class="graph-stack">' +
    '<section class="card" id="case-evidence"><div class="card-title"><h2>What the original says</h2></div>' +
    '<div class="card-body">' + inspector(graph, selected, base) + '</div></section>' +
    '<section class="card" id="review-queue" tabindex="-1"><div class="card-title"><h2>Needs your review</h2><p>Open a group to see what needs checking in that part of the account.</p></div>' +
    (queue.length ? queue.map(group => '<details class="review-group"' +
      (group.rows.some(row => row.id === selected?.id) ? ' open' : '') + '><summary><strong>' + safe(group.title) + '</strong>' +
      '<span class="status clarify">' + safe(group.rows.length + (group.rows.length === 1 ? ' item' : ' items')) + '</span>' +
      '<small>' + safe(excerpt(group.source)) + '</small></summary>' +
      group.rows.map(row => graphRow(row.type, row.id, row.title, row.detail)).join('') + '</details>').join('') :
      '<p class="card-body subtle">No details need your review.</p>') +
    '</section></div></div>';
}

function reviewGroups(graph) {
  const groups = new Map();
  const groupKey = (evidence, passageId) => evidence ? evidence.document_id + '/' + passageId : 'case';
  const batchHolds = new Set(graph.issues.filter(i => i.name === 'Batch shape' && i.evidence)
    .map(i => groupKey(i.evidence, i.passage_id)));
  function add(row, evidence, passageId) {
    const id = groupKey(evidence, passageId);
    if (!groups.has(id)) groups.set(id, {
      title: evidence ? evidence.reporter + ' · ' + passageId : 'Case-wide issues',
      source: evidence?.source || 'Compare the accounts involved.',
      rows: []
    });
    groups.get(id).rows.push(row);
  }
  for (const claim of graph.claims.filter(c => c.status === 'escalated')) {
    const failed = claim.checks.find(c => !c.ok);
    add({type: 'claim', id: claim.id, title: excerpt(claim.text, 130),
      detail: claim.decision ? 'Left open by an officer' : failed ? checkHelp(failed) : 'Needs your review'},
    claim.evidence, claim.passage_id);
  }
  for (const issue of graph.issues) {
    if (issue.decision?.action === 'prefer' || (issue.name === 'Frame coverage' &&
      batchHolds.has(groupKey(issue.evidence, issue.passage_id)))) continue;
    add({type: 'issue', id: issue.id, title: issueTitle(issue),
      detail: issue.decision ? 'Left open by an officer' : issue.frame_evidence ?
        excerpt(issue.frame_evidence, 130) : issueHelp(issue)},
    issue.evidence, issue.passage_id);
  }
  return [...groups.values()];
}

function graphButton(type, id, label, style = 'button secondary') {
  return '<button type="button" class="' + style + '" data-graph-type="' + safe(type) + '" data-graph-id="' +
    safe(id) + '">' + safe(label) + '</button>';
}

function graphRow(type, id, title, description) {
  return '<button type="button" class="graph-row" data-graph-type="' + safe(type) + '" data-graph-id="' +
    safe(id) + '"><strong>' + safe(title) + '</strong><small>' + safe(description) + '</small></button>';
}

function neighborhood(graph, focus) {
  const nearby = new Set([focus.id]);
  for (const edge of [...graph.order_links, ...graph.inferred_connections]) {
    if (edge.from === focus.id || edge.to === focus.id) {
      nearby.add(edge.from);
      nearby.add(edge.to);
    }
  }
  const claim = graph.claims.find(c => c.id === focus.claim_id);
  return graph.events.filter(e => nearby.has(e.id) || (e.document_id === focus.document_id &&
    Math.abs(Number(e.passage_id.slice(1)) - Number(claim.passage_id.slice(1))) <= 1)).slice(0, 24);
}

function timeline(graph, events) {
  const byId = new Map(events.map(event => [event.id, event]));
  const order = graph.order_links.filter(edge => byId.has(edge.from) && byId.has(edge.to));
  const infer = graph.inferred_connections.filter(edge => byId.has(edge.from) && byId.has(edge.to));
  const conflicts = graph.issues.filter(issue => issue.claim_ids.length > 1 && !issue.decision).flatMap(issue => {
    const involved = events.filter(event => issue.claim_ids.includes(event.claim_id));
    return involved.length === 2 ? [{...issue, from: involved[0].id, to: involved[1].id, basis: 'conflict'}] : [];
  });
  const indegree = new Map(events.map(e => [e.id, 0]));
  const outgoing = new Map(events.map(e => [e.id, []]));
  const rank = new Map(events.map(e => [e.id, 0]));
  for (const edge of order) {
    indegree.set(edge.to, indegree.get(edge.to) + 1);
    outgoing.get(edge.from).push(edge.to);
  }
  const ready = events.filter(e => indegree.get(e.id) === 0).map(e => e.id);
  while (ready.length) {
    const from = ready.shift();
    for (const to of outgoing.get(from)) {
      rank.set(to, Math.max(rank.get(to), rank.get(from) + 1));
      indegree.set(to, indegree.get(to) - 1);
      if (!indegree.get(to)) ready.push(to);
    }
  }
  const last = Math.max(0, ...rank.values());
  const positions = new Map();
  const lanes = [];
  let y = 52;
  for (const statement of graph.lanes) {
    const laneEvents = events.filter(e => e.lane_id === statement.id);
    const occupied = new Map();
    for (const event of laneEvents) {
      const col = event.unordered || indegree.get(event.id) ? last + 1 : rank.get(event.id);
      const row = occupied.get(col) || 0;
      occupied.set(col, row + 1);
      positions.set(event.id, {x: 155 + col * 184, y: y + 19 + row * 60});
    }
    const height = Math.max(84, Math.max(1, ...occupied.values()) * 60 + 30);
    lanes.push({y, height, statement});
    y += height;
  }
  const width = 162 + (last + 2) * 184;
  const edgeSvg = [...order, ...infer, ...conflicts].map(edge => {
    const from = positions.get(edge.from);
    const to = positions.get(edge.to);
    const mid = (from.x + to.x + 158) / 2;
    const conflict = edge.basis === 'conflict';
    const label = conflict ? edge.name : graphLabel(edge.relation) + ' · ' + graphLabel(edge.basis);
    const path = from.x < to.x
      ? 'M' + (from.x + 158) + ' ' + (from.y + 23) + ' H' + mid + ' V' + (to.y + 23) + ' H' + to.x
      : 'M' + (from.x + 70) + ' ' + (from.y + 46) + ' Q' + (from.x + 181) + ' ' + (from.y + 55) +
        ' ' + (to.x + 110) + ' ' + (to.y + 46);
    return '<a href="#graph/' + safe(graph.case.id) + '" class="timeline-edge ' + safe(edge.basis) +
      '" data-graph-type="' + (conflict ? 'issue' : 'edge') + '" data-graph-id="' + safe(edge.id) + '" aria-label="' +
      safe(label) + '"><title>' + safe(label) + '</title><path class="hit" d="' + path + '"/><path class="stroke" d="' + path +
      '"' + (edge.basis === 'stated' || edge.basis === 'from_times' ? ' marker-end="url(#timeline-arrow)"' : '') + '/></a>';
  }).join('');
  const nodes = events.map(event => {
    const p = positions.get(event.id);
    const label = event.description;
    const timeLabel = event.time?.text || 'No stated time';
    return '<a href="#graph/' + safe(graph.case.id) + '" class="timeline-node ' +
      (event.unordered ? 'unordered ' : '') + (event.evidentiality === 'reported' ? 'reported ' : '') +
      (!event.time?.start ? 'untimed ' : '') +
      (current.graphSelection?.id === event.id ? 'selected' : '') + '" data-graph-type="event" data-graph-id="' +
      safe(event.id) + '" aria-label="' + safe(label + '. ' + timeLabel) + '"><title>' + safe(label) + '</title>' +
      '<rect x="' + p.x + '" y="' + p.y + '" width="158" height="46" rx="8"/><text x="' + (p.x + 9) +
      '" y="' + (p.y + 18) + '">' + safe(graphShort(label, 22)) + '</text><text class="event-time" x="' + (p.x + 9) +
      '" y="' + (p.y + 35) + '">' + safe(graphShort(timeLabel, 26)) + '</text></a>';
  }).join('');
  return '<div class="timeline-scroll"><svg class="timeline" width="' + width + '" height="' + (y + 8) +
    '" aria-label="Interactive event timeline"><defs><marker id="timeline-arrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
    'markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"/></marker></defs>' +
    '<text class="axis" x="154" y="28">Known order →</text><text class="axis" x="' + (156 + (last + 1) * 184) +
    '" y="28">Order unknown</text>' +
    lanes.map(lane => '<rect class="lane" x="0" y="' + lane.y + '" width="' + width + '" height="' + lane.height +
      '"/><text class="lane-name" x="12" y="' + (lane.y + 27) + '"><title>' + safe(lane.statement.label) +
      '</title>' + safe(graphShort(lane.statement.label, 18)) + '</text>').join('') + edgeSvg + nodes + '</svg></div>';
}

function sources(graph) {
  const rows = graph.statements.map(doc => '<a class="case-row" href="#case/' + safe(doc.id) + '"><div><strong>' +
    safe(doc.reporter) + '</strong><small>' + safe(doc.filename + ' · ' + timestamp(doc.received_at)) +
    '</small></div><span class="status">' + safe(doc.kind) + '</span></a>').join('');
  return '<section class="card"><div class="card-title"><h2>Complaint and witness accounts</h2><p>Open an account to read the original and its review notes.</p></div>' +
    rows + '<div class="card-body"><details class="fold"><summary>Add witness statement</summary>' +
    '<form data-form="witness"><input type="hidden" name="parent_case" value="' + safe(graph.case.id) + '">' +
    '<label class="field">Witness name or reference<input name="reporter" maxlength="100" required></label>' +
    '<label class="field">Typed statement<input name="file" type="file" accept=".pdf,.docx,.doc,.txt" required></label>' +
    sourceLanguageControl() +
    '<div class="field-grid"><label class="field">Can you ask the witness questions now?<select name="mode"><option value="live">Yes, they are here</option>' +
    '<option value="deferred">No, follow up later</option></select></label><label class="field">Language for follow-up questions' +
    '<select name="officer_language"><option value="en">English</option><option value="hi">हिन्दी</option>' +
    '<option value="gu">ગુજરાતી</option></select></label></div>' +
    '<p class="form-error" role="alert" hidden></p><button class="button" type="submit">Check witness statement</button></form></details></div></section>';
}

function inspector(graph, selected, base) {
  if (!selected) return '<p class="subtle">Choose a detail or an item to review. Its original wording will appear here.</p>';
  const claimById = id => graph.claims.find(c => c.id === id);
  if (selected.type === 'event' || selected.type === 'claim') {
    const event = graph.events.find(e => e.id === selected.id);
    const claim = claimById(event?.claim_id || selected.id);
    if (!claim) return '<p class="subtle">This item has changed. Choose another item to review.</p>';
    const related = graph.entity_links.filter(l => l.from === claim.id);
    const badges = '<span class="status ' + (claim.handoff ? 'pass' : 'clarify') + '">' + safe(claimState(claim)) + '</span>';
    const failed = claim.checks.filter(check => !check.ok);
    const badQuote = failed.some(check => check.name === 'Exact quote');
    const controls = ['unresolved'];
    if (!badQuote) controls.unshift('correct');
    if (claim.status === 'escalated' && !failed.some(check => check.hard)) controls.unshift('confirm');
    if (claim.status === 'escalated' && claim.checks.some(c => c.name === 'Local verification')) controls.unshift('retry');
    const fields = ['actor', 'act', 'object', 'place', 'time', 'knowledge', 'quantity']
      .filter(field => claim[field]).map(field => [detailField[field], claim[field]]);
    return '<h3 class="inspector-title">' + safe(claim.text) + '</h3><div class="actions">' + badges + '<span class="status">' +
      safe(kinds[claim.kind]) + '</span><span class="status">' + safe(sourcesOfKnowledge[claim.evidentiality]) + '</span></div>' +
      '<p class="subtle graph-description">' + safe(claim.reporter + ' · ' + claim.passage_id + ' · ' + polarityLabels[claim.polarity]) + '</p>' +
      (failed.length && !claim.handoff ? '<div class="notice"><strong>Why this needs your review</strong><ul>' +
        failed.map(check => '<li>' + safe(checkHelp(check)) +
          (check.differences ? '<ul>' + check.differences.map(d => '<li>' + safe((detailField[d.field] || d.field) + ' · First check: ' +
            (d.intake || 'No detail found') + ' · Second check: ' + (d.claim || 'No detail found')) + '</li>').join('') + '</ul>' : '') +
          '</li>').join('') + '</ul></div>' : '') +
      '<div class="evidence-stack">' + panel(badQuote ? 'Quote the app could not match' : 'Words that support this detail', claim.quote) +
      (badQuote && claim.evidence.english !== claim.evidence.source ? panel('Checked English text', claim.evidence.english) : '') +
      (claim.quote !== claim.evidence.source ? panel('Original text', claim.evidence.source) : '') +
      (claim.evidence.effective_source !== claim.evidence.source ? panel('Text after clarification', claim.evidence.effective_source) : '') +
      '</div><a class="back" href="' + passageUrl(claim.document_id, claim.passage_id) + '">Read full section ' + safe(claim.passage_id) + ' →</a>' +
      (related.length ? '<div class="actions graph-entities">' + related.map(link => {
        const entity = graph.entities.find(e => e.id === link.to);
        return entity ? graphButton('entity', entity.id, link.relation + ' · ' + entity.label, 'entity-chip') : '';
      }).join('') + '</div>' : '') +
      (event?.time ? '<p class="subtle graph-description">' + safe(event.time.precision + ' time · ' + event.time.text) + '</p>' : '') +
      (fields.length ? '<details class="fold"><summary>What the app took from the text</summary><dl class="claim-fields">' +
        fields.map(([field, value]) => '<dt>' + safe(field) + '</dt><dd>' + safe(value) + '</dd>').join('') + '</dl></details>' : '') +
      (claim.decision ? '<p class="notice info">' + safe(claim.decision.officer + ' · ' + claim.decision.reason) + '</p>' : '') +
      '<details class="fold"><summary>What the app checked</summary>' +
      [...claim.checks, ...(claim.officer_checks || [])].map(c => '<p><strong>' + safe(checkName(c)) + ' · ' +
        (c.ok ? 'Passed' : 'Needs review') + '</strong></p>').join('') +
      '</details>' + decisionForm(graph, base, claim.id, controls, []);
  }
  if (selected.type === 'entity') {
    const entity = graph.entities.find(e => e.id === selected.id);
    if (!entity) return '<p class="subtle">This entity was merged or revised.</p>';
    const others = graph.entities.filter(e => e.type === entity.type && e.id !== entity.id && entity.type !== 'time');
    return '<h3 class="inspector-title">' + safe(entity.label) + '</h3><p class="subtle">' +
      safe(entity.type === 'person' && entity.identity === 'unidentified' ? 'Person whose name is unknown' : entity.type) +
      '</p><div class="graph-linked">' +
      entity.claim_ids.map(id => graphRow('claim', id, claimById(id).text, claimById(id).reporter)).join('') + '</div>' +
      (others.length ? decisionForm(graph, base, entity.id, ['merge'], others.map(e => ({id: e.id, label: e.label}))) : '');
  }
  const issue = graph.issues.find(i => i.id === selected.id);
  const link = [...graph.order_links, ...graph.inferred_connections].find(e => e.id === selected.id);
  if (issue || link) {
    const heading = issue ? issueTitle(issue) : graphLabel(link.relation);
    const retryable = issue && ['Local model', 'Internal error', 'Batch shape'].includes(issue.name);
    return '<h3 class="inspector-title">' + safe(heading) + '</h3><p class="subtle graph-description">' +
      safe(issue ? issueHelp(issue) : (graphLabel(link.basis) + (link.cue ? ' · “' + link.cue + '”' : '') +
        (link.reason ? ' · ' + link.reason : ''))) + '</p>' +
      ((issue || link).claim_ids.map(id => {
        const c = claimById(id);
        return c ? graphRow('claim', id, c.text, c.reporter + ' · ' + c.passage_id) : '';
      }).join('')) + (issue?.frame_evidence ? panel('Words the app could not add as a detail', issue.frame_evidence) : '') +
      (issue?.evidence ? panel('Original text', issue.evidence.source) +
        '<a class="back" href="' + passageUrl(issue.evidence.document_id, issue.passage_id) + '">Read full section ' +
        safe(issue.passage_id) + ' →</a>' : '') +
      (issue?.decision ? '<p class="notice info">' + safe(issue.decision.officer + ' · ' + issue.decision.reason) + '</p>' : '') +
      (issue ? decisionForm(graph, base, issue.id, issue.claim_ids.length > 1 ? ['prefer', 'unresolved'] :
        retryable ? ['retry', 'unresolved'] : ['unresolved'],
        issue.claim_ids.map(id => ({id, label: claimById(id)?.text || id}))) : '');
  }
  return '<p class="subtle">This item has changed. Choose another item to review.</p>';
}

function decisionForm(graph, base, id, actions, others) {
  const labels = {confirm: 'Confirm this detail', correct: 'Change a detail', unresolved: 'Leave open for now',
    prefer: 'Choose the account to use', merge: 'Link two names for the same person or thing', retry: 'Try again'};
  return '<details class="fold"><summary>' + (actions[0] === 'retry' ? 'Try again or leave open' : 'Review this item') +
    '</summary><form data-form="decision" data-url="' + base +
    '/decisions" data-version="' + safe(graph.version) + '" data-target="' + safe(id) + '">' +
    '<label class="field">Officer name or ID<input name="officer" maxlength="100" required></label>' +
    '<label class="field">What do you want to do?<select name="action">' +
    actions.map(action => '<option value="' + action + '">' + labels[action] + '</option>').join('') + '</select></label>' +
    (others.length ? '<label class="field" data-actions="prefer merge">' +
      (actions.includes('merge') ? 'Which name refers to the same person or thing?' : 'Which account should the case use?') +
      '<select name="other" required><option value="">Choose one</option>' +
      others.map(other => '<option value="' + safe(other.id) + '">' + safe(other.label) + '</option>').join('') + '</select></label>' : '') +
    (actions.includes('correct') ? '<div class="field-grid" data-actions="correct"><label class="field">What needs to change?<select name="field" required>' +
      '<option value="">Choose a detail</option>' + Object.entries(detailField).map(([field, label]) => '<option value="' + field + '">' + label + '</option>').join('') +
      '</select></label><label class="field">What should it say?<span data-correction-value><input name="value" maxlength="500"></span></label></div>' : '') +
    '<label class="field" data-actions="confirm correct unresolved prefer merge">Why did you choose this?<textarea name="reason" maxlength="1500" required rows="2"></textarea></label>' +
    '<p class="form-error" role="alert" hidden></p><button class="button" type="submit">Save review</button></form></details>';
}

function updateCorrectionValue(form, field) {
  const choiceLabels = {kind: kinds, evidentiality: sourcesOfKnowledge, polarity: polarityLabels, certainty: certaintyLabels};
  const choices = choiceLabels[field];
  form.querySelector('[data-correction-value]').innerHTML = choices ?
    '<select name="value" required><option value="">Choose one</option>' +
    Object.entries(choices).map(([key, label]) => '<option value="' + safe(key) + '">' + safe(label) + '</option>').join('') +
    '</select>' : '<input name="value" maxlength="500">';
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-graph-id], [data-graph-toggle]');
  if (!button || current.view !== 'graph' || !current.data) return;
  event.preventDefault();
  if (current.draft && !window.confirm('Discard the unsaved review?')) return;
  current.draft = false;
  if (button.dataset.graphToggle) current.fullGraph = !current.fullGraph;
  else current.graphSelection = {type: button.dataset.graphType, id: button.dataset.graphId};
  const scroll = content.querySelector('.timeline-scroll')?.scrollLeft || 0;
  content.innerHTML = graphView(current.data);
  prepareReviewForms();
  const timeline = content.querySelector('.timeline-scroll');
  if (timeline) timeline.scrollLeft = scroll;
  if (!button.dataset.graphToggle) {
    const inspectorHeading = content.querySelector('.inspector-title');
    if (inspectorHeading) {
      inspectorHeading.tabIndex = -1;
      inspectorHeading.scrollIntoView({block: 'nearest'});
      inspectorHeading.focus({preventScroll: true});
    }
  }
});
