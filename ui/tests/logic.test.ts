import { describe, expect, it } from 'vitest';
import { DICT } from '../src/lib/i18n';
import { buildModel } from '../src/lib/model';
import { buildSld } from '../src/lib/sld';
import { readFileSync } from 'node:fs';

describe('i18n', () => {
  it('Hindi and English define exactly the same keys', () => {
    expect(Object.keys(DICT.hi).sort()).toEqual(Object.keys(DICT.en).sort());
  });
  it('no empty translations; Hindi differs from English for user-facing text', () => {
    for (const [k, v] of Object.entries(DICT.hi)) expect(v.length, k).toBeGreaterThan(0);
    // unit symbols and standard technical abbreviations that are intentionally identical in Hindi
    const SYMBOLS = new Set(['cont.n1', 'nav.agc', 'sh.mw', 'dts.value', 'cont.n2', 'sc.kv', 'sc.ik', 'sc.mva', 'sc.xr', 'op.opf']);
    const same = Object.keys(DICT.en).filter(k => !SYMBOLS.has(k) && (DICT.hi as Record<string, string>)[k] === (DICT.en as Record<string, string>)[k]);
    expect(same, `untranslated: ${same.join(', ')}`).toEqual([]);
  });
  it('placeholders are preserved across languages', () => {
    for (const k of Object.keys(DICT.en) as (keyof typeof DICT.en)[]) {
      const ph = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join();
      expect(ph(DICT.hi[k]), k).toBe(ph(DICT.en[k]));
    }
  });
});

const raw = JSON.parse(readFileSync(new URL('../../data/ieee118.cim.json', import.meta.url), 'utf8'));
const m = buildModel(raw, 1);

describe('model-driven SLD', () => {
  it('derives substations, branches and breaker links from the CIM node-breaker model', () => {
    expect(m.subs.size).toBe(118);
    expect(m.branches.length).toBe(186);
    expect(m.branches.every(b => b.brkF && b.brkT)).toBe(true);
  });
  it('every equipment terminal in a substation becomes exactly one bay', () => {
    for (const id of ['SS-0001', 'SS-0010', 'SS-0069']) {
      const s = buildSld(m, id)!;
      const expected = m.branches.filter(b => m.nodeSub.get(b.nodeF) === id).length + m.branches.filter(b => m.nodeSub.get(b.nodeT) === id).length + m.equip.filter(e => e.sub === id).length;
      expect(s.bays.length).toBe(expected);
      expect(new Set(s.bays.map(b => b.brk)).size).toBe(s.bays.length);   // one breaker per bay
    }
  });
  it('bay telemetry tags exist in the SCADA point list naming convention', () => {
    const s = buildSld(m, 'SS-0010')!;
    for (const b of s.bays) { expect(b.brkTag).toMatch(/^BRK-.*\.STATE$/); if (b.pTag) expect(b.pTag).toMatch(/\.P$/); }
    expect(s.busTag).toBe('CN-0010-BB.V');
  });
  it('unknown substation returns null', () => { expect(buildSld(m, 'SS-9999')).toBeNull(); });
});
