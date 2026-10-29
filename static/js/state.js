// Состояние страницы: константы регламента, стор S (реестр + фильтры), справочные помощники, права на экране.
// Права по этапам — зеркало матрицы сервера (registry/tasks.py: EDIT_RIGHTS); проверяет всё равно сервер.
import { AUTH } from './api.js';
import { isoOf } from './util.js';

export const ST = ['Новая','В работе','На согласовании','На доработке','Согласована','Завершена','Принята'];
export const ST_VAR = {'Новая':'--st-new','В работе':'--st-work','На согласовании':'--st-review','На доработке':'--st-rework','Согласована':'--st-approved','Завершена':'--st-done','Принята':'--st-ok'};
export const RAIL = ['Новая','В работе','На согласовании','Согласована','Завершена','Принята'];
export const RAIL_POS = {'Новая':0,'В работе':1,'На доработке':1,'На согласовании':2,'Согласована':3,'Завершена':4,'Принята':5};
export const ACTIVE = new Set(['Новая','В работе','На доработке']);
export const CLOSED = new Set(['Завершена','Принята']);
export const RESULT_OK = new Set(['Согласована','Завершена','Принята']);   // результат согласован тимлидом
export const F_SETUP = ['title','description','materials','block','block_note','level','regularity','deadline','executor','substitute'];
export const F_RESULT = ['result','result_desc','report_event','report_date'];
export const F_APPROVE = ['block','block_note','level','bz_save','bz_reason'];
export const EDIT_RIGHTS = {'Новая':{tl:F_SETUP}, 'В работе':{ex:F_RESULT}, 'На доработке':{ex:F_RESULT}, 'На согласовании':{tl:F_APPROVE}, 'Согласована':{ex:['bz_card']}};
export const WAIT_HINT = {'Новая':'Ждёт исполнителя: взять в работу', 'В работе':'Исполнитель готовит результат', 'На доработке':'Поправьте результат по замечаниям тимлида и снова отправьте на согласование',
  'На согласовании':'Ждёт тимлида: согласовать или вернуть на доработку', 'Согласована':'Исполнитель готовит базу знаний и завершает задачу',
  'Завершена':'Ждёт тимлида: проверить базу знаний и принять', 'Принята':'Задача принята'};
export const PALETTE = ['#1D7A46','#2C5BA8','#8A4FB0','#B5651D','#0F7C86','#A3366B','#5B6B1F','#46506B','#C0392B','#6D4C41'];
export const ROLE_LABEL = {head:'Руководитель ЦК', deputy:'Заместитель руководителя ЦК', bz_owner:'Ответственный за карточки БЗ', bz_metrics:'Ответственный за контроль метрик БЗ'};

// localStorage-обёртка: настройки интерфейса (вид, фильтры, сортировка, позиции окон). Сессий здесь нет.
export const store = {
  get(k, d){ try{ const v = localStorage.getItem('ck.'+k); return v == null ? d : JSON.parse(v); }catch{ return d; } },
  set(k, v){ try{ localStorage.setItem('ck.'+k, JSON.stringify(v)); }catch{} }
};

export const S = {
  cfg:null, tasks:[], cards:[], meta:{}, log:null,
  view: store.get('view','list'),
  // Охват (scope) в localStorage не хранится: при входе и выходе — значение по умолчанию для роли
  f: Object.assign({kind:'once', q:'', status:[], tl:'', ex:'', block:'', overdue:false}, store.get('f',{}), {scope:'all'}),
  sort: store.get('sort',{k:'id', d:-1}),
  orig:null, edit:null, cfgEdit:null
};
delete S.f.mine;  // старый флажок «Мои» из localStorage — заменён охватом

/* ---------- люди, команды, даты ---------- */
export const today = () => S.meta.today || isoOf(new Date());
export const people = () => S.cfg?.people || [];
export const person = n => people().find(p => p.name === n);
export const colorOf = n => person(n)?.color || 'transparent';
export const short = n => { if(!n) return ''; const p = n.trim().split(/\s+/); return p.length >= 2 ? `${p[0]} ${p.slice(1).map(x => x[0]+'.').join('')}` : n; };
export const me = () => AUTH.user && !AUTH.user.admin ? AUTH.user.name : '';
export const isAdmin = () => !!AUTH.user?.admin;
export const isOver = t => !!t.deadline && ACTIVE.has(t.status) && t.deadline < today();
// Команды (T-007): права тимлида — у лида и зама команды задачи; задача без команды — по полю «Тимлид»
export const teams = () => S.cfg?.teams || [];
export const teamName = id => teams().find(x => x.id === id)?.name || '';
export const myTeams = () => AUTH.user?.lead_teams || [];
export const isTL = t => !!me() && (t.team ? myTeams().includes(t.team) : t.teamlead === me());
export const isEx = t => !!me() && [t.executor, t.substitute].includes(me());
// Охват «Мои»: у сотрудника — исполнитель или заместитель, у тимлида — ещё и задачи его реестра
export const defaultScope = () => me() ? 'mine' : 'all';
export const myScope = t => { const m = me(); return !!m && (t.executor === m || t.substitute === m || (myTeams().length > 0 && isTL(t))); };
export const inScope = t => S.f.scope !== 'mine' || !me() || myScope(t);
export const canCreate = () => isAdmin() || myTeams().length > 0;
export const canCard = t => isAdmin() || (isEx(t) && t.status === 'Согласована');
// какие поля текущий пользователь может менять в открытой карточке (без смены статуса)
export function editable(t){
  if(isAdmin()) return null;   // null — всё
  if(t.id == null) return new Set(canCreate() ? [...F_SETUP, 'report_event', 'report_date', ...(myTeams().length > 1 ? ['team'] : [])] : []);
  const r = EDIT_RIGHTS[t.status] || {};
  return new Set([...(isTL(t) ? r.tl || [] : []), ...(isEx(t) ? r.ex || [] : [])]);
}
export const canEdit = k => { const e = S._ed; return e === null || e === undefined || e.has(k); };
export const pfx = t => t.kind === 'regular' ? '{id}_Рег' : '{id}';
export const label = t => (t.kind === 'regular' ? 'Рег. ' : '') + (t.id ?? 'новая');
export const blockName = c => (S.cfg?.blocks||[]).find(b => b.code === c)?.name || '';
export const stColor = s => `var(${ST_VAR[s]||'--st-new'})`;
