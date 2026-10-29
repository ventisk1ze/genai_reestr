// Главная страница: карта действий data-act, обработчики событий, запуск.
// Это вход ES-модулей (единственный <script type="module"> в templates/index.html).
import { $, clone, setPath } from './util.js';
import { api } from './api.js';
import { S, store, teams, PALETTE, short } from './state.js';
import { busy, toast, ask } from './ui.js';
import { render, applyState, load } from './render.js';
import { showGate, login, logout, restoreSession } from './gate.js';
import { openTask, closeDrawer, prepared, saveEdits, renderDrawer, collect, isDirty } from './task-card.js';
import { cardDialog } from './kb-card.js';
import { bbUpload } from './bb.js';
import { MAIL_REC, openMail } from './mail.js';
import { aiSend, aiExamples, aiReset, aiQToggle, aiGrow, aiRender, aiToggle,
         aiToTask, aiToResult, aiToCard, aiTakeOpen, aiClearQueries } from './ai.js';
import { AI } from './ai-state.js';
import { aiFilesRender, initFileDrop } from './ai-files.js';
import { initFloating } from './floating.js';

/* ---------- фильтры: что хранится в localStorage (охват — нет) ---------- */
const storeF = () => { const {scope, mine, ...f} = S.f; store.set('f', f); };
const saveF = () => { storeF(); render(); };

/* ---------- карта действий data-act ---------- */
const ACT = {
  nav: el => { S.view = el.dataset.v; store.set('view', S.view); render(); document.querySelector('#main').scrollTop = 0; },
  sync: () => load(true),
  fkind: el => { S.f.kind = el.dataset.k; saveF(); },
  fscope: el => { S.f.scope = el.dataset.k; render(); },
  fst: el => { const s = el.dataset.s, a = S.f.status; S.f.status = a.includes(s) ? a.filter(x => x !== s) : [...a, s]; saveF(); },
  sort: el => { const k = el.dataset.k; S.sort = {k, d: S.sort.k === k ? -S.sort.d : (k === 'id' ? -1 : 1)}; store.set('sort', S.sort); render(); },
  new: () => openTask(S.f.kind === 'regular' ? 'regular' : 'once', null),
  open: el => openTask(el.dataset.kind, el.dataset.id),
  close: () => { if(!document.querySelector('#dlg').open) closeDrawer(); },
  flow: el => S._acts[+el.dataset.i]?.run(),
  save: () => saveEdits(),
  mailmenu: () => document.querySelector('#mailMenu')?.classList.toggle('open'),
  mail: el => { document.querySelector('#mailMenu')?.classList.remove('open'); openMail(prepared(), el.dataset.stage || MAIL_REC[S.edit.status] || 'new'); },
  newcard: () => cardDialog(''),
  card: el => cardDialog(el.dataset.path),
  'bb-upload': () => bbUpload(),
  cadd: el => { const l = el.dataset.list, c = S.cfgEdit;
    const nid = () => 't' + (Math.max(0, ...[...c.teams, ...teams()].map(x => +String(x.id).replace(/^t/, '') || 0)) + 1);
    c[l].push(l === 'people' ? {name:'', email:'', color: PALETTE[c.people.length % PALETTE.length], team:'', active:true} : l === 'teams' ? {id:nid(), name:'', lead:'', deputy:''} : {code:'', name:''}); render(); },
  cdel: el => { const l = el.dataset.list, c = S.cfgEdit, x = c[l][+el.dataset.i];
    if(l === 'teams' && S.tasks.some(t => t.team === x.id)) return toast(`Команду «${x.name}» нельзя удалить: у неё есть задачи`, 'err');
    c[l].splice(+el.dataset.i, 1);
    if(l === 'people') c.teams.forEach(t => ['lead','deputy'].forEach(k => { if(t[k] && t[k] === x.name.trim()) t[k] = ''; }));
    render(); },
  creset: () => { S.cfgEdit = null; render(); },
  ai: () => aiToggle(),
  'ai-send': () => aiSend(),
  'ai-ex': el => aiSend(aiExamples()[+el.dataset.i]),
  'ai-task': el => aiToTask(+el.dataset.i),
  'ai-result': el => aiToResult(+el.dataset.i, +el.dataset.j),
  'ai-card': el => aiToCard(+el.dataset.i, +el.dataset.j),
  'ai-take': () => aiTakeOpen(),
  'ai-reset': () => { aiReset(); },
  'ai-qtoggle': () => aiQToggle(),
  'ai-q': el => { const t = document.querySelector('#aiIn'); t.value = AI.queries[+el.dataset.i] || ''; aiQToggle(false); aiGrow(); t.focus(); },
  'ai-qclear': () => { aiClearQueries(); },
  'ai-attach': () => document.querySelector('#aiFileIn').click(),
  'ai-file-rm': el => { AI.files = AI.files.filter(f => f.id !== +el.dataset.i); aiFilesRender(); document.querySelector('#aiIn').focus(); },
  logout: () => logout(),
  pinset: async el => {
    const n = el.dataset.n;
    const r = await ask('PIN для ' + n, [{k:'pin', l:'Новый PIN (4–8 цифр)', t:'password', req:1}, {k:'pin2', l:'Повторите PIN', t:'password', req:1}], 'Задать PIN',
      'Сообщите PIN сотруднику лично. Посмотреть его потом нельзя — только задать новый.');
    if(!r) return;
    if(!/^\d{4,8}$/.test(r.pin)) return toast('PIN — от 4 до 8 цифр', 'err');
    if(r.pin !== r.pin2) return toast('PIN не совпали', 'err');
    busy('Сохраняю PIN…');
    try{ await api('/api/admin/pin', {name:n, pin:r.pin}); applyState(await api('/api/state')); toast('PIN задан: ' + short(n)); }
    catch(e){ toast(e.message, 'err'); } finally{ busy(); render(); }
  },
  pinreset: async el => {
    const n = el.dataset.n;
    if(!confirm(`Сбросить PIN для ${n}? Сотрудник не сможет войти, пока вы не зададите новый.`)) return;
    busy('Сбрасываю PIN…');
    try{ await api('/api/admin/pin', {name:n, reset:true}); applyState(await api('/api/state')); toast('PIN сброшен: ' + short(n)); }
    catch(e){ toast(e.message, 'err'); } finally{ busy(); render(); }
  },
  csave: async () => {
    const c = clone(S.cfgEdit);
    c.people.forEach(p => { if(p._was && p._was !== p.name.trim()) c.teams.forEach(t => ['lead','deputy'].forEach(k => { if(t[k] === p._was) t[k] = p.name.trim(); })); });   // переименование — и в командах
    c.people = c.people.filter(p => p.name.trim()).map(({has_pin, _was, role, ...p}) => ({...p, name:p.name.trim(), email:(p.email||'').trim(), team: p.team || '', active: p.active !== false}));
    c.teams = c.teams.map(t => ({id:t.id, name:(t.name||'').trim(), lead:t.lead||'', deputy:t.deputy||''}));
    c.blocks = c.blocks.filter(b => b.code.trim()).map(b => ({code:b.code.trim(), name:b.name.trim()}));
    const names = new Set(c.people.map(p => p.name)), codes = new Set(c.blocks.map(b => b.code));
    c.matrix = Object.fromEntries(Object.entries(c.matrix||{}).filter(([b]) => codes.has(b))
      .map(([b,r]) => [b, Object.fromEntries(Object.entries(r).filter(([n,v]) => names.has(n) && v !== '' && v != null).map(([n,v]) => [n, +v]))]));
    busy('Сохраняю справочники…');
    try{ await api('/api/config', {config:c, expect_v:S.cfg._v}); S.cfgEdit = null; applyState(await api('/api/state')); toast('Справочники сохранены'); }
    catch(e){ toast(e.message,'err'); } finally{ busy(); render(); }
  }
};

