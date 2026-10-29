// Базы знаний (T-012): одна БЗ из настроек владельца — онтология и отправка карточки через прокси.
// Узлы онтологии — папки: карточка уезжает в папку выбранного узла, у неё в реестре — ссылка на файл.
import { $, esc, httpUrl, clone } from './util.js';
import { api, AUTH } from './api.js';
import { S } from './state.js';
import { toast, busy } from './ui.js';
import { applyState, render } from './render.js';
import { isDirty, renderDrawer } from './task-card.js';

export const KB = {status: null, ontology: null};   // онтология единственной БЗ — дерево узлов

export async function kbLoadStatus(){
  if(!AUTH.user){ KB.status = null; return; }
  try{ KB.status = await api('/api/kb/status'); }
  catch(e){ KB.status = {available: false, reason: e.status ? e.message : 'База знаний недоступна: нет связи с реестром'}; }
}

export async function kbOntology(force){
  if(KB.ontology && !force) return KB.ontology;
  const r = await api('/api/kb/ontology');
  KB.ontology = r.nodes || [];
  return KB.ontology;
}

// Отправка карточки в базу знаний: клон (постоянный, у сотрудника) → папка узла → commit → push → pull request
export async function kbSend(t, entry){
  const node = $('#kbNode')?.value || '';
  if(!node) return toast('Выберите узел онтологии', 'warn');
  if(!entry?.path) return toast('Сначала сохраните карточку', 'warn');
  if(isDirty()) return toast('Сначала сохраните изменения в задаче, потом отправляйте карточку', 'warn');
  busy('Отправляю карточку в базу знаний…');
  try{
    const r = await api('/api/kb/upload', {kind: t.kind, id: t.id, path: entry.path, node});
    toast('Карточка отправлена в базу знаний', '',
          httpUrl(r.pr_url) ? {l:'Открыть PR', href:r.pr_url} : null);
    try{ applyState(await api('/api/state')); }catch{}
    const nt = S.tasks.find(x => x.kind === t.kind && String(x.id) === String(t.id));
    if(nt && !isDirty()){ S.orig = nt; S.edit = clone(nt); renderDrawer(); }
    render();
    return r;
  }catch(e){ toast(e.message, 'err'); return null; }
  finally{ busy(); }
}

/* ---------- разметка ---------- */
export function kbNodeOptionsHTML(nodes, cur){
  const out = [];
  const walk = (items, depth) => items.forEach(n => {
    out.push(`<option value="${esc(n.path)}" ${n.path === cur ? 'selected' : ''}>${'&nbsp;&nbsp;'.repeat(depth)}${esc(n.name)}</option>`);
    walk(n.children || [], depth + 1);
  });
  walk(nodes || [], 0);
  return out.join('');
}

export function kbBadge(entry){
  const bb = entry?.bb;
  if(!bb) return '';
  const link = httpUrl(bb.file_url)
    ? `<a href="${esc(bb.file_url)}" target="_blank" rel="noopener" title="Файл в Bitbucket">в БЗ · ${esc(bb.node)}</a>`
    : `в БЗ · ${esc(bb.node)}`;
  return `<span class="kb-badge">${link}</span>`;
}
