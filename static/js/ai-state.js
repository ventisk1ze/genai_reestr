// Состояние помощника без логики: объект AI и лимиты файлов (общий для ai.js и ai-files.js,
// чтобы они не импортировали друг друга).
// files — приложенные к следующему сообщению (T-009): только в памяти страницы, не в localStorage/sessionStorage
export const AI = {open:false, busy:false, status:null, msgs: [], queries: [], files: [], fileSeq: 0};
try{ localStorage.removeItem('ck.ai.msgs'); }catch{}   // старый диалог из localStorage удаляем

export const AI_POSTANOVKA = 'postanovka';

export const AI_FILE_DEF = {types:['.docx','.txt'], max_mb:10, max_bytes:10*1024*1024, max_chars:50000, total_chars:100000, per_msg:5};
export const aiLimits = () => Object.assign({}, AI_FILE_DEF, AI.status?.file_limits || {});
