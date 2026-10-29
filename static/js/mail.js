// Письма по шаблонам регламента: меню в карточке задачи и mailto-ссылка.
import { uniq, cut, fmt } from './util.js';
import { S, person, pfx, short } from './state.js';
import { toast } from './ui.js';
import { icon, NAV_ICONS } from './icons.js';

export const MAIL_OPTS = [
  ['new', 'Постановка задачи', 'Исполнителю; в копии замещающий и руководитель ЦК'],
  ['review', 'Результат на согласование', 'Тимлиду; в копии замещающий и руководитель ЦК'],
  ['rework', 'Возврат на доработку', 'Исполнителю; в копии замещающий'],
  ['approved', 'Результат согласован', 'Исполнителю; в копии замещающий'],
  ['done', 'Задача завершена, база знаний готова', 'Тимлиду; в копии замещающий'],
  ['accepted', 'Задача принята', 'Руководителю ЦК; в копии исполнитель и замещающий'],
  ['bz', 'Сформировать карточку БЗ', 'Ответственному за базу знаний ЦК']];
export const MAIL_REC = {'Новая':'new', 'В работе':'review', 'На согласовании':'review', 'На доработке':'rework', 'Согласована':'approved', 'Завершена':'done', 'Принята':'accepted'};

export function mailMenu(t){
  const rec = MAIL_REC[t.status] || 'new';
  const opts = [...MAIL_OPTS].sort((a, b) => (b[0] === rec) - (a[0] === rec));
  return `<div class="mailwrap"><button class="btn" data-act="mailmenu" title="Открыть письмо в Outlook по шаблону регламента">${icon(NAV_ICONS.mail)}Написать письмо</button>
    <div class="menu" id="mailMenu">${opts.map(([k, l, d]) => `<button data-act="mail" data-stage="${k}"><b>${l}${k === rec ? '<span class="rec">по этапу</span>' : ''}</b><small>${d}</small></button>`).join('')}</div></div>`;
}

export function mailSpec(t, stage){
  const R = S.cfg.roles || {}, p = pfx(t).replace('{id}', t.id);
  return {
    new:   {to:[t.executor], cc:[t.substitute, R.head], subj:`${p}_Новая_${fmt(t.deadline)}`},
    review:{to:[t.teamlead], cc:[t.substitute, R.head], subj:`${p}_На согласовании`},
    rework:{to:[t.executor], cc:[t.substitute], subj:`${p}_На доработку`},
    approved:{to:[t.executor], cc:[t.substitute], subj:`${p}_Согласована`},
    done:  {to:[t.teamlead], cc:[t.substitute], subj:`${p}_${cut(t.title,50)}_Завершена`},
    accepted:{to:[R.head], cc:[t.executor, t.substitute], subj:`${p}_${cut(t.title,50)}_Принята`},
    bz:    {to:[R.bz_owner], cc:[], subj:`${p}_Сформировать карточку(ки)`}
  }[stage];
}

export function openMail(t, stage){
  const s = mailSpec(t, stage); if(!s) return;
  const miss = [];
  const addr = l => uniq(l).map(n => { const e = person(n)?.email; if(!e) miss.push(n); return e; }).filter(Boolean);
  const to = addr(s.to), cc = addr(s.cc).filter(e => !to.includes(e));
  const bodyWith = desc => [
    `Задача ${t.kind === 'regular' ? 'Рег ' : ''}${t.id}: ${t.title}`,
    t.deadline ? `Срок: ${fmt(t.deadline)}` : '',
    t.regularity ? `Регулярность: ${t.regularity}` : '',
    desc ? `\nОписание задачи:\n${desc}` : '',
    t.materials ? `\nМатериалы: ${t.materials}` : '',
    ['review','approved','done','accepted','bz'].includes(stage) && t.result ? `\nРезультат: ${t.result}` : '',
    ['review','approved','done','accepted','bz'].includes(stage) && t.result_desc ? `Что сделано: ${t.result_desc}` : '',
    stage === 'rework' && t.iterations?.length ? `\nЧто доработать: ${t.iterations.at(-1).comment}` : '',
    stage === 'approved' ? (t.bz_save === 'да' ? `\nРезультат согласован. Сохраняем в базу знаний ЦК${t.bz_reason ? ' (' + t.bz_reason + ')' : ''}: подготовьте карточку(и) знаний в карточке задачи и завершите задачу.`
      : `\nРезультат согласован. В базу знаний не сохраняем${t.bz_reason ? ' (' + t.bz_reason + ')' : ''} — задача завершена.`) : '',
    ['done','accepted'].includes(stage) && t.bz_save === 'да' ? `\nБаза знаний: ${[...(t.cards||[]).map(c => c.replace(/^cards\//, '')), t.bz_card].filter(Boolean).join('; ') || 'карточек нет'}` : '',
    stage === 'done' && t.bz_save === 'да' ? 'Прошу проверить карточки базы знаний и принять задачу.' : stage === 'done' ? '\nПрошу принять задачу.' : ''
  ].filter(Boolean).join('\n');
  const urlFor = body => `mailto:${to.join(';')}?subject=${encodeURIComponent(s.subj)}${cc.length ? '&cc=' + cc.join(';') : ''}&body=${encodeURIComponent(body)}`;
  const full = bodyWith(t.description || '');
  let url = urlFor(full);
  // Outlook обрезает длинные mailto-ссылки: укорачиваем описание, полный текст кладём в буфер обмена
  if(url.length > 1900 && t.description){
    let d = t.description;
    while(d.length > 150 && urlFor(bodyWith(d + '… [полный текст — в буфере обмена]')).length > 1900) d = d.slice(0, Math.floor(d.length * 0.85));
    url = urlFor(bodyWith(d.trimEnd() + '… [полный текст — в буфере обмена]'));
    navigator.clipboard?.writeText(full).then(
      () => toast('Описание длинное: полный текст письма скопирован в буфер обмена — вставьте его в письмо', 'warn'),
      () => toast('Описание длинное и обрезано в письме; скопировать в буфер не удалось', 'warn'));
  }
  if(miss.length) toast('Нет email в справочнике: ' + miss.map(short).join(', '), 'warn');
  const a = document.createElement('a'); a.href = url; a.click();
}
