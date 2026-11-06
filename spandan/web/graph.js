'use strict';

const relationLabels = {before: 'Before', triggered: 'Triggered', possible_same_event: 'Possibly the same event',
  possible_actor: 'Possible actor connection', from_times: 'From times', stated: 'Stated', inferred: 'Inferred'};
const graphLabel = value => relationLabels[value] || String(value || '').replaceAll('_', ' ');
const graphShort = (text, limit) => text.length <= limit ? text : text.slice(0, limit - 1) + '…';

function graphView(graph) {
  const base = '/api/cases/' + safe(graph.case.id) + '/graph';
  const selected = current.graphSelection;
  const claims = graph.claims.filter(c => c.status === 'escalated');
  const activeIssues = graph.issues.filter(issue => issue.decision?.action !== 'prefer');
  const checked = graph.claims.filter(c => c.handoff).length;
  const focus = selected && graph.events.find(e => e.id === selected.id && selected.type === 'event');
  const visible = graph.events.length > 18 && !current.fullGraph
    ? neighborhood(graph, focus || graph.events[0]) : graph.events;
  const unresolved = claims.length + activeIssues.length;
  return '<a class="back" href="#case/' + safe(graph.case.id) + '">← Passage intake</a>' +
    '<div class="heading"><div><span class="eyebrow">Component 02 · Claims and timeline</span><h1>' +
    safe(graph.case.case_ref || graph.case.filename) + '</h1>' +
    '<p>Trace each account to the checked passage and original evidence.</p></div><div class="actions">' +
    (checked ? '<a class="button secondary" download="spandan-claims.json" href="' + base + '/handoff">Export cleared claims</a>' : '') +
    '<a class="button secondary" download="spandan-claim-audit.json" href="' + base + '/audit">Export audit</a></div></div>' +
    '<div class="metric-grid">' + metric('Source statements', graph.statements.length) + metric('Cleared claims', checked) +
    metric('Review items', unresolved) + metric('Pending passages', graph.pending.length) + '</div>' +
    '<div class="graph-layout"><div class="graph-stack"><section class="card"><div class="card-title graph-header">' +
    '<div><h2>Event timeline</h2><p>Source lanes · Order, without time scale</p></div>' +
    (graph.events.length > 18 ? '<button type="button" class="button secondary" data-graph-toggle="true">' +
      (current.fullGraph ? 'Focus view' : 'Full graph') + '</button>' : '') + '</div>' +
    '<div class="graph-legend"><span><i></i> Stated or timed</span><span class="hyp"><i></i> Inferred connection</span>' +
    '<span class="hearsay"><i></i> Reported account</span><span class="conflict"><i></i> Conflict</span></div>' +
    (visible.length ? timeline(graph, visible) : '<div class="empty"><h3>No cleared events yet</h3><p>Events appear as claim checks finish.</p></div>') +
    '</section><section class="card"><div class="card-title"><h2>Entities</h2><p>Select a node to inspect its linked evidence.</p></div>' +
    '<div class="card-body graph-entities">' +
    (graph.entities.map(e => graphButton('entity', e.id, e.label + (e.identity === 'unidentified' ? ' · unidentified' : ''), 'entity-chip')).join('') ||
      '<p class="subtle">People, objects, places and times will appear here.</p>') +
    '</div></section>' + sources(graph) + '</div><div class="graph-stack">' +
    '<section class="card"><div class="card-title"><h2>Review queue</h2><p>Open an item to record an officer decision.</p></div>' +
    ((claims.length || graph.issues.length) ? claims.map(c => graphRow('claim', c.id, c.text, c.decision ? 'Unresolved claim' : 'Claim escalation')).join('') +
      graph.issues.map(i => graphRow('issue', i.id, i.name, i.decision ? 'Decision recorded' : i.detail)).join('') :
      '<p class="card-body subtle">No escalations or conflicts.</p>') +
    '</section><section class="card"><div class="card-title"><h2>Evidence inspector</h2></div>' +
    '<div class="card-body">' + inspector(graph, selected, base) + '</div></section></div></div>';
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
    '<text class="axis" x="154" y="28">Supported order →</text><text class="axis" x="' + (156 + (last + 1) * 184) +
    '" y="28">Unordered</text>' +
    lanes.map(lane => '<rect class="lane" x="0" y="' + lane.y + '" width="' + width + '" height="' + lane.height +
      '"/><text class="lane-name" x="12" y="' + (lane.y + 27) + '"><title>' + safe(lane.statement.label) +
      '</title>' + safe(graphShort(lane.statement.label, 18)) + '</text>').join('') + edgeSvg + nodes + '</svg></div>';
}

