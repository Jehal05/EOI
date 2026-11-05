import type { Model } from './model';

export interface Bay {
  brk: string; brkTag: string; kind: 'line' | 'transformer' | 'gen' | 'load' | 'shunt'; id: string; name: string;
  far?: string; pTag?: string; qTag?: string; x: number;
}
export interface Sld { sub: string; name: string; kv: number; busTag: string; bays: Bay[]; width: number }

/** Build a single-line diagram for one substation directly from the CIM node-breaker model (model-driven displays). */
export function buildSld(m: Model, subId: string): Sld | null {
  const s = m.subs.get(subId);
  if (!s) return null;
  const bays: Bay[] = [];
  const addEq = (node: string, kind: Bay['kind'], id: string, name: string, p?: string, q?: string, far?: string) => {
    const brk = m.nodeToBrk.get(node);
    if (!brk) return;
    bays.push({ brk, brkTag: `${brk}.STATE`, kind, id, name, far, pTag: p, qTag: q, x: 0 });
  };
  for (const b of m.branches) {
    if (m.nodeSub.get(b.nodeF) === subId) addEq(b.nodeF, b.kind, b.id, b.name, `${b.id}-F.P`, `${b.id}-F.Q`, m.subs.get(b.b)?.name);
    if (m.nodeSub.get(b.nodeT) === subId) addEq(b.nodeT, b.kind, b.id, b.name, `${b.id}-T.P`, `${b.id}-T.Q`, m.subs.get(b.a)?.name);
  }
  for (const e of m.equip) if (e.sub === subId) addEq(e.node, e.kind, e.id, e.name, `${e.id}.P`, e.kind === 'shunt' ? undefined : `${e.id}.Q`);
  const order = { gen: 0, transformer: 1, line: 2, shunt: 3, load: 4 } as const;
  bays.sort((a, b) => order[a.kind] - order[b.kind] || a.id.localeCompare(b.id));
  const gap = 118;
  bays.forEach((b, i) => { b.x = 70 + i * gap; });
  return { sub: subId, name: s.name, kv: s.kv, busTag: `${s.busNode}.V`, bays, width: Math.max(420, 70 + bays.length * gap) };
}
