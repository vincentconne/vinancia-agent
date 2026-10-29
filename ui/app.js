/*
 * Interface de Vinancia Agent. Toute la logique (banque, trousseau, synchro) est en
 * Python (agent_gui.Bridge) ; cette page ne fait qu'afficher et transmettre.
 *  - JS → Python : window.pywebview.api.<méthode>() (renvoie une promesse) ;
 *  - Python → JS : window.agentEvent(type, données) pour les opérations longues.
 */
'use strict';

const $screen = document.getElementById('screen');
const $layer = document.getElementById('layer');
let api = null;
let state = { connected: false, banks: [], catalog: [] };
let pending = null;        // opération longue en cours : { kind, onDone }

// ─── Outils ──────────────────────────────────────────────────────────────────

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ICON = {
  lock: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
  bolt: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
  eye: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  search: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  check: '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17l9-10"/></svg>',
  checkSm: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17l9-10"/></svg>',
  alert: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  phone: '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M11 18h2"/></svg>',
  sync: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 0 1-15.5 6.3L3 16M3 12a9 9 0 0 1 15.5-6.3L21 8"/><path d="M21 3v5h-5M3 21v-5h5"/></svg>',
  plus: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  trash: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>',
  ext: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></svg>',
};

function logo(bank, size = '') {
  if (bank && bank.logo) return `<span class="logo ${size}"><img src="${esc(bank.logo)}" alt=""></span>`;
  const initials = String(bank?.name || '?').split(/\s+/).slice(0, 2).map((w) => w[0] || '').join('').toUpperCase();
  return `<span class="logo fallback ${size}">${esc(initials)}</span>`;
}

function render(html, { center = false } = {}) {
  $screen.className = `screen${center ? ' center' : ''}`;
  $screen.innerHTML = html;
  const first = $screen.querySelector('[autofocus]');
  if (first) first.focus();
}

function toast(message, kind = 'error') {
  const el = document.createElement('div');
  el.className = `toast ${kind === 'info' ? 'info' : ''}`;
  el.setAttribute('role', kind === 'info' ? 'status' : 'alert');
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

function busy(title, detail = '', note = '') {
  render(`
    <div class="spinner" aria-hidden="true"></div>
    <h1>${esc(title)}</h1>
    <p class="lead" id="busy-detail" aria-live="polite">${esc(detail)}</p>
    ${note ? `<p class="note center-note">${ICON.lock}${esc(note)}</p>` : ''}`, { center: true });
}

function setDetail(text) {
  const el = document.getElementById('busy-detail');
  if (el) el.textContent = text;
}

// Délégation : <button data-action="nom" data-arg="…">
$screen.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action]');
  if (btn && ACTIONS[btn.dataset.action]) ACTIONS[btn.dataset.action](btn.dataset.arg, btn);
});

// ─── Écrans ──────────────────────────────────────────────────────────────────

function showWelcome() {
  render(`
    <div class="hero-mark"><img src="vinancia.png" alt=""></div>
    <h1>Ta banque dans ton budget, automatiquement</h1>
    <p class="lead">Vinancia Agent range tes opérations bancaires dans Vinancia toutes les 6 heures.</p>
    <div class="stack" style="margin:26px 0">
      <div class="feature"><span class="ico">${ICON.lock}</span><div><b>Tes identifiants restent ici</b><span>Dans le trousseau sécurisé de ton ordinateur. Vinancia ne les voit jamais.</span></div></div>
      <div class="feature"><span class="ico">${ICON.bolt}</span><div><b>Une seule fois, en 2 minutes</b><span>Ensuite tout se fait tout seul, même fenêtre fermée.</span></div></div>
      <div class="feature"><span class="ico">${ICON.eye}</span><div><b>Lecture seule</b><span>L’application lit tes opérations, elle ne peut faire aucun virement.</span></div></div>
    </div>
    <div class="grow"></div>
    <button class="btn btn-primary" data-action="pair">Relier à mon compte Vinancia</button>
    <button class="btn btn-ghost" data-action="token">J’ai un jeton d’agent</button>
    <button class="link" data-action="open" data-arg="privacy" style="margin-top:6px">Comment mes données sont protégées</button>`);
}

