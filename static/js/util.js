// Общие помощники без состояния: DOM, форматирование, разбор путей.
export const $ = s => document.querySelector(s);
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const clone = o => JSON.parse(JSON.stringify(o));
export const uniq = a => [...new Set(a.filter(Boolean))].sort((x,y) => x.localeCompare(y,'ru'));
export const cut = (s, n=60) => (s||'').length > n ? s.slice(0, n-1).trimEnd()+'…' : (s||'');
export const httpUrl = u => /^https?:\/\/[^\s"'<>\\]+$/i.test(String(u ?? ''));   // ссылка наружу — только http(s)
export const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
export const nfmt = n => Number(n || 0).toLocaleString('ru-RU');

/* ---------- даты ---------- */
export const pad = n => String(n).padStart(2,'0');
export const isoOf = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
export const fmt = d => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d||''); return m ? `${m[3]}.${m[2]}.${m[1]}` : (d||''); };
export const fmtTs = ts => ts ? `${fmt(ts)} ${ts.slice(11,16)}` : '';

/* ---------- вложенные поля админки (data-c="people.0.name") ---------- */
export function setPath(obj, path, val){ const k = path.split('.'); let o = obj; k.slice(0,-1).forEach(x => o = o[x] ??= {}); o[k.at(-1)] = val; }
