// Журнал — последние действия по задачам реестра.
import { esc, fmtTs, cut } from '../util.js';
import { S, short } from '../state.js';

export function logView(){
  const rows = S.tasks.flatMap(t => (t.history || []).map(h => ({...h, t})))
    .sort((a,b) => (b.ts || '').localeCompare(a.ts || '')).slice(0, 300)
    .map(r => `<li data-act="open" data-kind="${r.t.kind}" data-id="${esc(r.t.id)}" style="cursor:pointer"><span class="h">${esc(fmtTs(r.ts))}</span><span class="h">${r.t.kind === 'regular' ? 'Р' : ''}${esc(r.t.id)}</span><span>${esc(r.action)}: ${esc(cut(r.t.title, 80))} <span class="muted">${esc(short(r.by))}</span></span></li>`).join('');
  return `<h1>Журнал</h1><p class="lead">Последние действия по задачам реестра — кто, когда и что сделал. Нажмите на строку, чтобы открыть задачу.</p>`
    + `<div class="panel"><ul class="log">${rows || '<li class="muted">Действий пока нет.</li>'}</ul></div>`;
}