function showPairing(p) {
  const chars = p.user_code.split('').map((c) => (c === '-' ? '<span class="sep">–</span>' : `<span>${esc(c)}</span>`)).join('');
  render(`
    <h1>Autorise cet ordinateur</h1>
    <p class="lead">Une page Vinancia s’est ouverte dans ton navigateur. Vérifie que le code est le même, puis clique sur « Autoriser ».</p>
    <div class="code" aria-label="Code ${esc(p.user_code)}">${chars}</div>
    <div class="waiting" aria-live="polite"><div class="spinner sm"></div>En attente de ton autorisation…</div>
    <p class="note" style="margin-top:18px">${ICON.lock}Aucun mot de passe n’est échangé : ce code relie seulement cet ordinateur à ton compte Vinancia.</p>
    <div class="grow"></div>
    <div class="btn-row">
      <button class="btn btn-secondary" data-action="reopen">Rouvrir la page</button>
      <button class="btn btn-secondary" data-action="cancel-pair">Annuler</button>
    </div>`);
}

function showPicker() {
  render(`
    <h1>Quelle est ta banque ?</h1>
    <div class="search">${ICON.search}<input type="text" id="q" placeholder="Rechercher" aria-label="Rechercher une banque" autofocus></div>
    <div class="bank-grid" id="grid"></div>
    <div class="row between" style="margin-top:14px">
      <button class="link" data-action="other-bank">Autre banque…</button>
      <span class="note">${ICON.lock}Connexion directe entre cet ordinateur et ta banque</span>
      ${state.banks.length ? '<button class="link" data-action="home">Retour</button>' : ''}
    </div>`);
  const grid = document.getElementById('grid');
  const draw = (term) => {
    const t = term.trim().toLowerCase();
    grid.innerHTML = state.catalog
      .filter((b) => !t || b.name.toLowerCase().includes(t) || b.id.includes(t))
      .map((b) => `<button class="bank-tile" data-action="pick" data-arg="${esc(b.id)}">${logo(b, 'lg')}<span>${esc(b.name)}</span></button>`)
      .join('') || '<p class="muted small">Aucune banque ne correspond. Essaie « Autre banque ».</p>';
  };
  draw('');
  const q = document.getElementById('q');
  q.addEventListener('input', () => draw(q.value));
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') { const b = grid.querySelector('.bank-tile'); if (b) b.click(); } });
}

// Banques que Vinancia connaît déjà pour ce compte (synchro précédente) : proposées après une réinstallation.
function remainingKnown() {
  const have = new Set((state.banks || []).map((b) => b.module));
  return (state.known || []).filter((b) => !have.has(b.id));
}

function showReconnect() {
  const list = remainingKnown();
  const many = list.length > 1;
  render(`
    <h1>Content de te revoir !</h1>
    <p class="lead">Tu synchronisais déjà ${many ? 'ces banques' : 'cette banque'} avec Vinancia. Reconnecte-${many ? 'les' : 'la'} en quelques secondes :
      tes comptes et ton historique sont déjà dans Vinancia, rien ne sera dupliqué.</p>
    <div class="stack" style="margin-top:18px">
      ${list.map((b) => `<button type="button" class="bank-row bank-pick" data-action="pick" data-arg="${esc(b.id)}">${logo(b, 'lg')}
        <div class="info"><b>${esc(b.name)}</b><span class="muted small">Reconnecter</span></div></button>`).join('')}
    </div>
    <div class="grow"></div>
    <button class="btn btn-ghost" data-action="picker">Ajouter une autre banque</button>
    <button class="link" style="margin-top:6px" data-action="skip-known">Plus tard</button>`);
}

