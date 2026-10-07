// Κοινή συμπεριφορά όλων των σελίδων. Τα επιμέρους scripts μένουν στο template τους.
(function () {
  const root = document.documentElement;

  // ---- Θέμα (light/dark) ----
  function currentTheme() {
    return root.dataset.theme ||
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function syncTheme() { root.dataset.themeNow = currentTheme(); }
  syncTheme();
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncTheme);
  document.addEventListener('click', e => {
    if (!e.target.closest('[data-theme-toggle]')) return;
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = next;
    try { localStorage.setItem('theme', next); } catch (_) {}
    syncTheme();
  });

  // ---- Μενού: μάζεμα/ξεδίπλωμα (desktop) με κλικ στο εικονίδιο της εφαρμογής ----
  document.addEventListener('click', e => {
    if (!e.target.closest('.sidebar [data-nav-mini]') || matchMedia('(max-width:1023px)').matches) return;
    e.preventDefault();
    const mini = !('navMini' in root.dataset);
    if (mini) root.dataset.navMini = ''; else delete root.dataset.navMini;
    try { mini ? localStorage.setItem('navMini', '1') : localStorage.removeItem('navMini'); } catch (_) {}
  });

  // ---- Ημερομηνίες: εμφάνιση dd/mm/yyyy, υποβολή ISO (yyyy-mm-dd) όπως πριν ----
  // Καλείται ξανά για περιεχόμενο που φορτώνεται με AJAX (καρτέλες βιβλίων).
  window.initDates = root => {
    if (!window.flatpickr) return;
    flatpickr(root.querySelectorAll('input[type=date]'), {
      locale: 'gr', dateFormat: 'Y-m-d', altInput: true, altFormat: 'd/m/Y',
      allowInput: true, disableMobile: true,
      onReady(_, __, fp) {
        // Το <label for> να δείχνει στο ορατό πεδίο.
        if (fp.input.id) { fp.altInput.id = fp.input.id; fp.input.removeAttribute('id'); }
        fp.altInput.placeholder = 'ηη/μμ/εεεε';
      },
    });
  };
  initDates(document);

  // ---- Βιβλία: οι επικεφαλίδες στηλών κολλάνε ακριβώς κάτω από το σταθερό toolbar ----
  const toolbar = document.querySelector('.ledger-toolbar');
  if (toolbar && window.ResizeObserver) {
    new ResizeObserver(() => root.style.setProperty('--toolbar-h', toolbar.offsetHeight + 'px'))
      .observe(toolbar);
  }

  // ---- Διόρθωση επωνυμίας από τους πίνακες (κοινός κατάλογος ΑΦΜ → επωνυμία) ----
  // Enter/κλικ αλλού = αποθήκευση, Esc = ακύρωση. Ενημερώνει όλες τις γραμμές με ίδιο ΑΦΜ.
  document.addEventListener('click', e => {
    const btn = e.target.closest('.name-edit');
    if (!btn) return;
    const box = btn.closest('.issuer'), b = box.querySelector('.issuer-name');
    const old = b.textContent.trim() === '—' ? '' : b.textContent.trim();
    // Πολλές γραμμές, για να φαίνονται ολόκληρες οι μεγάλες επωνυμίες.
    const input = Object.assign(document.createElement('textarea'), {
      className: 'name-input', value: old, maxLength: 200, placeholder: 'Επωνυμία',
    });
    input.setAttribute('aria-label', 'Επωνυμία για ΑΦΜ ' + box.dataset.vat);
    b.hidden = btn.hidden = true;
    b.after(input);
    input.focus();
    input.select();
    let busy = false;
    const close = () => {
      input.removeEventListener('blur', save);  // η αφαίρεση του πεδίου στέλνει blur → όχι αποθήκευση
      input.remove();
      b.hidden = btn.hidden = false;
    };
    async function save() {
      if (busy || !input.isConnected) return;
      const name = input.value.replace(/\s+/g, ' ').trim();
      if (!name || name === old) return close();
      busy = true;
      input.disabled = true;
      try {
        const r = await fetch('/suppliers/rename', {
          method: 'POST', body: new URLSearchParams({vat: box.dataset.vat, name}),
        });
        const data = await r.json();
        if (!data.ok) throw new Error(data.error);
        const sel = '.issuer[data-vat="' + CSS.escape(box.dataset.vat) + '"] .issuer-name';
        document.querySelectorAll(sel).forEach(n => {
          n.textContent = data.name;
          n.classList.remove('is-saved');
          void n.offsetWidth;  // επανεκκίνηση του animation
          n.classList.add('is-saved');
        });
        close();
      } catch (err) {
        input.disabled = false;
        input.classList.add('is-error');
        input.title = err.message || 'Σφάλμα αποθήκευσης';
      } finally {
        busy = false;
      }
    }
    input.addEventListener('keydown', ev => {
      // Enter μέσα στη φόρμα του βιβλίου δεν πρέπει να υποβάλει τη μαζική ενέργεια.
      if (ev.key === 'Enter') { ev.preventDefault(); save(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); close(); }
    });
    input.addEventListener('blur', save);
  });

  // ---- Εμφάνιση/απόκρυψη πλαισίου: <button data-toggle="#id"> ----
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-toggle]');
    if (!b) return;
    const panel = document.querySelector(b.dataset.toggle);
    if (!panel) return;
    panel.hidden = !panel.hidden;
    b.setAttribute('aria-expanded', String(!panel.hidden));
  });

  // ---- Mobile μενού ----
  document.addEventListener('click', e => {
    if (e.target.closest('[data-nav-toggle]')) document.body.classList.toggle('nav-open');
    else if (e.target.closest('.scrim')) document.body.classList.remove('nav-open');
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.body.classList.remove('nav-open');
  });

  // ---- Flash: κλείσιμο (και αυτόματα για τα επιτυχή) ----
  function dismiss(el) {
    el.classList.add('is-gone');
    setTimeout(() => el.remove(), 300);
  }
  document.addEventListener('click', e => {
    const x = e.target.closest('.flash-x');
    if (x) dismiss(x.closest('.flash'));
  });
  document.querySelectorAll('.flash.ok').forEach(el => setTimeout(() => dismiss(el), 6000));

  // ---- Επιβεβαίωση: data-confirm σε κουμπί ή φόρμα ----
  document.addEventListener('click', e => {
    const b = e.target.closest('button[data-confirm]');
    if (b && !confirm(b.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  // ---- Απαιτεί επιλεγμένα: data-require=".sel" ----
  document.addEventListener('click', e => {
    const b = e.target.closest('button[data-require]');
    if (b && !document.querySelector(b.dataset.require + ':checked')) {
      e.preventDefault();
      alert('Επίλεξε τουλάχιστον ένα παραστατικό.');
    }
  });

  // ---- Επιλογή όλων: data-check-all=".sel" ----
  document.addEventListener('change', e => {
    const all = e.target.closest('[data-check-all]');
    if (all) document.querySelectorAll(all.dataset.checkAll).forEach(c => { c.checked = all.checked; });
    // Αυτόματη υποβολή (π.χ. επιλογή αρχείου εισαγωγής).
    if (e.target.matches('[data-autosubmit]')) e.target.form.requestSubmit();
  });

  // ---- Εμφάνιση μυστικού κλειδιού όσο έχει focus ----
  document.addEventListener('focusin', e => { if (e.target.matches('[data-reveal]')) e.target.type = 'text'; });
  document.addEventListener('focusout', e => { if (e.target.matches('[data-reveal]')) e.target.type = 'password'; });

  // ---- Εταιρεία της σελίδας σε κάθε POST: ο server απορρίπτει αν άλλαξε από άλλη καρτέλα ----
  // window + capture: τρέχει πριν από κάθε άλλο submit handler (και τα ajax με new FormData(form)).
  window.addEventListener('submit', e => {
    const form = e.target, cid = document.querySelector('meta[name=active-company]')?.content;
    if (form.method.toLowerCase() !== 'post' || !cid) return;
    let inp = form.querySelector(':scope > input[name=_cid]');
    if (!inp) { inp = Object.assign(document.createElement('input'), {type: 'hidden', name: '_cid'}); form.prepend(inp); }
    inp.value = cid;
  }, true);

  // ---- Υποβολή: επιβεβαίωση φόρμας + ένδειξη φόρτωσης ----
  // Δεν κάνουμε το κουμπί disabled: θα χανόταν το name/value του (π.χ. action=manual).
  document.addEventListener('submit', e => {
    const form = e.target;
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) { e.preventDefault(); return; }
    if (form.method.toLowerCase() === 'get') return;
    // Φόρμα που κατεβάζει αρχείο: η σελίδα δεν αλλάζει, άρα καμία ένδειξη φόρτωσης·
    // μετά την υποβολή κλείνει ο διάλογος και σβήνουν τα πεδία (π.χ. κωδικοί).
    if ('download' in form.dataset) {
      setTimeout(() => { form.reset(); form.closest('dialog')?.close(); });
      return;
    }
    if (e.submitter) e.submitter.classList.add('is-loading');
    document.body.classList.add('is-busy');
  });
  // ---- Αριθμοί που «μετράνε» μέχρι την τιμή τους: data-num (ποσό €) / data-int (ακέραιος) ----
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches && !('samePage' in root.dataset)) {
    const money = new Intl.NumberFormat('el-GR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    // Η τελική τιμή γράφεται σε κάθε περίπτωση στο τέλος (και αν τα frames παγώσουν,
    // π.χ. κρυφή καρτέλα) — ποτέ δεν μένει «0,00» στην οθόνη.
    const run = (el, to, fmt, delay) => {
      const final = el.textContent;
      let t0 = null, done = false;
      setTimeout(() => requestAnimationFrame(function step(t) {
        if (done) return;
        t0 = t0 || t;
        const k = Math.min((t - t0) / 1100, 1);
        el.textContent = k < 1 ? fmt(to * (1 - Math.pow(1 - k, 3))) : final;
        if (k < 1) requestAnimationFrame(step);
      }), delay);
      setTimeout(() => { done = true; el.textContent = final; }, delay + 1300);
    };
    document.querySelectorAll('[data-num]').forEach((el, i) => {
      const to = parseFloat(el.dataset.num);
      if (to) run(el, to, v => money.format(v), 150 + i * 90);
    });
    document.querySelectorAll('[data-int]').forEach(el => {
      const to = parseInt(el.dataset.int, 10);
      if (to) run(el, to, v => Math.round(v), 400);
    });
  }

  // Επιστροφή με «Πίσω» (bfcache): καθάρισε τις ενδείξεις.
  addEventListener('pageshow', () => {
    document.body.classList.remove('is-busy', 'nav-open');
    document.querySelectorAll('.is-loading').forEach(b => b.classList.remove('is-loading'));
  });
})();
