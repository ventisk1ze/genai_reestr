// Общий рендер: шапка, навигация, текущий вид; загрузка реестра с сервера.
import { $, esc } from './util.js';
import { api, AUTH } from './api.js';
import { S, people, isAdmin, CLOSED, isOver, today, short } from './state.js';
import { busy, toast } from './ui.js';
import { icon, NAV_ICONS } from './icons.js';
import { bzQueue } from './views/knowledge.js';
import { listView } from './views/registry.js';
import { boardView } from './views/board.js';
import { reportsView } from './views/reports.js';
import { bzView } from './views/knowledge.js';
import { adminView } from './views/admin.js';
import { logView } from './views/log.js';
import { showGate } from './gate.js';
import { bbLoadStatus } from './bb.js';
import { kbLoadStatus } from './kb.js';

export function applyState(st){ S.cfg = st.config || null; S.tasks = st.tasks || []; S.cards = st.cards || []; S.meta = st.meta || {}; S.log = null; }

export async function load(withSync){
  busy('Загружаю реестр…');
  try{ applyState(withSync ? await api('/api/sync', {}) : await api('/api/state')); }
  catch(e){ toast(e.message, 'err'); if(!S.cfg){ try{ applyState(await api('/api/state')); }catch{} } }
  finally{ busy(); render(); if(!AUTH.user) showGate(); else { bbLoadStatus(); kbLoadStatus(); } }   // BB и БЗ: готовы ли (T-010, T-011)
}

export function render(){
  $('#ckname').textContent = 'Реестр задач ' + (S.cfg?.ck_name || 'ЦК');
  const m = S.meta;
  $('#meta').innerHTML = m.storage ? `<span class="tag ${m.storage === 'json' ? 'warn' : ''}" title="${esc(m.storage_info || '')}">${m.storage === 'gp' ? 'Greenplum' : m.storage === 'json' ? 'локальные JSON, БД выключена' : 'SQLite'}</span>` : '';
  $('#me').innerHTML = AUTH.user ? `<span>вошёл как <b>${esc(isAdmin() ? AUTH.user.name : short(AUTH.user.name))}</b></span><button class="btn ghost sm" data-act="logout">Выйти</button>` : '';
  $('#dl-people').innerHTML = people().map(p => `<option value="${esc(p.name)}">`).join('');
  renderNav();
  if(S.view === 'dict') S.view = 'admin';   // «Справочники» переехали в админ-панель
  const v = {list:listView, board:boardView, reports:reportsView, bz:bzView, admin: isAdmin() ? adminView : listView, log:logView}[S.view] || listView;
  $('#main').innerHTML = S.cfg ? v() : `<div class="empty">Реестр не загрузился. Проверьте, что сервер запущен (bash start.sh, лог /tmp/ck_registry.log), и нажмите «Обновить».</div>`;
}

export function renderNav(){
  const over = S.tasks.filter(isOver).length;
  const active = S.tasks.filter(t => !CLOSED.has(t.status)).length;
  const bzq = bzQueue().length;
  const rep = S.tasks.filter(t => t.report_event && (t.report_date||'') >= today()).length;
  const it = (k, l, c, bad) => `<button class="${S.view === k ? 'on' : ''}" data-act="nav" data-v="${k}">${icon(NAV_ICONS[k], 'ico')}<span class="lbl">${l}</span>${c !== '' ? `<span class="cnt ${bad ? 'bad' : ''}">${c}</span>` : ''}</button>`;
  $('#nav').innerHTML = it('list','Реестр', active, false) + it('board','Доска', over ? over : '', !!over)
    + it('reports','Доклады', rep || '', false) + it('bz','База знаний', bzq || '', false)
    + '<div class="sect">' + (isAdmin() ? 'Настройка и история' : 'История') + '</div>' + (isAdmin() ? it('admin','Админ-панель','',false) : '') + it('log','Журнал','',false)
    + '<div class="credit">Разработано Artem Serdyuk и Claude Code</div>';
}