/* ---------- события ---------- */
document.addEventListener('click', e => { if(!e.target.closest('.mailwrap')) document.querySelector('#mailMenu')?.classList.remove('open'); if(!e.target.closest('#aiQList,#aiQBtn')) aiQToggle(false); const el = e.target.closest('[data-act]'); if(el && ACT[el.dataset.act]){ e.preventDefault(); ACT[el.dataset.act](el); } });
document.addEventListener('input', e => {
  const el = e.target;
  if(el.dataset.f === 'q'){ S.f.q = el.value; storeF(); const pos = el.selectionStart; render(); const n = document.querySelector('[data-f="q"]'); n.focus(); n.setSelectionRange(pos,pos); }
  if(el.dataset.c){ setPath(S.cfgEdit, el.dataset.c, el.value); }
  if(el.dataset.cb){ setPath(S.cfgEdit, el.dataset.cb, el.checked); }
  if(el.dataset.cl){ S.cfgEdit[el.dataset.cl] = el.value.split('\n').map(s => s.trim()).filter(Boolean); }
});
document.addEventListener('change', e => {
  const el = e.target;
  if(el.id === 'gateAdm'){ document.querySelector('#gate').dataset.admin = el.checked ? '1' : ''; showGate(); return; }
  if(el.dataset.f && el.dataset.f !== 'q'){ S.f[el.dataset.f] = el.type === 'checkbox' ? el.checked : el.value; saveF(); }
  if(el.dataset.m){ const m = S.cfgEdit.matrix; (m[el.dataset.m] ||= {})[el.dataset.p] = el.value === '' ? '' : +el.value; el.classList.toggle('r0', el.value === '0'); }
  if(el.dataset.c && /\.(name|team|lead|deputy)$/.test(el.dataset.c) || el.dataset.cb) render();
  if(el.dataset.k === 'block'){ collect(); renderDrawer(); }
});
document.addEventListener('keydown', e => {
  if(e.target.id === 'aiIn' && e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); aiSend(); return; }
  if(e.key === 'Escape' && !document.querySelector('#dlg').open){ if(document.querySelector('#aiQList').classList.contains('show')) aiQToggle(false); else if(AI.open) aiToggle(false); else if(S.edit) closeDrawer(); }
});
document.querySelector('#aiIn').addEventListener('input', aiGrow);
document.querySelector('#aiEngine').addEventListener('change', e => store.set('ai.engine', e.target.value));
document.querySelector('#aiSkill').addEventListener('change', e => { store.set('ai.skill', e.target.value); aiRender(); if(AI.msgs.length) toast('Скилл сменён — для чистого результата начните новый диалог'); });
document.addEventListener('submit', e => { if(e.target.id === 'gateForm'){ e.preventDefault(); login(e.target); } });
window.addEventListener('beforeunload', e => { if(isDirty()){ e.preventDefault(); e.returnValue = ''; } });

/* ---------- запуск ---------- */
initFloating();
initFileDrop();
restoreSession().finally(() => load(false));
