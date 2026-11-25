'use strict';
// ReguLens command centre. All server data is rendered with textContent. Agent output is untrusted.

const $ = id => document.getElementById(id);
const el = (tag, text = '', cls = '') => {
  const e = document.createElement(tag);
  e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
const td = (...nodes) => { const c = el('td'); c.append(...nodes); return c; };
const emptyRow = (cols, text) => { const tr = el('tr'); const c = td(text); c.colSpan = cols; c.className = 'muted'; tr.append(c); return tr; };

const VIEWS = ['cockpit', 'sources', 'agent-audit', 'findings', 'rulesets', 'ledger', 'settings'];
const STAGES = ['discovery', 'snapshot', 'review', 'persist', 'notify'];
const STATUS = {
  NON_COMPLIANT: { label: 'Non-compliant', tone: 'risk' },
  NEEDS_CONTEXT: { label: 'Needs context', tone: 'warn' },
  COMPLIANT_WITHIN_SCOPE: { label: 'Within scope', tone: 'ok' },
};
const PHASE_TONE = { error: 'risk', review: 'violet', persist: 'ok' };

let data = null;
let selected = null;
let selectedKey = null;
let view = 'cockpit';
let statusFilter = 'ALL';
let severityFilter = 'ALL';
let query = '';
let busy = false;
let judgeOpen = false;

// ---------- Data helpers ----------
const names = () => data?.discovered ?? [];
const scanOf = name => data?.repositories.find(r => r.repo === name) ?? null;
const metaOf = name => data?.metadata?.[name] ?? {};
const pill = (text, tone = '') => el('span', text, `pill ${tone}`.trim());
const when = ts => (ts ? new Date(ts * 1000).toLocaleString() : '—');
const shortSha = sha => (sha || '').slice(0, 12) || '—';
const isRisk = f => f.status === 'NON_COMPLIANT';
const tone = f => STATUS[f.status]?.tone ?? '';
const label = f => STATUS[f.status]?.label ?? f.status;
const keyOf = f => [f.file, f.line_start, f.title].join('|');
const openCount = scan => (scan?.result.findings ?? []).filter(isRisk).length;
const allFindings = () => names().flatMap(name => (scanOf(name)?.result.findings ?? []).map(f => ({ ...f, repo: name })));
const kpi = (labelText, value, sub = '', cls = '') => {
  const c = el('div', '', 'kpi' + (cls ? ' ' + cls : ''));
  c.append(el('div', labelText, 'label'), el('div', String(value), 'value'), el('div', sub, 'sub'));
  return c;
};
const addDef = (dl, key, value) => dl.append(el('dt', key), el('dd', value));

function authority(regulation = '') {
  if (/UK GDPR|\bICO\b/i.test(regulation)) return 'Information Commissioner (ICO)';
  if (/\bPRA\b/i.test(regulation)) return 'Bank of England / PRA';
  if (/\bCCA\b|Consumer Credit Act/i.test(regulation)) return 'UK Parliament (Statute)';
  if (/\bFCA\b|CONC|PRIN/i.test(regulation)) return 'Financial Conduct Authority';
  return 'Unclassified';
}

function coverage(scans) {
  const files = scans.reduce((s, r) => s + r.result.files_reviewed, 0);
  const skipped = scans.reduce((s, r) => s + r.result.skipped.length, 0);
  const total = files + skipped;
  return { files, skipped, pct: total ? Math.round((files * 100) / total) : null };
}

function alertFor(f, repo) {
  const sha = scanOf(repo)?.sha;
  return (data?.alerts ?? []).find(a => a.repo === repo && a.body.commit === sha
    && a.body.finding.title === f.title && a.body.finding.file === f.file && a.body.finding.line_start === f.line_start);
}
function alertStatus(f, repo) {
  if (!isRisk(f)) return 'Not applicable (no alert for this status)';
  const a = alertFor(f, repo);
  if (!a) return 'Not recorded';
  if (a.state === 'sent') return 'Relay accepted; email delivery unverified';
  return 'Queued' + (data.relay_configured ? '' : ' — relay not configured');
}

const matchesQuery = f => {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [f.title, f.file, f.regulation, f.evidence, f.remediation, f.agent, f.repo]
    .some(v => String(v ?? '').toLowerCase().includes(q));
};

// ---------- Navigation ----------
function currentView() {
  const h = location.hash.slice(1);
  return VIEWS.includes(h) ? h : 'cockpit';
}
function showView() {
  view = currentView();
  for (const v of VIEWS) $('view-' + v).hidden = v !== view;
  document.querySelectorAll('[data-nav]').forEach(a => {
    const on = a.dataset.nav === view;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
}
function selectSource(name) {
  selected = name;
  selectedKey = null;
  render();
}

// ---------- Render ----------
function render() {
  if (!data) return;
  if (!names().includes(selected)) selected = names()[0] ?? null;
  renderChrome();
  const renderers = {
    cockpit: renderCockpit, sources: renderSources, 'agent-audit': renderAgentAudit,
    findings: renderFindings, rulesets: renderRulesets, ledger: renderLedger, settings: renderSettings,
  };
  renderers[view]();
}

function renderChrome() {
  const live = data.mode !== 'demo';
  const text = data.error ? 'Worker warning: ' + data.error
    : data.running ? `${data.phase} · ${data.current_repo || 'Background worker'}`
    : 'Monitoring for changes';
  $('status').textContent = text;
  $('header-status').textContent = text;
  $('header-status').className = 'chip ' + (data.error ? 'risk' : data.running ? 'violet' : 'ok');
  $('mode-pill').textContent = live ? 'LIVE' : 'DEMO';
  $('mode-pill').className = 'pill ' + (live ? 'ok' : 'warn');
  $('mode-note').textContent = live ? 'LIVE · configured agent endpoints' : 'DEMO · synthetic checks and simulated providers';
  $('selected-name').textContent = selected ?? 'None';
  $('warning').hidden = !data.error;
  $('warning').textContent = data.error ? 'Worker warning: ' + data.error : '';
  const bell = $('bell-count');
  bell.hidden = !data.pending_alerts;
  bell.textContent = String(data.pending_alerts);
  for (const id of ['cockpit-rescan', 'verify', 'rescan']) $(id).disabled = busy || !selected;
}

function renderCockpit() {
  const scans = names().map(scanOf).filter(Boolean);
  const findings = allFindings();
  const open = findings.filter(isRisk);
  const urgent = open.filter(f => f.severity === 'CRITICAL' || f.severity === 'HIGH').length;
  const cov = coverage(scans);

  $('kpis').replaceChildren(
    kpi('Open risk signals', open.length, `${urgent} critical or high`, open.length ? 'risk' : ''),
    kpi('Sources reviewed', `${scans.length} / ${names().length}`,
      data.running ? `Cycle ${data.completed} of ${data.total}` : 'Latest completed reviews'),
    kpi('Files reviewed', cov.files, cov.pct == null ? 'No eligible files yet' : `${cov.skipped} skipped · ${cov.pct}% coverage`),
    kpi('Alerts', `${data.sent_alerts} / ${data.pending_alerts}`,
      data.relay_configured ? 'Relay accepted / pending' : 'Relay not configured'),
  );

  const rows = names().map(name => {
    const r = scanOf(name), m = metaOf(name);
    const tr = el('tr');
    const count = openCount(r);
    const inspect = el('button', 'Inspect', 'btn small');
    inspect.setAttribute('aria-label', `Inspect findings for ${name}`);
    inspect.onclick = () => { selectSource(name); location.hash = '#findings'; };
    tr.append(
      td(el('span', name, 'cell-title'), el('span', m.owner ?? 'Unknown owner', 'muted')),
      td(pill(m.provider ?? 'Unknown')),
      td(r ? String(r.result.files_reviewed) : '—'),
      td(r ? pill(`${count} open`, count ? 'risk' : 'ok') : pill('Awaiting', '')),
      td(el('span', r ? shortSha(r.sha) : 'Not reviewed', 'mono')),
      td(r ? when(r.updated) : 'Awaiting review'),
      td(inspect),
    );
    return tr;
  });
  $('streams').replaceChildren(...(rows.length ? rows : [emptyRow(7, 'No sources discovered yet.')]));

  const total = findings.length;
  $('matrix').replaceChildren(...Object.entries(STATUS).map(([key, s]) => {
    const count = findings.filter(f => f.status === key).length;
    const row = el('div', '', 'bar-row');
    const head = el('div', '', 'bar-head');
    head.append(el('span', s.label), el('strong', String(count)));
    const bar = el('div', '', 'bar');
    const fill = el('span', '', s.tone);
    fill.style.width = (total ? (count * 100) / total : 0) + '%';
    bar.append(fill);
    row.append(head, bar);
    return row;
  }));

  $('coverage-bar').style.width = (cov.pct ?? 0) + '%';
  $('coverage-text').textContent = cov.pct == null
    ? 'No eligible files reviewed yet.'
    : `${cov.files} reviewed, ${cov.skipped} skipped by size, count or secret-screening limits. Skipped files are not reviewed.`;

  const routines = names().map(name => {
    const row = el('div', '', 'routine');
    const info = el('div');
    info.append(el('span', name, 'cell-title'), el('span', metaOf(name).provider ?? 'Unknown', 'muted small'));
    const run = el('button', '▶ Run', 'btn small');
    run.disabled = busy;
    run.setAttribute('aria-label', `Run review for ${name}`);
    run.onclick = () => rescan(name);
    row.append(info, run);
    return row;
  });
  $('routines').replaceChildren(...(routines.length ? routines : [el('p', 'No sources discovered yet.', 'muted small')]));

  const events = data.events ?? [];
  $('timeline').replaceChildren(...(events.length ? events.map(e => {
    const li = el('li');
    li.dataset.tone = PHASE_TONE[e.phase] ?? '';
    const head = el('div', '', 'tl-head');
    head.append(pill(e.phase, PHASE_TONE[e.phase] ?? ''), el('time', new Date(e.time * 1000).toLocaleTimeString()));
    li.append(head, el('div', e.message));
    return li;
  }) : [el('li', 'No activity recorded yet.', 'muted')]));
}

function renderSources() {
  const groups = new Map();
  for (const name of names()) {
    const p = metaOf(name).provider ?? 'Other';
    if (!groups.has(p)) groups.set(p, []);
    groups.get(p).push(name);
  }
  const list = [];
  if (!names().length) list.push(el('p', 'No sources discovered yet.', 'muted'));
  for (const [provider, items] of groups) {
    list.push(el('div', provider, 'group-label'));
    for (const name of items) {
      const r = scanOf(name);
      const b = el('button', '', 'source-btn');
      b.setAttribute('aria-pressed', String(name === selected));
      const left = el('span');
      left.append(el('span', name, 'cell-title'), el('small', r ? `${openCount(r)} risk signals` : 'Awaiting review'));
      b.append(left);
      b.onclick = () => selectSource(name);
      list.push(b);
    }
  }
  $('sources').replaceChildren(...list);

  const box = $('source-detail');
  box.replaceChildren();
  if (!selected) {
    box.append(el('p', 'Sources appear here once discovery completes.', 'muted'));
    return;
  }
  const r = scanOf(selected), m = metaOf(selected);
  box.append(el('h2', selected), el('p', `${m.provider ?? 'Unknown provider'} · ${m.owner ?? 'Unknown owner'}`, 'muted small'));
  const dl = el('dl', '', 'defs');
  addDef(dl, 'Owner', m.owner ?? 'Unknown');
  addDef(dl, 'Email', m.email || 'Not configured; relay routing required');
  addDef(dl, 'Revision', r ? shortSha(r.sha) : 'Not reviewed yet');
  addDef(dl, 'Last reviewed', r ? when(r.updated) : '—');
  addDef(dl, 'Files reviewed', r ? String(r.result.files_reviewed) : '—');
  addDef(dl, 'Skipped', r ? String(r.result.skipped.length) : '—');
  addDef(dl, 'Scope', r ? r.result.scope : 'Default branch, eligible text files only');
  box.append(dl);
  const rescanBtn = el('button', '⟳ Rescan this source', 'btn primary');
  rescanBtn.disabled = busy;
  rescanBtn.onclick = () => rescan(selected);
  const openBtn = el('button', 'Open findings', 'btn');
  openBtn.onclick = () => { location.hash = '#findings'; };
  box.append(el('div', '', 'row'));
  box.lastChild.append(rescanBtn, openBtn);
  if (r?.result.skipped.length) {
    box.append(el('h3', 'Coverage limitations'), el('pre', r.result.skipped.join('\n')));
  }
}

function renderAgentAudit() {
  $('pipeline').replaceChildren(...STAGES.map(stage => {
    const active = data.running && data.phase === stage;
    const card = el('div', '', 'stage' + (active ? ' active' : ''));
    card.append(el('strong', stage[0].toUpperCase() + stage.slice(1)), el('small', active ? 'Running now' : 'Standby'));
    return card;
  }));
  const total = data.total || 0;
  const pct = total ? Math.min(100, Math.round((data.completed / total) * 100)) : 0;
  $('progress-bar').style.width = pct + '%';
  $('progress-text').textContent = `${data.completed} of ${total} sources processed this cycle · polling every ${data.poll_seconds}s · last cycle ${data.last_cycle ? when(data.last_cycle) : 'not finished yet'}`;

  const agents = Object.entries(data.agents ?? {});
  $('agents').replaceChildren(...(agents.length ? agents.map(([name, status]) => {
    const row = el('div', '', 'agent-row');
    const tone = status === 'completed' ? 'ok' : status === 'failed' ? 'risk' : status === 'running' ? 'violet' : '';
    row.append(el('span', name, 'cell-title'), pill(status, tone));
    return row;
  }) : [el('p', 'No specialist activity in the current cycle.', 'muted small')]));

  const events = data.events ?? [];
  $('events').replaceChildren(...(events.length ? events.map(e => {
    const row = el('div', '', 'log-row');
    row.append(el('time', new Date(e.time * 1000).toLocaleTimeString()), el('span', `[${e.phase}] ${e.message}`));
    return row;
  }) : [el('p', 'No activity yet.', 'muted')]));
  renderJudge();
}

function renderJudge() {
  const panel = $('judge-results');
  panel.hidden = !judgeOpen;
  $('judge-button').textContent = judgeOpen ? 'Hide evaluation' : 'Show evaluation for selected source';
  if (!judgeOpen) return;
  panel.replaceChildren();
  const r = scanOf(selected);
  const e = r?.result.evaluation;
  if (!e) {
    panel.append(el('p', 'No evaluation yet. Rescan this source.', 'muted'));
    return;
  }
  panel.append(el('p', `${selected} · ${shortSha(r.sha)} · ${e.reason}`, 'muted small'));
  const grid = el('div', '', 'kpis');
  for (const [text, key] of [['Benchmark accuracy', 'accuracy'], ['Precision', 'precision'], ['Recall', 'recall'], ['F1', 'f1'], ['Literal evidence match', 'evidence_match']]) {
    grid.append(kpi(text, e[key] == null ? 'N/A' : e[key] + '%'));
  }
  panel.append(grid);
  if (e.tp != null) panel.append(el('p', `True positives ${e.tp} · False positives ${e.fp} · False negatives ${e.fn} · True negatives ${e.tn}`, 'muted small'));
  panel.append(el('p', 'File-level synthetic benchmark; evidence match is a literal line check, not citation or fix correctness.', 'muted small'));
}

function visibleFindings() {
  const all = scanOf(selected)?.result.findings ?? [];
  return all.filter(f =>
    (statusFilter === 'ALL' || f.status === statusFilter) &&
    (severityFilter === 'ALL' || f.severity === severityFilter) &&
    matchesQuery({ ...f, repo: selected }));
}

function renderFindings() {
  const r = scanOf(selected);
  $('heading').textContent = selected ?? 'Findings';
  $('findings-meta').textContent = r
    ? `${r.result.source} · revision ${shortSha(r.sha)} · ${r.result.files_reviewed} files reviewed · ${r.result.skipped.length} skipped · ${when(r.updated)}`
    : (selected ? 'Awaiting first review' : 'Awaiting discovery');
  document.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.filter === statusFilter)));

  const all = r?.result.findings ?? [];
  const list = visibleFindings();
  const current = list.find(f => keyOf(f) === selectedKey) ?? list[0] ?? null;
  selectedKey = current ? keyOf(current) : null;

  $('findings-list').replaceChildren(...(list.length ? list.map(f => {
    const b = el('button', '', 'finding-card');
    b.dataset.tone = tone(f);
    b.setAttribute('aria-pressed', String(f === current));
    const meta = el('div', '', 'meta');
    meta.append(pill(f.severity), pill(label(f), tone(f)));
    b.append(meta, el('div', f.title, 'title'), el('div', `${f.file}:${f.line_start} · ${f.regulation}`, 'loc'));
    b.onclick = () => { selectedKey = keyOf(f); render(); };
    return b;
  }) : [el('p', all.length ? 'No findings match the current filters.' : 'No findings recorded for this source. Not a compliance certification.', 'muted')]));

  const box = $('finding-detail');
  box.replaceChildren();
  if (!current) {
    box.append(el('p', 'Select a finding to see its evidence and recommended remediation.', 'muted'));
  } else {
    const m = metaOf(selected);
    box.append(el('p', `${label(current)} · ${current.severity}`, 'eyebrow'));
    box.append(el('h2', current.title, 'detail-title'));
    const dl = el('dl', '', 'defs');
    addDef(dl, 'Regulation', current.regulation);
    addDef(dl, 'Authority', authority(current.regulation));
    addDef(dl, 'Location', `${current.file}:${current.line_start}`);
    addDef(dl, 'Agent', current.agent ?? 'Unknown');
    addDef(dl, 'Owner', m.owner ?? 'Unknown');
    box.append(dl);

    box.append(el('h3', 'Evidence'));
    const code = el('div', '', 'code');
    const head = el('div', '', 'code-head');
    head.append(el('span', current.file), el('span', 'line ' + current.line_start));
    const line = el('div', '', 'code-line' + (isRisk(current) ? ' risk' : ''));
    line.append(el('span', String(current.line_start), 'ln'), el('code', current.evidence));
    code.append(head, line);
    box.append(code);

    box.append(el('h3', 'Recommended remediation'), el('p', current.remediation));
    const copy = el('button', 'Copy recommendation', 'btn');
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(current.remediation); copy.textContent = 'Copied'; }
      catch { copy.textContent = 'Copy unavailable'; }
    };
    const verify = el('button', '✓ Rescan to verify fix', 'btn primary');
    verify.disabled = busy;
    verify.onclick = () => rescan(selected);
    box.append(el('div', '', 'row'));
    box.lastChild.append(copy, verify);
    box.append(el('p', 'Email alert: ' + alertStatus(current, selected), 'muted small'));
    box.append(el('p', 'Absence of a finding after a rescan is not proof of a fix. Human verification is required.', 'muted small'));
  }

  const cov = $('coverage-card');
  cov.replaceChildren(
    el('h2', 'Coverage limitations'),
    el('p', r ? `${r.result.files_reviewed} files reviewed · ${r.result.skipped.length} skipped` : 'No review yet.', 'muted small'),
  );
  if (r?.result.skipped.length) cov.append(el('pre', r.result.skipped.join('\n')));
}

