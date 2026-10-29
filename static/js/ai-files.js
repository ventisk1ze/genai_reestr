// Файлы в помощнике (T-009): .docx и .txt, скрепка, плашки, перетаскивание в окно.
// Файл читается в браузере (FileReader → base64), текст извлекает сервер реестра (/api/ai/file)
// и проверяет лимиты. Ограничения на клиенте — только чтобы не гнать по сети заведомо
// неподходящий файл; решает всё равно сервер.
import { $, esc, nfmt } from './util.js';
import { api, AUTH } from './api.js';
import { AI, aiLimits } from './ai-state.js';
import { toast } from './ui.js';
import { CLIP_SVG } from './icons.js';
import { aiSendSync } from './ai.js';

export function aiFileProblem(file){
  const L = aiLimits(), n = file.name, ext = (/\.[^.\\/]+$/.exec(n) || [''])[0].toLowerCase();
  if(ext === '.doc') return `Файл «${n}»: формат .doc (старый Word) не поддерживается — откройте его в Word и сохраните как .docx`;
  if(ext === '.pdf') return `Файл «${n}»: PDF не поддерживается — сохраните документ как .docx или .txt`;
  if(!ext) return `У файла «${n}» нет расширения — приложите .docx или .txt`;
  if(!L.types.includes(ext)) return `Файл «${n}»: формат ${ext} не поддерживается — приложите .docx или .txt`;
  if(!file.size) return `Файл «${n}» пустой`;
  if(file.size > L.max_bytes) return `Файл «${n}» больше ${String(L.max_mb).replace('.', ',')} МБ — приложите файл поменьше или только нужную часть`;
  return '';
}

const readB64 = file => new Promise((res, rej) => {
  const r = new FileReader();
  r.onload = () => res(String(r.result).replace(/^data:[^,]*,/, ''));
  r.onerror = () => rej(new Error(`Не удалось прочитать файл «${file.name}»`));
  r.readAsDataURL(file);
});

export function aiAttach(list){
  const L = aiLimits();
  for(const file of list){
    if(AI.files.length >= L.per_msg){ toast(`К одному сообщению — не больше ${L.per_msg} файлов`, 'warn'); break; }
    const bad = aiFileProblem(file);
    if(bad){ toast(bad, 'err'); continue; }
    const f = {id: ++AI.fileSeq, name: file.name, loading: true};
    AI.files.push(f);
    aiUpload(f, file);
  }
  aiFilesRender();
}

async function aiUpload(f, file){
  try{
    const r = await api('/api/ai/file', {name: file.name, data: await readB64(file)});
    if(!AI.files.includes(f)) return;   // плашку убрали или начали новый диалог, пока извлекался текст
    Object.assign(f, {name: r.name, text: r.text, chars: r.chars, truncated: !!r.truncated, loading: false});
  }catch(e){
    if(!AI.files.includes(f)) return;
    AI.files = AI.files.filter(x => x !== f);   // при ошибке плашка не остаётся
    toast(e.status === 413 ? `Файл «${file.name}» не пропустил прокси: слишком большой — приложите файл поменьше`
      : e.status || e.name !== 'TypeError' ? e.message : `Файл «${file.name}» не дошёл до сервера реестра — проверьте связь и приложите его ещё раз`, 'err');
  }
  aiFilesRender();
}

// Плашка файла: над полем ввода (pending) — имя, объём, «обрезан», крестик; в сообщении — имя и пометки, без текста
export function aiFileChip(f, pending){
  const L = aiLimits();
  const tip = f.loading ? 'Извлекаю текст…' : f.dropped ? 'Текст файла не передаётся помощнику: не поместился в общий лимит текста файлов диалога'
    : f.truncated ? `Текст длиннее ${nfmt(L.max_chars)} символов — помощнику уйдёт только начало` : f.name;
  return `<span class="ai-file${f.dropped ? ' ai-foff' : ''}" title="${esc(tip)}">${f.loading ? '<span class="spin"></span>' : CLIP_SVG}<span class="ai-fname">${esc(f.name)}</span>`
    + (pending ? `<span class="ai-fsize">${f.loading ? 'извлекаю текст…' : nfmt(f.chars) + ' симв.'}</span>` : '')
    + (f.dropped ? '<span class="ai-fmark">текст не передаётся</span>' : f.truncated ? '<span class="ai-fmark">обрезан</span>' : '')
    + (pending ? `<button class="ai-fx" data-act="ai-file-rm" data-i="${f.id}" title="Убрать файл" aria-label="Убрать файл ${esc(f.name)}">×</button>` : '')
    + '</span>';
}

export function aiFilesRender(){
  const L = aiLimits(), total = AI.files.reduce((s, f) => s + Math.min(f.chars || 0, L.max_chars), 0);
  $('#aiFiles').innerHTML = AI.files.length ? AI.files.map(f => aiFileChip(f, true)).join('')
    + (total > L.total_chars ? `<div class="ai-fnote">Вместе файлы больше ${nfmt(L.total_chars)} символов — помощнику уйдёт не весь текст</div>` : '') : '';
  aiSendSync();
}

// Перетаскивание файлов в окно помощника; брошенный мимо окна файл браузер не открывает вместо реестра
export function initFileDrop(){
  const ai = $('#ai'), hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
  let depth = 0;
  const off = () => { depth = 0; ai.classList.remove('ai-drop'); };
  ai.addEventListener('dragenter', e => { if(!hasFiles(e)) return; e.preventDefault(); depth++; ai.classList.add('ai-drop'); });
  ai.addEventListener('dragover', e => { if(!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
  ai.addEventListener('dragleave', e => { if(hasFiles(e) && --depth <= 0) off(); });
  ai.addEventListener('drop', e => { if(!hasFiles(e)) return; e.preventDefault(); off(); if(AUTH.user) aiAttach([...e.dataTransfer.files]); });
  addEventListener('dragover', e => { if(hasFiles(e) && !e.target.closest?.('#ai')){ e.preventDefault(); e.dataTransfer.dropEffect = 'none'; } });
  addEventListener('drop', e => { if(hasFiles(e) && !e.target.closest?.('#ai')) e.preventDefault(); });
  $('#aiFileIn').addEventListener('change', e => { const list = [...e.target.files]; e.target.value = ''; aiAttach(list); });
}
