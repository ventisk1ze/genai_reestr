// Карточка задачи (drawer): поля по этапам, кнопки действий, сохранение и переходы статусов.
// Кнопки — только доступные текущему пользователю; статус и служебные поля выставляет сервер по коду действия.
import { $, esc, clone, fmt, fmtTs, cut } from './util.js';
import { api } from './api.js';
import { S, ST, WAIT_HINT, teams, myTeams, isAdmin, isTL, isEx, canCreate, canCard, editable, canEdit, today, me, label, short } from './state.js';
import { busy, toast, ask } from './ui.js';
import { railHTML } from './rail.js';
import { render, applyState } from './render.js';
import { mailMenu, openMail } from './mail.js';
import { bbHTML } from './bb.js';
import { kbBadge } from './kb.js';
import { aiRender, aiTakeSync } from './ai.js';
import { AI } from './ai-state.js';

export function newTask(kind){
  return {kind, id:null, _v:0, status:'Новая', created:today(), deadline:'', deadline_initial:'', title:'', description:'', block:'', block_note:'', level:'',
    teamlead:me(), team: myTeams().length === 1 ? myTeams()[0] : '', executor:'', substitute:'', materials:'', regularity: kind === 'regular' ? (S.cfg.regularities||[])[1] || '' : '',
    result:'', result_desc:'', report_event:'', report_date:'', done_date:'', bz_save:'', bz_reason:'', bz_notified:'', bz_card:'',
    iterations:[], feedback:[], cycles:[], cards:[], history:[]};
}

export function openTask(kind, id){
  const t = id == null ? newTask(kind) : S.tasks.find(x => x.kind === kind && String(x.id) === String(id));
  if(!t) return toast('Задача не найдена — обновите реестр', 'err');
  S.orig = t; S.edit = clone(t); renderDrawer(); aiTakeSync();
  $('#drawer').classList.add('open'); $('#scrim').classList.add('open'); $('#drawer').setAttribute('aria-hidden','false'); document.body.classList.add('drawer-open');
  setTimeout(() => (id == null ? $('#drawer .title-in') : $('#drawer .x'))?.focus(), 230);
}

export function closeDrawer(force){
  if(!S.edit) return;
  if(!force && isDirty() && !confirm('Есть несохранённые изменения. Закрыть без сохранения?')) return;
  S.edit = S.orig = null; aiTakeSync();
  $('#drawer').classList.remove('open'); $('#scrim').classList.remove('open'); $('#drawer').setAttribute('aria-hidden','true'); document.body.classList.remove('drawer-open');
}

export function collect(){ document.querySelectorAll('#drawer [data-k]').forEach(el => S.edit[el.dataset.k] = el.value); }

export function isDirty(){
  if(!S.edit) return false; collect();
  const keys = Object.keys(S.edit).filter(k => typeof S.edit[k] === 'string');
  return S.edit.id == null ? !!(S.edit.title || S.edit.description) : keys.some(k => (S.edit[k]||'').trim() !== (S.orig[k]||'').trim());
}

export function fld(k, l, o={}){
  const v = S.edit[k] ?? '', id = 'f_' + k, ro = o.ro || !canEdit(k), RO = ro ? ' readonly' : ''; let ctl;
  if(o.t === 'area') ctl = `<textarea id="${id}" data-k="${k}" rows="${o.rows||3}" placeholder="${esc(ro ? '' : o.ph||'')}"${RO}>${esc(v)}</textarea>`;
  else if(o.t === 'date') ctl = `<input type="date" id="${id}" data-k="${k}" value="${esc(v)}"${RO}>`;
  else if(o.t === 'sel'){ let opts = o.opts.map(x => Array.isArray(x) ? x : [x, x || '—']); if(v && !opts.some(([a]) => a === v)) opts.push([v, v]);
    ctl = `<select id="${id}" data-k="${k}"${ro ? ' disabled' : ''}>${opts.map(([a,b]) => `<option value="${esc(a)}" ${a === v ? 'selected' : ''}>${esc(b)}</option>`).join('')}</select>`; }
  else if(o.t === 'person') ctl = `<input id="${id}" data-k="${k}" list="dl-people" value="${esc(v)}" placeholder="${ro ? '' : 'Фамилия Имя Отчество'}" autocomplete="off"${RO}>`;
  else ctl = `<input id="${id}" data-k="${k}" value="${esc(v)}" placeholder="${esc(ro ? '' : o.ph||'')}"${RO}>`;
  return `<label class="fld ${o.w||''} ${ro ? 'fld-ro' : ''}" for="${id}" ${ro ? 'title="На этом этапе вам это поле не изменить"' : ''}><span>${esc(l)}</span>${ctl}${o.hint ? `<small>${esc(o.hint)}</small>` : ''}</label>`;
}

