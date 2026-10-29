// Выгрузка карточек в BB (T-010): выгружает прокси сотрудника из его контейнера и с его учёткой BB.
// Статус (/api/bb/status) — после входа; выгрузка не блокирует интерфейс: идёт минуту и дольше.
import { esc, fmtTs, httpUrl, clone, plural } from './util.js';
import { api, AUTH } from './api.js';
import { S, canCard, label, short } from './state.js';
import { toast } from './ui.js';
import { applyState, render } from './render.js';
import { isDirty, renderDrawer } from './task-card.js';
import { aiRender } from './ai.js';
import { AI } from './ai-state.js';

export const BB = {status:null, busy:''};   // busy — 'kind:id' задачи, которая сейчас выгружается
export const bbKey = t => `${t.kind}:${t.id}`;
export const bbCan = t => t.id != null && t.status === 'Согласована' && canCard(t) && (t.cards||[]).length > 0;

export async function bbLoadStatus(){
  if(!AUTH.user){ BB.status = null; return; }
  try{ BB.status = await api('/api/bb/status'); }
  catch(e){ BB.status = {available:false, reason: e.status ? e.message : 'Выгрузка в BB недоступна: нет связи с реестром'}; }
  bbRender();
}

export function bbHTML(t){
  let h = '';
  if(bbCan(t)){
    const st = BB.status, run = BB.busy === bbKey(t);
    const why = run ? 'Создаю ветку и pull request — это может занять минуту и дольше' : BB.busy ? 'Идёт выгрузка другой задачи — дождитесь её'
      : !st ? 'Проверяю, доступна ли выгрузка в BB…' : st.available ? '' : (st.reason || 'Выгрузка в BB недоступна');
    const off = run || !!BB.busy || !st?.available;
    h += `<div class="bb-row"><button class="btn" data-act="bb-upload" ${off ? 'disabled' : ''} title="${esc(why || 'Новая ветка и pull request в репозиторий БЗ — от вашего имени')}">${run ? '<span class="spin"></span>Выгружаю в BB…' : 'Выгрузить карточки в BB'}</button>${why ? `<small class="bb-why">${esc(why)}</small>` : ''}</div>`;
  }
  const list = (t.bb || []).filter(x => x && typeof x === 'object');
  if(list.length) h += `<div class="bb-list"><div class="bb-h">Выгрузки в BB</div><ul class="hist">${[...list].reverse().map(x => {
    const pr = `PR №${esc(x.pr_id)}`;
    return `<li><span class="h">${esc(fmtTs(x.ts))}</span><span>${httpUrl(x.pr_url) ? `<a href="${esc(x.pr_url)}" target="_blank" rel="noopener">${pr}</a>` : pr} <span class="muted">${esc(short(x.by))}${x.branch ? ' · ветка ' + esc(x.branch) : ''}</span></span></li>`;
  }).join('')}</ul></div>`;
  return h;
}

export function bbRender(){ const box = document.querySelector('#bbBox'); if(box && S.edit) box.innerHTML = bbHTML(S.edit); }

export async function bbUpload(){
  const t = S.edit;
  if(!t || !bbCan(t) || BB.busy || !BB.status?.available) return;
  if(isDirty()) return toast('Сначала сохраните изменения в задаче, потом выгружайте карточки в BB', 'warn');
  const n = (t.cards||[]).length, tg = BB.status.target || {};
  const where = tg.repo ? `${tg.repo}${tg.branch ? ` (${tg.branch})` : ''}` : 'репозиторий БЗ';
  // «без изменений» сравнивается с целевой веткой: повтор до слияния прошлого PR даст второй PR с теми же карточками
  const last = (t.bb || []).filter(x => x && typeof x === 'object').at(-1);
  const again = last ? `\n\nКарточки уже выгружались: PR №${last.pr_id}, ${fmtTs(last.ts)}. Если он ещё не влит в ${tg.branch || 'целевую ветку'}, появится второй pull request с теми же карточками.` : '';
  if(!confirm(`Выгрузить ${n} ${plural(n, 'карточку', 'карточки', 'карточек')} в BB: новая ветка и pull request в ${where}?${again}`)) return;
  const kind = t.kind, id = t.id;
  BB.busy = bbKey(t); bbRender();
  try{
    const r = await api('/api/bb/upload', {kind, id});
    toast(`${label(t)}: карточки выгружены в BB — pull request №${r.pr_id}`, '', httpUrl(r.pr_url) ? {l:'Открыть PR', href:r.pr_url} : null);
    try{ applyState(await api('/api/state')); }catch{ if(r.task) S.tasks = S.tasks.map(x => x.kind === kind && String(x.id) === String(id) ? r.task : x); }
    const nt = S.tasks.find(x => x.kind === kind && String(x.id) === String(id));
    if(nt && S.edit && S.edit.kind === kind && String(S.edit.id) === String(id)){
      if(!isDirty()){ S.orig = nt; S.edit = clone(nt); }
      else {   // правки, начатые во время выгрузки, — поверх новой версии задачи: без конфликта версий и без потери
        const mine = Object.keys(S.edit).filter(k => typeof S.edit[k] === 'string' && (S.edit[k]||'').trim() !== (S.orig[k]||'').trim());
        const ed = clone(nt); mine.forEach(k => ed[k] = S.edit[k]);
        const add = 'Pull request в BB: ' + r.pr_url;
        if(mine.includes('bz_card') && !String(ed.bz_card).includes(r.pr_url)) ed.bz_card = ed.bz_card.trim() ? `${ed.bz_card.trim()}; ${add}` : add;
        S.orig = nt; S.edit = ed;
      }
      const top = document.querySelector('#drawer .db')?.scrollTop || 0;   // карточка задачи остаётся на том же месте
      BB.busy = ''; renderDrawer();
      const db = document.querySelector('#drawer .db'); if(db) db.scrollTop = top;
    }
    render(); if(AI.open) aiRender();
  }catch(e){ toast(e.message, 'err'); }
  finally{ BB.busy = ''; bbRender(); }
}
