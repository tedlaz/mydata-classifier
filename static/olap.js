// OLAP: κύβος εσόδων-εξόδων. Όλο το pivot γίνεται εδώ από τα γεγονότα του #olap-data
// (app._olap_cube): slice (φίλτρα), dice (γραμμές × στήλες), drill-down, παραστατικά ανά κελί.
// Γεγονός = [doc, κατηγ. Ε3, τύπος Ε3, κατηγ. ΦΠΑ, καθαρή, ΦΠΑ, σύνολο, παρακρ., λοιποί φόροι, ψηφ. τέλος, τέλη, κρατήσεις,
//           μη εκπιπτόμενος ΦΠΑ (έξοδα χωρίς χαρακτηρισμό ΦΠΑ 361–366, όπως στο Φ2)].
(function () {
  'use strict';
  const root = document.getElementById('olap');
  if (!root) return;
  const {docs, facts, labels} = JSON.parse(document.getElementById('olap-data').textContent);
  const $ = s => root.querySelector(s);
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
  const nf2 = new Intl.NumberFormat('el-GR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const nf0 = new Intl.NumberFormat('el-GR', {maximumFractionDigits: 0});
  const MONTHS = ['Ιαν.', 'Φεβ.', 'Μαρ.', 'Απρ.', 'Μαΐ.', 'Ιουν.', 'Ιουλ.', 'Αυγ.', 'Σεπ.', 'Οκτ.', 'Νοέ.', 'Δεκ.'];
  const KIND = {in: 'Έσοδα', ex: 'Έξοδα'};
  const REST = '\u0000rest';  // στήλη «Λοιπά»
  const ROW_LIMIT = 20, COL_LIMIT = 12;

  // Συναλλασσόμενος: κλειδί = ΑΦΜ (ή «~επωνυμία» όταν λείπει το ΑΦΜ).
  const cpKey = d => d.cp || (d.cpn ? '~' + d.cpn : '');
  const cpName = {};
  docs.forEach(d => { const k = cpKey(d); if (k && d.cpn && !cpName[k]) cpName[k] = d.cpn; });
  const withDesc = (k, desc) => desc ? k + ' · ' + desc : k;

  // Διαστάσεις: key(γεγονός, παραστατικό) → κλειδί μέλους· next = προεπιλογή για drill-down.
  const DIMS = {
    kind: {label: 'Είδος', acc: 'είδος', key: (f, d) => d.k, name: k => KIND[k], next: 'e3c'},
    year: {label: 'Έτος', acc: 'έτος', time: true, next: 'quarter', key: (f, d) => d.date.slice(0, 4), name: k => k},
    quarter: {label: 'Τρίμηνο', acc: 'τρίμηνο', time: true, next: 'month',
      key: (f, d) => d.date && d.date.slice(0, 4) + '-Q' + Math.ceil(d.date.slice(5, 7) / 3),
      name: k => 'ΑΒΓΔ'[k.slice(-1) - 1] + '΄ τρίμ. ' + k.slice(0, 4)},
    month: {label: 'Μήνας', acc: 'μήνα', time: true, next: 'day',
      key: (f, d) => d.date.slice(0, 7), name: k => MONTHS[k.slice(5) - 1] + ' ' + k.slice(0, 4)},
    day: {label: 'Ημέρα', acc: 'ημέρα', time: true, key: (f, d) => d.date, name: k => k.split('-').reverse().join('/')},
    cp: {label: 'Συναλλασσόμενος', acc: 'συναλλασσόμενο', next: 'e3t', none: 'Χωρίς ΑΦΜ (λιανική)', key: (f, d) => cpKey(d),
      name: k => k[0] === '~' ? k.slice(1) : cpName[k] ? cpName[k] + ' · ' + k : 'ΑΦΜ ' + k},
    type: {label: 'Τύπος παραστατικού', acc: 'τύπο παραστατικού', next: 'cp', key: (f, d) => d.type, name: k => withDesc(k, labels.type[k])},
    series: {label: 'Σειρά', acc: 'σειρά', next: 'month', none: 'Χωρίς σειρά', key: (f, d) => d.series, name: k => k},
    e3c: {label: 'Κατηγορία Ε3', acc: 'κατηγορία Ε3', next: 'e3t', none: 'Χωρίς χαρακτηρισμό Ε3', key: f => f[1],
      name: k => withDesc(k.replace('category', '').replace('_', '.'), labels.e3c[k])},
    e3t: {label: 'Τύπος Ε3', acc: 'τύπο Ε3', next: 'cp', none: 'Χωρίς χαρακτηρισμό Ε3', key: f => f[2], name: k => withDesc(k, labels.e3t[k])},
    vat: {label: 'Συντελεστής ΦΠΑ', acc: 'συντελεστή ΦΠΑ', next: 'e3t', none: 'Χωρίς γραμμές', key: f => f[3],
      name: k => labels.vat[k] || 'Κατηγορία ' + k},
  };
  const TIME = ['year', 'quarter', 'month', 'day'];
  const DRILL_FALLBACK = ['month', 'cp', 'e3c', 'e3t', 'type', 'vat', 'series', 'day'];
  const dimName = (dim, k) => k ? DIMS[dim].name(k) : (DIMS[dim].none || 'Χωρίς τιμή');

  // Μέτρα: i = θέση στο γεγονός, minus = θέση που αφαιρείται· signed = τα έξοδα αφαιρούνται.
  // Εκπιπτόμενος ΦΠΑ: στα έσοδα όλος ο ΦΠΑ (εκροών), στα έξοδα όσος μετρά στη δήλωση Φ2.
  const MEASURES = {
    net: {label: 'Καθαρή αξία', i: 4},
    vat: {label: 'ΦΠΑ (όλος)', i: 5},
    vatded: {label: 'ΦΠΑ εκπιπτόμενος', i: 5, minus: 12},
    vatnd: {label: 'ΦΠΑ μη εκπιπτόμενος', i: 12},
    gross: {label: 'Συνολική αξία', i: 6},
    // Αποτέλεσμα: ο μη εκπιπτόμενος ΦΠΑ είναι κόστος → προστίθεται στα έξοδα (στα έσοδα είναι 0).
    res: {label: 'Αποτέλεσμα (Εσ. − Εξ.)', i: 4, plus: 12, signed: true},
    vatbal: {label: 'Διαφορά ΦΠΑ (εκρ. − εκπ. εισρ.)', i: 5, minus: 12, signed: true},
    wh: {label: 'Παρακρατούμενοι φόροι', i: 7},
    ot: {label: 'Λοιποί φόροι', i: 8},
    sd: {label: 'Ψηφιακό τέλος συν.', i: 9},
    fe: {label: 'Τέλη', i: 10},
    de: {label: 'Κρατήσεις', i: 11},
    cnt: {label: 'Πλήθος παραστατικών', count: true},
  };
  const MEASURE_COLS = ['net', 'vat', 'vatnd', 'wh', 'ot', 'sd', 'fe', 'de', 'gross'];  // στήλες «Μέτρα»
  const TAX_COLS = ['wh', 'ot', 'sd', 'fe', 'de'];
  const HIDE_ZERO = [...TAX_COLS, 'vatnd'];  // στις «Μέτρα» κρύβονται όταν είναι παντού μηδέν
  const mv = (f, m) => f[m.i] - (m.minus ? f[m.minus] : 0) + (m.plus ? f[m.plus] : 0);

  const PRESETS = [
    {label: 'Χρονική εξέλιξη', rows: 'month', cols: 'kind', m: 'net'},
    {label: 'Αποτέλεσμα ανά τρίμηνο', rows: 'quarter', cols: '', m: 'res'},
    {label: 'Πελάτες', rows: 'cp', cols: 'year', m: 'net', kind: 'in'},
    {label: 'Προμηθευτές', rows: 'cp', cols: 'year', m: 'net', kind: 'ex'},
    {label: 'Χαρακτηρισμοί Ε3', rows: 'e3c', cols: 'kind', m: 'net'},
    {label: 'ΦΠΑ ανά συντελεστή', rows: 'vat', cols: 'kind', m: 'vatded'},
    {label: 'Φόροι & κρατήσεις', rows: 'month', cols: 'measures', m: 'net', kind: 'in'},
    {label: 'Τύποι παραστατικών', rows: 'type', cols: 'kind', m: 'cnt'},
  ];

  // Σύνολο παραστατικού (για το modal): άθροισμα των γεγονότων του.
  const docGross = new Float64Array(docs.length);
  facts.forEach(f => { docGross[f[0]] += f[6]; });
  const years = [...new Set(docs.map(d => d.date.slice(0, 4)).filter(Boolean))].sort().reverse();

  // ---------------------------------------------------------------- κατάσταση (και στο URL #…)
  const state = {rows: 'month', cols: 'kind', m: 'net', from: '', to: '', slices: {}, sort: null, all: false, heat: true};
  (function load() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (!p.has('rows')) {  // πρώτη επίσκεψη: τρέχον έτος (ή το πιο πρόσφατο με δεδομένα)
      const y = String(new Date().getFullYear()), yy = years.includes(y) ? y : years[0];
      if (yy) Object.assign(state, {from: yy + '-01-01', to: yy + '-12-31'});
      return;
    }
    if (p.get('rows') in DIMS) state.rows = p.get('rows');
    const c = p.get('cols');
    if (c === '' || c === 'measures' || (c in DIMS && c !== state.rows)) state.cols = c;
    if (p.get('m') in MEASURES) state.m = p.get('m');
    state.from = p.get('from') || '';
    state.to = p.get('to') || '';
    for (const [k, v] of p) if (k.startsWith('s.') && k.slice(2) in DIMS) state.slices[k.slice(2)] = v;
  })();
  function save() {
    const p = new URLSearchParams({rows: state.rows, cols: state.cols, m: state.m, from: state.from, to: state.to});
    for (const [k, v] of Object.entries(state.slices)) p.set('s.' + k, v);
    history.replaceState(null, '', '#' + p);
  }

  // ---------------------------------------------------------------- υπολογισμός
  function filtered(slices, pred) {
    const sl = Object.entries(slices);
    return facts.filter(f => {
      const d = docs[f[0]];
      if ((state.from && d.date < state.from) || (state.to && d.date > state.to)) return false;
      return sl.every(([dim, k]) => DIMS[dim].key(f, d) === k) && (!pred || pred(f, d));
    });
  }
  const cell = () => ({v: 0, docs: new Set()});
  const put = (map, k) => map.get(k) || map.set(k, cell()).get(k);
  const bump = (c, v, i) => { c.v += v; c.docs.add(i); };

  function sortKeys(keys, dim, weight) {
    if (dim === 'kind') return keys.sort().reverse();  // Έσοδα (in), μετά Έξοδα (ex)
    if (!dim || DIMS[dim].time) {  // χρονική σειρά· το «χωρίς τιμή» στο τέλος
      return keys.sort((a, b) => (a === '') - (b === '') || (a < b ? -1 : a > b ? 1 : 0));
    }
    return keys.sort((a, b) => weight(b) - weight(a));
  }

  let cube;
  function compute() {
    const R = DIMS[state.rows], mm = state.cols === 'measures', m = MEASURES[state.m];
    const C = mm ? null : DIMS[state.cols] || null;
    const count = !mm && m.count;
    const kindAxis = state.rows === 'kind' ? 'rows' : state.cols === 'kind' ? 'cols' : null;
    // Έσοδα και έξοδα μαζί (χωρίς άξονα ή φίλτρο «Είδος»): τα έξοδα αφαιρούνται, αλλιώς το
    // άθροισμα δεν έχει νόημα. Τα σύνολα κατά μήκος του «Είδους» = Έσοδα − Έξοδα.
    const signed = !count && (m.signed && !mm || (!kindAxis && !state.slices.kind));
    const rows = new Map(), cols = new Map(), grand = cell();
    for (const f of filtered(state.slices)) {
      const d = docs[f[0]], s = d.k === 'ex' ? -1 : 1, rk = R.key(f, d);
      let row = rows.get(rk);
      if (!row) rows.set(rk, row = {k: rk, cells: new Map(), total: cell()});
      const parts = mm ? MEASURE_COLS.map(id => [id, mv(f, MEASURES[id])]) : [[C ? C.key(f, d) : '', count ? 0 : mv(f, m)]];
      for (const [ck, v] of parts) {
        const cv = signed ? v * s : v, tv = !count && kindAxis ? v * s : cv;
        bump(put(row.cells, ck), cv, f[0]);
        bump(row.total, kindAxis === 'cols' ? tv : cv, f[0]);
        bump(put(cols, ck), kindAxis === 'rows' ? tv : cv, f[0]);
        bump(grand, tv, f[0]);
      }
    }
    const val = c => c ? (count ? c.docs.size : c.v) : 0;
    let colKeys = mm ? MEASURE_COLS.filter(k => !HIDE_ZERO.includes(k) || [...rows.values()].some(r => Math.abs(r.cells.get(k).v) >= 0.005))
      : sortKeys([...cols.keys()], C && state.cols, k => Math.abs(val(cols.get(k))));
    let rest = null;
    if (C && !C.time && colKeys.length > COL_LIMIT) {  // πολλές στήλες: οι μεγαλύτερες + «Λοιπά»
      rest = new Set(colKeys.splice(COL_LIMIT - 1));
      const merge = map => {
        const c = cell();
        rest.forEach(k => { const x = map.get(k); if (x) { c.v += x.v; x.docs.forEach(i => c.docs.add(i)); } });
        map.set(REST, c);
      };
      rows.forEach(r => merge(r.cells));
      merge(cols);
      colKeys.push(REST);
    }
    let rowList = [...rows.values()];
    const sort = state.sort;
    if (sort) {
      const key = sort.by === '@label' ? r => dimName(state.rows, r.k) : sort.by === '@total' ? r => val(r.total) : r => val(r.cells.get(sort.by));
      rowList.sort((a, b) => {
        const x = key(a), y = key(b);
        return sort.dir * (typeof x === 'string' ? x.localeCompare(y, 'el') : x - y);
      });
    } else {
      const order = sortKeys(rowList.map(r => r.k), state.rows, k => Math.abs(val(rows.get(k).total)));
      rowList = order.map(k => rows.get(k));
    }
    cube = {R, C, mm, m, count, kindAxis, signed, rows: rowList, cols, colKeys, rest, grand, val};
    return cube;
  }

  // ---------------------------------------------------------------- μορφοποίηση
  const fmt = v => cube.count ? nf0.format(v) : nf2.format(Math.abs(v) < 0.005 ? 0 : v);
  const numCls = v => Math.abs(v) < 0.005 ? 'yz' : v < 0 ? 'yneg' : '';
  const colLabel = k => k === REST ? 'Λοιπά' : cube.mm ? MEASURES[k].label : cube.C ? dimName(state.cols, k) : MEASURES[state.m].label;
  const kindDot = (dim, k) => dim === 'kind' ? `<i class="yr-dot yr-dot--${k === 'in' ? 'in' : 'out'}"></i>` : '';
  const totalLabel = () => cube.kindAxis === 'cols' && !cube.count ? 'Εσ. − Εξ.' : 'Σύνολο';
  const showTotalCol = () => cube.C && !cube.mm;
  const measureTitle = () => cube.mm ? 'Ποσά, φόροι & κρατήσεις' :
    MEASURES[state.m].label + (cube.signed && !MEASURES[state.m].signed ? ' (Εσ. − Εξ.)' : '');

  // ---------------------------------------------------------------- controls
  const selRows = $('[data-dim="rows"]'), selCols = $('[data-dim="cols"]'), selM = $('[data-measure]');
  const inFrom = $('[data-from]'), inTo = $('[data-to]');
  const opts = (list, cur) => list.map(([v, l, dis]) => `<option value="${v}"${v === cur ? ' selected' : ''}${dis ? ' disabled' : ''}>${esc(l)}</option>`).join('');

  function renderControls() {
    const dims = Object.entries(DIMS);
    selRows.innerHTML = opts(dims.map(([k, d]) => [k, d.label, k === state.cols]), state.rows);
    selCols.innerHTML = opts([['', '— Χωρίς (μόνο σύνολο)'], ['measures', 'Μέτρα: ποσά, φόροι & κρατήσεις'],
      ...dims.map(([k, d]) => [k, d.label, k === state.rows])], state.cols);
    selM.innerHTML = opts(Object.entries(MEASURES).map(([k, m]) => [k, m.label]), state.m);
    selM.disabled = state.cols === 'measures';
    setDate(inFrom, state.from);
    setDate(inTo, state.to);

    $('[data-years]').innerHTML = [['', 'Όλα'], ...years.map(y => [y, y])].map(([y, l]) => {
      const on = y ? state.from === y + '-01-01' && state.to === y + '-12-31' : !state.from && !state.to;
      return `<a href="#" data-year="${y}"${on ? ' aria-current="page"' : ''}>${l}</a>`;
    }).join('');

    $('[data-presets]').innerHTML = PRESETS.map((p, i) => {
      const on = p.rows === state.rows && p.cols === state.cols && (p.cols === 'measures' || p.m === state.m) &&
        (p.kind || '') === (state.slices.kind || '') && Object.keys(state.slices).every(k => k === 'kind');
      return `<button type="button" class="ol-preset" data-preset="${i}" aria-pressed="${on}">${esc(p.label)}</button>`;
    }).join('');

    const kind = state.slices.kind || '';
    const chips = Object.entries(state.slices).filter(([dim]) => dim !== 'kind').map(([dim, k]) =>
      `<span class="ol-chip"><small>${esc(DIMS[dim].label)}</small>${esc(dimName(dim, k))}` +
      `<button type="button" data-unslice="${dim}" aria-label="Αφαίρεση φίλτρου ${esc(DIMS[dim].label)}">✕</button></span>`);
    $('[data-slices]').innerHTML =
      `<div class="ol-seg" role="group" aria-label="Είδος">` +
      [['', 'Όλα'], ['in', 'Έσοδα'], ['ex', 'Έξοδα']].map(([k, l]) =>
        `<button type="button" data-kind="${k}" aria-pressed="${k === kind}">${k ? kindDot('kind', k) : ''}${l}</button>`).join('') +
      `</div>` +
      (chips.length ? `<span class="ol-slices-label">Φίλτρα</span>${chips.join('')}<button type="button" class="ol-clear" data-clear>Καθαρισμός</button>`
        : `<span class="ol-hint">Κλικ σε γραμμή του πίνακα ή του διαγράμματος για εμβάθυνση.</span>`);
  }
  function setDate(input, v) {
    if (input._flatpickr) input._flatpickr.setDate(v || null, false);
    else input.value = v;
  }

  // ---------------------------------------------------------------- KPIs (ανεξάρτητα από άξονες και «Είδος»)
  function renderKpis() {
    const sl = Object.assign({}, state.slices);
    delete sl.kind;
    const t = {in: {net: 0, vat: 0, nd: 0, gross: 0, tax: 0, n: new Set()}, ex: {net: 0, vat: 0, nd: 0, gross: 0, tax: 0, n: new Set()}};
    for (const f of filtered(sl)) {
      const x = t[docs[f[0]].k];
      x.net += f[4]; x.vat += f[5]; x.nd += f[12]; x.gross += f[6]; x.tax += f[7] + f[8] + f[9] + f[10] + f[11]; x.n.add(f[0]);
    }
    const res = t.in.net - t.ex.net - t.ex.nd, vatIn = t.ex.vat - t.ex.nd, vat = t.in.vat - vatIn;
    const margin = t.in.net ? `περιθώριο ${nf0.format(res / t.in.net * 100)}% επί των εσόδων` : 'καθαρές αξίες';
    const resSub = Math.abs(t.ex.nd) >= 0.005 ? margin + ` · με μη εκπ. ΦΠΑ εξόδων ${nf2.format(t.ex.nd)}` : margin;
    const tile = (cls, kicker, v, sub, neg) =>
      `<div class="kpi ol-kpi ${cls}${neg ? ' is-neg' : ''}"><div class="kicker">${kicker}</div>` +
      `<div class="kpi-v">${nf2.format(v)} €</div><div class="kpi-sub">${sub}</div></div>`;
    $('[data-kpis]').innerHTML =
      tile('ol-kpi--in', `${kindDot('kind', 'in')}Έσοδα · καθαρά <span>${t.in.n.size} παρ.</span>`, t.in.net,
        `με ΦΠΑ ${nf2.format(t.in.gross)} €`) +
      tile('ol-kpi--out', `${kindDot('kind', 'ex')}Έξοδα · καθαρά <span>${t.ex.n.size} παρ.</span>`, t.ex.net,
        `με ΦΠΑ ${nf2.format(t.ex.gross)} €`) +
      tile('ol-kpi--res', 'Αποτέλεσμα <span>Εσ. − Εξ.</span>', res, resSub, res < 0) +
      tile('ol-kpi--vat', 'Διαφορά ΦΠΑ <span>' + (vat < 0 ? 'πιστωτική' : 'χρεωστική') + '</span>', vat,
        `εκρ. ${nf2.format(t.in.vat)} − εκπ. εισρ. ${nf2.format(vatIn)}` +
        (Math.abs(t.ex.nd) >= 0.005 ? ` · μη εκπ. ${nf2.format(t.ex.nd)}` : '')) +
      tile('ol-kpi--tax', 'Φόροι & κρατήσεις', t.in.tax + t.ex.tax,
        `εσόδων ${nf2.format(t.in.tax)} · εξόδων ${nf2.format(t.ex.tax)}`);
  }

  // ---------------------------------------------------------------- πίνακας
  function renderTable() {
    const {rows, colKeys, cols, grand, val} = cube;
    const shown = state.all ? rows : rows.slice(0, ROW_LIMIT);
    const sortMark = by => state.sort && state.sort.by === by ? (state.sort.dir > 0 ? ' ▲' : ' ▼') : '';
    const th = (by, html, cls) => `<th class="${cls || 'num'}" aria-sort="${state.sort && state.sort.by === by ? (state.sort.dir > 0 ? 'ascending' : 'descending') : 'none'}">` +
      `<button type="button" class="ol-sort" data-sort="${esc(by)}">${html}${sortMark(by)}</button></th>`;
    const bodyVals = shown.flatMap(r => colKeys.map(k => Math.abs(val(r.cells.get(k)))));
    const maxCell = Math.max(...bodyVals, 0) || 1;
    const maxTotal = Math.max(...shown.map(r => Math.abs(val(r.total))), 0) || 1;

    let html = '<thead><tr>' + th('@label', esc(cube.R.label), 'yr-sticky') +
      colKeys.map((k, j) => th(k, kindDot(state.cols, k) + esc(colLabel(k)))).join('') +
      (showTotalCol() ? th('@total', totalLabel(), 'num ol-total-h') : '') + '</tr></thead><tbody>';
    shown.forEach((r, i) => {
      html += `<tr><th class="yr-sticky ol-rh" scope="row"><button type="button" data-drill="${i}" title="Εμβάθυνση">` +
        `${kindDot(state.rows, r.k)}${esc(dimName(state.rows, r.k))}</button></th>`;
      colKeys.forEach((k, j) => {
        const c = r.cells.get(k);
        if (!c) { html += '<td class="num ol-c"><span class="yz">—</span></td>'; return; }
        const v = val(c);
        html += `<td class="num ol-c" style="--h:${(Math.abs(v) / maxCell).toFixed(3)}"><button type="button" data-cell="${i}:${j}" ` +
          `title="Τα παραστατικά του κελιού"><span class="${numCls(v)}">${fmt(v)}</span></button></td>`;
      });
      if (showTotalCol()) {
        const v = val(r.total);
        html += `<td class="num ol-t${v < 0 ? ' is-neg' : ''}" style="--b:${(Math.abs(v) / maxTotal * 100).toFixed(1)}%">` +
          `<button type="button" data-cell="${i}:t"><span class="${numCls(v)}">${fmt(v)}</span></button></td>`;
      }
      html += '</tr>';
    });
    if (!rows.length) {
      html += `<tr><td class="ol-empty" colspan="${colKeys.length + 2}">Δεν υπάρχουν παραστατικά για τα τρέχοντα φίλτρα.</td></tr>`;
    }
    html += '</tbody>';
    if (rows.length > 1 || cube.C) {
      html += `<tfoot><tr class="ol-foot"><th class="yr-sticky" scope="row">${cube.kindAxis === 'rows' && !cube.count ? 'Εσ. − Εξ.' : 'Σύνολο'}</th>` +
        colKeys.map((k, j) => { const v = val(cols.get(k)); return `<td class="num"><button type="button" data-cell="f:${j}"><span class="${numCls(v)}">${fmt(v)}</span></button></td>`; }).join('') +
        (showTotalCol() ? `<td class="num ol-grand"><button type="button" data-cell="f:t"><span class="${numCls(val(grand))}">${fmt(val(grand))}</span></button></td>` : '') +
        '</tr></tfoot>';
    }
    const table = $('[data-table]');
    table.innerHTML = html;
    table.classList.toggle('ol-heat', state.heat);
    table.classList.toggle('ol-has-total', !!showTotalCol());
    $('[data-table-title]').innerHTML = `${esc(measureTitle())} <span class="pill">${rows.length} ${rows.length === 1 ? 'γραμμή' : 'γραμμές'}</span>`;
    $('[data-more]').innerHTML = rows.length > ROW_LIMIT
      ? `<button type="button" class="btn-ghost btn-sm" data-all>${state.all ? 'Λιγότερες γραμμές' : `Εμφάνιση όλων (+${rows.length - ROW_LIMIT})`}</button>` : '';
  }

  // ---------------------------------------------------------------- διάγραμμα
  function niceStep(span, ticks) {
    const raw = Math.max(span, 1) / ticks, mag = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 2.5, 5, 10].map(x => x * mag).find(x => x >= raw);
  }
  function chartSeries() {
    const {val} = cube;
    if (state.cols === 'kind' && !cube.mm) {
      return {series: [['in', 'Έσοδα', 'in'], ['ex', 'Έξοδα', 'out']], value: (r, k) => val(r.cells.get(k))};
    }
    if (cube.mm) {
      return {series: [['tax', 'Φόροι & κρατήσεις', 'acc']], value: r => TAX_COLS.reduce((s, k) => s + val(r.cells.get(k)), 0)};
    }
    return {series: [['v', measureTitle(), 'acc']], value: r => cube.C ? val(r.total) : val(r.cells.get(''))};
  }

  function renderChart() {
    const box = $('[data-chart]'), {series, value} = chartSeries();
    const vertical = !!cube.R.time;
    const rows = cube.rows.slice(0, vertical ? 36 : (state.all ? 40 : 15));
    $('[data-chart-title]').textContent = series.length > 1 ? `${measureTitle()} ανά ${cube.R.acc}`
      : `${series[0][1]} ανά ${cube.R.acc}`;
    $('[data-legend]').innerHTML = series.length > 1
      ? series.map(([, l, c]) => `<li><i class="yr-dot yr-dot--${c}"></i>${esc(l)}</li>`).join('') : '';
    if (!rows.length) { box.innerHTML = '<p class="ol-empty">Χωρίς δεδομένα.</p>'; return; }

    const vals = rows.flatMap(r => series.map(([k]) => value(r, k)));
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const step = niceStep(hi - lo, 5);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step || step;
    const pct = v => (v - lo) / (hi - lo) * 100, zero = pct(0);
    const bar = (v, c) => {
      const a = pct(Math.min(v, 0)), b = pct(Math.max(v, 0));
      return `<i class="ol-bar ol-bar--${c}${v < 0 ? ' is-neg' : ''}" style="--a:${a.toFixed(2)}%;--s:${(b - a).toFixed(2)}%"></i>`;
    };
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(v);
    const tickHtml = ticks.map(v => `<i class="${Math.abs(v) < step / 2 ? 'is-zero' : ''}" style="--p:${pct(v).toFixed(2)}%"><span>${cube.count ? nf0.format(v) : nf0.format(v)}</span></i>`).join('');

    if (vertical) {
      const every = Math.ceil(rows.length / Math.max(4, Math.floor(box.clientWidth / 64)));  // ετικέτα ανά ~64px
      box.className = 'ol-chart ol-chart--v';
      box.style.setProperty('--n', rows.length);
      box.style.setProperty('--k', series.length);
      box.innerHTML = `<div class="ol-grid">${tickHtml}</div><div class="ol-cols">` + rows.map((r, i) =>
        `<button type="button" class="ol-col" data-drill="${i}" data-tip="${i}" aria-label="${esc(dimName(state.rows, r.k))}">` +
        `<span class="ol-plot">${series.map(([k, , c]) => `<span class="ol-slot">${bar(value(r, k), c)}</span>`).join('')}</span>` +
        `<span class="ol-lab">${i % every ? '' : esc(shortTime(r.k))}</span></button>`).join('') + '</div>';
    } else {
      box.className = 'ol-chart ol-chart--h';
      box.style.setProperty('--k', series.length);
      box.style.setProperty('--z', zero.toFixed(2) + '%');
      box.innerHTML = `<div class="ol-hgrid"><span></span><div class="ol-grid">${tickHtml}</div><span></span></div>` + rows.map((r, i) =>
        `<button type="button" class="ol-hrow" data-drill="${i}" data-tip="${i}">` +
        `<span class="ol-hlab">${kindDot(state.rows, r.k)}${esc(dimName(state.rows, r.k))}</span>` +
        `<span class="ol-plot">${series.map(([k, , c]) => `<span class="ol-slot">${bar(value(r, k), c)}</span>`).join('')}</span>` +
        `<span class="ol-hval">${series.length > 1 ? '' : `<span class="${numCls(value(r, series[0][0]))}">${fmt(value(r, series[0][0]))}</span>`}</span>` +
        `</button>`).join('');
    }
    box.dataset.series = JSON.stringify(series);
  }
  function shortTime(k) {
    if (!k) return '—';
    if (state.rows === 'month') return MONTHS[k.slice(5) - 1] + (multiYear() ? ' ’' + k.slice(2, 4) : '');
    if (state.rows === 'day') return k.slice(8) + '/' + k.slice(5, 7);
    if (state.rows === 'quarter') return 'ΑΒΓΔ'[k.slice(-1) - 1] + '΄' + (multiYear() ? ' ’' + k.slice(2, 4) : '');
    return k;
  }
  const multiYear = () => new Set(cube.rows.map(r => r.k.slice(0, 4))).size > 1;

  // Tooltip διαγράμματος
  const card = $('.ol-chart-card'), tip = $('.ol-tip');
  function showTip(el) {
    const r = cube.rows[+el.dataset.tip], {series, value} = chartSeries();
    tip.innerHTML = `<b>${esc(dimName(state.rows, r.k))}</b>` + series.map(([k, l, c]) => {
      const v = value(r, k);
      return `<span>${series.length > 1 ? `<i class="yr-dot yr-dot--${c}"></i>` : ''}${esc(l)}<em class="${numCls(v)}">${fmt(v)}${cube.count ? '' : ' €'}</em></span>`;
    }).join('') + (series.length > 1 && !cube.count
      ? (v => `<span class="yr-tip-bal${v < 0 ? ' is-neg' : ''}">Εσ. − Εξ.<em>${fmt(v)} €</em></span>`)(value(r, 'in') - value(r, 'ex')) : '') +
      '<span class="yr-tip-sum">κλικ για εμβάθυνση</span>';
    tip.hidden = false;
    const c = card.getBoundingClientRect(), e = el.getBoundingClientRect();
    const x = e.left - c.left + e.width / 2;
    tip.style.left = Math.min(Math.max(x, tip.offsetWidth / 2 + 8), c.width - tip.offsetWidth / 2 - 8) + 'px';
    const below = e.bottom - c.top + 8, above = e.top - c.top - tip.offsetHeight - 8;
    tip.style.top = (cube.R.time ? Math.max(8, e.top - c.top) : (above > 0 ? above : below)) + 'px';
    card.querySelectorAll('[data-tip]').forEach(x => x.classList.toggle('is-active', x === el));
  }
  function hideTip() { tip.hidden = true; card.querySelectorAll('[data-tip].is-active').forEach(x => x.classList.remove('is-active')); }
  card.addEventListener('mouseover', e => { const el = e.target.closest('[data-tip]'); if (el) showTip(el); });
  card.addEventListener('focusin', e => { const el = e.target.closest('[data-tip]'); if (el) showTip(el); });
  card.addEventListener('mouseleave', hideTip);
  card.addEventListener('focusout', e => { if (!card.contains(e.relatedTarget)) hideTip(); });

  // ---------------------------------------------------------------- drill-down & modal παραστατικών
  function drill(i) {
    const r = cube.rows[i];
    const finest = Math.max(-1, ...Object.keys(state.slices).concat(state.rows).map(d => TIME.indexOf(d)));
    const usable = d => d && d !== state.rows && d !== state.cols && !(d in state.slices) && !(TIME.includes(d) && TIME.indexOf(d) <= finest);
    const next = [cube.R.next, ...DRILL_FALLBACK].find(usable);
    if (!next) return openDocs(i, 't');
    state.slices[state.rows] = r.k;
    state.rows = next;
    state.sort = null;
    state.all = false;
    render();
  }

  const dlg = document.getElementById('ol-docs');
  function openDocs(ri, cj) {
    const slices = Object.assign({}, state.slices), parts = [];
    if (ri !== 'f') {
      const r = cube.rows[ri];
      slices[state.rows] = r.k;
      parts.push(dimName(state.rows, r.k));
    }
    let pred = null, mid = cube.mm ? null : state.m;
    if (cj !== 't') {
      const ck = cube.colKeys[cj];
      if (cube.mm) mid = ck;
      else if (ck === REST) { const C = cube.C; pred = (f, d) => cube.rest.has(C.key(f, d)); }
      else if (cube.C) slices[state.cols] = ck;
      parts.push(colLabel(ck));
    }
    const m = MEASURES[mid] || MEASURES.gross, byDoc = new Map();
    for (const f of filtered(slices, pred)) byDoc.set(f[0], (byDoc.get(f[0]) || 0) + (m.count ? 0 : mv(f, m)));
    const list = [...byDoc].sort((a, b) => docs[a[0]].date.localeCompare(docs[b[0]].date));
    const showPart = !m.count && mid !== 'gross';
    const url = root.dataset.docUrl, back = encodeURIComponent(location.pathname + location.hash);
    const sum = list.reduce((s, [i, v]) => s + (docs[i].k === 'ex' && cube.signed ? -v : v), 0);
    dlg.innerHTML = `<div class="modal-head"><h2>${esc(parts.join(' · ') || 'Σύνολο')}</h2>` +
      `<button type="button" class="btn-ghost btn-sm" data-close aria-label="Κλείσιμο">✕</button></div>` +
      `<p class="muted small">${list.length} ${list.length === 1 ? 'παραστατικό' : 'παραστατικά'}${showPart ? ` · ${esc(m.label)} ${nf2.format(sum)} €` : ''}` +
      `${Object.keys(state.slices).length ? ' · με τα ενεργά φίλτρα' : ''}. Τα πιστωτικά αφαιρούνται.</p>` +
      (list.length ? `<div class="yr-scroll"><table class="yr-table yr-docs ol-docs"><thead><tr><th>Ημ/νία</th><th>Είδος</th><th>MARK</th>` +
        `<th>Συναλλασσόμενος</th><th>Τύπος · Σειρά/Α/Α</th>${showPart ? `<th class="num">${esc(m.label)}<br><small>στο κελί</small></th>` : ''}` +
        `<th class="num">Σύνολο<br>παραστατικού</th></tr></thead><tbody>` +
        list.slice(0, 500).map(([i, v]) => {
          const d = docs[i];
          return `<tr><td>${d.date.split('-').reverse().join('/')}</td><td>${kindDot('kind', d.k)}${KIND[d.k]}</td>` +
            `<td class="yr-docs-mark"><a href="${esc(url.replace('__MARK__', encodeURIComponent(d.mark)))}?back=${back}">${esc(d.mark)}</a></td>` +
            `<td class="yr-docs-cp"><b>${esc(d.cpn || '—')}</b><span class="vat">${esc(d.cp)}</span></td>` +
            `<td class="yr-docs-type"><span title="${esc(labels.type[d.type] || '')}">${esc(d.type)}</span> · ${esc(d.series)}/${esc(d.aa)}</td>` +
            (showPart ? `<td class="num"><span class="${numCls(v)}">${nf2.format(v)}</span></td>` : '') +
            `<td class="num"><span class="${numCls(docGross[i])}">${nf2.format(docGross[i])}</span></td></tr>`;
        }).join('') + '</tbody></table></div>' +
        (list.length > 500 ? '<p class="muted small">Εμφανίζονται τα πρώτα 500.</p>' : '') : '');
    dlg.showModal();
  }
  dlg.addEventListener('click', e => { if (e.target.closest('[data-close]')) dlg.close(); });
  dlg.addEventListener('close', () => { dlg.innerHTML = ''; });

  // ---------------------------------------------------------------- CSV (όλες οι γραμμές, για Excel ελληνικό)
  function csv() {
    const {rows, colKeys, cols, grand, val} = cube;
    const n = v => cube.count ? String(v) : v.toFixed(2).replace('.', ',');
    const q = s => '"' + String(s).replace(/"/g, '""') + '"';
    const head = [cube.R.label, ...colKeys.map(colLabel), ...(showTotalCol() ? [totalLabel()] : [])];
    const lines = [head.map(q).join(';')];
    rows.forEach(r => lines.push([q(dimName(state.rows, r.k)), ...colKeys.map(k => n(val(r.cells.get(k)))),
      ...(showTotalCol() ? [n(val(r.total))] : [])].join(';')));
    lines.push([q('Σύνολο'), ...colKeys.map(k => n(val(cols.get(k)))), ...(showTotalCol() ? [n(val(grand))] : [])].join(';'));
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8'})),
      download: `olap-${state.rows}${state.cols ? '-' + state.cols : ''}.csv`,
    });
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // ---------------------------------------------------------------- events
  function render() {
    compute();
    renderControls();
    renderKpis();
    renderTable();
    renderChart();
    hideTip();
    save();
  }

  selRows.addEventListener('change', () => {
    state.rows = selRows.value;
    delete state.slices[state.rows];  // γραμμές σε διάσταση-φίλτρο: το φίλτρο φεύγει
    state.sort = null;
    render();
  });
  selCols.addEventListener('change', () => {
    state.cols = selCols.value;
    if (state.cols in DIMS) delete state.slices[state.cols];
    if (state.sort && state.sort.by[0] !== '@') state.sort = null;
    render();
  });
  selM.addEventListener('change', () => { state.m = selM.value; render(); });
  const onDate = (input, key) => input.addEventListener('change', () => {
    const v = input.value;
    if (v && !/^\d{4}-\d{2}-\d{2}$/.test(v)) return;
    state[key] = v;
    render();
  });
  onDate(inFrom, 'from');
  onDate(inTo, 'to');

  root.addEventListener('click', e => {
    const t = e.target.closest('button, a[data-year]');
    if (!t || dlg.contains(t)) return;
    const d = t.dataset;
    if ('year' in d) {
      e.preventDefault();
      Object.assign(state, d.year ? {from: d.year + '-01-01', to: d.year + '-12-31'} : {from: '', to: ''});
    } else if ('preset' in d) {
      const p = PRESETS[+d.preset];
      Object.assign(state, {rows: p.rows, cols: p.cols, m: p.m, slices: p.kind ? {kind: p.kind} : {}, sort: null, all: false});
    } else if ('swap' in d) {
      if (!(state.cols in DIMS)) return;
      [state.rows, state.cols] = [state.cols, state.rows];
      state.sort = null;
    } else if ('kind' in d) {
      if (d.kind) state.slices.kind = d.kind; else delete state.slices.kind;
    } else if ('unslice' in d) {
      delete state.slices[d.unslice];
    } else if ('clear' in d) {
      state.slices = state.slices.kind ? {kind: state.slices.kind} : {};
    } else if ('drill' in d) {
      return drill(+d.drill);
    } else if ('cell' in d) {
      const [ri, cj] = d.cell.split(':');
      return openDocs(ri === 'f' ? 'f' : +ri, cj === 't' ? 't' : +cj);
    } else if ('sort' in d) {
      const by = d.sort, cur = state.sort;
      state.sort = {by, dir: cur && cur.by === by ? -cur.dir : by === '@label' ? 1 : -1};
    } else if ('all' in d) {
      state.all = !state.all;
    } else if ('heat' in d) {
      state.heat = !state.heat;
      t.setAttribute('aria-pressed', state.heat);
    } else if ('csv' in d) {
      return csv();
    } else return;
    render();
  });

  render();
})();
