// Помощник (лава-болл): панель, диалог, рендер сообщений, отправка; скиллы — по ролям (список с сервера, /api/llm).
// Диалог и запросы — на сервере у каждого сотрудника (T-006).
import { $, esc, cut } from './util.js';
import { api, AUTH } from './api.js';
import { S, isAdmin, isEx, isTL, store } from './state.js';
import { toast, busy } from './ui.js';
import { lavaSVG, LAVA_STILL } from './icons.js';
import { AI, AI_POSTANOVKA } from './ai-state.js';
import { aiFilesRender, aiFileChip } from './ai-files.js';
import { applyState, render } from './render.js';
import { openTask, closeDrawer, isDirty, collect, renderDrawer } from './task-card.js';
import { placePanel } from './floating.js';

export async function aiLoadHistory(){
  AI.status = null;   // скиллы зависят от того, кто вошёл
  if(AI.open) await aiLoadStatus();
  try{ const h = await api('/api/ai/history'); AI.msgs = h.msgs || []; AI.queries = h.queries || []; }catch{}
  aiRender(); aiQRender();
}

// При выходе диалог и запросы исчезают с экрана
export function aiForget(){ AI.msgs = []; AI.queries = []; AI.files = []; AI.status = null; aiQToggle(false); aiRender(); }

// «Новый диалог» убирает и файлы, ещё не отправленные с сообщением
export async function aiReset(){
  if(AI.busy) return;
  try{ const h = await api('/api/ai/history', {action:'reset'}); AI.msgs = h.msgs || []; AI.queries = h.queries || []; AI.files = []; }
  catch(e){ toast(e.message, 'err'); return; }
  aiRender(); aiQRender(); $('#aiIn').focus();
}

export async function aiClearQueries(){
  try{ const h = await api('/api/ai/history', {action:'clear_queries'}); AI.queries = h.queries || []; }
  catch(e){ toast(e.message, 'err'); return; }
  aiQRender();
}

export function aiQRender(){
  $('#aiQList').innerHTML = AI.queries.length
    ? AI.queries.map((q,k) => `<button data-act="ai-q" data-i="${k}" role="menuitem" title="${esc(q)}">${esc(cut(q, 90))}</button>`).join('')
      + `<button class="ai-qclear" data-act="ai-qclear" role="menuitem">Очистить историю запросов</button>`
    : `<div class="ai-qempty">Запросов пока нет</div>`;
}

export function aiQToggle(force){
  const on = $('#aiQList').classList.toggle('show', force ?? !$('#aiQList').classList.contains('show'));
  $('#aiQBtn').setAttribute('aria-expanded', String(on));
  if(on) aiQRender();
}

export const aiSkill = () => (AI.status?.skills || []).find(x => x.name === $('#aiSkill').value) || null;
export const aiExamples = () => aiSkill()?.examples || [];

/* ---------- markdown ответов (безопасный: исходник уже экранирован) ---------- */
function mdInline(s){
  return s.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
}