export function renderDrawer(){
  const t = S.edit, reg = t.kind === 'regular', isNew = t.id == null, c = S.cfg, adm = isAdmin();
  S._ed = editable(t);
  const blocks = [['','—'], ...(c.blocks||[]).map(b => [b.code, `${b.code} ${b.name}`]), ['other','Другое (нет в справочнике)']];
  const hist = list => `<ul class="hist">${list.join('')}</ul>`;
  const li = (h, txt) => `<li><span class="h">${h}</span><span>${txt}</span></li>`;
  const sec = [];
  sec.push(`<section><h3>Постановка</h3><div class="grid">
    ${fld('team','Команда',{t:'sel', opts:[['','—'], ...teams().filter(x => adm || !isNew || myTeams().includes(x.id)).map(x => [x.id, x.name])], ro: !adm && !(isNew && myTeams().length > 1), hint: isNew && !adm && myTeams().length > 1 ? 'Вы руководите несколькими командами — выберите, в какую поставить задачу' : ''})}
    ${fld('teamlead','Тимлид',{t:'person', ro:!adm})}${fld('executor','Исполнитель',{t:'person'})}${fld('substitute','Замещающий',{t:'person', hint:'При отпуске, болезни, отсутствии'})}
    ${isAdmin() && !isNew ? fld('status','Статус — правка администратором',{t:'sel', opts:ST, w:'full', hint:'Меняет статус в обход этапов регламента; в историю пишется «Правка администратором»'}) : ''}
    ${fld('created','Дата создания',{t:'date', ro:!adm})}${fld('deadline', reg ? 'Срок текущего цикла' : 'Дедлайн (план)',{t:'date', hint: t.deadline_initial && t.deadline_initial !== t.deadline ? 'Исходный: ' + fmt(t.deadline_initial) : 'С учётом времени на верификацию'})}
    ${reg ? fld('regularity','Регулярность',{t:'sel', opts:['', ...(c.regularities||[])]}) : fld('level','Уровень',{t:'sel', opts:[['','—'],['run','run — организация процесса'],['change','change — изменение процесса'],['disrupt','disrupt — нововведение']]})}
    ${fld('block','Блок',{t:'sel', opts:blocks, w:'two'})}${reg ? fld('level','Уровень',{t:'sel', opts:['','run','change','disrupt']}) : ''}
    ${t.block === 'other' ? fld('block_note','Блок: краткое описание',{w: reg ? 'two' : 'full'}) : ''}
    ${fld('description','Описание задачи',{t:'area', rows:5, w:'full', ph:'Правила для выходного результата, процесс решения, доп. информация, критерии приёмки'})}
    ${fld('materials','Материалы для выполнения',{t:'area', rows:2, w:'full', ph:'Ссылки на СберДиск. Пусто — материалов нет'})}
  </div></section>`);
  if(!isNew) sec.push(`<section><h3>Результат</h3><div class="grid">
    ${fld('result','Результат',{t:'area', rows:2, w:'full', ph:'Ссылка на файл «ID_Краткое название_ФИО» или иное место. Для коммуникаций — дата, с какого ящика, куда и что отправлено'})}
    ${fld('result_desc','Описание результата (что было сделано)',{t:'area', rows:3, w:'full', ph:'Какие шаги выполнены, с какой целью, что получили'})}
    ${fld('report_event','Признак доклада: событие',{t:'sel', opts:['', ...(c.events||[])]})}${fld('report_date','Дата доклада',{t:'date'})}${fld('done_date','Дата завершения (факт)',{t:'date', ro:!adm, hint:'Ставится, когда тимлид принимает задачу'})}
  </div></section>`);
  if(!isNew) sec.push(`<section><h3>База знаний <small>решение — тимлид при согласовании, карточки — исполнитель в «Согласована»</small></h3><div class="grid">
    ${fld('bz_save','Сохранять в БЗ ЦК',{t:'sel', opts:[['','—'],['да','да'],['нет','нет']]})}${fld('bz_reason','Причина',{w:'two', ph:'Почему результат ценен или не ценен для БЗ'})}
    ${fld('bz_card','Факт размещения карточки',{w:'full', ph:'Размещена в АС Ретривер + ссылка'})}
  </div><div class="cards-list">${(t.cards||[]).map(c => { const p = c?.path || '';
    return `<div class="card-kb-row"><button class="link" data-act="card" data-path="${esc(p)}">${esc(p.replace(/^cards\//,''))}</button>${kbBadge(c)}</div>`; }).join('')}
    ${canCard(t) ? '<div><button class="btn" data-act="newcard">Новая карточка знаний</button></div>' : ''}</div><div class="bb-box" id="bbBox">${bbHTML(t)}</div></section>`);
  if(t.iterations?.length) sec.push(`<section><h3>Доработки</h3>${hist(t.iterations.map((x,i) => li(fmt(x.date), `${i+1}. ${esc(x.comment)} <span class="muted">новый срок ${fmt(x.deadline)}</span>`)))}</section>`);
  if(reg) sec.push(`<section><h3>Обратная связь исполнителю <small>не реже раза в квартал</small></h3>${t.feedback?.length ? hist(t.feedback.map(x => li(fmt(x.date), `${esc(x.text)} <span class="muted">${esc(short(x.by))}</span>`))) : '<p class="muted" style="margin:0">Пока не было.</p>'}</section>`);
  if(reg && t.cycles?.length) sec.push(`<section><h3>Прошлые циклы</h3>${hist([...t.cycles].reverse().map(x => li(fmt(x.deadline), `${esc(x.status)}${x.result_desc ? ': ' + esc(cut(x.result_desc,120)) : ''}`)))}</section>`);
  if(t.history?.length) sec.push(`<section><h3>История</h3>${hist([...t.history].reverse().slice(0,20).map(x => li(fmtTs(x.ts), `${esc(x.action)} <span class="muted">${esc(short(x.by))}</span>`)))}</section>`);

  const btns = actions(t).map((a,i) => `<button class="btn ${a.p ? 'primary' : ''}" data-act="flow" data-i="${i}">${esc(a.l)}</button>`).join('');
  // «На доработке» — подсказка всем, вместе с последним замечанием тимлида; на других этапах — только если кнопок нет
  const note = t.status === 'На доработке' && t.iterations?.length ? t.iterations.at(-1).comment || '' : '';
  const hint = isNew ? 'Ставить задачи могут тимлиды и администратор' : WAIT_HINT[t.status] || '';
  const acts = (t.status === 'На доработке' || !btns) && hint
    ? btns + `<span class="wait-hint" ${note ? `title="${esc(note)}"` : ''}>${esc(hint)}${note ? `. Замечание тимлида: «${esc(cut(note, 140))}»` : ''}</span>` : btns;
  const canSave = !isNew && (S._ed === null || S._ed.size > 0);
  $('#drawer').innerHTML = `<div class="dh">
      <div class="dh-top"><span class="kid">${isNew ? (reg ? 'Новая регулярная задача' : 'Новая задача') : (reg ? 'Регулярная задача ' : 'Задача ') + esc(t.id)}</span>
        ${t.updated ? `<span>изменено ${fmtTs(t.updated)}${t.updated_by ? ', ' + esc(short(t.updated_by)) : ''}</span>` : ''}<span class="sp"></span>
        <button class="x" data-act="close" aria-label="Закрыть">×</button></div>
      <input class="title-in" data-k="title" placeholder="Название — суть результата, понятная руководству" value="${esc(t.title)}" ${canEdit('title') ? '' : 'readonly'}>
      ${isNew ? '' : railHTML(t, true)}
    </div>
    <div class="db">${sec.join('')}</div>
    <div class="df">${acts}<span class="sp"></span>
      ${isNew ? '' : mailMenu(t)}
      ${canSave ? `<button class="btn" data-act="save">Сохранить изменения</button>` : ''}</div>`;
  S._acts = actions(t);
}

/* ---------- Действия жизненного цикла ---------- */
export function actions(t){
  if(t.id == null) return canCreate() ? [{l:'Поставить задачу', p:1, run:createTask}] : [];
  const a = [], tl = isAdmin() || isTL(t), ex = isAdmin() || isEx(t);
  switch(t.status){
    case 'Новая': if(ex) a.push({l:'Взять в работу', p:1, run:() => step('take')}); break;
    case 'В работе': case 'На доработке': if(ex) a.push({l:'Отправить на согласование', p:1, run:toReview}); break;
    case 'На согласовании': if(tl) a.push({l:'Согласовать', p:1, run:approve}, {l:'Вернуть на доработку', run:rework}); break;
    case 'Согласована': if(ex) a.push({l:'Завершить', p:1, run:finish}); break;
    case 'Завершена': if(tl) a.push({l:'Принять', p:1, run:() => step('accept', 'accepted')}, {l:'Вернуть', run:giveBack}); break;
  }
  if(t.kind === 'regular' && tl){
    if(t.status === 'Принята') a.push({l:'Начать новый цикл', run:newCycle});
    a.push({l:'Обратная связь', run:feedback});
  }
  return a;
}

export function prepared(){ collect(); const t = clone(S.edit); for(const k in t) if(typeof t[k] === 'string') t[k] = t[k].trim(); return t; }

function require(t, fields, why){
  const miss = fields.filter(([k]) => !t[k]);
  document.querySelectorAll('#drawer .bad-field').forEach(e => e.classList.remove('bad-field'));
  if(!miss.length) return true;
  miss.forEach(([k]) => document.querySelector(`#drawer [data-k="${k}"]`)?.classList.add('bad-field'));
  document.querySelector(`#drawer [data-k="${miss[0][0]}"]`)?.focus();
  toast(`${why}: ${miss.map(([,l]) => l).join(', ')}`, 'err');
  return false;
}

// code — код действия (контракт /api/task: create, edit, take, to_review, approve, rework, finish, accept, return, new_cycle, feedback)
async function commit(t, code, params, mailStage, action){
  busy('Сохраняю…');
  try{
    const r = await api('/api/task', {task:t, expect_v: S.orig?._v ?? null, action_code:code, params: params || {}, ...(action ? {action} : {})});
    applyState(await api('/api/state'));
    const nt = S.tasks.find(x => x.kind === r.task.kind && String(x.id) === String(r.task.id)) || r.task;
    S.orig = nt; S.edit = clone(nt); renderDrawer(); render(); if(AI.open) aiRender();   // кнопки переноса зависят от статуса
    const done = (nt.history || []).at(-1)?.action || 'сохранено';
    const ms = typeof mailStage === 'function' ? mailStage(nt) : mailStage;
    toast(`${label(nt)}: ${done.charAt(0).toLowerCase() + done.slice(1)}`, '', ms ? {l:'Открыть письмо', run:() => openMail(nt, ms)} : null);
  }catch(e){ toast(e.message, 'err'); }
  finally{ busy(); }
}

export async function step(code, mailStage, params){ await commit(prepared(), code, params, mailStage); }

export async function createTask(){
  const t = prepared();
  if(!isAdmin()) t.teamlead = me();
  if(!require(t, [['title','название'],['teamlead','тимлид'],...(!isAdmin() && myTeams().length > 1 ? [['team','команда']] : []),['executor','исполнитель'],['deadline', t.kind === 'regular' ? 'срок цикла' : 'дедлайн']].concat(t.kind === 'regular' ? [['regularity','регулярность']] : []), 'Для постановки заполните')) return;
  await commit(t, 'create', t.team ? {team:t.team} : null, 'new', 'Поставлена');
}

export async function toReview(){
  const t = prepared();
  if(!require(t, [['result','результат'],['result_desc','описание результата']], 'Для согласования заполните')) return;
  await commit(t, 'to_review', null, 'review');
}

export async function approve(){
  const t = prepared();
  const need = [['block','блок'],['level','уровень'],['bz_save','сохранять в БЗ'],['bz_reason','причина по БЗ']];
  if(t.block === 'other') need.push(['block_note','описание блока']);
  if(!require(t, need, 'Чтобы согласовать, заполните')) return;
  await commit(t, 'approve', null, 'approved');
}

export async function rework(){
  const t = prepared();
  const r = await ask('Вернуть на доработку', [{k:'comment', l:'Что доработать', t:'area', req:1}, {k:'deadline', l:'Новый срок', t:'date', req:1, v:t.deadline}], 'Вернуть');
  if(!r) return;
  await commit(t, 'rework', {comment:r.comment, deadline:r.deadline}, 'rework');
}

export async function finish(){
  const t = prepared();
  if(!(t.cards||[]).length && !t.bz_card){
    document.querySelector('#drawer [data-k="bz_card"]')?.classList.add('bad-field');
    return toast('Чтобы завершить, добавьте карточку знаний или заполните «Факт размещения карточки»', 'err');
  }
  await commit(t, 'finish', null, 'done');
}

export async function giveBack(){
  const t = prepared(), bz = t.bz_save === 'да';
  const r = await ask('Вернуть исполнителю', [
    {k:'to', l:'Что доработать', t:'sel', v: bz ? 'approved' : 'rework', opts:[...(bz ? [['approved','Базу знаний — задача вернётся в «Согласована»']] : []), ['rework','Результат — задача вернётся «На доработке»']]},
    {k:'comment', l:'Комментарий исполнителю (для доработки результата — обязательно)', t:'area'},
    {k:'deadline', l:'Срок доработки результата', t:'date', v:t.deadline}], 'Вернуть');
  if(!r) return;
  if(r.to === 'rework' && !r.comment) return toast('Напишите, что доработать в результате', 'err');
  await commit(t, 'return', r.to === 'rework' ? {to:'rework', comment:r.comment, deadline:r.deadline} : {to:'approved', comment:r.comment}, r.to === 'rework' ? 'rework' : null);
}

export async function newCycle(){
  const r = await ask('Новый цикл регулярной задачи', [{k:'deadline', l:'Срок нового цикла', t:'date', req:1}], 'Начать', 'Результаты текущего цикла уйдут в «Прошлые циклы», поля результата и базы знаний очистятся.');
  if(!r) return;
  await commit(prepared(), 'new_cycle', {deadline:r.deadline}, 'new');
}

export async function feedback(){
  const r = await ask('Обратная связь исполнителю', [{k:'text', l:'Оценка тимлида', t:'area', req:1}], 'Записать');
  if(!r) return;
  await commit(prepared(), 'feedback', {text:r.text});
}

export async function saveEdits(){
  if(!isDirty()) return toast('Изменений нет');
  await commit(prepared(), 'edit');
}