function sources(graph) {
  const rows = graph.statements.map(doc => '<a class="case-row" href="#case/' + safe(doc.id) + '"><div><strong>' +
    safe(doc.reporter) + '</strong><small>' + safe(doc.filename + ' · ' + timestamp(doc.received_at)) +
    '</small></div><span class="status">' + safe(doc.kind) + '</span></a>').join('');
  return '<section class="card"><div class="card-title"><h2>Source statements</h2><p>Each document keeps its own passage history.</p></div>' +
    rows + '<div class="card-body"><details class="fold"><summary>Add witness statement</summary>' +
    '<form data-form="witness"><input type="hidden" name="parent_case" value="' + safe(graph.case.id) + '">' +
    '<label class="field">Witness name or reference<input name="reporter" maxlength="100" required></label>' +
    '<label class="field">Typed statement<input name="file" type="file" accept=".pdf,.docx,.doc,.txt" required></label>' +
    '<div class="field-grid"><label class="field">Witness<select name="mode"><option value="live">At the desk</option>' +
    '<option value="deferred">Call back later</option></select></label><label class="field">Officer language' +
    '<select name="officer_language"><option value="en">English</option><option value="hi">हिन्दी</option>' +
    '<option value="gu">ગુજરાતી</option></select></label></div>' +
    '<p class="form-error" role="alert" hidden></p><button class="button" type="submit">Queue witness statement</button></form></details></div></section>';
}

function inspector(graph, selected, base) {
  if (!selected) return '<p class="subtle">Select an event, link, entity or review item.</p>';
  const claimById = id => graph.claims.find(c => c.id === id);
  if (selected.type === 'event' || selected.type === 'claim') {
    const event = graph.events.find(e => e.id === selected.id);
    const claim = claimById(event?.claim_id || selected.id);
    if (!claim) return '<p class="subtle">This item is no longer in the current graph.</p>';
    const related = graph.entity_links.filter(l => l.from === claim.id);
    const badges = '<span class="status ' + (claim.handoff ? 'pass' : 'clarify') + '">' + safe(graphLabel(claim.status)) + '</span>';
    const controls = claim.status === 'escalated' ? ['confirm', 'correct', 'unresolved'] : ['correct', 'unresolved'];
    if (claim.status === 'escalated' && claim.checks.some(c => c.name === 'Local verification')) controls.unshift('retry');
    return '<h3 class="inspector-title">' + safe(claim.text) + '</h3><div class="actions">' + badges + '<span class="status">' +
      safe(graphLabel(claim.kind)) + '</span><span class="status">' + safe(graphLabel(claim.evidentiality)) + '</span></div>' +
      '<p class="subtle graph-description">' + safe(claim.reporter + ' · ' + claim.passage_id + ' · ' + graphLabel(claim.polarity)) + '</p>' +
      '<div class="evidence-stack">' + panel('Exact checked English quote', claim.quote) + panel('Original passage', claim.evidence.source) +
      (claim.evidence.effective_source !== claim.evidence.source ? panel('Officer-corrected source', claim.evidence.effective_source) : '') +
      '</div><a class="back" href="#case/' + safe(claim.document_id) + '">Open source statement →</a>' +
      (related.length ? '<div class="actions graph-entities">' + related.map(link => {
        const entity = graph.entities.find(e => e.id === link.to);
        return entity ? graphButton('entity', entity.id, link.relation + ' · ' + entity.label, 'entity-chip') : '';
      }).join('') + '</div>' : '') +
      (event?.time ? '<p class="subtle graph-description">' + safe(event.time.precision + ' time · ' + event.time.text) + '</p>' : '') +
      (claim.decision ? '<p class="notice info">' + safe(claim.decision.officer + ' · ' + claim.decision.reason) + '</p>' : '') +
      '<details class="fold"><summary>Verification checks</summary>' +
      [...claim.checks, ...(claim.officer_checks || [])].map(c => '<p><strong>' + safe(c.name) + ' · ' +
        (c.ok ? 'Passed' : 'Review') + '</strong><br>' + safe(c.reason) + '</p>').join('') +
      '</details>' + decisionForm(graph, base, claim.id, controls, []);
  }
  if (selected.type === 'entity') {
    const entity = graph.entities.find(e => e.id === selected.id);
    if (!entity) return '<p class="subtle">This entity was merged or revised.</p>';
    const others = graph.entities.filter(e => e.type === entity.type && e.id !== entity.id && entity.type !== 'time');
    return '<h3 class="inspector-title">' + safe(entity.label) + '</h3><p class="subtle">' +
      safe(entity.type + ' · ' + entity.identity) + '</p><div class="graph-linked">' +
      entity.claim_ids.map(id => graphRow('claim', id, claimById(id).text, claimById(id).reporter)).join('') + '</div>' +
      (others.length ? decisionForm(graph, base, entity.id, ['merge'], others.map(e => ({id: e.id, label: e.label}))) : '');
  }
  const issue = graph.issues.find(i => i.id === selected.id);
  const link = [...graph.order_links, ...graph.inferred_connections].find(e => e.id === selected.id);
  if (issue || link) {
    const heading = issue?.name || graphLabel(link.relation);
    return '<h3 class="inspector-title">' + safe(heading) + '</h3><p class="subtle graph-description">' +
      safe(issue?.detail || (graphLabel(link.basis) + (link.cue ? ' · “' + link.cue + '”' : '') +
        (link.reason ? ' · ' + link.reason : ''))) + '</p>' +
      ((issue || link).claim_ids.map(id => {
        const c = claimById(id);
        return c ? graphRow('claim', id, c.text, c.reporter + ' · ' + c.passage_id) : '';
      }).join('')) + (issue?.evidence ? panel('Original passage', issue.evidence.source) : '') +
      (issue?.decision ? '<p class="notice info">' + safe(issue.decision.officer + ' · ' + issue.decision.reason) + '</p>' : '') +
      (issue ? decisionForm(graph, base, issue.id, issue.claim_ids.length > 1 ? ['prefer', 'unresolved'] :
        ['Local model', 'Internal error'].includes(issue.name) ? ['retry', 'unresolved'] : ['unresolved'],
        issue.claim_ids.map(id => ({id, label: claimById(id)?.text || id}))) : '');
  }
  return '<p class="subtle">This item is no longer in the current graph.</p>';
}

