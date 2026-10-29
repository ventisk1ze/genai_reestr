// Иконки: лава-болл (SVG с фильтром-«желе»), навигация, скрепка.
export const LAVA_BLOBS = [
  [20, 54, 9,  '#5CC98A', '--d:11s;--h:-34px;--x1:4px;--x2:-3px'],
  [42, 56, 11, '#7BD88F', '--d:14s;--dl:-5s;--h:-30px;--x1:-4px;--x2:3px'],
  [31, 58, 7,  '#A6E57E', '--d:9s;--dl:-2s;--h:-40px;--x1:3px;--x2:2px'],
  [27, 12, 7,  '#6FD6A0', '--d:12s;--dl:-7s;--h:26px;--x1:-3px;--x2:4px'],
  [46, 18, 5,  '#93E2A6', '--d:10s;--dl:-3s;--h:22px;--x1:2px;--x2:-3px']];

export function lavaSVG(still){
  return `<svg viewBox="0 0 64 64" aria-hidden="true"><g clip-path="url(#lavaClip)"><rect width="64" height="64" fill="url(#lavaBg)"/><g filter="url(#lavaGoo)"><ellipse cx="32" cy="70" rx="40" ry="15" fill="#5CC98A"/>`
    + LAVA_BLOBS.map(([x, y, r, c, st]) => `<circle class="blob${still ? ' still' : ''}" cx="${x}" cy="${y}" r="${r}" fill="${c}" style="${st}"/>`).join('')
    + `</g><ellipse cx="24" cy="15" rx="13" ry="6" fill="#fff" opacity=".5" transform="rotate(-22 24 15)"/></g></svg>`;
}
export const LAVA_STILL = lavaSVG(true);

export const NAV_ICONS = {
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
  board: '<rect x="3" y="4" width="5" height="16" rx="1.5"/><rect x="10" y="4" width="5" height="11" rx="1.5"/><rect x="17" y="4" width="4" height="7" rx="1.5"/>',
  reports: '<path d="M3 4h18M5 4v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V4M12 15v5M8.5 20h7"/>',
  bz: '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19"/>',
  admin: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  log: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m4 7 8 6 8-6"/>'};

export const icon = (paths, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

export const CLIP_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';