export function md(src){
  const L = esc(src).split('\n'), out = []; let i = 0;
  const isTable = l => /^\s*\|.*\|\s*$/.test(l), isList = l => /^\s*([-*•]|\d+[.)])\s+/.test(l);
  while(i < L.length){
    const l = L[i];
    if(/^\s*```/.test(l)){ const b = []; i++; while(i < L.length && !/^\s*```/.test(L[i])) b.push(L[i++]); i++; out.push(`<pre>${b.join('\n')}</pre>`); continue; }
    if(/^#{1,6}\s/.test(l)){ const n = Math.min(l.match(/^#+/)[0].length + 2, 5); out.push(`<h${n}>${mdInline(l.replace(/^#+\s*/, ''))}</h${n}>`); i++; continue; }
    if(isTable(l)){ const rows = []; while(i < L.length && isTable(L[i])) rows.push(L[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
      out.push('<table>' + rows.filter(r => !/^\s*\|[\s:|-]+\|\s*$/.test(r)).map((r,k) => '<tr>' + cells(r).map(c => k ? `<td>${mdInline(c)}</td>` : `<th>${mdInline(c)}</th>`).join('') + '</tr>').join('') + '</table>'); continue; }
    if(isList(l)){ const ord = /^\s*\d/.test(l), items = []; while(i < L.length && isList(L[i])) items.push(L[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, ''));
      out.push(`<${ord ? 'ol' : 'ul'}>${items.map(x => `<li>${mdInline(x)}</li>`).join('')}</${ord ? 'ol' : 'ul'}>`); continue; }
    if(!l.trim()){ i++; continue; }
    const p = []; while(i < L.length && L[i].trim() && !/^(#{1,6}\s|\s*```)/.test(L[i]) && !isTable(L[i]) && !isList(L[i])) p.push(L[i++]);
    out.push(`<p>${mdInline(p.join('<br>'))}</p>`);
  }
  return out.join('');
}

/* ---------- служебные блоки ответа: ```task```, ```result```, ```card``` ---------- */
function splitBlock(text, kind){
  const m = new RegExp('```' + kind + '\\s*([\\s\\S]*?)```', 'i').exec(text);
  if(!m) return {card:text, meta:null};
  let meta = null; try{ meta = JSON.parse(m[1].trim()); }catch{}
  return {card: text.replace(m[0], '').trim(), meta};
}
export const splitTaskBlock = text => splitBlock(text, 'task');
// Все блоки одного вида (модель может предложить несколько карточек в одном ответе)
function splitBlocks(text, kind){
  const metas = []; let b;
  while((b = splitBlock(text, kind)).meta || b.card !== text){ metas.push(b.meta); text = b.card; }
  return {card:text, metas};
}

// Задача из блока result/card и можно ли перенести: {t, why} — why пусто, если можно
function aiTarget(meta, kind){
  const k = meta.kind === 'regular' ? 'regular' : 'once';
  const t = S.tasks.find(x => x.kind === k && String(x.id) === String(meta.id));
  const n = (k === 'regular' ? 'Рег. ' : '') + meta.id;
  if(!t) return {t, n, why:`Задачи ${n} нет в реестре — обновите реестр`};
  if(!isAdmin() && !isEx(t)) return {t, n, why:`Вы не исполнитель и не замещающий задачи ${n} — переносит исполнитель`};
  const ok = kind === 'result' ? ['В работе','На доработке'] : ['Согласована'];
  if(!ok.includes(t.status)) return {t, n, why: kind === 'result'
    ? `Задача ${n} «${t.status}»: результат вносится в статусах «В работе» и «На доработке»`
    : `Задача ${n} «${t.status}»: карточку прикрепляют, когда задача «Согласована»`};
  return {t, n, why:''};
}

function aiBlockBtn(meta, kind, k, j){
  if(!meta || meta.id == null) return '';
  const {n, why} = aiTarget(meta, kind);
  const text = kind === 'result' ? `Перенести результат в задачу ${n}` : `Прикрепить карточку к задаче ${n}`;
  return `<button class="btn primary to-task" data-act="ai-${kind}" data-i="${k}" data-j="${j}" ${why ? `disabled title="${esc(why)}"` : ''}>${esc(text)}</button>${why ? `<small class="ai-why">${esc(why)}</small>` : ''}`;
}

export function aiRender(){
  const box = $('#aiMsgs');
  let html = '';
  if(AI.status && !AI.status.configured && $('#aiEngine').value === 'api') html += `<div class="ai-off">Ассистент выключен: на сервере не задан GIGACHAT_API_URL. Пропишите его в registry.env и перезапустите start.sh.</div>`;
  if(!AI.msgs.length){
    const sk = aiSkill();
    html += `<div class="ai-intro"><p>${esc(sk?.greeting || 'Выберите скилл помощника и опишите, с чем помочь.')}</p>
      <div class="ai-chips">${aiExamples().map((x,k) => `<button data-act="ai-ex" data-i="${k}">${esc(x)}</button>`).join('')}</div></div>`;
  }
  AI.msgs.forEach((m,k) => {
    if(m.role === 'user') html += `<div class="msg user">${m.files?.length ? `<div class="ai-mfiles">${m.files.map(f => aiFileChip(f, false)).join('')}</div>` : ''}${esc(m.content)}</div>`;
    else if(m.role === 'error') html += `<div class="msg err">${esc(m.content)}</div>`;
    else { const {card, meta} = splitTaskBlock(m.content), r = splitBlocks(card, 'result'), c = splitBlocks(r.card, 'card');
      html += `<div class="row-bot"><span class="ava">${LAVA_STILL}</span><div class="msg bot">${md(c.card)}${meta ? `<button class="btn primary to-task" data-act="ai-task" data-i="${k}">Создать задачу из карточки</button>` : ''}${r.metas.map((x,j) => aiBlockBtn(x, 'result', k, j)).join('')}${c.metas.map((x,j) => aiBlockBtn(x, 'card', k, j)).join('')}</div></div>`; }
  });
  if(AI.busy) html += `<div class="thinking"><span class="ava fast">${lavaSVG()}</span><div class="bubble"><span class="dots"><i></i><i></i><i></i></span><span class="label">${AI.engine === 'gigacode' ? 'GigaCode работает' : 'Думаю над ответом'}</span><span class="sec" id="aiSec">${aiElapsed()}</span></div></div>`;
  box.innerHTML = html;
  box.scrollTop = box.scrollHeight;
  $('#lava').classList.toggle('is-busy', AI.busy);
  $('#lava').classList.toggle('fast', AI.busy);
  $('#aiAva').classList.toggle('fast', AI.busy);
  aiFilesRender();   // плашки файлов над полем ввода и доступность «Отправить»
  aiTakeSync();
}

// «Доработать постановку» — только тимлиду (лид или зам) открытой задачи и в скилле постановки
export function aiTakeSync(){
  $('#aiTake').style.display = S.edit && isTL(S.edit) && $('#aiSkill').value === AI_POSTANOVKA ? '' : 'none';
  $('#aiIn').placeholder = aiSkill()?.placeholder || 'Напишите сообщение';
}

export async function aiLoadStatus(){
  try{ AI.status = await api('/api/llm'); }catch{ AI.status = {configured:false, skills:[]}; }
  const sk = AI.status.skills || [], saved = store.get('ai.skill', AI.status.default);
  const cur = sk.some(x => x.name === saved) ? saved : AI.status.default;
  const eng = $('#aiEngine'), gcOpt = eng.querySelector('[value=gigacode]');
  gcOpt.disabled = !AI.status.gigacode;
  gcOpt.textContent = AI.status.gigacode ? 'GigaCode' : 'GigaCode недоступен';
  eng.value = AI.status.gigacode ? store.get('ai.engine', 'api') : 'api';
  $('#aiSkill').innerHTML = sk.map(s => `<option value="${esc(s.name)}" title="${esc(s.description)}" ${s.name === cur ? 'selected' : ''}>${esc(s.title || s.name)}</option>`).join('');
}

export async function aiToggle(force){
  AI.open = force ?? !AI.open;
  if(AI.open) placePanel();
  $('#ai').classList.toggle('open', AI.open); $('#ai').setAttribute('aria-hidden', String(!AI.open));
  if(AI.open){
    if(!AI.status) await aiLoadStatus();
    aiRender(); setTimeout(() => $('#aiIn').focus(), 120);
  }
}

// Сообщение можно отправить и без текста — только с файлами; файлы уходят в сообщение, плашки над полем очищаются
export async function aiSend(text){
  text = (text ?? $('#aiIn').value).trim();
  if(AI.busy) return;
  if(AI.files.some(f => f.loading)) return toast('Дождитесь, пока извлечётся текст файла', 'warn');
  const files = AI.files.map(f => ({name: f.name, text: f.text, truncated: !!f.truncated}));
  if(!text && !files.length) return;
  $('#aiIn').value = ''; AI.files = []; aiGrow();
  AI.msgs.push(files.length ? {role:'user', content:text, files} : {role:'user', content:text});
  AI.busy = true; AI.t0 = Date.now(); AI.engine = $('#aiEngine').value;
  clearInterval(AI.timer); AI.timer = setInterval(() => { const el = $('#aiSec'); if(el) el.textContent = aiElapsed(); }, 1000);
  aiRender();
  const sid = AUTH.session;   // ответ, пришедший после выхода, в диалог следующего вошедшего не попадает
  try{
    const r = await api('/api/llm', {messages: AI.msgs.filter(m => m.role !== 'error'), skill: $('#aiSkill').value, engine: $('#aiEngine').value});
    if(AUTH.session !== sid) return;
    // диалог как сохранён на сервере: у файлов, не влезших в общий лимит, — пометка «текст не передаётся»
    if(Array.isArray(r.msgs)) AI.msgs = r.msgs; else AI.msgs.push({role:'assistant', content:r.reply});
    if(text){ AI.queries = [text, ...AI.queries.filter(q => q !== text)].slice(0, 30); aiQRender(); }   // сервер сохранил то же
  }catch(e){ if(AUTH.session === sid) AI.msgs.push({role:'error', content:e.message}); }
  finally{ AI.busy = false; clearInterval(AI.timer); aiRender(); }
}

export function aiGrow(){ const t = $('#aiIn'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 160) + 'px'; aiSendSync(); }

// «Отправить» доступна, если есть текст или файлы и текст файлов уже извлечён
export function aiSendSync(){
  const loading = AI.files.some(f => f.loading), b = $('#aiSendBtn');
  b.disabled = AI.busy || loading || (!$('#aiIn').value.trim() && !AI.files.length);
  b.title = loading ? 'Дождитесь, пока извлечётся текст файла' : 'Отправить (Enter)';
}

function aiElapsed(){ const s = Math.max(0, Math.round((Date.now() - (AI.t0 || Date.now())) / 1000)); return s < 60 ? `${s} с` : `${Math.floor(s / 60)} мин ${s % 60} с`; }

/* ---------- перенос служебных блоков в реестр ---------- */
export function aiToTask(k){
  const {card, meta} = splitTaskBlock(AI.msgs[k].content);
  if(!meta) return;
  if(S.edit && isDirty() && !confirm('В открытой задаче есть несохранённые изменения. Заменить её новой задачей из карточки?')) return;
  if(S.edit) closeDrawer(true);
  openTask('once', null);
  const codes = new Set((S.cfg.blocks||[]).map(b => b.code));
  Object.assign(S.edit, {
    title: String(meta.title || '').slice(0, 200),
    description: card.replace(/^\*\*Тип задачи:\*\*.*\n+/m, '').trim(),
    level: ['run','change','disrupt'].includes(meta.level) ? meta.level : '',
    block: codes.has(meta.block) ? meta.block : '',
    deadline: /^\d{4}-\d{2}-\d{2}$/.test(meta.deadline || '') ? meta.deadline : ''
  });
  renderDrawer(); aiToggle(false);
  toast('Карточка перенесена в новую задачу: проверьте тимлида, исполнителя и срок, затем «Поставить задачу»');
}

export function aiToResult(k, j){
  const meta = splitBlocks(AI.msgs[k].content, 'result').metas[j];
  if(!meta) return;
  const {t, why} = aiTarget(meta, 'result');
  if(why) return toast(why, 'warn');
  if(S.edit && isDirty() && !confirm('В открытой задаче есть несохранённые изменения. Открыть другую задачу?')) return;
  if(S.edit) closeDrawer(true);
  openTask(t.kind, t.id);
  Object.assign(S.edit, {result: String(meta.result || ''), result_desc: String(meta.result_desc || '')});
  renderDrawer(); aiToggle(false);
  toast('Результат перенесён в задачу: проверьте поля и нажмите «Отправить на согласование»');
}

export async function aiToCard(k, j){
  const meta = splitBlocks(AI.msgs[k].content, 'card').metas[j];
  if(!meta) return;
  const {t, n, why} = aiTarget(meta, 'card');
  if(why) return toast(why, 'warn');
  if(!String(meta.title || '').trim() || !String(meta.body || '').trim()) return toast('У карточки нет названия-вопроса или текста — попросите помощника собрать её заново', 'err');
  busy('Прикрепляю карточку…');
  try{
    await api('/api/card', {kind:t.kind, id:t.id, title:String(meta.title).trim(), body:String(meta.body).trim()});
    applyState(await api('/api/state'));
    if(S.edit && S.edit.kind === t.kind && String(S.edit.id) === String(t.id) && !isDirty()){
      const nt = S.tasks.find(x => x.kind === t.kind && String(x.id) === String(t.id));
      if(nt){ S.orig = nt; S.edit = JSON.parse(JSON.stringify(nt)); renderDrawer(); }
    }
    render(); aiRender(); toast(`Карточка прикреплена к задаче ${n}`);
  }catch(e){ toast(e.message, 'err'); } finally{ busy(); }
}

export function aiTakeOpen(){
  if(!S.edit) return;
  collect();
  const t = S.edit;
  $('#aiIn').value = `Помоги доработать постановку задачи «${t.title || 'без названия'}».` + (t.description ? `\nТекущее описание:\n${t.description}` : '');
  aiGrow(); $('#aiIn').focus();
}
