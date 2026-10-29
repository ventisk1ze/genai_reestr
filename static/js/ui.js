// Общий интерфейс: заставка «занято», тосты, диалог-форма ask().
import { $, esc, httpUrl } from './util.js';

export function busy(text){ $('#busy').classList.toggle('on', !!text); $('#busyText').textContent = text || ''; }

export function toast(msg, kind='', action=null){
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = `<span>${esc(msg)}</span>`;
  // action: {l, run} — кнопка; {l, href} — ссылка в новой вкладке (только http/https)
  if(action?.href){   // ссылка открывается в новой вкладке, тост остаётся до своего таймаута
    if(httpUrl(action.href)){ const a = document.createElement('a'); a.textContent = action.l; a.href = action.href; a.target = '_blank'; a.rel = 'noopener'; a.onclick = e => e.stopPropagation(); el.append(a); }
  }
  else if(action){ const b = document.createElement('button'); b.textContent = action.l; b.onclick = () => { action.run(); el.remove(); }; el.append(b); }
  // клик по тосту закрывает его (кнопка действия — как раньше; выделенный для копирования текст не закрывает)
  el.title = 'Нажмите, чтобы закрыть';
  el.addEventListener('click', e => { const s = getSelection(); if(!e.target.closest('button') && !(s && !s.isCollapsed && el.contains(s.anchorNode))) el.remove(); });
  $('#toasts').append(el);
  setTimeout(() => el.remove(), kind === 'err' ? 14000 : action ? 12000 : 5000);
}

// Диалог-форма: ask(заголовок, [{k, l, t, v, req, ro, ph, opts, rows}], ok, note) → Promise значений | null
export function ask(title, fields, ok='Сохранить', note=''){
  return new Promise(res => {
    const d = $('#dlg');
    d.innerHTML = `<form method="dialog" class="dlg"><h3>${esc(title)}</h3>${note ? `<p class="note">${esc(note)}</p>` : ''}
      ${fields.map(f => `<label class="fld"><span>${esc(f.l)}</span>${f.t === 'area'
        ? `<textarea name="${f.k}" rows="${f.rows||4}" ${f.req ? 'required' : ''} ${f.ro ? 'readonly' : ''} placeholder="${esc(f.ph||'')}">${esc(f.v||'')}</textarea>`
        : f.t === 'sel' ? `<select name="${f.k}">${f.opts.map(([a,b]) => `<option value="${esc(a)}" ${a === f.v ? 'selected' : ''}>${esc(b)}</option>`).join('')}</select>`
        : `<input name="${f.k}" type="${f.t||'text'}" value="${esc(f.v||'')}" ${f.req ? 'required' : ''} placeholder="${esc(f.ph||'')}">`}</label>`).join('')}
      <div class="dlg-f"><button value="cancel" formnovalidate class="btn">Отмена</button><button value="ok" class="btn primary">${esc(ok)}</button></div></form>`;
    d.returnValue = '';
    d.addEventListener('close', () => {
      if(d.returnValue !== 'ok') return res(null);
      const o = {}; d.querySelectorAll('[name]').forEach(e => o[e.name] = e.value.trim()); res(o);
    }, {once:true});
    d.showModal();
  });
}
