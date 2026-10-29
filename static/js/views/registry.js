// Реестр — таблица задач.
import { esc, fmt } from '../util.js';
import { S, colorOf, short, blockName, isOver, CLOSED } from '../state.js';
import { sorted, filtered, filtersBar, emptyHTML } from './filters.js';
import { statusCell } from '../rail.js';

export function listView(){
  const rows = sorted(filtered());
  const cols = [['id','ID'],['title','Задача'],['block','Блок'],['executor','Исполнитель'],['deadline','Срок'],['status','Статус'],[null,'Доклад'],[null,'БЗ']];
  const th = cols.map(([k,l]) => k ? `<th data-act="sort" data-k="${k}">${l}${S.sort.k === k ? `<span class="dir">${S.sort.d > 0 ? '▲' : '▼'}</span>` : ''}</th>` : `<th>${l}</th>`).join('');
  const body = rows.map(t => `<tr data-act="open" data-kind="${t.kind}" data-id="${esc(t.id)}" style="--tl:${colorOf(t.teamlead)}">
      <td class="id">${t.kind === 'regular' ? 'Р' : ''}${esc(t.id)}</td>
      <td><div class="t-title">${esc(t.title || 'Без названия')}</div><div class="t-sub">${esc(short(t.teamlead))}${t.regularity ? ', ' + esc(t.regularity) : ''}${t.level ? ', ' + esc(t.level) : ''}</div></td>
      <td title="${esc(blockName(t.block) || t.block_note)}">${esc(t.block === 'other' ? 'другое' : t.block)}</td>
      <td>${esc(short(t.executor))}${t.substitute ? `<span class="sub">зам. ${esc(short(t.substitute))}</span>` : ''}</td>
      <td class="num">${fmt(t.deadline)}${isOver(t) ? '<span class="over-badge">просрочено</span>' : ''}</td>
      <td>${statusCell(t)}</td>
      <td class="num">${t.report_event ? `${esc(t.report_event)}<span class="sub">${fmt(t.report_date)}</span>` : ''}</td>
      <td>${bzCell(t)}</td></tr>`).join('');
  return `<h1>Реестр задач</h1><p class="lead">Каждое действие в карточке задачи сразу сохраняется в общем реестре. Цвет полосы слева — команда тимлида.</p>`
    + filtersBar() + (rows.length ? `<div class="tbl-wrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>` : emptyHTML());
}

export function bzCell(t){
  if(!t.bz_save) return '';
  if(t.bz_save === 'нет') return '<span class="muted">не нужно</span>';
  if(CLOSED.has(t.status) && (t.bz_card || t.cards?.length)) return '<span class="pill ok">в БЗ</span>';
  return t.status === 'Согласована' ? '<span class="pill warn">карточка готовится</span>' : '<span class="muted">да</span>';
}
