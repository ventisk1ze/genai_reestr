// База знаний — очередь на карточки и список карточек; у отправленных в БЗ — бейдж с узлом и ссылкой.
import { esc, fmt } from '../util.js';
import { S, colorOf, short } from '../state.js';
import { kbBadge } from '../kb.js';

export function bzQueue(){ return S.tasks.filter(t => t.status === 'Согласована' && t.bz_save === 'да'); }
export const approvedOn = t => [...(t.history||[])].reverse().find(h => /Согласована/.test(h.action||''))?.ts || '';

export function bzView(){
  const q = bzQueue().sort((a,b) => approvedOn(a).localeCompare(approvedOn(b)));
  const rows = q.map(t => `<tr data-act="open" data-kind="${t.kind}" data-id="${esc(t.id)}" style="--tl:${colorOf(t.teamlead)}">
      <td class="id">${t.kind === 'regular' ? 'Р' : ''}${esc(t.id)}</td><td><div class="t-title">${esc(t.title)}</div><div class="t-sub">${esc(t.bz_reason||'')}</div></td>
      <td>${esc(short(t.executor))}</td><td class="num">${fmt(approvedOn(t))}</td><td class="num">${(t.cards||[]).length || ''}</td></tr>`).join('');
  const bbOf = {};
  S.tasks.forEach(t => (t.cards || []).forEach(c => { if(c?.path && c.bb) bbOf[c.path] = c; }));
  const cards = S.cards.map(p => `<li><div class="card-kb-row"><button class="link" data-act="card" data-path="${esc(p)}">${esc(p.replace(/^cards\//,''))}</button>${kbBadge(bbOf[p] || null)}</div></li>`).join('');
  return `<h1>База знаний</h1><p class="lead">Очередь на карточки: согласованные задачи с признаком «сохранять в БЗ — да». Исполнитель (или замещающий) добавляет карточки знаний в карточке задачи и завершает её; тимлид проверяет и принимает.</p>`
    + (q.length ? `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>Задача</th><th>Исполнитель</th><th>Согласована</th><th>Карточек</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="panel muted">Очередь пуста: по согласованным задачам карточки готовить не нужно.</div>`)
    + `<h2>Карточки знаний</h2>` + (cards ? `<div class="panel"><ul style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:5px">${cards}</ul></div>` : `<div class="panel muted">Карточек пока нет. Они создаются из карточки задачи, раздел «База знаний».</div>`);
}
