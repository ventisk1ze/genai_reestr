// Сеть: ключ страницы, базовый путь, сессия и один-единственный запрос-помощник api().
// Ключ TOKEN выдаёт сервер с HTML (templates/index.html); команды GigaCode берут его с той же страницы.

// Базовый путь страницы: за jupyter-server-proxy это /user/<user>/proxy/<port>/, локально — /
// Адрес …/proxy/39246 (без слеша) — это тоже корень реестра, иначе API уйдёт мимо сервера
export const TOKEN = window.TOKEN;
export const BASE = (p => p.endsWith('/') ? p : /\.html?$/.test(p) ? p.replace(/[^/]*$/, '') : p + '/')(location.pathname);

// Сессия — в памяти и в хранилище вкладки (sessionStorage, T-005): переживает F5, в новой вкладке — вход заново.
// Хранится только идентификатор сессии; пользователь — с сервера (/api/whoami). В localStorage сессии нет.
export const AUTH = {session:'', user:null};
export const SESS = {
  get(){ try{ return sessionStorage.getItem('ck.session') || ''; }catch{ return ''; } },
  set(v){ try{ v ? sessionStorage.setItem('ck.session', v) : sessionStorage.removeItem('ck.session'); }catch{} }
};

// Импорты ниже образуют цикл (gate/ai тоже зовут api()) — это допустимо: обращения только внутри функций.
import { aiForget } from './ai.js';
import { showGate } from './gate.js';

export async function api(path, body){
  const opt = {headers:{'X-Token':TOKEN, 'X-Session': AUTH.session}};
  if(body !== undefined){ opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(BASE + path.replace(/^\//, ''), opt);
  let j = {}; try{ j = await r.json(); }catch{}
  if(r.status === 401 && j.login && AUTH.session){ AUTH.session = ''; AUTH.user = null; SESS.set(''); aiForget(); showGate(j.error); }
  if(!r.ok){ const err = new Error(j.error || `Ошибка ${r.status}`); err.status = r.status; throw err; }
  return j;
}
