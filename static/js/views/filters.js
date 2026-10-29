// Фильтры реестра и доски: охват «Мои/Все», поиск, сортировка, панель фильтров.
import { esc, uniq } from '../util.js';
import { AUTH } from '../api.js';
import { S, isOver, inScope, myScope, me, canCreate, people, stColor, ST } from '../state.js';

// Все фильтры, кроме статуса и охвата — от них считаются числа на переключателе охвата
export function unscoped(){
  const f = S.f, q = f.q.trim().toLowerCase();
  return S.tasks.filter(t =>
    (f.kind === 'all' || t.kind === f.kind) &&
    (!f.tl || t.teamlead === f.tl) && (!f.ex || t.executor === f.ex || t.substitute === f.ex) &&
    (!f.block || t.block === f.block) && (!f.overdue || isOver(t)) &&
    (!q || [t.id, t.title, t.description, t.result_desc].join(' ').toLowerCase().includes(q)));
}
export const baseFiltered = () => unscoped().filter(inScope);
// Пустой охват «Мои» — своих задач нет совсем; если они есть, но скрыты фильтрами, — обычное «по этим фильтрам»
export const scopeMine = () => S.f.scope === 'mine' && !!me();
export const scopeEmpty = () => scopeMine() && !S.tasks.some(myScope);
export const showAllBtn = '<button class="btn primary" data-act="fscope" data-k="all">Показать все</button>';
export const emptyHTML = () => scopeEmpty()
  ? `<div class="tbl-wrap empty">${AUTH.user?.role === 'teamlead' ? 'В вашем реестре задач нет' : 'Ваших задач нет'}.<br>${showAllBtn}</div>`
  : `<div class="tbl-wrap empty">Задач по этим фильтрам нет.${[scopeMine() ? showAllBtn : '', canCreate() ? '<button class="btn primary" data-act="new">Поставить задачу</button>' : ''].filter(Boolean).map((x,i) => (i ? ' ' : '<br>') + x).join('')}</div>`;
export const filtered = () => baseFiltered().filter(t => !S.f.status.length || S.f.status.includes(t.status));

export function sorted(list){
  const {k, d} = S.sort;
  const val = t => k === 'id' ? +t.id || 0 : k === 'status' ? ST.indexOf(t.status) : k === 'deadline' ? (t.deadline || '9999') : String(t[k] || '').toLowerCase();
  return [...list].sort((a,b) => (val(a) > val(b) ? 1 : val(a) < val(b) ? -1 : 0) * d);
}

export function filtersBar(withStatus=true){
  const f = S.f, base = baseFiltered(), un = unscoped(), sc = me() ? f.scope : 'all';
  const scopes = me() ? [['mine', AUTH.user.role === 'teamlead' ? 'Мой реестр' : 'Мои', un.filter(myScope).length], ['all','Все', un.length]] : [];
  const tls = uniq([...S.tasks.map(t => t.teamlead), ...Object.values(S.cfg.matrix||{}).flatMap(r => Object.entries(r).filter(([,v]) => +v === 0).map(([k]) => k))]);
  const exs = uniq([...S.tasks.map(t => t.executor), ...people().map(p => p.name)]);
  const opt = (list, cur) => list.map(x => `<option ${x === cur ? 'selected' : ''}>${esc(x)}</option>`).join('');
  return `<div class="filters">
    <div class="seg">${[['once','Разовые'],['regular','Регулярные'],['all','Все']].map(([k,l]) => `<button data-act="fkind" data-k="${k}" class="${f.kind === k ? 'on' : ''}">${l}</button>`).join('')}</div>
    ${scopes.length ? `<div class="seg" title="Охват">${scopes.map(([k,l,n]) => `<button data-act="fscope" data-k="${k}" class="${sc === k ? 'on' : ''}">${l} <span class="seg-n">${n}</span></button>`).join('')}</div>` : ''}
    <input type="search" data-f="q" placeholder="Поиск по ID, названию, описанию" value="${esc(f.q)}">
    <select data-f="tl"><option value="">Все тимлиды</option>${opt(tls, f.tl)}</select>
    <select data-f="ex"><option value="">Все исполнители</option>${opt(exs, f.ex)}</select>
    <select data-f="block"><option value="">Все блоки</option>${(S.cfg.blocks||[]).map(b => `<option value="${esc(b.code)}" ${b.code === f.block ? 'selected' : ''}>${esc(b.code)}</option>`).join('')}</select>
    <label class="chk"><input type="checkbox" data-f="overdue" ${f.overdue ? 'checked' : ''}> Просроченные</label>
    <span class="sp"></span>
    ${canCreate() ? '<button class="btn primary" data-act="new">Новая задача</button>' : ''}
  </div>` + (withStatus ? `<div class="chips">${ST.map(s => `<button class="chip ${f.status.includes(s) ? 'on' : ''}" data-act="fst" data-s="${s}" style="--c:${stColor(s)}">${s}<span>${base.filter(t => t.status === s).length}</span></button>`).join('')}</div>` : '');
}