function renderRulesets() {
  const findings = allFindings();
  const byReg = new Map();
  for (const f of findings) {
    const key = f.regulation || 'Unspecified';
    if (!byReg.has(key)) byReg.set(key, { key, auth: authority(f.regulation), risk: 0, warn: 0, ok: 0, repos: new Set() });
    const g = byReg.get(key);
    if (f.status === 'NON_COMPLIANT') g.risk++;
    else if (f.status === 'NEEDS_CONTEXT') g.warn++;
    else if (f.status === 'COMPLIANT_WITHIN_SCOPE') g.ok++;
    g.repos.add(f.repo);
  }
  const regRows = [...byReg.values()]
    .sort((a, b) => b.risk - a.risk || a.key.localeCompare(b.key))
    .map(g => {
      const tr = el('tr');
      tr.append(td(el('span', g.key, 'cell-title')), td(g.auth), td(String(g.risk)), td(String(g.warn)), td(String(g.ok)), td([...g.repos].join(', ')));
      return tr;
    });
  $('rules-body').replaceChildren(...(regRows.length ? regRows : [emptyRow(6, 'No regulations cited by current findings.')]));

  const clauseRows = findings.filter(matchesQuery).slice(0, 200).map(f => {
    const tr = el('tr');
    tr.append(
      td(authority(f.regulation)),
      td(el('span', f.regulation, 'cell-title')),
      td(el('span', `${f.repo} · ${f.file}:${f.line_start}`, 'mono small')),
      td(pill(label(f), tone(f))),
      td(f.agent ?? '—'),
    );
    return tr;
  });
  $('clauses-body').replaceChildren(...(clauseRows.length ? clauseRows : [emptyRow(5, query ? 'No mappings match the search.' : 'No findings mapped yet.')]));
}

