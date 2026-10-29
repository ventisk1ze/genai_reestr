// Админ-панель: люди и PIN, команды, справочники (видна и доступна только администратору).
import { esc, clone, uniq } from '../util.js';
import { S, ROLE_LABEL, people, teams, short } from '../state.js';

export function adminView(){
  if(!S.cfgEdit){ S.cfgEdit = clone(S.cfg); S.cfgEdit.people.forEach(p => p._was = p.name); }   // _was — чтобы предупредить о сбросе PIN при переименовании
  const c = S.cfgEdit; c.roles ||= {}; c.matrix ||= {}; c.people ||= []; c.blocks ||= []; c.teams ||= [];
  const roles = Object.entries(ROLE_LABEL).map(([k,l]) => `<label class="fld"><span>${l}</span><input list="dl-people" data-c="roles.${k}" value="${esc(c.roles[k]||'')}"></label>`).join('');
  const active = c.people.filter(p => p.active !== false && p.name.trim()).map(p => p.name.trim());
  const teamOpts = cur => `<option value="">— без команды —</option>` + c.teams.map(x => `<option value="${esc(x.id)}" ${x.id === cur ? 'selected' : ''}>${esc(x.name || 'Без названия')}</option>`).join('');
  const roleOf = n => { const l = c.teams.filter(x => x.lead === n).map(x => x.name), d = c.teams.filter(x => x.deputy === n).map(x => x.name);
    return [...l.map(x => 'тимлид: ' + x), ...d.map(x => 'зам: ' + x)].join(', '); };
  const saved = n => people().find(p => p.name === n);
  const ppl = c.people.map((p,i) => { const sv = saved(p.name);
    const renamed = p._was && p._was !== p.name && saved(p._was)?.has_pin ? p._was : '';
    return `<tr class="${p.active === false ? 'off' : ''}"><td><input data-c="people.${i}.name" value="${esc(p.name)}" placeholder="Фамилия Имя Отчество">${renamed ? `<small class="pill warn" style="display:block;margin-top:4px;white-space:normal">Было «${esc(renamed)}»: после сохранения PIN сбросится — задайте новый</small>` : ''}</td>
      <td><input data-c="people.${i}.email" value="${esc(p.email||'')}" placeholder="login@sberbank.ru"></td>
      <td><input type="color" class="sw" data-c="people.${i}.color" value="${esc(p.color||'#888888')}" title="Цвет команды (для тимлидов)"></td>
      <td class="adm-team"><select data-c="people.${i}.team">${teamOpts(p.team)}</select>${roleOf(p.name.trim()) ? `<small class="muted" style="display:block;margin-top:4px">${esc(roleOf(p.name.trim()))}</small>` : ''}</td>
      <td style="width:1%"><label class="chk"><input type="checkbox" data-cb="people.${i}.active" ${p.active === false ? '' : 'checked'}> активен</label></td>
      <td style="width:1%"><div class="pinc">${!sv ? '<span class="muted" title="Сначала сохраните справочник">PIN — после сохранения</span>'
        : `<span class="pill ${sv.has_pin ? 'ok' : 'warn'}">${sv.has_pin ? 'PIN задан' : 'PIN не задан'}</span><button class="btn sm" data-act="pinset" data-n="${esc(sv.name)}">${sv.has_pin ? 'Новый PIN' : 'Задать PIN'}</button>${sv.has_pin ? `<button class="btn ghost sm danger" data-act="pinreset" data-n="${esc(sv.name)}">Сбросить</button>` : ''}`}</div></td>
      <td style="width:1%"><button class="btn ghost danger" data-act="cdel" data-list="people" data-i="${i}">Удалить</button></td></tr>`; }).join('');
  const pick = (i, k, cur, other) => `<select data-c="teams.${i}.${k}"><option value="">—</option>${uniq([...active, cur]).filter(n => n !== other).map(n => `<option ${n === cur ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>`;
  const teamBoxes = c.teams.map((x,i) => { const lp = c.people.find(p => p.name.trim() === x.lead);
      const mem = c.people.filter(p => p.team === x.id && p.name.trim() && ![x.lead, x.deputy].includes(p.name.trim()));
      const head = [[x.lead, 'тимлид'], [x.deputy, 'заместитель']].filter(([n]) => n).map(([n,r]) => `<li><span class="tm-role">${esc(n)}</span> — ${r}</li>`);
      const n = S.tasks.filter(t => t.team === x.id).length;
      return `<div class="team-box" style="--c:${esc(lp?.color || 'var(--line)')}">
        <label class="fld"><span>Название</span><input data-c="teams.${i}.name" value="${esc(x.name)}" placeholder="Название команды"></label>
        <div class="tm-pick"><label class="fld"><span>Тимлид</span>${pick(i, 'lead', x.lead, x.deputy)}</label><label class="fld"><span>Заместитель</span>${pick(i, 'deputy', x.deputy, x.lead)}</label></div>
        ${head.length || mem.length ? `<ul>${head.join('')}${mem.map(p => `<li>${esc(p.name)}${p.active === false ? ' (неактивен)' : ''}</li>`).join('')}</ul>` : '<span class="muted">в команде пока никого</span>'}
        <div class="tm-foot"><span class="muted">Задач: ${n}</span><button class="btn ghost sm danger" data-act="cdel" data-list="teams" data-i="${i}" ${n ? `disabled title="У команды есть задачи — удалить нельзя"` : ''}>Удалить команду</button></div></div>`; }).join('');
  const loose = c.people.filter(p => p.name.trim() && !c.teams.some(x => x.id === p.team) && !c.teams.some(x => [x.lead, x.deputy].includes(p.name.trim())));
  const blk = c.blocks.map((b,i) => `<tr><td style="width:130px"><input data-c="blocks.${i}.code" value="${esc(b.code)}"></td><td><input data-c="blocks.${i}.name" value="${esc(b.name)}"></td>
      <td style="width:1%"><button class="btn ghost danger" data-act="cdel" data-list="blocks" data-i="${i}">Удалить</button></td></tr>`).join('');
  const named = c.people.filter(p => p.name);
  const mx = named.length && c.blocks.length ? `<div class="tbl-wrap"><table class="matrix"><thead><tr><th>Блок</th>${named.map(p => `<th class="p" title="${esc(p.name)}">${esc(short(p.name))}</th>`).join('')}</tr></thead><tbody>`
    + c.blocks.map(b => `<tr><td title="${esc(b.name)}">${esc(b.code)}</td>` + named.map(p => { const v = c.matrix[b.code]?.[p.name]; const has = v !== undefined && v !== '';
        return `<td><select class="${has && +v === 0 ? 'r0' : ''}" data-m="${esc(b.code)}" data-p="${esc(p.name)}"><option value=""></option>${[0,1,2,3,4,5].map(r => `<option ${has && +v === r ? 'selected' : ''}>${r}</option>`).join('')}</select></td>`; }).join('') + `</tr>`).join('')
    + `</tbody></table></div><p class="legend">0 — тимлид, 1 — ответственный (основной исполнитель), 2 — замещающий, 3 и далее — дополнительные участники.</p>` : `<p class="muted">Заполните людей и блоки — здесь появится матрица.</p>`;
  return `<h1>Админ-панель</h1><p class="lead">Люди и PIN, команды и справочники — видны и доступны только администратору. Любую задачу администратор правит в её карточке, включая статус; в историю пишется «Правка администратором».</p>
    <div class="panel"><h3>Люди</h3><p class="legend" style="margin-bottom:10px">Email нужен для писем по шаблонам регламента, цвет — для команды тимлида. Неактивный сотрудник не может войти. PIN хранится только в виде хеша: посмотреть его нельзя, только задать новый. Переименование сбрасывает PIN и не меняет уже заведённые задачи.</p>
      <div class="tbl-wrap dict-grid adm-people"><table><thead><tr><th>ФИО</th><th>Email</th><th>Цвет</th><th>Команда</th><th></th><th>PIN</th><th></th></tr></thead><tbody>${ppl}</tbody></table></div>
      <button class="btn" style="margin-top:10px" data-act="cadd" data-list="people">Добавить человека</button></div>
    <div class="panel"><h3>Команды</h3><p class="legend">Тимлид и заместитель команды ставят задачи, согласуют и принимают их — права одинаковые. Роль человека следует из команд; состав — колонка «Команда» в таблице людей.</p>
      ${c.teams.length ? `<div class="team-grid">${teamBoxes}</div>` : '<p class="muted" style="margin:6px 0 0">Команд нет — добавьте команду и выберите тимлида.</p>'}
      ${loose.length ? `<p class="legend">Без команды: ${loose.map(p => esc(p.name)).join(', ')}</p>` : ''}
      <button class="btn" style="margin-top:10px" data-act="cadd" data-list="teams">Добавить команду</button></div>
    <div class="panel"><h3>ЦК и роли</h3><div class="grid" style="margin-top:12px"><label class="fld"><span>Название ЦК</span><input data-c="ck_name" value="${esc(c.ck_name||'')}"></label>${roles}</div></div>
    <div class="panel"><h3>Блоки направлений</h3><div class="tbl-wrap dict-grid" style="margin-top:10px"><table><tbody>${blk}</tbody></table></div>
      <button class="btn" style="margin-top:10px" data-act="cadd" data-list="blocks">Добавить блок</button></div>
    <div class="panel"><h3>Матрица участников ЦК</h3><div style="margin-top:10px">${mx}</div></div>
    <div class="panel"><div class="grid" style="grid-template-columns:1fr 1fr"><label class="fld"><span>События регулярного менеджмента, по одному в строке</span><textarea rows="4" data-cl="events">${esc((c.events||[]).join('\n'))}</textarea></label>
      <label class="fld"><span>Варианты регулярности, по одному в строке</span><textarea rows="4" data-cl="regularities">${esc((c.regularities||[]).join('\n'))}</textarea></label></div></div>
    <div class="savebar"><button class="btn ghost" data-act="creset">Отменить изменения</button><button class="btn primary" data-act="csave">Сохранить людей и справочники</button></div>`;
}
