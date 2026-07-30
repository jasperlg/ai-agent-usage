// Run jobs.html's inline script against a stub DOM to prove it executes,
// renders every group, and raises the staleness banner only when it should.
const fs = require('fs');
const vm = require('vm');
const html = fs.readFileSync('jobs.html', 'utf8');
const src = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const PAYLOAD_RE = /window\.JOBS_DATA=\{[\s\S]*?\};\n/;

function run(generatedAt) {
  const payload = JSON.parse(html.match(/window\.JOBS_DATA=(\{.*?\});\n/s)[1].replace(/<\\\//g, '</'));
  payload.generated_at = generatedAt;
  // The generated file inlines its own payload, so swap that assignment out --
  // otherwise the real timestamp overwrites the one under test.
  //
  // The replacement MUST go through a function. Inlined shell scripts contain
  // sequences like $' and $& which String.replace treats as backreferences,
  // silently corrupting the code into a syntax error.
  const code = src.replace(PAYLOAD_RE, () =>
    'window.JOBS_DATA=' + JSON.stringify(payload) + ';\n');
  if (code === src) throw new Error('payload assignment not found -- harness is stale');

  const els = {};
  const mk = () => ({ textContent: '', innerHTML: '', className: '', value: '',
                      children: [], dataset: {}, addEventListener() {},
                      setAttribute() {}, closest: () => null });
  for (const id of ['meta', 'age', 'tiles', 'groups', 'note', 'q', 'filter', 'theme']) els[id] = mk();
  const ctx = {
    window: {}, console, setTimeout,
    document: {
      documentElement: { setAttribute() {} },
      getElementById: id => els[id],
      addEventListener() {},
    },
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return els;
}

// The page as actually shipped must compile. Everything below runs a mutated
// copy, so without this a syntax error in the real file could slip through.
new vm.Script(src);

const fresh = run(new Date(Date.now() - 3600e3).toISOString());
const stale = run(new Date(Date.now() - 5 * 86400e3).toISOString());

const detail = fresh.groups.innerHTML;
const checks = [
  ['detail row per job',        (detail.match(/class="det"/g) || []).length ===
                                (detail.match(/class="row"/g) || []).length],
  ['detail rows start hidden',  detail.includes('class="det" hidden')],
  ['shows the plist',           detail.includes('StartCalendarInterval')],
  ['shows script source',       detail.includes('Unattended usage refresh')],
  ['identifies binaries',       detail.includes('signed by')],
  ['links the log path',        detail.includes('ai-agent-usage-refresh.log')],
  ['offers run now',            detail.includes('--run ') && detail.includes('run now…')],
  ['offers remove',             detail.includes('--remove ') && detail.includes('remove…')],
  ['renders group tables',      fresh.groups.innerHTML.includes('<table>')],
  ['renders all three groups',  (fresh.groups.innerHTML.match(/<table>/g) || []).length === 3],
  ['renders summary tiles',     fresh.tiles.innerHTML.includes('on a timer')],
  ['shows your job',            fresh.groups.innerHTML.includes('ai-agent-usage.refresh')],
  ['tags it yours',             fresh.groups.innerHTML.includes('pill mine')],
  ['escapes html in values',    !fresh.groups.innerHTML.includes('<script')],
  ['banner hidden when fresh',  !fresh.age.className.includes('show')],
  ['banner shown when stale',   stale.age.className.includes('show')],
  ['banner names the job',      stale.age.innerHTML.includes('ai-agent-usage.refresh')],
  ['banner counts 5 days',      stale.age.innerHTML.includes('5 days ago')],
  ['note rendered',             fresh.note.textContent.includes('launchctl')],
];
let bad = 0;
for (const [name, ok] of checks) { if (!ok) bad++; console.log((ok ? '  ok   ' : '  FAIL ') + name); }
process.exit(bad ? 1 : 0);
