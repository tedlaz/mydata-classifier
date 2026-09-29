// OLAP: κύβος εσόδων-εξόδων. Όλο το pivot γίνεται εδώ από τα γεγονότα του #olap-data
// (app._olap_cube): slice/φίλτρα (πολλαπλή επιλογή), dice (γραμμές × στήλες), υποανάλυση
// (κατηγορία → υποκατηγορία), drill-down, παραστατικά ανά κελί, διαγράμματα πέντε τύπων.
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
  const nf1 = new Intl.NumberFormat('el-GR', {maximumFractionDigits: 1});
  const MONTHS = ['Ιαν.', 'Φεβ.', 'Μαρ.', 'Απρ.', 'Μαΐ.', 'Ιουν.', 'Ιουλ.', 'Αυγ.', 'Σεπ.', 'Οκτ.', 'Νοέ.', 'Δεκ.'];
  const WDAYS = ['Δευτέρα', 'Τρίτη', 'Τετάρτη', 'Πέμπτη', 'Παρασκευή', 'Σάββατο', 'Κυριακή'];
  const KIND = {in: 'Έσοδα', ex: 'Έξοδα'};
  const REST = '\u0000rest';  // στήλη / κομμάτι «Λοιπά»
  const SEP = '⁣';  // κελί «στήλη › υποστήλη»: σύνθετο κλειδί «στήλη SEP υποστήλη»
  const ROW_LIMIT = 20, COL_LIMIT = 8, SUB_LIMIT = 25, CSUB_LIMIT = 6, SLOTS = 7;  // 7 χρώματα + «Λοιπά»
  const motion = !matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Κατηγορίες τύπων παραστατικών myDATA (το πρώτο μέρος του κωδικού, π.χ. 2.1 → 2).
  const TYPE_GROUPS = {
    1: 'Τιμολόγια πώλησης', 2: 'Τιμολόγια παροχής υπηρεσιών', 3: 'Τίτλοι κτήσης', 5: 'Πιστωτικά τιμολόγια',
    6: 'Αυτοπαράδοση / ιδιοχρησιμοποίηση', 7: 'Συμβόλαια – έσοδο', 8: 'Ειδικά στοιχεία (ενοίκια κ.λπ.)',
    9: 'Παραστατικά διακίνησης', 11: 'Λιανικές πωλήσεις', 13: 'Έξοδα – αγορές λιανικής',
    14: 'Παραστατικά οντοτήτων (από λήπτη)', 15: 'Συμβόλαια – έξοδο', 16: 'Ειδικά στοιχεία εξόδων',
    17: 'Εγγραφές τακτοποίησης',
  };
  // Κλίμακα ποσού παραστατικού (συνολική αξία, απόλυτη τιμή).
  const SIZES = [['a', 50, 'έως 50 €'], ['b', 200, '50 – 200 €'], ['c', 1000, '200 – 1.000 €'],
    ['d', 5000, '1.000 – 5.000 €'], ['e', Infinity, 'πάνω από 5.000 €']];

  // Συναλλασσόμενος: κλειδί = ΑΦΜ (ή «~επωνυμία» όταν λείπει το ΑΦΜ).
  const cpKey = d => d.cp || (d.cpn ? '~' + d.cpn : '');
  const cpName = {};
  docs.forEach(d => { const k = cpKey(d); if (k && d.cpn && !cpName[k]) cpName[k] = d.cpn; });
  const withDesc = (k, desc) => desc ? k + ' · ' + desc : k;

  // Σύνολο παραστατικού (για το modal και την κλίμακα ποσού): άθροισμα των γεγονότων του.
  const docGross = new Float64Array(docs.length);
  facts.forEach(f => { docGross[f[0]] += f[6]; });
  const docSize = docs.map((d, i) => SIZES.find(s => Math.abs(docGross[i]) < s[1])[0]);
  const docWday = docs.map(d => {
    if (!d.date) return '';
    const [y, m, dd] = d.date.split('-').map(Number);
    return String((new Date(y, m - 1, dd).getDay() + 6) % 7 + 1);  // Δευτέρα = 1
  });
  const e3group = t => t ? t.split('_').slice(0, 2).join('_') : '';

  // Διαστάσεις: key(γεγονός, παραστατικό) → κλειδί μέλους· next = προεπιλογή για drill-down·
  // time/ord = σταθερή σειρά (όχι κατά μέγεθος)· sub = προτεινόμενη υποανάλυση.
  const DIMS = {
    kind: {label: 'Είδος', acc: 'είδος', key: (f, d) => d.k, name: k => KIND[k], next: 'e3c', sub: 'e3c'},
    year: {label: 'Έτος', acc: 'έτος', time: true, next: 'quarter', key: (f, d) => d.date.slice(0, 4), name: k => k},
    quarter: {label: 'Τρίμηνο', acc: 'τρίμηνο', time: true, next: 'month', sub: 'month',
      key: (f, d) => d.date && d.date.slice(0, 4) + '-Q' + Math.ceil(d.date.slice(5, 7) / 3),
      name: k => 'ΑΒΓΔ'[k.slice(-1) - 1] + '΄ τρίμ. ' + k.slice(0, 4)},
    month: {label: 'Μήνας', acc: 'μήνα', time: true, next: 'day',
      key: (f, d) => d.date.slice(0, 7), name: k => MONTHS[k.slice(5) - 1] + ' ' + k.slice(0, 4)},
    day: {label: 'Ημέρα', acc: 'ημέρα', time: true, key: (f, d) => d.date, name: k => k.split('-').reverse().join('/')},
    wday: {label: 'Ημέρα εβδομάδας', acc: 'ημέρα εβδομάδας', ord: true, next: 'cp', key: f => docWday[f[0]], name: k => WDAYS[k - 1]},
    cp: {label: 'Συναλλασσόμενος', acc: 'συναλλασσόμενο', next: 'e3t', sub: 'e3c', none: 'Χωρίς ΑΦΜ (λιανική)', key: (f, d) => cpKey(d),
      name: k => k[0] === '~' ? k.slice(1) : cpName[k] ? cpName[k] + ' · ' + k : 'ΑΦΜ ' + k},
    tgrp: {label: 'Κατηγορία παραστατικού', acc: 'κατηγορία παραστατικού', next: 'type', sub: 'type', none: 'Χωρίς τύπο',
      key: (f, d) => d.type.split('.')[0], name: k => withDesc(k + '.x', TYPE_GROUPS[k])},
    type: {label: 'Τύπος παραστατικού', acc: 'τύπο παραστατικού', next: 'cp', key: (f, d) => d.type, name: k => withDesc(k, labels.type[k])},
    series: {label: 'Σειρά', acc: 'σειρά', next: 'month', none: 'Χωρίς σειρά', key: (f, d) => d.series, name: k => k},
    size: {label: 'Κλίμακα ποσού', acc: 'κλίμακα ποσού', ord: true, next: 'cp', key: f => docSize[f[0]],
      name: k => SIZES.find(s => s[0] === k)[2]},
    e3c: {label: 'Κατηγορία Ε3', acc: 'κατηγορία Ε3', next: 'e3g', sub: 'e3g', none: 'Χωρίς χαρακτηρισμό Ε3', key: f => f[1],
      name: k => withDesc(k.replace('category', '').replace('_', '.'), labels.e3c[k])},
    e3g: {label: 'Λογαριασμός Ε3', acc: 'λογαριασμό Ε3', next: 'e3t', sub: 'e3t', none: 'Χωρίς χαρακτηρισμό Ε3', key: f => e3group(f[2]),
      name: k => withDesc(k, (labels.e3g || {})[k])},
    e3t: {label: 'Τύπος Ε3', acc: 'τύπο Ε3', next: 'cp', none: 'Χωρίς χαρακτηρισμό Ε3', key: f => f[2], name: k => withDesc(k, labels.e3t[k])},
    vat: {label: 'Συντελεστής ΦΠΑ', acc: 'συντελεστή ΦΠΑ', next: 'e3t', sub: 'e3c', none: 'Χωρίς γραμμές', key: f => f[3],
      name: k => labels.vat[k] || 'Κατηγορία ' + k},
  };
  const TIME = ['year', 'quarter', 'month', 'day'];
  const DRILL_FALLBACK = ['month', 'cp', 'e3c', 'e3g', 'e3t', 'type', 'vat', 'series', 'day'];
  const dimName = (dim, k) => k === REST ? 'Λοιπά' : k ? DIMS[dim].name(k) : (DIMS[dim].none || 'Χωρίς τιμή');

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
  // Σύνολα στηλών «Μέτρα» (επιλογή στις Στήλες): cols = μέτρα-στήλες, hide = κρύβονται όταν είναι παντού 0,
  // series = σειρές του διαγράμματος (κλειδί, τίτλος, χρώμα) ή sum = μία σειρά-άθροισμα.
  const MEASURE_SETS = {
    measures: {label: 'Μέτρα: ποσά, φόροι & κρατήσεις', title: 'Ποσά, φόροι & κρατήσεις', cols: MEASURE_COLS, hide: HIDE_ZERO,
      sum: ['tax', 'Φόροι & κρατήσεις', TAX_COLS]},
    mvat: {label: 'Μέτρα: ποσά & ΦΠΑ', title: 'Ποσά & ΦΠΑ', cols: ['net', 'vat', 'vatded', 'vatnd', 'gross'], hide: ['vatnd'],
      series: [['net', 'Καθαρή αξία', 's1'], ['vat', 'ΦΠΑ', 's2']]},
  };
  const mv = (f, m) => f[m.i] - (m.minus ? f[m.minus] : 0) + (m.plus ? f[m.plus] : 0);

  const CHARTS = [
    ['bar', 'Μπάρες', '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'],
    ['stack', 'Στοιβαγμένες', '<path d="M5 20v-6h4v6M5 14v-4h4v4M13 20v-9h4v9M13 11V6h4v5M2 20h20"/>'],
    ['line', 'Γραμμή / περιοχή', '<path d="M3 17l5-6 4 3 5-8 4 4"/><path d="M3 21h18" opacity=".5"/>'],
    ['donut', 'Ντόνατ (μερίδια)', '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/><path d="M12 4v4.5M19.5 14.5l-4.2-1.4"/>'],
    ['tree', 'Treemap (ιεραρχία)', '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16M12 11h9M3 14h9"/>'],
  ];

  const PRESETS = [
    {label: 'Χρονική εξέλιξη', rows: 'month', cols: 'kind', m: 'net', chart: 'bar'},
    {label: 'Μήνες × είδος › κατηγορία Ε3', rows: 'month', cols: 'kind', csub: 'e3c', m: 'net', chart: 'bar'},
    {label: 'Αποτέλεσμα ανά τρίμηνο', rows: 'quarter', cols: '', m: 'res', chart: 'line'},
    {label: 'Κατηγορίες → υποκατηγορίες Ε3', rows: 'e3c', cols: '', m: 'net', sub: 'e3g', chart: 'tree'},
    {label: 'Έξοδα ανά λογαριασμό Ε3', rows: 'e3g', cols: '', m: 'net', sub: 'e3t', kind: 'ex', chart: 'donut'},
    {label: 'Πελάτες', rows: 'cp', cols: 'year', m: 'net', kind: 'in', chart: 'bar'},
    {label: 'Προμηθευτές ανά κατηγορία', rows: 'cp', cols: 'e3c', m: 'net', kind: 'ex', chart: 'stack'},
    {label: 'Μηνιαία εξέλιξη κατηγοριών', rows: 'month', cols: 'e3c', m: 'net', kind: 'ex', chart: 'stack'},
    {label: 'ΦΠΑ ανά συντελεστή', rows: 'vat', cols: 'kind', m: 'vatded', chart: 'bar'},
    {label: 'Τύποι παραστατικών', rows: 'tgrp', cols: 'kind', m: 'cnt', sub: 'type', chart: 'bar'},
    {label: 'Ημέρες εβδομάδας', rows: 'wday', cols: 'kind', m: 'cnt', chart: 'bar'},
    {label: 'Κλίμακα ποσών', rows: 'size', cols: 'kind', m: 'cnt', chart: 'stack'},
    {label: 'Φόροι & κρατήσεις', rows: 'month', cols: 'measures', m: 'net', kind: 'in', chart: 'bar'},
    {label: 'Ποσά & ΦΠΑ ανά μήνα', rows: 'month', cols: 'mvat', m: 'net', chart: 'bar'},
  ];

  const years = [...new Set(docs.map(d => d.date.slice(0, 4)).filter(Boolean))].sort().reverse();

  // ---------------------------------------------------------------- κατάσταση (και στο URL #…)
  // slices[dim] = κλειδί (ένα μέλος, π.χ. από drill-down) ή λίστα κλειδιών (φίλτρο πολλαπλής επιλογής).
  const state = {rows: 'month', cols: 'kind', m: 'net', sub: '', csub: '', copen: new Set(), chart: 'bar', from: '', to: '', slices: {},
    sort: null, all: false, heat: true, open: new Set()};
  (function load() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (!p.has('rows')) {  // πρώτη επίσκεψη: τρέχον έτος (ή το πιο πρόσφατο με δεδομένα)
      const y = String(new Date().getFullYear()), yy = years.includes(y) ? y : years[0];
      if (yy) Object.assign(state, {from: yy + '-01-01', to: yy + '-12-31'});
      return;
    }
    if (p.get('rows') in DIMS) state.rows = p.get('rows');
    const c = p.get('cols');
    if (c === '' || c in MEASURE_SETS || (c in DIMS && c !== state.rows)) state.cols = c;
    if (p.get('m') in MEASURES) state.m = p.get('m');
    const sub = p.get('sub');
    if (sub in DIMS && sub !== state.rows && sub !== state.cols) state.sub = sub;
    const csub = p.get('csub');
    if (csub in DIMS && csub !== state.rows && csub !== state.cols && state.cols in DIMS) state.csub = csub;
    if (CHARTS.some(x => x[0] === p.get('chart'))) state.chart = p.get('chart');
    state.from = p.get('from') || '';
    state.to = p.get('to') || '';
    for (const k of new Set([...p.keys()].filter(k => k.startsWith('s.') && k.slice(2) in DIMS))) {
      const v = p.getAll(k);
      state.slices[k.slice(2)] = v.length > 1 ? v : v[0];
    }
  })();
  function save() {
    const p = new URLSearchParams({rows: state.rows, cols: state.cols, m: state.m, sub: state.sub, csub: state.csub, chart: state.chart,
      from: state.from, to: state.to});
    for (const [k, v] of Object.entries(state.slices)) [].concat(v).forEach(x => p.append('s.' + k, x));
    history.replaceState(null, '', '#' + p);
  }

  // ---------------------------------------------------------------- υπολογισμός
  function filtered(slices, pred) {
    const sl = Object.entries(slices).map(([dim, v]) => [DIMS[dim].key, Array.isArray(v) ? new Set(v) : null, v]);
    return facts.filter(f => {
      const d = docs[f[0]];
      if ((state.from && d.date < state.from) || (state.to && d.date > state.to)) return false;
      return sl.every(([key, set, v]) => set ? set.has(key(f, d)) : key(f, d) === v) && (!pred || pred(f, d));
    });
  }
  const cell = () => ({v: 0, docs: new Set()});
  const put = (map, k) => map.get(k) || map.set(k, cell()).get(k);
  const bump = (c, v, i) => { c.v += v; c.docs.add(i); };

  function sortKeys(keys, dim, weight) {
    if (dim === 'kind') return keys.sort().reverse();  // Έσοδα (in), μετά Έξοδα (ex)
    if (!dim || DIMS[dim].time || DIMS[dim].ord) {  // σταθερή σειρά· το «χωρίς τιμή» στο τέλος
      return keys.sort((a, b) => (a === '') - (b === '') || (a < b ? -1 : a > b ? 1 : 0));
    }
    return keys.sort((a, b) => weight(b) - weight(a));
  }

  let cube;
  function compute() {
    const R = DIMS[state.rows], MS = MEASURE_SETS[state.cols] || null, mm = !!MS, m = MEASURES[state.m];
    const C = mm ? null : DIMS[state.cols] || null, S = state.sub ? DIMS[state.sub] : null;
    const CS = C && state.csub && state.csub !== state.cols ? DIMS[state.csub] : null;
    const csubs = new Map();  // στήλη → κλειδιά υποστηλών
    const count = !mm && m.count;
    const kindAxis = state.rows === 'kind' ? 'rows' : state.cols === 'kind' ? 'cols' : null;
    // Έσοδα και έξοδα μαζί (χωρίς άξονα ή φίλτρο «Είδος»): τα έξοδα αφαιρούνται, αλλιώς το
    // άθροισμα δεν έχει νόημα. Τα σύνολα κατά μήκος του «Είδους» = Έσοδα − Έξοδα.
    const signed = !count && (m.signed && !mm || (!kindAxis && !(state.slices.kind && !Array.isArray(state.slices.kind))));
    const rows = new Map(), cols = new Map(), grand = cell();
    for (const f of filtered(state.slices)) {
      const d = docs[f[0]], s = d.k === 'ex' ? -1 : 1, rk = R.key(f, d);
      let row = rows.get(rk);
      if (!row) rows.set(rk, row = {k: rk, cells: new Map(), total: cell(), subs: new Map()});
      let sr = null;
      if (S) {
        const sk = S.key(f, d);
        sr = row.subs.get(sk);
        if (!sr) row.subs.set(sk, sr = {k: sk, cells: new Map(), total: cell()});
      }
      const parts = mm ? MS.cols.map(id => [id, mv(f, MEASURES[id])]) : [[C ? C.key(f, d) : '', count ? 0 : mv(f, m)]];
      for (const [ck, v] of parts) {
        const cv = signed ? v * s : v, tv = !count && kindAxis ? v * s : cv, rv = kindAxis === 'cols' ? tv : cv;
        bump(put(row.cells, ck), cv, f[0]);
        bump(row.total, rv, f[0]);
        if (sr) { bump(put(sr.cells, ck), cv, f[0]); bump(sr.total, rv, f[0]); }
        bump(put(cols, ck), kindAxis === 'rows' ? tv : cv, f[0]);
        bump(grand, tv, f[0]);
        if (CS) {  // υποανάλυση στηλών: τα ίδια ποσά και στο κελί «στήλη › υποστήλη»
          const csk = CS.key(f, d), kk = ck + SEP + csk;
          (csubs.get(ck) || csubs.set(ck, new Set()).get(ck)).add(csk);
          bump(put(row.cells, kk), cv, f[0]);
          if (sr) bump(put(sr.cells, kk), cv, f[0]);
          bump(put(cols, kk), kindAxis === 'rows' ? tv : cv, f[0]);
        }
      }
    }
    const val = c => c ? (count ? c.docs.size : c.v) : 0;
    const nonZero = k => [...rows.values()].some(r => Math.abs(r.cells.get(k).v) >= 0.005);
    let colKeys = mm ? MS.cols.filter(k => !MS.hide.includes(k) || nonZero(k))
      : sortKeys([...cols.keys()].filter(k => !k.includes(SEP)), C && state.cols, k => Math.abs(val(cols.get(k))));
    // «Ποσά & ΦΠΑ»: χωρίς μη εκπιπτόμενο ΦΠΑ ο εκπιπτόμενος = όλος ο ΦΠΑ — περιττή στήλη.
    if (mm && MS.cols.includes('vatded') && !colKeys.includes('vatnd')) colKeys = colKeys.filter(k => k !== 'vatded');
    let rest = null;
    if (C && !C.time && colKeys.length > COL_LIMIT) {  // πολλές στήλες: οι μεγαλύτερες + «Λοιπά»
      rest = new Set(colKeys.splice(COL_LIMIT - 1));
      const merge = map => {
        const c = cell();
        rest.forEach(k => { const x = map.get(k); if (x) { c.v += x.v; x.docs.forEach(i => c.docs.add(i)); } });
        map.set(REST, c);
      };
      rows.forEach(r => { merge(r.cells); r.subs.forEach(sr => merge(sr.cells)); });
      merge(cols);
      colKeys.push(REST);
    }
    // Υποστήλες ανά στήλη: οι μεγαλύτερες CSUB_LIMIT − 1 + «Λοιπά» (η στήλη «Λοιπά» δεν αναλύεται).
    const colSubs = new Map(), csubRest = new Map();
    if (CS) {
      colKeys.filter(k => k !== REST && csubs.has(k)).forEach(k => {
        const keys = sortKeys([...csubs.get(k)], state.csub, sk => Math.abs(val(cols.get(k + SEP + sk))));
        if (keys.length > CSUB_LIMIT) {
          const extra = keys.splice(CSUB_LIMIT - 1);
          csubRest.set(k, new Set(extra));
          const merge = map => {
            const c = cell();
            extra.forEach(sk => { const x = map.get(k + SEP + sk); if (x) { c.v += x.v; x.docs.forEach(i => c.docs.add(i)); } });
            map.set(k + SEP + REST, c);
          };
          rows.forEach(r => { merge(r.cells); r.subs.forEach(sr => merge(sr.cells)); });
          merge(cols);
          keys.push(REST);
        }
        colSubs.set(k, keys);
      });
    }
    rows.forEach(r => {
      r.subList = S ? sortKeys([...r.subs.keys()], state.sub, k => Math.abs(val(r.subs.get(k).total))).map(k => r.subs.get(k)) : [];
    });
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
    cube = {R, C, S, CS, MS, mm, m, count, kindAxis, signed, rows: rowList, cols, colKeys, colSubs, csubRest, rest, grand, val};
    cube.disp = dispCols();
    return cube;
  }

  // ---------------------------------------------------------------- μορφοποίηση
  const fmt = v => cube.count ? nf0.format(v) : nf2.format(Math.abs(v) < 0.005 ? 0 : v);
  const numCls = v => Math.abs(v) < 0.005 ? 'yz' : v < 0 ? 'yneg' : '';
  const colLabel = k => k === REST ? 'Λοιπά' : cube.mm ? MEASURES[k].label : cube.C ? dimName(state.cols, k) : MEASURES[state.m].label;
  const kindDot = (dim, k) => dim === 'kind' ? `<i class="ol-dot c-${k === 'in' ? 'in' : 'out'}"></i>` : '';
  const totalLabel = () => cube.kindAxis === 'cols' && !cube.count ? 'Εσ. − Εξ.' : 'Σύνολο';
  const showTotalCol = () => cube.C && !cube.mm;
  const measureTitle = () => cube.mm ? cube.MS.title :
    MEASURES[state.m].label + (cube.signed && !MEASURES[state.m].signed ? ' (Εσ. − Εξ.)' : '');
  const unit = () => cube.count ? '' : ' €';
  // Χρώμα: σταθερό για Έσοδα/Έξοδα, αλλιώς οι θέσεις της παλέτας με τη σειρά (το 8ο = «Λοιπά»).
  const colorOf = (dim, k, i) => dim === 'kind' ? (k === 'in' ? 'in' : 'out') : k === REST || i >= SLOTS ? 'rest' : 's' + (i + 1);

  // ---------------------------------------------------------------- controls
  const selRows = $('[data-dim="rows"]'), selCols = $('[data-dim="cols"]'), selM = $('[data-measure]'), selSub = $('[data-sub]');
  const selCsub = $('[data-csub]');
  const inFrom = $('[data-from]'), inTo = $('[data-to]');
  const opts = (list, cur) => list.map(([v, l, dis]) => `<option value="${v}"${v === cur ? ' selected' : ''}${dis ? ' disabled' : ''}>${esc(l)}</option>`).join('');

  function renderControls() {
    const dims = Object.entries(DIMS);
    selRows.innerHTML = opts(dims.map(([k, d]) => [k, d.label, k === state.cols]), state.rows);
    selCols.innerHTML = opts([['', '— Χωρίς (μόνο σύνολο)'], ...Object.entries(MEASURE_SETS).map(([k, s]) => [k, s.label]),
      ...dims.map(([k, d]) => [k, d.label, k === state.rows])], state.cols);
    selSub.innerHTML = opts([['', '— Χωρίς υποανάλυση'], ...dims.map(([k, d]) => [k, d.label, k === state.rows || k === state.cols])], state.sub);
    const colDim = state.cols in DIMS;
    selCsub.innerHTML = opts([['', colDim ? '— Χωρίς υποανάλυση' : '— (διάλεξε διάσταση στις στήλες)'],
      ...dims.map(([k, d]) => [k, d.label, k === state.rows || k === state.cols])], colDim ? state.csub : '');
    selCsub.disabled = !colDim;
    selM.innerHTML = opts(Object.entries(MEASURES).map(([k, m]) => [k, m.label]), state.m);
    selM.disabled = state.cols in MEASURE_SETS;
    setDate(inFrom, state.from);
    setDate(inTo, state.to);

    $('[data-years]').innerHTML = [['', 'Όλα'], ...years.map(y => [y, y])].map(([y, l]) => {
      const on = y ? state.from === y + '-01-01' && state.to === y + '-12-31' : !state.from && !state.to;
      return `<a href="#" data-year="${y}"${on ? ' aria-current="page"' : ''}>${l}</a>`;
    }).join('');

    $('[data-presets]').innerHTML = PRESETS.map((p, i) => {
      const on = p.rows === state.rows && p.cols === state.cols && (p.cols in MEASURE_SETS || p.m === state.m) &&
        (p.sub || '') === state.sub && (p.csub || '') === state.csub && (p.kind || '') === (state.slices.kind || '') &&
        Object.keys(state.slices).every(k => k === 'kind');
      return `<button type="button" class="ol-preset" data-preset="${i}" aria-pressed="${on}">${esc(p.label)}</button>`;
    }).join('');

    const kind = Array.isArray(state.slices.kind) ? '' : state.slices.kind || '';
    const chips = Object.entries(state.slices).filter(([dim]) => dim !== 'kind').map(([dim, k]) => {
      const many = Array.isArray(k);
      const text = many ? `${k.length} επιλογές` : dimName(dim, k);
      const title = many ? k.map(x => dimName(dim, x)).join('\n') : text;
      return `<span class="ol-chip${many ? ' is-many' : ''}"><button type="button" class="ol-chip-b" data-filter="${dim}" title="${esc(title)}">` +
        `<small>${esc(DIMS[dim].label)}</small>${esc(text)}</button>` +
        `<button type="button" data-unslice="${dim}" aria-label="Αφαίρεση φίλτρου ${esc(DIMS[dim].label)}">✕</button></span>`;
    });
    $('[data-slices]').innerHTML =
      `<div class="ol-seg" role="group" aria-label="Είδος">` +
      [['', 'Όλα'], ['in', 'Έσοδα'], ['ex', 'Έξοδα']].map(([k, l]) =>
        `<button type="button" data-kind="${k}" aria-pressed="${k === kind}">${k ? kindDot('kind', k) : ''}${l}</button>`).join('') +
      `</div>` +
      `<button type="button" class="ol-addf" data-filter="">＋ Φίλτρο</button>` +
      (chips.length ? chips.join('') + `<button type="button" class="ol-clear" data-clear>Καθαρισμός</button>`
        : `<span class="ol-hint">Κλικ σε γραμμή του πίνακα ή του διαγράμματος για εμβάθυνση.</span>`);

    $('[data-charts]').innerHTML = CHARTS.map(([k, l, svg]) => {
      const ok = chartOk(k);
      return `<button type="button" data-ctype="${k}" aria-pressed="${k === chartType()}" title="${esc(l)}${ok ? '' : ' — δεν ταιριάζει στην τρέχουσα προβολή'}"` +
        `${ok ? '' : ' disabled'} aria-label="${esc(l)}"><svg viewBox="0 0 24 24" aria-hidden="true">${svg}</svg></button>`;
    }).join('');
  }
  function setDate(input, v) {
    if (input._flatpickr) input._flatpickr.setDate(v || null, false);
    else input.value = v;
  }

  // ---------------------------------------------------------------- KPIs (ανεξάρτητα από άξονες και «Είδος»)
  const kpiPrev = {};
  function tween(el, id, to, fmt2) {
    const from = kpiPrev[id] ?? 0;
    kpiPrev[id] = to;
    if (!motion || from === to) { el.textContent = fmt2(to); return; }
    let t0 = null;
    const final = fmt2(to);
    requestAnimationFrame(function step(t) {
      t0 = t0 || t;
      const k = Math.min((t - t0) / 650, 1);
      el.textContent = k < 1 ? fmt2(from + (to - from) * (1 - Math.pow(1 - k, 3))) : final;
      if (k < 1) requestAnimationFrame(step);
    });
    setTimeout(() => { el.textContent = final; }, 800);  // και αν παγώσουν τα frames
  }
  function renderKpis() {
    const box = $('[data-kpis]');
    if (!box.firstChild) {
      box.innerHTML = [['in', 'Έσοδα'], ['ex', 'Έξοδα'], ['res', 'Αποτέλεσμα'], ['vat', 'Διαφορά ΦΠΑ'], ['tax', 'Φόροι & κρατήσεις']]
        .map(([id, l], i) => `<div class="db-kpi ol-kpi--${id}" style="--i:${i}"><small>${id === 'in' || id === 'ex' ? kindDot('kind', id) : ''}${l}` +
          `<span class="ol-kpi-n" data-kn="${id}"></span></small><b><span data-kv="${id}">0,00</span><i>€</i></b>` +
          `${id === 'in' || id === 'ex' ? `<span class="ol-kpi-bar"><span data-kb="${id}"></span></span>` : ''}` +
          `<span class="db-kpi-note" data-ks="${id}"></span></div>`).join('');
    }
    const sl = Object.assign({}, state.slices);
    delete sl.kind;
    const t = {in: {net: 0, vat: 0, nd: 0, gross: 0, tax: 0, n: new Set()}, ex: {net: 0, vat: 0, nd: 0, gross: 0, tax: 0, n: new Set()}};
    for (const f of filtered(sl)) {
      const x = t[docs[f[0]].k];
      x.net += f[4]; x.vat += f[5]; x.nd += f[12]; x.gross += f[6]; x.tax += f[7] + f[8] + f[9] + f[10] + f[11]; x.n.add(f[0]);
    }
    const res = t.in.net - t.ex.net - t.ex.nd, vatIn = t.ex.vat - t.ex.nd, vat = t.in.vat - vatIn;
    const margin = t.in.net ? `περιθώριο ${nf0.format(res / t.in.net * 100)}% επί των εσόδων` : 'καθαρές αξίες';
    const big = Math.max(Math.abs(t.in.net), Math.abs(t.ex.net), 1);
    const set = (id, v, sub, n) => {
      tween(box.querySelector(`[data-kv="${id}"]`), id, v, x => nf2.format(x));
      box.querySelector(`[data-kv="${id}"]`).classList.toggle('neg', v < -0.005);
      box.querySelector(`[data-ks="${id}"]`).textContent = sub;
      box.querySelector(`[data-kn="${id}"]`).textContent = n || '';
      const bar = box.querySelector(`[data-kb="${id}"]`);
      if (bar) bar.style.width = (Math.abs(v) / big * 100).toFixed(1) + '%';
    };
    set('in', t.in.net, `με ΦΠΑ ${nf2.format(t.in.gross)} €`, `${t.in.n.size} παρ.`);
    set('ex', t.ex.net, `με ΦΠΑ ${nf2.format(t.ex.gross)} €`, `${t.ex.n.size} παρ.`);
    set('res', res, Math.abs(t.ex.nd) >= 0.005 ? margin + ` · με μη εκπ. ΦΠΑ ${nf2.format(t.ex.nd)}` : margin, 'Εσ. − Εξ.');
    set('vat', vat, `εκρ. ${nf2.format(t.in.vat)} − εκπ. εισρ. ${nf2.format(vatIn)}`, vat < 0 ? 'πιστωτική' : 'χρεωστική');
    set('tax', t.in.tax + t.ex.tax, `εσόδων ${nf2.format(t.in.tax)} · εξόδων ${nf2.format(t.ex.tax)}`);
  }

  // ---------------------------------------------------------------- πίνακας
  // Ορατές στήλες: κάθε ανοιχτή στήλη (υποανάλυση στηλών) γίνεται υποστήλες + «Σύνολο» της ομάδας.
  function dispCols() {
    const out = [];
    cube.colKeys.forEach(k => {
      const subs = cube.colSubs.get(k);
      if (subs && state.copen.has(k)) {
        subs.forEach((sk, i) => out.push({key: k + SEP + sk, k, sk, first: !i, label: dimName(state.csub, sk)}));
        out.push({key: k, k, gt: true, label: 'Σύνολο'});
      } else out.push({key: k, k, label: colLabel(k)});
    });
    return out;
  }
  function renderTable() {
    const {rows, cols, grand, val, S, CS} = cube, disp = cube.disp = dispCols();
    const two = disp.some(c => c.sk !== undefined);
    const shown = state.all ? rows : rows.slice(0, ROW_LIMIT);
    const sortMark = by => state.sort && state.sort.by === by ? (state.sort.dir > 0 ? ' ▲' : ' ▼') : '';
    // data-sort: «@…» ή «#δείκτης ορατής στήλης» (τα κλειδιά μπορεί να έχουν χαρακτήρες ελέγχου).
    const th = (by, html, cls, attrs, sortId, pre) => `<th class="${cls || 'num'}"${attrs || ''} aria-sort="${state.sort && state.sort.by === by ? (state.sort.dir > 0 ? 'ascending' : 'descending') : 'none'}">` +
      (pre ? `<span class="ol-th-in">${pre}` : '') +
      `<button type="button" class="ol-sort" data-sort="${esc(sortId || by)}">${html}${sortMark(by)}</button>` + (pre ? '</span>' : '') + '</th>';
    const ctog = (k, open) => {
      const n = cube.colSubs.get(k).length;
      return `<button type="button" class="ol-ctog" data-ctoggle="${cube.colKeys.indexOf(k)}" aria-expanded="${open}" ` +
        `title="${open ? 'Σύμπτυξη' : 'Ανάπτυξη σε ' + esc(CS.label.toLowerCase())} (${n})"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>`;
    };
    const colKeys = disp;  // για τα colspan
    const bodyVals = shown.flatMap(r => disp.filter(c => !c.gt).map(c => Math.abs(val(r.cells.get(c.key)))));
    const maxCell = Math.max(...bodyVals, 0) || 1;
    const maxTotal = Math.max(...shown.map(r => Math.abs(val(r.total))), 0) || 1;
    const cells = (c, id, cls) => {
      if (!c) return `<td class="num ol-c ${cls || ''}"><span class="yz">—</span></td>`;
      const v = val(c);
      return `<td class="num ol-c ${cls || ''}" style="--h:${(Math.abs(v) / maxCell).toFixed(3)}"><button type="button" data-cell="${id}" ` +
        `title="Τα παραστατικά του κελιού"><span class="${numCls(v)}">${fmt(v)}</span></button></td>`;
    };
    const dcls = c => c.gt ? 'ol-gt' : c.sk !== undefined ? 'ol-sc' + (c.first ? ' ol-sc0' : '') : '';
    const totalCell = (c, id, cls) => {
      const v = val(c);
      return `<td class="num ol-t${v < 0 ? ' is-neg' : ''} ${cls || ''}" style="--b:${(Math.abs(v) / maxTotal * 100).toFixed(1)}%">` +
        `<button type="button" data-cell="${id}"><span class="${numCls(v)}">${fmt(v)}</span></button></td>`;
    };

    const rs = two ? ' rowspan="2"' : '';
    let html = '<thead><tr>' + th('@label', esc(cube.R.label) + (S ? ` <small class="ol-subh">› ${esc(S.label)}</small>` : '') +
      '', 'yr-sticky', rs);
    cube.colKeys.forEach(k => {
      const group = disp.filter(c => c.k === k), j = disp.indexOf(group[0]);
      const canOpen = CS && cube.colSubs.has(k);
      if (group.length > 1) {  // ανοιχτή στήλη: επικεφαλίδα ομάδας πάνω από τις υποστήλες
        html += `<th class="ol-cg" colspan="${group.length}"><span class="ol-cg-in">${ctog(k, true)}${kindDot(state.cols, k)}${esc(colLabel(k))}</span></th>`;
      } else {
        html += th(k, kindDot(state.cols, k) + esc(colLabel(k)), 'num' + (canOpen ? ' ol-cth' : ''), rs, '#' + j, canOpen ? ctog(k, false) : '');
      }
    });
    html += (showTotalCol() ? th('@total', totalLabel(), 'num ol-total-h', rs) : '') + '</tr>';
    if (two) {
      html += '<tr class="ol-subhead">' + disp.map((c, j) => c.sk === undefined && !c.gt ? ''
        : th(c.key, esc(c.label), 'num ' + dcls(c), '', '#' + j)).join('') + '</tr>';
    }
    html += '</thead><tbody>';
    shown.forEach((r, i) => {
      const open = S && state.open.has(r.k);
      html += `<tr class="ol-row${open ? ' is-open' : ''}" style="--i:${Math.min(i, 20)}"><th class="yr-sticky ol-rh" scope="row"><span class="ol-rh-in">` +
        (S ? `<button type="button" class="ol-tog" data-toggle="${i}" aria-expanded="${!!open}" title="${open ? 'Σύμπτυξη' : 'Ανάπτυξη'} (${r.subList.length})">` +
          `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>` : '') +
        `<button type="button" class="ol-rl" data-drill="${i}" title="Εμβάθυνση">${kindDot(state.rows, r.k)}${esc(dimName(state.rows, r.k))}` +
        (S ? `<small class="ol-nsub">${r.subList.length}</small>` : '') + `</button></span></th>`;
      disp.forEach((c, j) => { html += cells(r.cells.get(c.key), `${i}:${j}`, dcls(c)); });
      if (showTotalCol()) html += totalCell(r.total, `${i}:t`);
      html += '</tr>';
      if (open) {
        r.subList.slice(0, SUB_LIMIT).forEach((s, si) => {
          html += `<tr class="ol-sub" style="--i:${si}"><th class="yr-sticky ol-rh" scope="row"><button type="button" class="ol-rl" data-cell="${i}:t:${si}" ` +
            `title="Τα παραστατικά">${kindDot(state.sub, s.k)}${esc(dimName(state.sub, s.k))}</button></th>` +
            disp.map((c, j) => cells(s.cells.get(c.key), `${i}:${j}:${si}`, dcls(c))).join('') +
            (showTotalCol() ? totalCell(s.total, `${i}:t:${si}`) : '') + '</tr>';
        });
        if (r.subList.length > SUB_LIMIT) {
          html += `<tr class="ol-sub ol-sub-more"><th class="yr-sticky" colspan="${colKeys.length + 2}">+ ${r.subList.length - SUB_LIMIT} ακόμη — εμβάθυνση για όλες</th></tr>`;
        }
      }
    });
    if (!rows.length) {
      html += `<tr><td class="ol-empty" colspan="${colKeys.length + 2}">Δεν υπάρχουν παραστατικά για τα τρέχοντα φίλτρα.</td></tr>`;
    }
    html += '</tbody>';
    if (rows.length > 1 || cube.C) {
      html += `<tfoot><tr class="ol-foot"><th class="yr-sticky" scope="row">${cube.kindAxis === 'rows' && !cube.count ? 'Εσ. − Εξ.' : 'Σύνολο'}</th>` +
        disp.map((c, j) => { const v = val(cols.get(c.key)); return `<td class="num ${dcls(c)}"><button type="button" data-cell="f:${j}"><span class="${numCls(v)}">${fmt(v)}</span></button></td>`; }).join('') +
        (showTotalCol() ? `<td class="num ol-grand"><button type="button" data-cell="f:t"><span class="${numCls(val(grand))}">${fmt(val(grand))}</span></button></td>` : '') +
        '</tr></tfoot>';
    }
    const table = $('[data-table]');
    table.innerHTML = html;
    table.classList.toggle('ol-heat', state.heat);
    table.classList.toggle('ol-has-total', !!showTotalCol());
    $('[data-table-title]').innerHTML = `${esc(measureTitle())} <span class="pill">${rows.length} ${rows.length === 1 ? 'γραμμή' : 'γραμμές'}</span>`;
    const exp = $('[data-expand]');
    exp.style.display = S ? '' : 'none';
    exp.textContent = S && rows.length && rows.every(r => state.open.has(r.k)) ? '⊟ Σύμπτυξη όλων' : '⊞ Ανάπτυξη όλων';
    $('[data-more]').innerHTML = rows.length > ROW_LIMIT
      ? `<button type="button" class="btn-ghost btn-sm" data-all>${state.all ? 'Λιγότερες γραμμές' : `Εμφάνιση όλων (+${rows.length - ROW_LIMIT})`}</button>` : '';
  }

  // ---------------------------------------------------------------- διαγράμματα
  function niceStep(span, ticks) {
    const raw = Math.max(span, 1) / ticks, mag = 10 ** Math.floor(Math.log10(raw));
    return [1, 2, 2.5, 5, 10].map(x => x * mag).find(x => x >= raw);
  }
  function scale(vals) {
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const step = niceStep(hi - lo, 5);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step || step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(v);
    return {lo, hi, step, ticks, pct: v => (v - lo) / (hi - lo) * 100};
  }
  const tickText = v => nf0.format(v);

  // Επιτρεπτοί τύποι: στοιβαγμένες θέλουν διάσταση στις στήλες· γραμμή ≥ 2 γραμμές.
  function chartOk(type) {
    if (!cube) return true;
    if (type === 'stack') return !!cube.C && cube.colKeys.length > 1;
    if (type === 'line') return cube.rows.length > 1;
    return true;
  }
  const chartType = () => chartOk(state.chart) ? state.chart : 'bar';

  function chartSeries(type) {
    const {val} = cube;
    if (cube.mm && cube.MS.sum) {
      const [key, label, ks] = cube.MS.sum;
      return {series: [[key, label, 'acc']], value: r => ks.reduce((s, k) => s + val(r.cells.get(k)), 0)};
    }
    if (cube.mm) return {series: cube.MS.series, value: (r, k) => val(r.cells.get(k))};
    if (cube.C && (state.cols === 'kind' || type === 'stack' || type === 'line')) {
      return {series: cube.colKeys.map((k, i) => [k, colLabel(k), colorOf(state.cols, k, i)]), value: (r, k) => val(r.cells.get(k))};
    }
    return {series: [['v', measureTitle(), 'acc']], value: r => cube.C ? val(r.total) : val(r.cells.get(''))};
  }

  const chartBox = () => $('[data-chart]');
  function renderChart() {
    const box = chartBox(), type = chartType();
    const S = chartSeries(type);
    cube.chart = {type, ...S};
    const title = {bar: 'ανά', stack: 'ανά', line: 'ανά', donut: 'μερίδια ανά', tree: 'ιεραρχία ανά'}[type];
    const what = type === 'donut' || type === 'tree' || S.series.length > 1 ? measureTitle() : S.series[0][1];
    $('[data-chart-title]').textContent = `${what} ${title} ${cube.R.acc}` +
      (type === 'tree' && cube.S ? ' › ' + cube.S.acc : '');
    $('[data-legend]').innerHTML = S.series.length > 1 && type !== 'donut' && type !== 'tree'
      ? S.series.map(([, l, c]) => `<li><i class="ol-dot c-${c}"></i>${esc(l)}</li>`).join('') : '';
    box.style.cssText = '';
    if (!cube.rows.length) { box.className = 'ol-chart'; box.innerHTML = '<p class="ol-empty">Χωρίς δεδομένα για τα τρέχοντα φίλτρα.</p>'; return; }
    ({bar: drawBars, stack: drawBars, line: drawLine, donut: drawDonut, tree: drawTree})[type](box, type, S);
  }

  // Μπάρες (ομαδοποιημένες ή στοιβαγμένες): κάθετες για χρόνο/σταθερή σειρά, αλλιώς οριζόντιες.
  function drawBars(box, type, {series, value}) {
    const stack = type === 'stack', vertical = !!(cube.R.time || cube.R.ord);
    const rows = cube.rows.slice(0, vertical ? 36 : (state.all ? 40 : 15));
    const vals = stack
      ? rows.flatMap(r => { let p = 0, n = 0; series.forEach(([k]) => { const v = value(r, k); if (v > 0) p += v; else n += v; }); return [p, n]; })
      : rows.flatMap(r => series.map(([k]) => value(r, k)));
    const sc = scale(vals);
    const seg = (lo, hi, c, j, cls) => {
      const a = sc.pct(Math.min(lo, hi)), b = sc.pct(Math.max(lo, hi));
      return `<i class="ol-bar c-${c}${hi < lo ? ' is-neg' : ''}${cls || ''}" style="--a:${a.toFixed(2)}%;--s:${(b - a).toFixed(2)}%;--j:${j}"></i>`;
    };
    const plot = r => {
      if (!stack) return series.map(([k, , c], j) => `<span class="ol-slot">${seg(0, value(r, k), c, j)}</span>`).join('');
      let p = 0, n = 0, out = '';
      series.forEach(([k, , c], j) => {
        const v = value(r, k);
        if (Math.abs(v) < 0.005) return;
        if (v > 0) { out += seg(p, p + v, c, j, ' is-seg'); p += v; } else { out += seg(n, n + v, c, j, ' is-seg'); n += v; }
      });
      return `<span class="ol-slot">${out}</span>`;
    };
    const tickHtml = sc.ticks.map(v => `<i class="${Math.abs(v) < sc.step / 2 ? 'is-zero' : ''}" style="--p:${sc.pct(v).toFixed(2)}%"><span>${tickText(v)}</span></i>`).join('');
    const k = stack ? 1 : series.length;
    if (vertical) {
      const every = Math.ceil(rows.length / Math.max(4, Math.floor(box.clientWidth / 64)));  // ετικέτα ανά ~64px
      box.className = 'ol-chart ol-chart--v' + (stack ? ' is-stack' : '');
      box.style.setProperty('--n', rows.length);
      box.style.setProperty('--k', k);
      box.innerHTML = `<div class="ol-grid">${tickHtml}</div><div class="ol-cols">` + rows.map((r, i) =>
        `<button type="button" class="ol-col" style="--i:${i}" data-drill="${i}" data-tip="${i}" aria-label="${esc(dimName(state.rows, r.k))}">` +
        `<span class="ol-plot">${plot(r)}</span>` +
        `<span class="ol-lab">${i % every ? '' : esc(shortLabel(r.k))}</span></button>`).join('') + '</div>';
    } else {
      box.className = 'ol-chart ol-chart--h' + (stack ? ' is-stack' : '');
      box.style.setProperty('--k', k);
      box.innerHTML = `<div class="ol-hgrid"><span></span><div class="ol-grid">${tickHtml}</div><span></span></div>` + rows.map((r, i) => {
        const tot = stack ? series.reduce((s, [k2]) => s + value(r, k2), 0) : value(r, series[0][0]);
        return `<button type="button" class="ol-hrow" style="--i:${i}" data-drill="${i}" data-tip="${i}">` +
          `<span class="ol-hlab">${kindDot(state.rows, r.k)}${esc(dimName(state.rows, r.k))}</span>` +
          `<span class="ol-plot">${plot(r)}</span>` +
          `<span class="ol-hval">${series.length > 1 && !stack ? '' : `<span class="${numCls(tot)}">${fmt(tot)}</span>`}</span></button>`;
      }).join('');
    }
  }
  function shortLabel(k) {
    if (!k) return '—';
    if (state.rows === 'month') return MONTHS[k.slice(5) - 1] + (multiYear() ? ' ’' + k.slice(2, 4) : '');
    if (state.rows === 'day') return k.slice(8) + '/' + k.slice(5, 7);
    if (state.rows === 'quarter') return 'ΑΒΓΔ'[k.slice(-1) - 1] + '΄' + (multiYear() ? ' ’' + k.slice(2, 4) : '');
    if (state.rows === 'wday') return WDAYS[k - 1].slice(0, 3);
    const n = dimName(state.rows, k);
    return n.length > 12 ? n.slice(0, 11) + '…' : n;
  }
  const multiYear = () => new Set(cube.rows.map(r => r.k.slice(0, 4))).size > 1;

  // Γραμμή / περιοχή (SVG): μία γραμμή ανά σειρά, με περιοχή όταν είναι μία.
  function drawLine(box, type, {series, value}) {
    const rows = cube.rows.slice(0, 60);
    const W = Math.max(box.clientWidth, 320), H = 290, L = 58, R = 14, T = 12, B = 30;
    const sc = scale(rows.flatMap(r => series.map(([k]) => value(r, k))));
    const x = i => rows.length > 1 ? L + i * (W - L - R) / (rows.length - 1) : (L + W - R) / 2;
    const y = v => T + (100 - sc.pct(v)) / 100 * (H - T - B);
    const every = Math.ceil(rows.length / Math.max(3, Math.floor((W - L) / 70)));
    let svg = sc.ticks.map(v => `<line class="ol-gl${Math.abs(v) < sc.step / 2 ? ' is-zero' : ''}" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>` +
      `<text class="ol-tk" x="${L - 8}" y="${(y(v) + 3.5).toFixed(1)}" text-anchor="end">${tickText(v)}</text>`).join('');
    const colW = rows.length > 1 ? (W - L - R) / (rows.length - 1) : W - L - R;
    svg += rows.map((r, i) => `<rect class="ol-hit" data-tip="${i}" data-drill="${i}" x="${(x(i) - colW / 2).toFixed(1)}" y="${T}" width="${colW.toFixed(1)}" height="${H - T - B}"/>` +
      (i % every ? '' : `<text class="ol-xl" x="${x(i).toFixed(1)}" y="${H - 10}" text-anchor="middle">${esc(shortLabel(r.k))}</text>`)).join('');
    series.forEach(([k, , c], j) => {
      const pts = rows.map((r, i) => [x(i), y(value(r, k))]);
      const d = 'M' + pts.map(p => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L');
      if (series.length === 1) {
        svg += `<path class="ol-area c-${c}" d="${d}L${pts[pts.length - 1][0].toFixed(1)},${y(0).toFixed(1)}L${pts[0][0].toFixed(1)},${y(0).toFixed(1)}Z"/>`;
      }
      svg += `<path class="ol-line c-${c}" style="--j:${j}" pathLength="1" d="${d}"/>` +
        pts.map((p, i) => `<circle class="ol-pt c-${c}" style="--i:${i};--j:${j}" cx="${p[0].toFixed(1)}" cy="${p[1].toFixed(1)}" r="4"/>`).join('');
    });
    box.className = 'ol-chart ol-chart--line';
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc($('[data-chart-title]').textContent)}">${svg}</svg>`;
  }

  // Μερίδια: οι μεγαλύτερες 7 γραμμές (απόλυτες τιμές) + «Λοιπά».
  function shares() {
    const {val} = cube, v = r => Math.abs(cube.C ? val(r.total) : val(r.cells.get('')) || val(r.total));
    const list = cube.rows.map((r, i) => ({r, i, v: v(r)})).filter(x => x.v >= 0.005).sort((a, b) => b.v - a.v);
    const top = list.length > SLOTS + 1 ? list.slice(0, SLOTS) : list;
    const restV = list.slice(top.length).reduce((s, x) => s + x.v, 0);
    const items = top.map((x, j) => ({...x, c: colorOf(state.rows, x.r.k, j)}));
    if (restV) items.push({r: null, i: -1, v: restV, c: 'rest', n: list.length - top.length});
    const total = items.reduce((s, x) => s + x.v, 0) || 1;
    const neg = cube.rows.some(r => (cube.C ? cube.val(r.total) : cube.val(r.cells.get(''))) < -0.005);
    return {items, total, neg};
  }
  function drawDonut(box) {
    const {items, total, neg} = shares();
    cube.chart.shares = {items, total};
    let acc = 0;
    const gap = items.length > 1 ? 0.5 : 0;
    const segs = items.map((x, j) => {
      const p = x.v / total * 100, s = `<circle class="ol-seg c-${x.c}" style="--j:${j}" data-tip="d${j}" ${x.r ? `data-drill="${x.i}"` : ''} cx="21" cy="21" r="15.9" pathLength="100" ` +
        `stroke-dasharray="${Math.max(p - gap, 0.01).toFixed(2)} ${(100 - p + gap).toFixed(2)}" stroke-dashoffset="${(-acc).toFixed(2)}"/>`;
      acc += p;
      return s;
    }).join('');
    const name = x => x.r ? dimName(state.rows, x.r.k) : `Λοιπά (${x.n})`;
    box.className = 'ol-chart ol-chart--donut';
    box.innerHTML = `<div class="ol-donut"><svg viewBox="0 0 42 42" role="img" aria-label="Μερίδια"><circle class="ol-seg-bg" cx="21" cy="21" r="15.9"/>${segs}</svg>` +
      `<div class="ol-donut-c"><b>${fmt(total)}</b><small>${cube.count ? 'παραστατικά' : '€ σύνολο'}${neg ? ' · απόλ.' : ''}</small></div></div>` +
      `<ul class="ol-dlist">` + items.map((x, j) =>
        `<li style="--j:${j}"><button type="button" data-tip="d${j}" ${x.r ? `data-drill="${x.i}"` : 'disabled'}>` +
        `<i class="ol-dot c-${x.c}"></i><span class="ol-dl-n">${esc(name(x))}</span>` +
        `<span class="ol-dl-p">${nf1.format(x.v / total * 100)}%</span><span class="ol-dl-v">${fmt(x.v)}</span>` +
        `<span class="ol-dl-bar"><span class="c-${x.c}" style="width:${(x.v / items[0].v * 100).toFixed(1)}%"></span></span></button></li>`).join('') + '</ul>';
  }

  // Treemap: επίπεδο 1 = γραμμές, επίπεδο 2 = υποανάλυση (αν έχει οριστεί). Squarified.
  function squarify(items, x, y, w, h) {
    const out = [], total = items.reduce((s, it) => s + it.v, 0);
    if (!total || w <= 0 || h <= 0) return out;
    let rest = items.map(it => ({...it, a: it.v * w * h / total}));
    const worst = (row, side) => {
      const s = row.reduce((t, it) => t + it.a, 0), mx = Math.max(...row.map(it => it.a)), mn = Math.min(...row.map(it => it.a));
      return Math.max(side * side * mx / (s * s), (s * s) / (side * side * mn));
    };
    while (rest.length) {
      const side = Math.min(w, h);
      let row = [rest[0]], i = 1;
      while (i < rest.length && worst([...row, rest[i]], side) <= worst(row, side)) row.push(rest[i++]);
      const s = row.reduce((t, it) => t + it.a, 0), thick = s / side;
      let off = 0;
      for (const it of row) {
        const len = it.a / thick;
        out.push(w >= h ? {...it, x, y: y + off, w: thick, h: len} : {...it, x: x + off, y, w: len, h: thick});
        off += len;
      }
      if (w >= h) { x += thick; w -= thick; } else { y += thick; h -= thick; }
      rest = rest.slice(row.length);
    }
    return out;
  }
  function drawTree(box) {
    const {items, total, neg} = shares();
    const W = Math.max(box.clientWidth, 300), H = Math.min(Math.max(W * 0.5, 300), 440), HEAD = 20;
    cube.chart.shares = {items, total};
    const val = cube.val, tiles = [];
    squarify(items, 0, 0, W, H).forEach((t, j) => {
      const label = t.r ? dimName(state.rows, t.r.k) : `Λοιπά (${t.n})`;
      const subs = t.r && cube.S ? t.r.subList.map((s, si) => ({s, si, v: Math.abs(val(s.total))})).filter(s => s.v >= 0.005) : [];
      const group = subs.length > 1 && t.h > HEAD + 24 && t.w > 60;
      tiles.push(`<div class="ol-tile c-${t.c}${group ? ' is-group' : ''}" style="--j:${j};left:${t.x.toFixed(1)}px;top:${t.y.toFixed(1)}px;width:${t.w.toFixed(1)}px;height:${t.h.toFixed(1)}px"` +
        (group ? '>' : ` data-tip="d${j}" ${t.r ? `data-drill="${t.i}" role="button" tabindex="0"` : ''}>`) +
        tileText(label, t.v, total, t.w, group ? HEAD + 4 : t.h, group));
      if (group) {
        const top = subs.slice(0, 12), restV = subs.slice(12).reduce((s, x) => s + x.v, 0);
        if (restV) top.push({s: null, si: -1, v: restV, n: subs.length - 12});
        squarify(top, 0, HEAD, t.w, t.h - HEAD).forEach((c, ci) => {
          const cl = c.s ? dimName(state.sub, c.s.k) : `Λοιπά (${c.n})`;
          tiles.push(`<div class="ol-leaf" style="--o:${Math.max(0.1, 0.34 - ci * 0.025).toFixed(3)};left:${c.x.toFixed(1)}px;top:${c.y.toFixed(1)}px;` +
            `width:${c.w.toFixed(1)}px;height:${c.h.toFixed(1)}px" data-tip="d${j}.${c.si}" ${c.s ? `data-cell="${t.i}:t:${c.si}" role="button" tabindex="0"` : ''}>` +
            tileText(cl, c.v, total, c.w, c.h, false) + '</div>');
        });
      }
      tiles.push('</div>');
    });
    box.className = 'ol-chart ol-chart--tree';
    box.innerHTML = `<div class="ol-tree" style="height:${H}px">${tiles.join('')}</div>` +
      (neg ? '<p class="ol-note">Απόλυτες τιμές — οι αρνητικές γραμμές μετρούν με το μέγεθός τους.</p>' : '');
  }
  function tileText(label, v, total, w, h, head) {
    if (w < 54 || h < 22) return '';
    return `<span class="ol-tt${head ? ' is-head' : ''}"><b>${esc(label)}</b>` +
      (h >= 40 || head ? `<small>${fmt(v)}${unit()} · ${nf1.format(v / total * 100)}%</small>` : '') + '</span>';
  }

  // Tooltip διαγράμματος
  const card = $('.ol-chart-card'), tip = $('.ol-tip');
  function tipHtml(el) {
    const t = el.dataset.tip, ch = cube.chart;
    if (t[0] === 'd') {  // ντόνατ / treemap
      const [j, si] = t.slice(1).split('.').map(Number), x = ch.shares.items[j];
      const name = x.r ? dimName(state.rows, x.r.k) : `Λοιπά (${x.n})`;
      let html = `<b>${esc(name)}</b><span>${esc(measureTitle())}<em>${fmt(x.v)}${unit()}</em></span>` +
        `<span>Μερίδιο<em>${nf1.format(x.v / ch.shares.total * 100)}%</em></span>`;
      if (si >= 0 && x.r) {
        const s = x.r.subList[si], v = Math.abs(cube.val(s.total));
        html += `<span class="yr-tip-bal">› ${esc(dimName(state.sub, s.k))}<em>${fmt(v)}${unit()}</em></span>` +
          `<span class="yr-tip-sum">${nf1.format(v / x.v * 100)}% της ομάδας · κλικ για τα παραστατικά</span>`;
      } else if (x.r) html += '<span class="yr-tip-sum">κλικ για εμβάθυνση</span>';
      return html;
    }
    const r = cube.rows[+t], {series, value} = ch;
    const multi = series.length > 1;
    return `<b>${esc(dimName(state.rows, r.k))}</b>` + series.map(([k, l, c]) => {
      const v = value(r, k);
      return Math.abs(v) < 0.005 && multi && ch.type === 'stack' ? '' :
        `<span>${multi ? `<i class="ol-dot c-${c}"></i>` : ''}${esc(l)}<em class="${numCls(v)}">${fmt(v)}${unit()}</em></span>`;
    }).join('') + (multi && state.cols === 'kind' && !cube.count
      ? (v => `<span class="yr-tip-bal${v < 0 ? ' is-neg' : ''}">Εσ. − Εξ.<em>${fmt(v)} €</em></span>`)(value(r, 'in') - value(r, 'ex'))
      : multi && ch.type === 'stack' ? `<span class="yr-tip-bal">Σύνολο<em>${fmt(series.reduce((s, [k]) => s + value(r, k), 0))}${unit()}</em></span>` : '') +
      '<span class="yr-tip-sum">κλικ για εμβάθυνση</span>';
  }
  function showTip(el) {
    tip.innerHTML = tipHtml(el);
    tip.hidden = false;
    const c = card.getBoundingClientRect(), e = el.getBoundingClientRect();
    const x = e.left - c.left + e.width / 2;
    tip.style.left = Math.min(Math.max(x, tip.offsetWidth / 2 + 8), c.width - tip.offsetWidth / 2 - 8) + 'px';
    const below = e.bottom - c.top + 8, above = e.top - c.top - tip.offsetHeight - 8;
    const vertical = ['line'].includes(cube.chart.type) || (cube.chart.type !== 'donut' && cube.chart.type !== 'tree' && (cube.R.time || cube.R.ord));
    tip.style.top = (vertical ? Math.max(8, Math.min(e.top - c.top, c.height - tip.offsetHeight - 8)) : (above > 0 ? above : below)) + 'px';
    const key = el.dataset.tip.split('.')[0];
    card.querySelectorAll('[data-tip]').forEach(x => x.classList.toggle('is-active', x.dataset.tip.split('.')[0] === key));
    card.classList.add('has-active');
  }
  function hideTip() {
    tip.hidden = true;
    card.classList.remove('has-active');
    card.querySelectorAll('[data-tip].is-active').forEach(x => x.classList.remove('is-active'));
  }
  card.addEventListener('mouseover', e => { const el = e.target.closest('[data-tip]'); if (el) showTip(el); });
  card.addEventListener('focusin', e => { const el = e.target.closest('[data-tip]'); if (el) showTip(el); });
  card.addEventListener('mouseleave', hideTip);
  card.addEventListener('focusout', e => { if (!card.contains(e.relatedTarget)) hideTip(); });
  card.addEventListener('keydown', e => {
    const el = e.target.closest('[role="button"]');
    if (el && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); el.click(); }
  });

  // ---------------------------------------------------------------- drill-down & modal παραστατικών
  function drill(i) {
    const r = cube.rows[i];
    const finest = Math.max(-1, ...Object.keys(state.slices).concat(state.rows).map(d => TIME.indexOf(d)));
    const usable = d => d && d !== state.rows && d !== state.cols && !(d in state.slices) && !(TIME.includes(d) && TIME.indexOf(d) <= finest);
    const next = [cube.R.next, ...DRILL_FALLBACK].find(usable);
    if (!next) return openDocs(i, 't');
    state.slices[state.rows] = r.k;
    state.rows = next;
    if (state.sub === next) state.sub = '';
    state.sort = null;
    state.all = false;
    state.open.clear();
    render();
  }

  const dlg = document.getElementById('ol-docs');
  function openDocs(ri, cj, si) {
    const slices = Object.assign({}, state.slices), parts = [];
    if (ri !== 'f') {
      const r = cube.rows[ri];
      slices[state.rows] = r.k;
      parts.push(dimName(state.rows, r.k));
      if (si != null && cube.S) {
        const s = r.subList[si];
        slices[state.sub] = s.k;
        parts.push(dimName(state.sub, s.k));
      }
    }
    let pred = null, mid = cube.mm ? null : state.m;
    if (cj !== 't') {
      const dc = cube.disp[cj], ck = dc.k;
      if (cube.mm) mid = ck;
      else if (ck === REST) { const C = cube.C; pred = (f, d) => cube.rest.has(C.key(f, d)); }
      else if (cube.C) slices[state.cols] = ck;
      parts.push(colLabel(ck));
      if (dc.sk !== undefined) {  // υποστήλη (η στήλη «Λοιπά» δεν αναλύεται, άρα ένα μόνο pred)
        const K = cube.CS.key, extra = cube.csubRest.get(ck);
        if (dc.sk === REST) pred = (f, d) => extra.has(K(f, d)); else slices[state.csub] = dc.sk;
        parts.push(dimName(state.csub, dc.sk));
      }
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

  // ---------------------------------------------------------------- φίλτρα πολλαπλής επιλογής
  const fdlg = document.getElementById('ol-filter');
  const fx = {dim: 'cp', sel: new Set(), q: '', members: []};
  function openFilter(dim) {
    fx.dim = dim || (Object.keys(state.slices).find(k => k !== 'kind') || 'cp');
    loadFilter();
    fdlg.showModal();
    fdlg.querySelector('[data-fq]').focus();
  }
  function loadFilter() {
    const cur = state.slices[fx.dim];
    fx.sel = new Set(cur == null ? [] : [].concat(cur));
    fx.q = '';
    // Μέλη της διάστασης με τα υπόλοιπα φίλτρα ενεργά (όχι το δικό της).
    const sl = Object.assign({}, state.slices);
    delete sl[fx.dim];
    const D = DIMS[fx.dim], map = new Map();
    for (const f of filtered(sl)) {
      const d = docs[f[0]], k = D.key(f, d);
      let x = map.get(k);
      if (!x) map.set(k, x = {k, name: dimName(fx.dim, k), v: 0, n: new Set()});
      x.v += (d.k === 'ex' ? -1 : 1) * f[4];
      x.n.add(f[0]);
    }
    fx.sel.forEach(k => { if (!map.has(k)) map.set(k, {k, name: dimName(fx.dim, k), v: 0, n: new Set()}); });
    fx.members = sortKeys([...map.keys()], fx.dim, k => Math.abs(map.get(k).v)).map(k => map.get(k));
    renderFilter();
  }
  function renderFilter() {
    const D = DIMS[fx.dim], max = Math.max(...fx.members.map(m => Math.abs(m.v)), 1);
    const q = fx.q.toLowerCase(), list = fx.members.filter(m => !q || m.name.toLowerCase().includes(q) || m.k.toLowerCase().includes(q));
    fdlg.innerHTML = `<div class="modal-head"><h2>⚲ Φίλτρο</h2><button type="button" class="btn-ghost btn-sm" data-fclose aria-label="Κλείσιμο">✕</button></div>` +
      `<div class="ol-f-top"><select data-fdim aria-label="Διάσταση">${opts(Object.entries(DIMS).filter(([k]) => k !== 'kind').map(([k, d]) => [k, d.label]), fx.dim)}</select>` +
      `<input type="search" data-fq placeholder="Αναζήτηση σε ${esc(D.label.toLowerCase())}…" value="${esc(fx.q)}" aria-label="Αναζήτηση"></div>` +
      `<div class="ol-f-bar"><span><b>${fx.sel.size}</b> από ${fx.members.length} επιλεγμένα</span>` +
      `<button type="button" class="ol-clear" data-fall>Όλα τα εμφανιζόμενα</button><button type="button" class="ol-clear" data-fnone>Κανένα</button></div>` +
      `<ul class="ol-f-list">` + (list.slice(0, 400).map(m =>
        `<li><label><input type="checkbox" value="${esc(m.k)}"${fx.sel.has(m.k) ? ' checked' : ''}>` +
        `<span class="ol-f-n">${esc(m.name)}</span><span class="ol-f-c">${m.n.size} παρ.</span>` +
        `<span class="ol-f-v ${numCls(m.v)}">${nf2.format(m.v)}</span>` +
        `<span class="ol-f-bar-i"><span style="width:${(Math.abs(m.v) / max * 100).toFixed(1)}%"></span></span></label></li>`).join('') ||
        '<li class="ol-empty">Κανένα αποτέλεσμα.</li>') + '</ul>' +
      (list.length > 400 ? `<p class="muted small">Εμφανίζονται 400 από ${list.length} — περιόρισε με την αναζήτηση.</p>` : '') +
      `<p class="muted small ol-f-note">Ποσά: καθαρή αξία, Έσοδα − Έξοδα, με τα υπόλοιπα φίλτρα ενεργά.</p>` +
      `<div class="ol-f-foot"><button type="button" class="btn-ghost" data-fclose>Άκυρο</button><button type="button" data-fapply>Εφαρμογή</button></div>`;
  }
  fdlg.addEventListener('change', e => {
    if (e.target.matches('[data-fdim]')) { fx.dim = e.target.value; loadFilter(); fdlg.querySelector('[data-fq]').focus(); return; }
    if (e.target.type === 'checkbox') {
      if (e.target.checked) fx.sel.add(e.target.value); else fx.sel.delete(e.target.value);
      fdlg.querySelector('.ol-f-bar b').textContent = fx.sel.size;
    }
  });
  fdlg.addEventListener('input', e => {
    if (!e.target.matches('[data-fq]')) return;
    fx.q = e.target.value;
    const pos = e.target.selectionStart;
    renderFilter();
    const inp = fdlg.querySelector('[data-fq]');
    inp.focus();
    inp.setSelectionRange(pos, pos);
  });
  fdlg.addEventListener('click', e => {
    const t = e.target.closest('button');
    if (!t) return;
    if ('fclose' in t.dataset) fdlg.close();
    else if ('fnone' in t.dataset) { fx.sel.clear(); renderFilter(); }
    else if ('fall' in t.dataset) {
      const q = fx.q.toLowerCase();
      fx.members.filter(m => !q || m.name.toLowerCase().includes(q) || m.k.toLowerCase().includes(q)).slice(0, 400).forEach(m => fx.sel.add(m.k));
      renderFilter();
    } else if ('fapply' in t.dataset) {
      const v = [...fx.sel];
      if (!v.length) delete state.slices[fx.dim];
      else state.slices[fx.dim] = v.length === 1 ? v[0] : v;
      state.open.clear();
      fdlg.close();
      render();
    }
  });
  fdlg.addEventListener('close', () => { fdlg.innerHTML = ''; });

  // ---------------------------------------------------------------- CSV (όλες οι γραμμές, για Excel ελληνικό)
  function csv() {
    const {rows, cols, grand, val} = cube, disp = dispCols();
    const n = v => cube.count ? String(v) : v.toFixed(2).replace('.', ',');
    const q = s => '"' + String(s).replace(/"/g, '""') + '"';
    const colHead = c => c.sk !== undefined || c.gt ? colLabel(c.k) + ' › ' + c.label : c.label;
    const head = [cube.R.label, ...(cube.S ? [cube.S.label] : []), ...disp.map(colHead), ...(showTotalCol() ? [totalLabel()] : [])];
    const lines = [head.map(q).join(';')];
    const colKeys = disp.map(c => c.key);
    const line = (a, b, r) => [q(a), ...(cube.S ? [q(b)] : []), ...colKeys.map(k => n(val(r.cells.get(k)))),
      ...(showTotalCol() ? [n(val(r.total))] : [])].join(';');
    rows.forEach(r => {
      lines.push(line(dimName(state.rows, r.k), '', r));
      r.subList.forEach(s => lines.push(line(dimName(state.rows, r.k), dimName(state.sub, s.k), s)));
    });
    lines.push([q('Σύνολο'), ...(cube.S ? [q('')] : []), ...colKeys.map(k => n(val(cols.get(k)))), ...(showTotalCol() ? [n(val(grand))] : [])].join(';'));
    const a = Object.assign(document.createElement('a'), {
      href: URL.createObjectURL(new Blob(['﻿' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8'})),
      download: `olap-${state.rows}${state.sub ? '-' + state.sub : ''}${state.cols ? '-' + state.cols : ''}.csv`,
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
    if (!Array.isArray(state.slices[state.rows])) delete state.slices[state.rows];  // το φίλτρο του drill-down φεύγει
    if (state.sub === state.rows) state.sub = '';
    if (state.csub === state.rows) state.csub = '';
    state.sort = null;
    state.open.clear();
    render();
  });
  selCols.addEventListener('change', () => {
    state.cols = selCols.value;
    if (state.cols in DIMS && !Array.isArray(state.slices[state.cols])) delete state.slices[state.cols];
    if (state.sub === state.cols) state.sub = '';
    if (state.csub === state.cols || !(state.cols in DIMS)) state.csub = '';
    state.copen.clear();
    if (state.sort && state.sort.by[0] !== '@') state.sort = null;
    render();
  });
  // Υποανάλυση στηλών: οι 2 πρώτες στήλες ανοίγουν αμέσως, οι άλλες με ▸ στην επικεφαλίδα.
  const openFirstCols = () => { state.copen.clear(); if (state.csub) { compute(); cube.colKeys.filter(k => cube.colSubs.has(k)).slice(0, 2).forEach(k => state.copen.add(k)); } };
  selCsub.addEventListener('change', () => {
    state.csub = selCsub.value;
    if (state.sort && state.sort.by[0] !== '@') state.sort = null;
    openFirstCols();
    render();
  });
  selSub.addEventListener('change', () => {
    state.sub = selSub.value;
    state.open.clear();
    if (state.sub) cube.rows.slice(0, 3).forEach(r => state.open.add(r.k));  // οι 3 πρώτες ανοιχτές
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
    const t = e.target.closest('button, a[data-year], [role="button"]');
    if (!t || dlg.contains(t) || fdlg.contains(t)) return;
    const d = t.dataset;
    if ('year' in d) {
      e.preventDefault();
      Object.assign(state, d.year ? {from: d.year + '-01-01', to: d.year + '-12-31'} : {from: '', to: ''});
    } else if ('preset' in d) {
      const p = PRESETS[+d.preset];
      Object.assign(state, {rows: p.rows, cols: p.cols, m: p.m, sub: p.sub || '', csub: p.csub || '', chart: p.chart || state.chart,
        slices: p.kind ? {kind: p.kind} : {}, sort: null, all: false});
      state.open.clear();
      openFirstCols();
      if (p.sub) { compute(); cube.rows.slice(0, 3).forEach(r => state.open.add(r.k)); }
    } else if ('swap' in d) {
      if (!(state.cols in DIMS)) return;
      [state.rows, state.cols] = [state.cols, state.rows];
      [state.sub, state.csub] = [state.csub, state.sub];  // οι υποαναλύσεις ακολουθούν τους άξονές τους
      state.sort = null;
      state.open.clear();
      openFirstCols();
      if (state.sub) { compute(); cube.rows.slice(0, 3).forEach(r => state.open.add(r.k)); }
    } else if ('ctoggle' in d) {
      const k = cube.colKeys[+d.ctoggle];
      if (state.copen.has(k)) state.copen.delete(k); else state.copen.add(k);
      if (state.sort && state.sort.by.includes(SEP)) state.sort = null;
      return renderTable();
    } else if ('ctype' in d) {
      state.chart = d.ctype;
    } else if ('kind' in d) {
      if (d.kind) state.slices.kind = d.kind; else delete state.slices.kind;
    } else if ('filter' in d) {
      return openFilter(d.filter);
    } else if ('unslice' in d) {
      delete state.slices[d.unslice];
    } else if ('clear' in d) {
      state.slices = state.slices.kind ? {kind: state.slices.kind} : {};
    } else if ('toggle' in d) {
      const k = cube.rows[+d.toggle].k;
      if (state.open.has(k)) state.open.delete(k); else state.open.add(k);
      return renderTable();
    } else if ('expand' in d) {
      const all = cube.rows.every(r => state.open.has(r.k));
      state.open.clear();
      if (!all) cube.rows.forEach(r => state.open.add(r.k));
      return renderTable();
    } else if ('drill' in d) {
      return drill(+d.drill);
    } else if ('cell' in d) {
      const [ri, cj, si] = d.cell.split(':');
      return openDocs(ri === 'f' ? 'f' : +ri, cj === 't' ? 't' : +cj, si === undefined ? null : +si);
    } else if ('sort' in d) {
      const by = d.sort[0] === '#' ? cube.disp[+d.sort.slice(1)].key : d.sort, cur = state.sort;
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

  // Τα SVG/treemap εξαρτώνται από το πλάτος: νέα σχεδίαση όταν αλλάξει.
  let lastW = 0, rt = 0;
  new ResizeObserver(() => {
    const w = chartBox().clientWidth;
    if (!cube || Math.abs(w - lastW) < 8) return;
    lastW = w;
    clearTimeout(rt);
    rt = setTimeout(() => { if (['line', 'tree'].includes(chartType())) renderChart(); }, 150);
  }).observe(chartBox());

  // Υποανάλυση από το URL: οι 3 πρώτες γραμμές και οι 2 πρώτες στήλες ανοιχτές.
  if (state.sub) { compute(); cube.rows.slice(0, 3).forEach(r => state.open.add(r.k)); }
  openFirstCols();
  render();
})();
