// Run the actual dashboard script with fixture data and a minimal DOM.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const template = fs.readFileSync('scripts/dashboard.template.html', 'utf8');
const script = [...template.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
const date = '2026-08-15';
const stats = (tool, count, tokens) => ({
  tools_daily: {[date]: {[tool]: count}},
  sessions_daily: {[date]: count},
  user_messages_daily: {[date]: count},
  hours_daily: {[date]: [tokens, ...Array(23).fill(0)]},
});
const data = {
  generated_at: '2026-08-15T14:00:00Z', months: ['2026-08'],
  range: {start: date, end: date},
  columns: ['date','project','model','input','output','cache_write_5m','cache_write_1h','cache_read','messages','agent'],
  rows: [[date,'Claude project','claude-test',1000000,0,0,0,0,1,'claude'],
         [date,'Codex project','gpt-test',1000000,1000000,0,0,1000000,1,'codex'],
         [date,'Unknown project','internal-model',100,0,0,0,0,1,'codex']],
  model_labels: {},
  pricing: {'claude-test': {input: 1, output: 5},
            'gpt-test': {input: 2, output: 10, cached_input: 0.5},
            'internal-model': {input: 0, output: 0, unpriced: true}},
  fallback: {input: 5, output: 25},
  cache_multipliers: {read: 0.1, write_5m: 1.25, write_1h: 2},
  agents: {claude: stats('ClaudeTool', 2, 1000000), codex: stats('CodexTool', 3, 3000100)},
  ...stats('ClaudeTool', 5, 4000100),
  tools_daily: {[date]: {ClaudeTool: 2, CodexTool: 3}},
};

function run(payload = data, hash = '', extra = {}) {
  const nodes = {};
  const makeNode = () => ({
    innerHTML: '', textContent: '', dataset: {}, style: {}, children: [], attrs: {},
    clientWidth: 800, offsetWidth: 100,
    classList: {add() {}, remove() {}, contains() {return false;}, toggle() {}},
    appendChild(n) {this.children.push(n);},
    setAttribute(k,v) {this.attrs[k] = v;}, addEventListener() {},
  });
  const context = {
    window: {USAGE_DATA: payload, ...extra}, console, URLSearchParams, setTimeout, clearTimeout,
    addEventListener() {}, location: {hash},
    history: {replaceState(_a,_b,value) {context.location.hash = value;}},
    getComputedStyle: () => ({getPropertyValue: () => '#123456'}),
    document: {
      body: makeNode(), documentElement: makeNode(),
      querySelector: s => nodes[s] ||= makeNode(), querySelectorAll: () => [],
      createElement: makeNode, createElementNS: makeNode,
    },
  };
  const done = vm.runInNewContext(script, context);
  return {nodes, context, done, pick(id, value) {
    nodes['#' + id].onclick({target: {closest: () => ({dataset: {v: value}})}});
  }};
}

const app = run();
assert.match(app.nodes['#price-note'].innerHTML, /1 model needs a rate/);
assert.match(app.nodes['#price-note'].innerHTML, /internal-model<\/code> \(Codex, 100 tokens, left out of cost\)/);
assert.match(app.nodes['#price-note'].innerHTML, /ingest\.py --rebuild-payload/);
assert.match(app.nodes['#tiles'].innerHTML, /\$13\.50/);
assert.match(app.nodes['#tiles'].innerHTML, /5<\/b> sessions/);
app.pick('agent', 'codex');
assert.match(app.context.location.hash, /agent=codex/);
assert.match(app.nodes['#tiles'].innerHTML, /\$12\.50/);
assert.match(app.nodes['#tiles'].innerHTML, /3<\/b> sessions/);
assert.match(app.nodes['#tiles'].innerHTML, /100<\/b> tokens excluded/);
assert.match(app.nodes['#model-table'].innerHTML, /no rate/);
assert.match(app.nodes['#model-table'].innerHTML, /Excluded/);
assert.doesNotMatch(app.nodes['#model-table'].innerHTML, /claude-test/);
assert.match(app.nodes['#tool-chart'].innerHTML, /CodexTool/);
assert.doesNotMatch(app.nodes['#tool-chart'].innerHTML, /ClaudeTool/);
assert.match(app.nodes['#heat-table'].innerHTML, /3\.0M/);
app.pick('hero-split', 'type');
app.pick('hero-metric', 'cost');
assert.match(app.nodes['#hero-table'].innerHTML, /\$0\.50/);
assert.match(app.nodes['#hero-table'].innerHTML, /\$12\.50/);
app.pick('agent', 'claude');
assert.equal(app.nodes['#price-note'].innerHTML, '');
assert.match(app.nodes['#tiles'].innerHTML, /\$1\.00/);
assert.match(app.nodes['#tiles'].innerHTML, /2<\/b> sessions/);
assert.doesNotMatch(app.nodes['#tiles'].innerHTML, /excluded/);
assert.doesNotMatch(app.nodes['#tool-chart'].innerHTML, /CodexTool/);
assert.doesNotMatch(app.nodes['#proj-chart'].innerHTML, /Codex project/);
assert.match(run(data, '#agent=codex').nodes['#tiles'].innerHTML, /\$12\.50/);
const empty = run({...data, rows: []});
assert.match(empty.context.document.body.innerHTML, /No usage data/);
const claudeOnly = run({...data, rows: data.rows.slice(0,1)});
claudeOnly.pick('agent', 'codex');
assert.match(claudeOnly.nodes['#tiles'].innerHTML, /\$0\.00/);
for (const node of Object.values(claudeOnly.nodes)) {
  assert.doesNotMatch(node.innerHTML, /NaN|Infinity/);
}

// Known costs across month, year and Monday boundaries, with empty periods.
const datedCosts = [
  ['2025-08-31',512], ['2025-12-31',1], ['2026-01-01',2], ['2026-01-04',4],
  ['2026-01-05',8], ['2026-02-01',16], ['2026-02-02',32],
  ['2026-03-08',64], ['2026-03-09',128],
];
const historyData = {...data, rows: datedCosts.map(([d,cost]) =>
  [d,'Project','claude-test',cost*1e6,0,0,0,0,1,'claude'])};
const trend = run(historyData);
function trendRows(app) {
  const body = app.nodes['#hero-table'].innerHTML.match(/<tbody>([\s\S]*?)<\/tbody>/)[1];
  return [...body.matchAll(/<tr><td>(.*?)<\/td>(.*?)<\/tr>/g)].map(m => {
    const cells = [...m[2].matchAll(/<td>(.*?)<\/td>/g)];
    return {label:m[1], total:Number(cells.at(-1)[1].replace(/[$,]/g,''))};
  });
}
const totalOf = app => trendRows(app).reduce((n,r) => n+r.total,0);
assert.equal(trend.nodes['#hero-title'].textContent, 'API cost per day');
assert.equal(totalOf(trend), 192);
trend.pick('range','all');
assert.equal(trend.nodes['#hero-title'].textContent, 'API cost per month');
assert.deepEqual(trendRows(trend).map(r => r.total), [512,0,0,0,1,14,48,192]);
assert.match(trendRows(trend)[0].label, /partial/);
assert.match(trendRows(trend).at(-1).label, /partial/);
assert.equal(trendRows(trend)[4].label, 'Dec 2025');
assert.equal(trendRows(trend)[5].label, 'Jan 2026');
trend.pick('hero-period','week');
assert.equal(totalOf(trend),767);
assert.equal(trendRows(trend).find(r => r.label === 'Dec 29, 2025 to Jan 4, 2026').total,7);
assert.equal(trendRows(trend).find(r => r.label === 'Jan 26, 2026 to Feb 1, 2026').total,16);
trend.pick('hero-period','day');
assert.equal(totalOf(trend),767);
trend.pick('hero-period','auto');
trend.pick('range','90');
assert.equal(trend.nodes['#hero-title'].textContent,'API cost per week');
assert.equal(totalOf(trend),255);
trend.pick('range','180');
assert.equal(trend.nodes['#hero-title'].textContent,'API cost per week');
trend.pick('range','365');
assert.equal(trend.nodes['#hero-title'].textContent,'API cost per month');
trend.pick('range','30');
trend.pick('hero-period','month');
assert.deepEqual(trendRows(trend).map(r => r.total),[0,192]);
assert.match(trendRows(trend)[0].label,/Feb 8, 2026 to Feb 28, 2026 \(partial\)/);
assert.equal(totalOf(trend),192);
trend.pick('agent','codex');
assert.equal(totalOf(trend),0);
trend.pick('agent','claude');
trend.pick('hero-split','type');
assert.equal(totalOf(trend),192);
trend.pick('hero-metric','tokens');
assert.equal(trend.nodes['#hero-title'].textContent,'Token consumption per month');
assert.match(trend.context.location.hash,/period=month/);

const leap = run({...data, rows: ['2024-02-28','2024-02-29','2024-03-01'].map(d =>
  [d,'Project','claude-test',1e6,0,0,0,0,1,'claude'])}, '#period=month');
leap.pick('range','all');
assert.deepEqual(trendRows(leap).map(r => r.total),[2,1]);
assert.match(trendRows(leap)[0].label,/Feb 29, 2024/);
// The published page: no inlined data, one shared-data record per machine and day.
const record = (machine, rows, sessions, updatedAt, deleted = false) => ({
  collection: 'usage', key: `${machine}:${date}`, version: 1, deleted, updatedAt,
  value: {v: 1, machine, date, columns: data.columns.slice(1),
          rows: rows.map(r => r.slice(1)),
          agents: {claude: {sessions_daily: sessions, tools_daily: {Read: sessions}, hours_daily: Array(24).fill(sessions), user_messages_daily: 0}}},
});
const pages = [
  {records: [record('agent-jasper', [data.rows[0]], 4, '2026-08-15T13:00:00Z')], next: 'agent-jasper:2026-08-15'},
  {records: [record('macbook-pro', [data.rows[0], data.rows[0]], 1, '2026-08-15T12:00:00Z'),
             record('old-box', [data.rows[0]], 9, '2026-08-01T12:00:00Z', true)], next: null},
];
const shared = {collection: 'usage', machines: {'agent-jasper': 'Devbox'},
  models: {'claude-test': {input: 1, output: 5}}, model_labels: {},
  fallback: {input: 5, output: 25}, codex_fallback: {input: 0, output: 0, unpriced: true},
  cache_multipliers: data.cache_multipliers};
const seen = [];
const both = run(null, '', {USAGE_SHARED: shared, MustryData: {
  list: async (collection, options) => { seen.push([collection, options.after]); return pages[seen.length - 1]; }}});
(async () => {
  await both.done;
  assert.deepEqual(seen, [['usage', undefined], ['usage', 'agent-jasper:2026-08-15']]);
  assert.match(both.nodes['#tiles'].innerHTML, /\$3\.00/);
  assert.match(both.nodes['#tiles'].innerHTML, /5<\/b> sessions/);
  assert.match(both.nodes['#machine'].innerHTML, /Devbox/);
  assert.match(both.nodes['#machine'].innerHTML, /macbook-pro/);
  assert.doesNotMatch(both.nodes['#machine'].innerHTML, /old-box/);
  assert.match(both.nodes['#foot-meta'].innerHTML, /Devbox/);
  assert.equal(both.context.document.documentElement.dataset.printReady, 'true');
  assert.equal(both.nodes['#price-note'].innerHTML, '');
  both.pick('machine', 'agent-jasper');
  assert.match(both.nodes['#tiles'].innerHTML, /\$1\.00/);
  assert.match(both.nodes['#tiles'].innerHTML, /4<\/b> sessions/);
  assert.match(both.context.location.hash, /machine=agent-jasper/);
  const failing = run(null, '', {USAGE_SHARED: shared, MustryData: {list: async () => { throw new Error('Open the original share link again'); }}});
  await failing.done;
  assert.match(failing.context.document.body.innerHTML, /Open the original share link again/);
  const none = run(null, '', {USAGE_SHARED: shared, MustryData: {list: async () => ({records: [], next: null})}});
  await none.done;
  assert.match(none.context.document.body.innerHTML, /No usage data/);
  console.log('Dashboard checks passed: filters, pricing, daily/weekly/monthly totals, partial periods, calendar boundaries and the shared page.');
})().catch(error => { console.error(error); process.exitCode = 1; });
