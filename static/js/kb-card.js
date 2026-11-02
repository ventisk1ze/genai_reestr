// Карточки знаний: диалог просмотра/создания (привязывается к задаче через /api/card)
// и отправка в базу знаний (T-012): база одна (в настройках владельца) — в диалоге только
// селектор узла онтологии и кнопка «Отправить в репозиторий» (через pull request).
import { $, esc } from './util.js';
import { api } from './api.js';
import { S, canCard } from './state.js';
import { busy, toast, ask } from './ui.js';
import { applyState, render } from './render.js';
import { isDirty, renderDrawer } from './task-card.js';
import { kbOntology, kbNodeOptionsHTML, kbSend, kbBadge, KB } from './kb.js';

const cardEntry = path => (S.edit?.cards || []).find(c => c && c.path === path) || null;

async function fillNodes(cur){
  const sel = $('#kbNode');
  sel.innerHTML = '<option value="">— загрузка онтологии… —</option>';
  sel.disabled = true;
  try{
    const nodes = await kbOntology();
    sel.innerHTML = '<option value="">— выберите узел онтологии —</option>'
      + kbNodeOptionsHTML(nodes, cur);
    sel.disabled = false;
  }catch(e){
    sel.innerHTML = `<option value="">— онтология недоступна: ${esc(e.message)} —</option>`;
    toast(e.message, 'err');
  }
}

// Диалог карточки: вопрос + текст + узел онтологии и «Отправить в репозиторий»
// (для исполнителя в «Согласована»; отправляем сохранённую карточку)
export async function cardDialog(path){
  const t = S.edit;
  let title = '', text = '';
  if(path){ try{ const r = await api('/api/card?path=' + encodeURIComponent(path)); const m = /^#\s*(.*)\n+([\s\S]*)$/.exec(r.text); title = m ? m[1] : ''; text = m ? m[2].trim() : r.text; }catch(e){ return toast(e.message,'err'); } }
  if(!t || !canCard(t)){ if(!path) return; return ask(title || path, [{k:'body', l:'Текст карточки', t:'area', rows:14, v:text, ro:1}], 'Закрыть'); }
  if(isDirty()) return toast('Сначала сохраните изменения в задаче', 'warn');

  const entry = path ? cardEntry(path) : null;
  const sent = entry?.bb || null;
  const canSend = !!path;   // отправляем сохранённую карточку; новая — сначала «Сохранить карточку»
  const d = $('#dlg');
  d.classList.add('wide');
  d.innerHTML = `<form method="dialog" class="dlg">
    <h3>${path ? 'Карточка знаний' : 'Новая карточка знаний'}</h3>
    <label class="fld"><span>Вопрос, на который отвечает карточка</span>
      <input name="title" required value="${esc(title)}" placeholder="Что? Как? Почему? Когда/где? Зачем? — например: Как получить доступ к метрикам качества доменов?"></label>
    <label class="fld"><span>Текст (Markdown)</span>
      <textarea name="body" rows="10" required>${esc(text)}</textarea></label>
    <div class="kb-send">
      <b>Отправить в базу знаний</b>
      ${sent ? `<div class="kb-sent">Отправлена: ${kbBadge(entry)}</div>` : ''}
      <label class="fld"><span>Узел онтологии (папка в репозитории БЗ)</span>
        <select id="kbNode"><option value="">— загрузка онтологии… —</option></select></label>
    </div>
    <div class="dlg-f">
      <button value="cancel" formnovalidate class="btn">Отмена</button>
      ${canSend ? '<button type="button" class="btn" id="kbSendBtn">Отправить в репозиторий</button>' : ''}
      <button value="ok" class="btn primary">Сохранить карточку</button>
    </div></form>`;
  d.returnValue = '';
  d.showModal();

  fillNodes(sent?.node || '');
  if(canSend) $('#kbSendBtn').addEventListener('click', async () => {
    const r = await kbSend(t, entry);
    if(r){ d.close('sent'); }
  });

  await new Promise(res => d.addEventListener('close', res, {once: true}));
  const ok = d.returnValue === 'ok';
  const o = {};
  d.querySelectorAll('[name]').forEach(e2 => o[e2.name] = e2.value.trim());   // значения — до очистки диалога
  d.classList.remove('wide');
  d.innerHTML = '';
  if(!ok) return;
  busy('Сохраняю карточку…');
  try{
    await api('/api/card', {kind:t.kind, id:t.id, title:o.title, body:o.body, path:path || ''});
    applyState(await api('/api/state'));
    const nt = S.tasks.find(x => x.kind === t.kind && String(x.id) === String(t.id));
    if(nt){ S.orig = nt; S.edit = JSON.parse(JSON.stringify(nt)); renderDrawer(); }
    render(); toast('Карточка сохранена');
  }catch(e){ toast(e.message,'err'); } finally{ busy(); }
}
