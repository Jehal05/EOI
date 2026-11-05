package gs.core;

import java.util.*;

/**
 * Parallel-run comparison: samples from a legacy SCADA feed are compared with this system's own value of the same
 * point at the moment they arrive. The comparison is a measurement aid, held in memory on the node that received the
 * samples (not replicated, not part of the state hash); the mode switch that disables control IS replicated and audited.
 * Tolerances are supplied by the caller: there is no standard value, and none is assumed beyond the defaults below.
 */
final class Parallel {
    static final class Stat {
        long n, within, stMismatch, oursBad;
        double sumAbs, maxAbs, lastLegacy = Double.NaN, lastOurs = Double.NaN;
        int lastLegacySt = -1, lastOursSt = -1;
        long lastTs;
    }

    private final Map<String, Stat> stats = new TreeMap<>();
    private final Set<String> unknown = new TreeSet<>();
    private double tolAbs = 0.5, tolRel = 0.005;
    private long since = System.currentTimeMillis(), samples;
    private String source = "";

    synchronized void reset() { stats.clear(); unknown.clear(); samples = 0; since = System.currentTimeMillis(); }

    synchronized void tolerance(double abs, double rel, String src) {
        if (!(abs >= 0) || !(rel >= 0) || !Double.isFinite(abs) || !Double.isFinite(rel)) throw new IllegalArgumentException("tolerances must be finite and >= 0");
        tolAbs = abs; tolRel = rel; if (src != null) source = src.length() > 80 ? src.substring(0, 80) : src;
    }

    /** Returns true when the tag is known to this system. */
    synchronized boolean ingest(Engine eng, String tag, Double legacyV, Integer legacySt, long ts) {
        samples++;
        Engine.Cfg cfg = eng.cfg.get(tag);
        if (cfg == null) { if (unknown.size() < 500) unknown.add(tag); return false; }
        Stat s = stats.computeIfAbsent(tag, k -> new Stat());
        Engine.PtState ours = eng.state.get(tag);
        s.n++; s.lastTs = ts;
        if (ours == null || !"good".equals(ours.q())) { s.oursBad++; return true; }
        if (cfg.status()) {
            int ls = legacySt == null ? -1 : legacySt;
            s.lastLegacySt = ls; s.lastOursSt = ours.st();
            if (ls == ours.st()) s.within++; else s.stMismatch++;
        } else if (legacyV != null && Double.isFinite(legacyV)) {
            double d = Math.abs(legacyV - ours.v());
            s.sumAbs += d; s.maxAbs = Math.max(s.maxAbs, d);
            s.lastLegacy = legacyV; s.lastOurs = ours.v();
            if (d <= tolAbs + tolRel * Math.abs(ours.v())) s.within++;
        }
        return true;
    }

    synchronized Map<String, Object> report(Engine eng, boolean enabled, Double minPct) {
        List<Map<String, Object>> rows = new ArrayList<>();
        long tn = 0, tw = 0;
        for (var e : stats.entrySet()) {
            Stat s = e.getValue();
            Engine.Cfg c = eng.cfg.get(e.getKey());
            boolean st = c != null && c.status();
            long cmp = s.n - s.oursBad;
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("tag", e.getKey()); r.put("kind", st ? "status" : "analog"); r.put("n", s.n); r.put("compared", cmp);
            r.put("withinPct", cmp == 0 ? null : 100.0 * s.within / cmp);
            if (st) { r.put("statusMismatches", s.stMismatch); r.put("lastLegacy", s.lastLegacySt); r.put("lastOurs", s.lastOursSt); }
            else { r.put("meanAbsDiff", cmp == 0 ? null : s.sumAbs / cmp); r.put("maxAbsDiff", s.maxAbs); r.put("lastLegacy", Double.isNaN(s.lastLegacy) ? null : s.lastLegacy); r.put("lastOurs", Double.isNaN(s.lastOurs) ? null : s.lastOurs); }
            r.put("oursNotGood", s.oursBad);
            rows.add(r);
            tn += cmp; tw += s.within;
        }
        rows.sort(Comparator.comparingDouble((Map<String, Object> r) -> r.get("withinPct") == null ? 101.0 : (Double) r.get("withinPct")));
        List<String> missing = new ArrayList<>();
        for (String t : eng.cfg.keySet()) if (!stats.containsKey(t)) missing.add(t);
        Collections.sort(missing);
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("enabled", enabled); out.put("sinceMs", since); out.put("samples", samples);
        out.put("tolerance", Map.of("abs", tolAbs, "rel", tolRel)); out.put("source", source);
        out.put("tagsCompared", stats.size()); out.put("comparisons", tn);
        out.put("agreementPct", tn == 0 ? null : 100.0 * tw / tn);
        out.put("ourTagsNeverSeenInLegacy", missing.size()); out.put("missingSample", missing.subList(0, Math.min(missing.size(), 50)));
        out.put("legacyTagsUnknownHere", unknown.size()); out.put("unknownSample", new ArrayList<>(unknown).subList(0, Math.min(unknown.size(), 50)));
        if (minPct != null) {
            Double a = (Double) out.get("agreementPct");
            out.put("criterion", Map.of("minAgreementPct", minPct, "met", a != null && a >= minPct && missing.isEmpty() && unknown.isEmpty() && stats.values().stream().allMatch(s -> s.stMismatch == 0)));
        }
        out.put("worst", rows.subList(0, Math.min(rows.size(), 100)));
        return out;
    }
}