function renderLedger() {
  $('ledger-kpis').replaceChildren(
    kpi('Pending alerts', data.pending_alerts, 'Queued for the notification relay', data.pending_alerts ? 'risk' : ''),
    kpi('Relay accepted', data.sent_alerts, 'Acceptance is not delivery confirmation'),
    kpi('Relay configured', data.relay_configured ? 'Yes' : 'No', 'Set ALERT_WEBHOOK_URL in .env'),
    kpi('Scan records', data.repositories.length, 'Latest result per source'),
  );
  const rows = (data.alerts ?? []).map(a => {
    const f = a.body?.finding ?? {};
    const tr = el('tr');
    tr.append(
      td(el('span', String(a.id).slice(0, 12), 'mono small')),
      td(a.repo),
      td(el('span', f.title ?? '—', 'cell-title'), el('span', f.regulation ?? '', 'muted small')),
      td(pill(f.severity ?? '—')),
      td(a.state === 'sent' ? pill('Relay accepted', 'ok') : pill('Queued', 'warn')),
      td(when(a.updated)),
      td(a.body?.owner ?? '—'),
    );
    return tr;
  });
  $('alerts-body').replaceChildren(...(rows.length ? rows : [emptyRow(7, 'No alerts recorded. Non-compliant findings queue alerts automatically.')]));
}

