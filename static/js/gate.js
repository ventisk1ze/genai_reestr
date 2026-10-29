// Вход по PIN: экран-гейт, вход/выход, восстановление сессии после F5.
import { $, esc } from './util.js';
import { api, AUTH, SESS } from './api.js';
import { S, people, defaultScope, isAdmin } from './state.js';
import { busy } from './ui.js';
import { LAVA_STILL } from './icons.js';
import { load } from './render.js';
import { closeDrawer, isDirty } from './task-card.js';
import { aiForget, aiLoadHistory, aiToggle } from './ai.js';
import { BB } from './bb.js';

export function showGate(err){
  const g = $('#gate'), act = people().filter(p => p.active !== false);
  const adm = !!S.meta.admin_login, asAdm = adm && g.dataset.admin === '1';
  const prev = g.querySelector('[name=name]')?.value || '';
  g.innerHTML = `<form id="gateForm" autocomplete="off"><h1><span class="logo">${LAVA_STILL}</span>${esc('Реестр задач ' + (S.cfg?.ck_name || 'ЦК'))}</h1>
    <p class="lead" style="margin:0">${asAdm ? 'Вход администратора — админский PIN, который владелец ввёл при запуске сервера.' : 'Выберите себя и введите PIN. PIN выдаёт администратор реестра.'}</p>
    ${asAdm ? '' : `<label class="fld"><span>Сотрудник</span><select name="name" required><option value="">— выберите себя —</option>${act.map(p => `<option ${p.name === prev ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>`}
    <label class="fld"><span>PIN</span><input name="pin" type="password" inputmode="numeric" pattern="[0-9]{4,8}" maxlength="8" required autocomplete="off" title="От 4 до 8 цифр"></label>
    <p class="gate-err" id="gateErr">${esc(err || (!S.cfg ? 'Реестр не загрузился. Проверьте, что сервер запущен (bash start.sh), и обновите страницу.' : !act.length && !asAdm ? 'В справочнике нет сотрудников — администратор добавляет их в админ-панели.' : ''))}</p>
    <button class="btn primary" style="justify-content:center">Войти</button>
    ${adm ? `<label class="chk"><input type="checkbox" id="gateAdm" ${asAdm ? 'checked' : ''}> Войти как администратор</label>` : ''}</form>`;
  g.classList.add('open'); g.setAttribute('aria-hidden', 'false');
  closeDrawer(true); aiToggle(false); $('#me').innerHTML = '';
  setTimeout(() => g.querySelector(asAdm ? '[name=pin]' : '[name=name]')?.focus(), 50);
}

export async function login(form){
  const pinEl = form.querySelector('[name=pin]'), pin = pinEl.value.trim(), asAdm = $('#gate').dataset.admin === '1' && S.meta.admin_login;
  try{
    const r = await api('/api/login', asAdm ? {admin:true, pin} : {name: form.querySelector('[name=name]').value, pin});
    AUTH.session = r.session; AUTH.user = r.user; SESS.set(r.session); S.f.scope = defaultScope();
    $('#gate').classList.remove('open'); $('#gate').setAttribute('aria-hidden', 'true'); $('#gate').innerHTML = '';
    if(S.view === 'admin' && !isAdmin()) S.view = 'list';
    aiLoadHistory();
    await load(false);
  }catch(e){ pinEl.value = ''; $('#gateErr').textContent = e.message; pinEl.focus(); }
}

export async function logout(){
  if(isDirty() && !confirm('Есть несохранённые изменения в задаче. Выйти без сохранения?')) return;
  try{ await api('/api/logout', {}); }catch{}
  AUTH.session = ''; AUTH.user = null; SESS.set(''); aiForget(); S.cfgEdit = null; S.f.scope = defaultScope(); BB.status = null; showGate();
}

// После F5: сохранённая сессия проверяется на сервере до загрузки реестра — форма входа не мигает
export async function restoreSession(){
  const sid = SESS.get();
  if(!sid) return;
  busy('Загружаю реестр…');
  AUTH.session = sid;
  let user = null;
  try{ user = (await api('/api/whoami')).user; }
  catch{ AUTH.session = ''; return; }   // сервер недоступен — ключ не стираем, вход как обычно
  if(!user){ AUTH.session = ''; SESS.set(''); return; }   // истекла, перезапуск сервера, сотрудник деактивирован
  AUTH.user = user; S.f.scope = defaultScope();
  if(S.view === 'admin' && !isAdmin()) S.view = 'list';
  aiLoadHistory();
}
