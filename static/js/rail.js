// Рельс жизненного цикла — сквозной визуальный элемент (в таблице и в карточке задачи).
import { esc } from './util.js';
import { RAIL, RAIL_POS, stColor } from './state.js';

export function railHTML(t, big){
  const i = RAIL_POS[t.status] ?? 0, rw = t.status === 'На доработке', n = (t.iterations||[]).length;
  return `<span class="rail ${big ? 'lg' : 'sm'}" style="--sc:${stColor(t.status)}">` + RAIL.map((s,k) => {
    const cls = [k < i ? 'done' : '', k === i ? 'cur' : '', (rw || n) && k === 2 ? 'loop' : ''].join(' ');
    const lab = k === 1 && rw ? 'На доработке' : s;
    return `<span class="step ${cls}" ${big ? '' : `title="${esc(lab)}"`}><i></i>${big ? `<em>${esc(lab)}${k === 2 && n ? `<b>доработок: ${n}</b>` : ''}</em>` : ''}</span>`;
  }).join('') + `</span>`;
}

export const statusCell = t => `<div class="stc">${railHTML(t,false)}<span class="stl" style="color:${stColor(t.status)}">${esc(t.status)}</span></div>`;
