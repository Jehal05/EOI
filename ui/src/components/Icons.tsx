import { t, type Key } from '../lib/i18n';

/** Severity is always shape + colour, never colour alone (WCAG 1.4.1; works in the colour-blind theme too). */
export function Sev({ p, label = true }: { p: number; label?: boolean }) {
  const name = t(`prio.${p}` as Key);
  return (
    <span className={`sev p${p}`} role="img" aria-label={name} title={name}>
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        {p === 1 && <path d="M7 1.5 13 12.5H1z" fill="currentColor" />}
        {p === 2 && <path d="M7 1 13 7 7 13 1 7z" fill="currentColor" />}
        {p === 3 && <circle cx="7" cy="7" r="5.5" fill="currentColor" />}
        {p >= 4 && <circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" strokeWidth="2" />}
      </svg>
      {label && <span style={{ marginLeft: 6 }}>{name}</span>}
    </span>
  );
}

const P: Record<string, string> = {
  map: 'M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15',
  alarms: 'M12 3a6 6 0 0 0-6 6v4l-2 3h16l-2-3V9a6 6 0 0 0-6-6zM10 19a2 2 0 0 0 4 0',
  sld: 'M12 3v5M12 16v5M8 8h8v8H8zM3 12h5M16 12h5',
  soe: 'M4 5h16M4 10h16M4 15h10M4 20h7',
  contingency: 'M12 3l9 16H3zM12 10v4M12 17v.5',
  dts: 'M3 5h18v11H3zM8 20h8M12 16v4M7 12l3-3 3 2 4-4',
  interop: 'M4 8h13M13 4l4 4-4 4M20 16H7M11 12l-4 4 4 4',
  governance: 'M6 3v12M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6M18 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6M18 9c0 6-12 3-12 6',
  shed: 'M4 20h16M7 20V10M12 20V4M17 20v-7M3 7l4 3',
  logbook: 'M6 3h12v18H6zM9 8h6M9 12h6M9 16h4',
  outage: 'M4 6h16M4 12h10M4 18h13M18 10l3 2-3 2',
  dsa: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l3 3M15 15l3 3M6 18l3-3M15 9l3-3',
  agc: 'M3 12h4l2-6 4 12 2-6h6M3 20h18',
  forecast: 'M3 17l5-6 4 3 5-8 4 4M3 20h18',
  tcm: 'M3 8h18M3 16h18M7 4v16M17 4v16M10 12h4M12 10l2 2-2 2',
  security: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4',
  dispatch: 'M4 20V10M10 20V4M16 20v-7M22 20H2M4 6l6-2 6 5 5-3',
  shortcircuit: 'M13 2 4 14h7l-1 8 9-12h-7z',
  se: 'M3 17l5-6 4 3 6-9 3 3M3 21h18',
  trends: 'M3 19h18M5 15l4-5 3 3 5-7',
  health: 'M3 12h4l2-5 3 10 2-5h7',
  model: 'M12 3l8 4v10l-8 4-8-4V7zM12 12l8-5M12 12v9M12 12L4 7',
};
export function NavIcon({ n }: { n: string }) {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={P[n]} /></svg>;
}
