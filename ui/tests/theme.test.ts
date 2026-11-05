import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

const css = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8');

function theme(name: string): Record<string, string> {
  const re = name === 'night' ? /:root,\s*:root\[data-theme="night"\]\s*\{([^}]*)\}/ : new RegExp(`:root\\[data-theme="${name}"\\]\\s*\\{([^}]*)\\}`);
  const m = css.match(re);
  if (!m) throw new Error('theme not found ' + name);
  return Object.fromEntries([...m[1].matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)].map(x => [x[1], x[2]]));
}
const lum = (hex: string) => {
  const c = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(v => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const ratio = (a: string, b: string) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };

describe.each(['night', 'day', 'cb'])('WCAG AA contrast: %s theme', name => {
  const t = theme(name);
  it('body text on every surface >= 4.5:1', () => {
    for (const s of ['bg', 'panel', 'panel2']) expect(ratio(t.text, t[s]), `text on ${s}`).toBeGreaterThanOrEqual(4.5);
  });
  it('muted text on panels >= 4.5:1', () => {
    for (const s of ['bg', 'panel', 'panel2']) expect(ratio(t.muted, t[s]), `muted on ${s}`).toBeGreaterThanOrEqual(4.5);
  });
  it('severity colours readable as text on panels >= 4.5:1', () => {
    for (const p of ['p1', 'p2', 'p3', 'p4']) for (const s of ['panel', 'panel2']) expect(ratio(t[p], t[s]), `${p} on ${s}`).toBeGreaterThanOrEqual(4.5);
  });
  it('accent on panel >= 4.5:1 and button label on accent >= 4.5:1', () => {
    expect(ratio(t.accent, t.panel)).toBeGreaterThanOrEqual(4.5);
    expect(ratio(t['accent-ink'], t.accent)).toBeGreaterThanOrEqual(4.5);
  });
  it('grid lines distinguishable from background (graphics >= 3:1)', () => {
    expect(ratio(t['grid-normal'], t.panel)).toBeGreaterThanOrEqual(3);
  });
});

describe('colour-blind safe theme', () => {
  it('severity colours are mutually distinguishable by luminance or hue family', () => {
    const t = theme('cb');
    expect(new Set([t.p1, t.p2, t.p3]).size).toBe(3);
    expect(Math.abs(lum(t.p1) - lum(t.p2))).toBeGreaterThan(0.1);
  });
});