function renderSettings() {
  const dl = $('config');
  dl.replaceChildren();
  addDef(dl, 'Mode', data.mode === 'demo' ? 'Demo (synthetic fixtures)' : 'Live');
  addDef(dl, 'Poll interval', `${data.poll_seconds} seconds`);
  addDef(dl, 'Alert relay', data.relay_configured ? 'Configured' : 'Not configured');
  addDef(dl, 'Sources discovered', String(names().length));
  addDef(dl, 'Last cycle', data.last_cycle ? when(data.last_cycle) : 'Not finished yet');
}

// ---------- Export ----------
function buildReport() {
  const lines = [
    '# ReguLens assurance summary', '',
    `Generated: ${new Date().toLocaleString()}`,
    `Mode: ${data.mode}`, '',
    'Not a compliance certification. Absence of a finding is not proof of compliance or of a fix. Human verification required.', '',
    '## Sources', '',
  ];
  for (const name of names()) {
    const r = scanOf(name), m = metaOf(name);
    const state = r
      ? `revision ${shortSha(r.sha)}, ${r.result.files_reviewed} files reviewed, ${r.result.skipped.length} skipped, ${openCount(r)} non-compliant`
      : 'not reviewed yet';
    lines.push(`- ${name} (${m.provider ?? 'unknown'}, owner: ${m.owner ?? 'unknown'}): ${state}`);
  }
  lines.push('', '## Findings', '');
  const findings = allFindings();
  if (!findings.length) lines.push('No findings recorded.', '');
  for (const f of findings) {
    lines.push(
      `### ${f.title}`, '',
      `- Source: ${f.repo}`,
      `- Status: ${label(f)} · Severity: ${f.severity}`,
      `- Regulation: ${f.regulation}`,
      `- Location: ${f.file}:${f.line_start}`,
      `- Evidence: ${f.evidence}`,
      `- Recommended remediation: ${f.remediation}`, '',
    );
  }
  return lines.join('\n');
}
function exportReport() {
  if (!data) return;
  const url = URL.createObjectURL(new Blob([buildReport()], { type: 'text/markdown' }));
  const a = el('a');
  a.href = url;
  a.download = 'regulens-summary.md';
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- Network and rescan ----------
async function api(url, opts = {}) {
  const r = await fetch(url, { ...opts, signal: AbortSignal.timeout(10000), cache: 'no-store' });
  if (!r.ok) throw Error('HTTP ' + r.status);
  return r.json();
}

async function refresh() {
  try {
    data = await api('/api/status');
    render();
  } catch {
    $('status').textContent = 'Disconnected — results may be stale';
    $('header-status').textContent = 'Disconnected';
    $('header-status').className = 'chip risk';
  } finally {
    setTimeout(refresh, 1500);
  }
}

function say(text, user = false) {
  const m = el('div', text, 'message' + (user ? ' user' : ''));
  $('messages').append(m);
  $('messages').scrollTop = $('messages').scrollHeight;
}

async function rescan(repo) {
  if (busy || !repo || !data) return;
  busy = true;
  render();
  const before = (scanOf(repo)?.result.findings ?? []).filter(isRisk);
  say('Requesting fresh agent review of ' + repo);
  try {
    const { job } = await api('/api/rescan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repository: repo }),
    });
    let last = '';
    for (let i = 0; i < 600; i++) {
      const j = await api('/api/rescan/' + job);
      if (j.message !== last) { say(j.message); last = j.message; }
      if (j.status === 'failed') break;
      if (j.status === 'completed') {
        const latest = await api('/api/status');
        const r = latest.repositories.find(x => x.repo === repo);
        if (r && r.sha === j.commit) {
          const after = r.result.findings.filter(isRisk);
          const missing = before.filter(f => !after.some(a => a.title === f.title && a.file === f.file));
          say(`${after.length} current risk signals. ${missing.length} previous signals not redetected. ${r.result.skipped.length} skipped files. Human verification required before marking fixed.`);
          data = latest;
        }
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  } catch (e) {
    say('Rescan status unavailable: ' + e.message);
  } finally {
    busy = false;
    render();
  }
}

// ---------- Assistant ----------
function toggleChat(open) {
  $('chat').hidden = !open;
  $('chat-toggle').setAttribute('aria-expanded', String(open));
  if (open) $('prompt').focus();
}

// ---------- Wiring ----------
window.addEventListener('hashchange', () => { showView(); render(); });
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    $('search').focus();
  }
});
$('search').addEventListener('input', e => { query = e.target.value; render(); });
$('filter').addEventListener('change', e => { severityFilter = e.target.value; render(); });
document.querySelectorAll('[data-filter]').forEach(b => {
  b.addEventListener('click', () => { statusFilter = b.dataset.filter; render(); });
});
$('judge-button').onclick = () => { judgeOpen = !judgeOpen; renderJudge(); };
$('export').onclick = exportReport;
$('cockpit-rescan').onclick = () => rescan(selected);
$('verify').onclick = () => rescan(selected);
$('rescan').onclick = () => rescan(selected);
$('chat-toggle').onclick = () => toggleChat($('chat').hidden);
$('close').onclick = () => toggleChat(false);
$('chat-form').onsubmit = e => {
  e.preventDefault();
  const q = $('prompt').value.trim();
  $('prompt').value = '';
  if (!q) return;
  say(q, true);
  if (/rescan|verify|scan|fix/i.test(q)) rescan(selected);
  else if (/status/i.test(q)) say($('status').textContent);
  else say('Supported commands: rescan, verify fix, status.');
};

showView();
say('Select a source and ask me to rescan or verify a fix.');
refresh();
