import { api } from './api';

export interface Sub { id: string; name: string; region: string; lat: number; lon: number; x: number; y: number; kv: number; busNode: string }
export interface Branch { id: string; name: string; kind: 'line' | 'transformer'; a: string; b: string; rating: number; brkF?: string; brkT?: string; nodeF: string; nodeT: string }
export interface Equip { kind: 'gen' | 'load' | 'shunt'; id: string; name: string; node: string; sub: string }
export interface Model {
  id: string; version: number; subs: Map<string, Sub>; branches: Branch[]; equip: Equip[];
  nodeSub: Map<string, string>; nodeToBrk: Map<string, string>; bbox: { x0: number; y0: number; x1: number; y1: number };
  raw: RawModel;
}
/* eslint-disable @typescript-eslint/no-explicit-any */
export type RawModel = any;

export function buildModel(raw: RawModel, version = 0): Model {
  const nodeVl = new Map<string, any>(raw.connectivityNodes.map((n: any) => [n.mrid, n]));
  const vlMap = new Map<string, any>(raw.voltageLevels.map((v: any) => [v.mrid, v]));
  const nodeSub = new Map<string, string>();
  for (const n of raw.connectivityNodes) nodeSub.set(n.mrid, vlMap.get(n.voltageLevel).substation);
  const subs = new Map<string, Sub>();
  const busOf = new Map<string, string>();
  for (const n of raw.connectivityNodes) if (n.mrid.endsWith('-BB')) busOf.set(nodeSub.get(n.mrid)!, n.mrid);
  const kvOf = new Map<string, number>();
  for (const v of raw.voltageLevels) kvOf.set(v.substation, v.nominalKV);
  // schematic projection: lon -> x, lat -> y (north up)
  const lats = raw.substations.map((s: any) => s.lat), lons = raw.substations.map((s: any) => s.lon);
  const x0 = Math.min(...lons), x1 = Math.max(...lons), y0 = Math.min(...lats), y1 = Math.max(...lats);
  for (const s of raw.substations) {
    subs.set(s.mrid, { id: s.mrid, name: s.name, region: s.region, lat: s.lat, lon: s.lon, x: (s.lon - x0) * 40, y: (y1 - s.lat) * 40, kv: kvOf.get(s.mrid) ?? 0, busNode: busOf.get(s.mrid) ?? '' });
  }
  // terminal node -> the breaker connecting it to a busbar
  const nodeToBrk = new Map<string, string>();
  for (const sw of raw.switches) {
    const bb1 = sw.node1.endsWith('-BB'), bb2 = sw.node2.endsWith('-BB');
    if (bb1 && !bb2) nodeToBrk.set(sw.node2, sw.mrid);
    else if (bb2 && !bb1) nodeToBrk.set(sw.node1, sw.mrid);
  }
  const branches: Branch[] = [];
  for (const [kind, arr] of [['line', raw.acLineSegments], ['transformer', raw.transformers]] as const) {
    for (const b of arr) {
      branches.push({ id: b.mrid, name: b.name, kind, a: nodeSub.get(b.node1)!, b: nodeSub.get(b.node2)!, rating: b.rating, nodeF: b.node1, nodeT: b.node2, brkF: nodeToBrk.get(b.node1), brkT: nodeToBrk.get(b.node2) });
    }
  }
  const equip: Equip[] = [];
  for (const [kind, arr] of [['gen', raw.generators], ['load', raw.loads], ['shunt', raw.shunts]] as const) for (const e of arr) equip.push({ kind, id: e.mrid, name: e.name, node: e.node, sub: nodeSub.get(e.node)! });
  void nodeVl;
  return { id: raw.id, version, subs, branches, equip, nodeSub, nodeToBrk, bbox: { x0: 0, y0: 0, x1: (x1 - x0) * 40, y1: (y1 - y0) * 40 }, raw };
}

export async function loadModel(id = 'ieee118'): Promise<Model> {
  const raw = await api<RawModel>(`/cim/models/${id}`);
  return buildModel(raw, raw.version ?? 0);
}