function showForm(bank) {
  const fields = bank.fields.map((f) => {
    const input = f.type === 'select'
      ? `<select id="f-${esc(f.id)}" name="${esc(f.id)}">${f.choices.map((c) => `<option value="${esc(c.value)}" ${c.value === f.default ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}</select>`
      : `<input id="f-${esc(f.id)}" name="${esc(f.id)}" type="${f.type === 'password' ? 'password' : 'text'}" value="${esc(f.default || '')}" autocomplete="off" spellcheck="false">`;
    return `<div class="field" id="w-${esc(f.id)}"><label for="f-${esc(f.id)}">${esc(f.label)}</label>${input}<div class="err" id="e-${esc(f.id)}"></div></div>`;
  }).join('');
  render(`
    <div class="row" style="margin-bottom:6px">${logo(bank, 'lg')}<div><h1 style="margin:0">${esc(bank.name)}</h1><p class="muted small">Tes identifiants habituels de banque en ligne</p></div></div>
    ${bank.hint ? `<div class="callout" style="margin-top:16px">${esc(bank.hint)}</div>` : ''}
    <form id="bank-form" class="stack" style="margin-top:18px" novalidate>${fields}
      <p class="note">${ICON.lock}Rangés dans le trousseau de cet ordinateur. Vinancia ne les reçoit jamais.</p>
      <p class="note">${ICON.eye}Lecture seule : aucun virement ni ordre possible.</p>
    </form>
    <div class="grow"></div>
    <button class="btn btn-primary" data-action="connect" data-arg="${esc(bank.id)}">Se connecter</button>
    <button class="btn btn-ghost" data-action="picker">Choisir une autre banque</button>`);
  const first = $screen.querySelector('#bank-form input, #bank-form select');
  if (first) first.focus();
  document.getElementById('bank-form').addEventListener('submit', (e) => { e.preventDefault(); ACTIONS.connect(bank.id); });
  $screen.querySelectorAll('#bank-form input').forEach((el) => el.addEventListener('keydown', (e) => { if (e.key === 'Enter') ACTIONS.connect(bank.id); }));
}

// Les comptes trouvés chez la banque : nom, type, solde et ce qui en est repris. Affiché seulement sur cet écran
// (rien de plus que ce que la banque montre déjà à l'utilisateur ; ça ne quitte pas l'appareil).
function accountsList(r) {
  const rows = r.accounts_detail || [];
  if (!rows.length) return '';
  const items = rows.map((a) => {
    let status = '';
    let tech = '';
    if (a.issue) { status = '<span class="acct-warn">Opérations indisponibles pour l’instant</span>'; tech = a.issue; }
    else if (!a.tracked) status = 'Non suivi par Vinancia';
    else if (a.history) status = a.operations ? `${a.operations} opération(s) lue(s)` : 'Aucune opération lue';
    else status = 'Solde suivi';
    const detail = tech ? `<details class="acct-tech"><summary>Détail technique</summary><p>${esc(tech)}</p></details>` : '';
    return `<li class="acct"><span class="acct-ico" aria-hidden="true">${esc((a.type || '?').charAt(0))}</span>`
      + `<div class="acct-main"><b>${esc(a.label)}</b><span>${esc(a.type)} · ${status}</span>${detail}</div>`
      + `<div class="acct-bal">${esc(a.balance)}</div></li>`;
  }).join('');
  const hasHistory = rows.some((a) => a.history);
  const issues = rows.filter((a) => a.issue).length;
  const note = issues
    ? '<p class="note soft">Les soldes sont bien suivis. Les opérations seront réessayées à la prochaine synchro.</p>'
    : r.operations === 0
    ? `<p class="note soft">${hasHistory
      ? 'Ta banque n’a renvoyé aucune opération pour tes comptes courants.'
      : 'Seuls les comptes courants remontent leurs opérations ; les livrets et placements remontent leur solde.'}</p>`
    : '';
  return `<ul class="accts">${items}</ul>${note}`;
}

function showDone(r) {
  const plural = (n, one, many) => `${n} ${n > 1 ? many : one}`;
  const todo = [];
  if (r.accounts_pending) {
    todo.push({
      title: plural(r.accounts_pending, 'compte à relier', 'comptes à relier'),
      text: r.accounts_pending > 1
        ? 'Ils ressemblent à des comptes que tu as déjà dans Vinancia : les relier, les remplacer ou les ajouter ?'
        : 'Il ressemble à un compte que tu as déjà dans Vinancia : le relier, le remplacer ou l’ajouter ?',
    });
  }
  if (r.suggestions) {
    todo.push({
      title: plural(r.suggestions, 'opération à comparer', 'opérations à comparer'),
      text: `${r.suggestions > 1 ? 'Elles ressemblent' : 'Elle ressemble'} à ${r.suggestions > 1 ? 'des dépenses' : 'une dépense'} que tu avais saisie${r.suggestions > 1 ? 's' : ''}. Rendez-vous sur Vinancia, où tu vois tes saisies : même dépense ou pas ?`,
    });
  }
  const todoCards = todo.map((t) => `
    <div class="todo">
      <span class="todo-ico" aria-hidden="true">${ICON.eye}</span>
      <div><b>${t.title}</b><p>${t.text}</p></div>
    </div>`).join('');
  const autoCard = r.auto_linked ? `
    <div class="good">
      <span class="good-ico" aria-hidden="true">${ICON.checkSm}</span>
      <div><b>${plural(r.auto_linked, 'opération reliée', 'opérations reliées')} d’office</b>
      <p>Même jour, même montant qu’une dépense que tu avais saisie : rien n’est compté en double.</p></div>
    </div>` : '';
  const toReview = r.accounts_pending || r.suggestions;
  const failed = (r.report || []).filter((x) => x.status !== 'ok');
  const failNote = failed.length
    ? `<div class="callout warn">⚠️ ${failed.map((x) => `<b>${esc(x.name)}</b> : ${esc(x.message || 'échec')}`).join('<br>')}</div>`
    : '';
  const stats = [
    { n: r.accounts, label: r.accounts > 1 ? (failed.length ? 'comptes trouvés' : 'comptes reliés') : (failed.length ? 'compte trouvé' : 'compte relié') },
    { n: r.operations, label: r.operations > 1 ? 'opérations importées' : 'opération importée' },
  ];
  if (r.positions) stats.push({ n: r.positions, label: r.positions > 1 ? 'positions suivies' : 'position suivie' });
  const statCards = stats.map((x) => `<div class="stat"><b>${x.n}</b><span>${x.label}</span></div>`).join('');
  const accounts = accountsList(r);
  render(`
    <div class="done-head">
      <div class="check sm">${ICON.check}</div>
      <div>
        <h1>${failed.length ? 'Connecté, mais l’import a échoué' : 'C’est prêt !'}</h1>
        <p class="lead">${failed.length
          ? 'Ta banque est ajoutée, mais la première synchro n’a pas abouti. Réessaie depuis l’accueil.'
          : 'Tes comptes se mettent à jour toutes les 6 heures, même fenêtre fermée.'}</p>
      </div>
    </div>
    ${failNote}
    <div class="stats stats-${stats.length}">${statCards}</div>
    ${todoCards || autoCard ? `<div class="cards">${todoCards}${autoCard}</div>` : ''}
    ${accounts ? `<h2 class="section-title">Tes comptes</h2>${accounts}` : ''}
    <p class="note secure">${ICON.lock}<span>Envoyé à Vinancia : soldes, opérations et positions. Jamais tes identifiants ni ton IBAN.</span></p>
    <div class="actions-bar">
      ${toReview
        ? `<button class="btn btn-primary" data-action="open" data-arg="review">${r.suggestions ? 'Comparer dans Vinancia' : 'Vérifier dans Vinancia'}</button>`
        : '<button class="btn btn-primary" data-action="open" data-arg="accounts">Voir mes comptes</button>'}
      <div class="btn-row">
        <button class="btn btn-secondary" data-action="add-bank">Ajouter une banque</button>
        <button class="btn btn-secondary" data-action="home">Terminer</button>
      </div>
    </div>`, { center: false });
}

function showHome() {
  const problems = state.banks.filter((b) => b.status !== 'ok');
  const rows = state.banks.map((b) => `
    <div class="bank-row">
      ${logo(b)}
      <div class="info"><b>${esc(b.name)}</b><span>${b.status === 'ok' ? `Synchronisée ${esc(b.last_sync_label)}` : esc(b.message || 'Action requise')}</span></div>
      <span class="chip ${b.status === 'ok' ? 'ok' : 'warn'}">${b.status === 'ok' ? 'OK' : 'À voir'}</span>
      <button class="icon-btn" data-action="remove" data-arg="${esc(b.key)}" aria-label="Retirer ${esc(b.name)}">${ICON.trash}</button>
    </div>`).join('');
  render(`
    <div class="card status ${problems.length ? 'warn' : ''}">
      <span class="big">${problems.length ? ICON.alert : ICON.checkSm}</span>
      <div>
        <b>${problems.length ? 'Une banque demande ton attention' : 'Synchro automatique active'}</b>
        <p class="muted small">${problems.length ? 'Clique sur « Synchroniser maintenant » pour la valider.' : 'Prochaine synchro dans les 6 heures, même fenêtre fermée.'}</p>
      </div>
    </div>
    <h2>Mes banques</h2>
    ${rows}
    <button class="btn btn-ghost" data-action="picker" style="margin-top:6px">${ICON.plus}Ajouter une banque</button>
    <div class="grow"></div>
    <button class="btn btn-primary" data-action="sync">${ICON.sync}Synchroniser maintenant</button>
    <button class="btn btn-secondary" data-action="open" data-arg="budget" style="margin-top:10px">${ICON.ext}Ouvrir Vinancia</button>
    <div class="footer"><span>${esc(state.user || '')}</span><span><button class="link" data-action="open" data-arg="privacy">Sécurité</button><button class="link" data-action="diagnostic">Diagnostic</button><button class="link danger" data-action="unlink">Délier cet ordinateur</button></span></div>`);
}

// ─── Fenêtres de dialogue (code SMS, validation sur téléphone…) ──────────────

function modal(html, onReady) {
  $layer.innerHTML = `<div class="modal" role="dialog" aria-modal="true"><div class="sheet">${html}</div></div>`;
  const sheet = $layer.querySelector('.sheet');
  onReady(sheet);
  const focusable = sheet.querySelector('input, button');
  if (focusable) focusable.focus();
}
const closeModal = () => { $layer.innerHTML = ''; };

function askUser(q) {
  if (q.kind === 'app') {
    modal(`
      <div class="phone">${ICON.phone}</div>
      <h3 style="text-align:center">Valide sur ton téléphone</h3>
      <p class="muted" style="text-align:center">${esc(q.message)}</p>
      <p class="note center-note">${ICON.lock}Validation entre toi et ta banque : Vinancia n’y a pas accès.</p>
      <div class="stack" style="margin-top:18px">
        <button class="btn btn-primary" id="m-ok">C’est fait</button>
        <button class="btn btn-ghost" id="m-cancel">Annuler</button>
      </div>`, (s) => {
      s.querySelector('#m-ok').onclick = () => { closeModal(); api.answer(q.id, 'ok'); };
      s.querySelector('#m-cancel').onclick = () => { closeModal(); api.answer(q.id, null); };
    });
    return;
  }
  const isRedirect = q.kind === 'redirect';
  modal(`
    <h3>${isRedirect ? 'Connexion à ta banque' : 'Code de sécurité'}</h3>
    <p class="muted">${esc(isRedirect ? 'Une page de ta banque s’est ouverte. Connecte-toi, puis colle ici l’adresse de la page finale.' : q.label)}</p>
    <input type="${q.masked ? 'password' : 'text'}" id="m-input" class="${isRedirect ? '' : 'otp'}" style="margin-top:16px" autocomplete="one-time-code" inputmode="${isRedirect ? 'url' : 'numeric'}" aria-label="${isRedirect ? 'Adresse de la page finale' : 'Code de sécurité'}">
    <p class="note" style="margin-top:10px">${ICON.lock}Transmis uniquement à ta banque, jamais à Vinancia.</p>
    <div class="btn-row" style="margin-top:16px">
      <button class="btn btn-secondary" id="m-cancel">Annuler</button>
      <button class="btn btn-primary" id="m-ok">Valider</button>
    </div>`, (s) => {
    const input = s.querySelector('#m-input');
    const ok = () => { if (input.value.trim()) { closeModal(); api.answer(q.id, input.value.trim()); } };
    s.querySelector('#m-ok').onclick = ok;
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
    s.querySelector('#m-cancel').onclick = () => { closeModal(); api.answer(q.id, null); };
  });
}

function confirmBox(title, text, okLabel, onOk) {
  modal(`
    <h3>${esc(title)}</h3><p class="muted">${esc(text)}</p>
    <div class="btn-row" style="margin-top:18px">
      <button class="btn btn-secondary" id="m-cancel">Annuler</button>
      <button class="btn btn-primary" id="m-ok">${esc(okLabel)}</button>
    </div>`, (s) => {
    s.querySelector('#m-cancel').onclick = closeModal;
    s.querySelector('#m-ok').onclick = () => { closeModal(); onOk(); };
  });
}

// ─── Actions ─────────────────────────────────────────────────────────────────

async function refresh() {
  state = { ...state, ...(await api.init()) };
  document.getElementById('version').textContent = `v${state.version}`;
  if (!state.connected) return showWelcome();
  if (!state.banks.length) {
    // Appli (ré)installée : on propose d'abord les banques déjà synchronisées avec ce compte Vinancia.
    state.known = await api.known_banks();
    return remainingKnown().length ? showReconnect() : showPicker();
  }
  return showHome();
}

const ACTIONS = {
  async pair() {
    busy('Ouverture de Vinancia…');
    const r = await api.start_pairing();
    if (r.error) { toast(r.error); return showWelcome(); }
    showPairing(r);
  },
  reopen() { api.reopen_pairing(); },
  async diagnostic() {
    const { text } = await api.diagnostic();
    modal(`
      <h3>Diagnostic</h3>
      <p class="muted small">Rapport sans mot de passe, identifiant ni numéro de compte. Copie-le et envoie-le au support si une banque pose problème.</p>
      <textarea id="m-diag" readonly rows="10" spellcheck="false" style="margin-top:12px;width:100%;background:var(--field);color:var(--ink);border:1px solid var(--line-strong);border-radius:12px;padding:10px;font:12px/1.4 ui-monospace,Menlo,monospace;user-select:text">${esc(text)}</textarea>
      <div class="btn-row" style="margin-top:14px">
        <button class="btn btn-secondary" id="m-close">Fermer</button>
        <button class="btn btn-primary" id="m-copy">Copier</button>
      </div>`, (s) => {
      s.querySelector('#m-close').onclick = closeModal;
      s.querySelector('#m-copy').onclick = async () => {
        const area = s.querySelector('#m-diag');
        try { await navigator.clipboard.writeText(area.value); toast('Diagnostic copié.', 'info'); }
        catch (e) { area.select(); document.execCommand('copy'); toast('Diagnostic copié.', 'info'); }
      };
    });
  },
  'add-bank'() { return remainingKnown().length ? showReconnect() : showPicker(); },
  async 'skip-known'() { await api.dismiss_known_banks(); state.known = []; showPicker(); },
  'cancel-pair'() { api.cancel_pairing(); showWelcome(); },
  token() {
    modal(`
      <h3>Jeton d’agent</h3>
      <p class="muted">Colle le jeton créé dans Vinancia (Comptes → Synchroniser mes banques → avancé).</p>
      <input type="password" id="m-input" style="margin-top:16px" placeholder="vna_…">
      <div class="btn-row" style="margin-top:16px">
        <button class="btn btn-secondary" id="m-cancel">Annuler</button>
        <button class="btn btn-primary" id="m-ok">Relier</button>
      </div>`, (s) => {
      s.querySelector('#m-cancel').onclick = closeModal;
      s.querySelector('#m-ok').onclick = async () => {
        const r = await api.connect_token(s.querySelector('#m-input').value.trim());
        if (r.error) return toast(r.error);
        closeModal(); refresh();
      };
    });
  },
  picker() { showPicker(); },
  home() { refresh(); },
  async pick(id) {
    busy('Préparation…', 'Téléchargement du module de ta banque (quelques secondes la première fois).');
    const r = await api.prepare_bank(id);
    if (r.error) { toast(r.error); return showPicker(); }
    showForm(r);
  },
  'other-bank'() {
    modal(`
      <h3>Autre banque</h3>
      <p class="muted">Nom du module Woob de ta banque (liste sur woob.tech).</p>
      <input type="text" id="m-input" style="margin-top:16px" placeholder="ex. creditmaritime">
      <div class="btn-row" style="margin-top:16px">
        <button class="btn btn-secondary" id="m-cancel">Annuler</button>
        <button class="btn btn-primary" id="m-ok">Continuer</button>
      </div>`, (s) => {
      s.querySelector('#m-cancel').onclick = closeModal;
      s.querySelector('#m-ok').onclick = () => { const v = s.querySelector('#m-input').value.trim().toLowerCase(); if (v) { closeModal(); ACTIONS.pick(v); } };
    });
  },
  async connect(id) {
    const values = {};
    $screen.querySelectorAll('#bank-form [name]').forEach((el) => { values[el.name] = el.value.trim(); });
    const check = await api.validate_bank(id, values);
    $screen.querySelectorAll('.field').forEach((f) => f.classList.remove('invalid'));
    $screen.querySelectorAll('.err').forEach((e) => { e.textContent = ''; });
    if (check.errors && Object.keys(check.errors).length) {
      Object.entries(check.errors).forEach(([field, message]) => {
        const w = document.getElementById(`w-${field}`); if (w) w.classList.add('invalid');
        const e = document.getElementById(`e-${field}`); if (e) e.textContent = message;
      });
      return;
    }
    const name = state.catalog.find((b) => b.id === id)?.name || id;
    busy(`Connexion à ${name}…`, 'Si ta banque envoie un code ou une demande de validation, une fenêtre va s’ouvrir.',
      'Connexion directe et chiffrée entre cet ordinateur et ta banque, en lecture seule.');
    pending = { kind: 'connect' };
    api.connect_bank(id, values);
  },
  sync() {
    busy('Synchronisation…');
    pending = { kind: 'sync' };
    api.sync_now();
  },
  open(where) { api.open_vinancia(where); },
  remove(key) {
    const bank = state.banks.find((b) => b.key === key);
    confirmBox('Retirer cette banque ?', `${bank ? bank.name : 'Cette banque'} ne sera plus synchronisée. Les opérations déjà envoyées restent dans Vinancia.`, 'Retirer', async () => {
      await api.remove_bank(key); refresh();
    });
  },
  unlink() {
    confirmBox('Délier cet ordinateur ?', 'Les mots de passe enregistrés et la synchro automatique seront supprimés. Tes données dans Vinancia restent.', 'Délier', async () => {
      await api.unlink(); refresh();
    });
  },
};

// ─── Événements envoyés par Python ───────────────────────────────────────────

window.agentEvent = (type, data) => {
  switch (type) {
    case 'paired': refresh(); break;
    case 'pairing_failed': toast(data.message); showWelcome(); break;
    case 'progress': setDetail(data.text); break;
    case 'ask': askUser(data); break;
    case 'done':
      pending = null;
      if (data.kind === 'connect') { state.banks = data.banks; showDone(data); }
      else {
        state.banks = data.banks;
        showHome();
        const failed = data.report.filter((r) => r.status !== 'ok');
        toast(failed.length ? failed.map((r) => `${r.name} : ${r.message}`).join(' · ') : `${data.operations} nouvelle(s) opération(s) envoyée(s) à Vinancia.`, failed.length ? 'error' : 'info');
        if (data.accounts_pending || data.suggestions) toast('Des éléments sont à vérifier dans Vinancia : la page s’ouvre.', 'info');
      }
      break;
    case 'failed':
      closeModal();
      toast(data.message);
      if (pending && pending.kind === 'connect') showPicker(); else refresh();
      pending = null;
      break;
    case 'update':
      document.getElementById('banner').innerHTML = `<div class="banner">Nouvelle version ${esc(data.version)} disponible.<button class="link" id="upd">Télécharger</button></div>`;
      document.getElementById('upd').onclick = () => api.open_vinancia('agent');
      break;
    default: break;
  }
};

function boot() {
  api = window.pywebview.api;
  api.catalog().then((c) => { state.catalog = c; refresh(); });
}
if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener('pywebviewready', boot);
