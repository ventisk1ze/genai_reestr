// Доклады — структура докладов на события регулярного менеджмента.
import { esc, fmt, uniq } from '../util.js';
import { S, today, RESULT_OK, colorOf, short } from '../state.js';

export function reportsView(){
  const rep = S.tasks.filter(t => t.report_event);
  const events = uniq([...(S.cfg.events||[]), ...rep.map(t => t.report_event)]);
  const ready = t => RESULT_OK.has(t.status);
  const blocks = events.map(ev => {
    const list = rep.filter(t => t.report_event === ev).sort((a,b) => (a.report_date||'').localeCompare(b.report_date||''));
    if(!list.length) return `<div class="panel"><h3>${esc(ev)}</h3><p class="muted" style="margin:4px 0 0">Задач с признаком доклада нет. Признак ставится в карточке задачи, в разделе «Результат».</p></div>`;
    const next = list.find(t => (t.report_date||'') >= today())?.report_date;
    const nextList = next ? list.filter(t => t.report_date === next) : [];
    const rows = list.map(t => `<tr data-act="open" data-kind="${t.kind}" data-id="${esc(t.id)}" style="--tl:${colorOf(t.teamlead)}">
      <td class="num">${fmt(t.report_date) || '<span class="muted">без даты</span>'}</td>
      <td class="id">${t.kind === 'regular' ? 'Р' : ''}${esc(t.id)}</td>
      <td><div class="t-title">${esc(t.title)}</div></td><td>${esc(short(t.executor))}</td>
      <td>${ready(t) ? '<span class="pill ok">результат готов</span>' : `<span class="pill ${(t.report_date||'') < today() ? 'bad' : 'warn'}">${esc(t.status)}</span>`}</td></tr>`).join('');
    return `<div class="panel"><div class="ev-head"><h3>${esc(ev)}</h3>${next ? `<span class="muted">ближайший доклад ${fmt(next)}: задач ${nextList.length}, готово ${nextList.filter(ready).length}</span>` : '<span class="muted">будущих дат нет</span>'}</div>
      <div class="tbl-wrap"><table><thead><tr><th>Дата</th><th>ID</th><th>Задача</th><th>Исполнитель</th><th>Готовность</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }).join('');
  return `<h1>Доклады</h1><p class="lead">Структура докладов на события регулярного менеджмента собирается из задач с признаком доклада.</p>` + blocks;
}
