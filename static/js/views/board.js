// Доска — задачи по статусам.
import { esc, fmt } from '../util.js';
import { ST, colorOf, short, isOver, stColor } from '../state.js';
import { sorted, filtered, filtersBar, emptyHTML, scopeMine } from './filters.js';

export function boardView(){
  const list = sorted(filtered());
  return `<h1>Доска</h1><p class="lead">Задачи по статусам. Статус меняется действиями в карточке — так соблюдается порядок этапов регламента.</p>`
    + filtersBar(false) + (!list.length && scopeMine() ? emptyHTML() : `<div class="board">` + ST.map(s => {
      const items = list.filter(t => t.status === s);
      return `<div class="col" style="--c:${stColor(s)}"><h4><span>${s}</span><span class="muted">${items.length}</span></h4>` + items.map(t =>
        `<button class="card ${isOver(t) ? 'over' : ''}" data-act="open" data-kind="${t.kind}" data-id="${esc(t.id)}" style="--tl:${colorOf(t.teamlead)}">
          <div class="t-title">${esc(t.title || 'Без названия')}</div>
          <div class="row"><span>${t.kind === 'regular' ? 'Р' : ''}${esc(t.id)}${t.executor ? ', ' + esc(short(t.executor)) : ''}</span><span class="num">${fmt(t.deadline)}</span></div></button>`).join('') + `</div>`;
    }).join('') + `</div>`);
}
