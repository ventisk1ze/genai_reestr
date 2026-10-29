// Перетаскивание: окно помощника (за шапку, размер за угол) и шар-лава.
import { store } from './state.js';
import { lavaSVG, LAVA_STILL } from './icons.js';
import { aiToggle } from './ai.js';
import { AI } from './ai-state.js';

function clampPlace(el, x, y){   // offset*: размеры без учёта transform (у закрытого окна он уменьшен)
  x = Math.max(8, Math.min(x, innerWidth - el.offsetWidth - 8));
  y = Math.max(8, Math.min(y, innerHeight - el.offsetHeight - 8));
  Object.assign(el.style, {left: x + 'px', top: y + 'px', right: 'auto', bottom: 'auto'});
}

function draggable(el, handle, key, onTap){
  let st = null;
  handle.addEventListener('pointerdown', e => {
    if(e.button !== 0 || (handle !== el && e.target.closest('button,select,textarea,input,a'))) return;
    st = {x: e.clientX, y: e.clientY, l: el.offsetLeft, t: el.offsetTop, moved: false};
    handle.setPointerCapture(e.pointerId);
  });
  handle.addEventListener('pointermove', e => {
    if(!st) return;
    const dx = e.clientX - st.x, dy = e.clientY - st.y;
    if(!st.moved && Math.hypot(dx, dy) < 5) return;
    st.moved = true; el.classList.add('dragging');
    clampPlace(el, st.l + dx, st.t + dy);
  });
  const end = e => {
    if(!st) return;
    const moved = st.moved; st = null; el.classList.remove('dragging');
    if(moved) store.set(key, {l: el.offsetLeft, t: el.offsetTop});
    else if(onTap && e.type === 'pointerup') onTap();
  };
  handle.addEventListener('pointerup', end);
  handle.addEventListener('pointercancel', end);
}

function restorePos(el, key){ const p = store.get(key, null); if(p) clampPlace(el, p.l, p.t); }

export function placePanel(){
  const ai = document.querySelector('#ai');
  if(store.get('ai.pos', null)) return restorePos(ai, 'ai.pos');
  // окно — над шаром, выровнено по его правому краю; координаты left/top, чтобы уголок размера тянулся вправо-вниз
  // приоритет: над шаром → под шаром → слева → справа, чтобы окно не накрывало шар
  const L = document.querySelector('#lava'), w = ai.offsetWidth, h = ai.offsetHeight, g = 12;
  const lx = L.offsetLeft, ly = L.offsetTop, lw = L.offsetWidth, lh = L.offsetHeight;
  if(ly - h - g >= 8) return clampPlace(ai, lx + lw - w, ly - h - g);
  if(ly + lh + g + h <= innerHeight - 8) return clampPlace(ai, lx + lw - w, ly + lh + g);
  if(lx - w - g >= 8) return clampPlace(ai, lx - w - g, ly + lh - h);
  clampPlace(ai, lx + lw + g, ly + lh - h);
}

export function initFloating(){
  const lava = document.querySelector('#lava'), ai = document.querySelector('#ai');
  lava.innerHTML = lavaSVG(); document.querySelector('#logo').innerHTML = LAVA_STILL; document.querySelector('#aiAva').innerHTML = lavaSVG();
  draggable(lava, lava, 'lava.pos', () => aiToggle());
  lava.addEventListener('click', e => { if(e.detail === 0) aiToggle(); });   // Enter/Space с клавиатуры
  draggable(ai, document.querySelector('#aiHead'), 'ai.pos');
  document.querySelector('#aiHead').addEventListener('dblclick', e => {
    if(e.target.closest('button,select')) return;
    store.set('ai.pos', null); store.set('ai.size', null);
    ['left', 'top', 'right', 'bottom', 'width', 'height'].forEach(k => ai.style[k] = ''); placePanel();
  });
  const size = store.get('ai.size', null);
  if(size){ ai.style.width = size.w + 'px'; ai.style.height = size.h + 'px'; }
  new ResizeObserver(() => { if(AI.open && !ai.classList.contains('dragging')) store.set('ai.size', {w: ai.offsetWidth, h: ai.offsetHeight}); }).observe(ai);
  restorePos(lava, 'lava.pos');
  addEventListener('resize', () => { restorePos(lava, 'lava.pos'); if(store.get('ai.pos', null)) restorePos(ai, 'ai.pos'); });
}