function decisionForm(graph, base, id, actions, others) {
  const labels = {confirm: 'Confirm this claim', correct: 'Correct one field', unresolved: 'Mark unresolved',
    prefer: 'Choose an account', merge: 'Merge into another entity', retry: 'Rerun local claim checks'};
  const fields = ['text', 'kind', 'actor', 'act', 'object', 'place', 'time', 'quantity', 'knowledge',
    'evidentiality', 'polarity', 'certainty'];
  return '<details class="fold"><summary>Record officer decision</summary><form data-form="decision" data-url="' + base +
    '/decisions" data-version="' + safe(graph.version) + '" data-target="' + safe(id) + '">' +
    '<label class="field">Officer name or ID<input name="officer" maxlength="100" required></label>' +
    '<label class="field">Decision<select name="action">' +
    actions.map(action => '<option value="' + action + '">' + labels[action] + '</option>').join('') + '</select></label>' +
    (others.length ? '<label class="field">Account or merge target<select name="other"><option value="">Select an item</option>' +
      others.map(other => '<option value="' + safe(other.id) + '">' + safe(other.label) + '</option>').join('') + '</select></label>' : '') +
    (actions.includes('correct') ? '<div class="field-grid"><label class="field">Field to correct<select name="field">' +
      '<option value="">Select a field</option>' + fields.map(f => '<option value="' + f + '">' + f + '</option>').join('') +
      '</select></label><label class="field">Corrected value<input name="value" maxlength="500"></label></div>' : '') +
    '<label class="field">Reason<textarea name="reason" maxlength="1500" required rows="2"></textarea></label>' +
    '<p class="form-error" role="alert" hidden></p><button class="button" type="submit">Save decision</button></form></details>';
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
  const timeline = content.querySelector('.timeline-scroll');
  if (timeline) timeline.scrollLeft = scroll;
  if (!button.dataset.graphToggle) {
    const inspectorHeading = content.querySelector('.inspector-title');
    if (inspectorHeading) {
      inspectorHeading.tabIndex = -1;
      inspectorHeading.focus({preventScroll: true});
    }
  }
});
