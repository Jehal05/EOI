export type ThemeName = 'night' | 'day' | 'cb';
export const THEMES: ThemeName[] = ['night', 'day', 'cb'];

/** Theme and language are remembered per user (so each operator's preference follows them). */
const key = (user: string, k: string) => `gs.${user || '_'}.${k}`;
function read(k: string): string | null { try { return localStorage.getItem(k); } catch { return null; } }
function write(k: string, v: string) { try { localStorage.setItem(k, v); } catch { /* storage may be unavailable */ } }

export function applyTheme(t: ThemeName) { document.documentElement.dataset.theme = t; }
export function applyDim(d: boolean) { document.documentElement.dataset.dim = d ? '1' : '0'; }
export function loadPrefs(user: string) {
  const t = read(key(user, 'theme')) as ThemeName | null;
  return { theme: (t && THEMES.includes(t) ? t : 'night') as ThemeName, dim: read(key(user, 'dim')) === '1', lang: (read(key(user, 'lang')) === 'hi' ? 'hi' : 'en') as 'en' | 'hi' };
}
export function savePref(user: string, k: 'theme' | 'dim' | 'lang', v: string) { write(key(user, k), v); }
