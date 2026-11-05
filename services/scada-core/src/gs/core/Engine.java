package gs.core;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Deterministic real-time core: RTDB + alarm engine + SOE.
 * State is a pure fold over the Rec stream; time comes only from Rec.ts (never the wall clock),
 * so replaying the event log reproduces state and alarms exactly.
 */
public final class Engine {
    public record Cfg(String tag, String kind, String qty, String unit, String substation, String element, Double low, Double high) {
        boolean status() { return "status".equals(kind); }
    }

    public record PtState(String tag, double v, int st, String q, String src, long ts, long seq) {}

    public record Soe(long seq, long ts, String src, String tag, String sub, String text) {
        Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("seq", seq); m.put("ts", ts); if (src != null) m.put("src", src); m.put("tag", tag); m.put("sub", sub); m.put("text", text);
            return m;
        }
    }

    static final long GROUP_WINDOW_MS = 10_000, CMD_WINDOW_MS = 15_000;
    static final int HIST_CAP = 20_000, SOE_CAP = 100_000;

    public final Map<String, Cfg> cfg;
    public final ConcurrentHashMap<String, PtState> state = new ConcurrentHashMap<>();
    private final LinkedHashMap<Long, Alarm> alarms = new LinkedHashMap<>();
    private final ArrayDeque<Alarm> history = new ArrayDeque<>();
    private final Map<String, Alarm> slot = new HashMap<>();          // "tag|L" limit/deadbus, "tag|Q" quality
    private final Map<String, Long> shelf = new TreeMap<>();
    private final Map<String, long[]> expect = new HashMap<>();       // tag -> {ts, wantedPosition}
    private final ArrayDeque<Soe> soe = new ArrayDeque<>();
    private final ArrayDeque<Long> raiseTimes = new ArrayDeque<>();
    private long nextId = 1, nextGroup = 1, groupStart = -1, groupId = 0;
    private long totalRaised, peak10, unknown;
    public volatile long alarmVersion;
    public volatile long lastSeq, lastTs;

    public Engine(Map<String, Cfg> cfg) { this.cfg = cfg; }

    // ------------------------------------------------------------------ apply
    /** True if applying this sample would change anything observable (used to avoid logging no-ops). */
    public boolean wouldChange(Rec r) {
        PtState p = state.get(r.tag);
        if (p == null) return cfg.containsKey(r.tag);
        Cfg c = cfg.get(r.tag);
        if (c == null) return false;
        return c.status() ? p.st != r.st || !p.q.equals(r.q) : Double.compare(p.v, r.v) != 0 || !p.q.equals(r.q);
    }

    public synchronized Object apply(Rec r) {
        lastSeq = r.seq; lastTs = r.ts;
        expireShelves(r.ts);
        switch (r.k) {
            case "S": sample(r); return null;
            case "CMD": {
                Cfg c = cfg.get(r.tag);
                expect.put(r.tag, new long[]{r.ts, "open".equals(r.action) ? 1 : 2});
                addSoe(r, null, r.tag, c == null ? "" : c.substation, r.action.toUpperCase() + " commanded by " + r.user + " (" + r.reason + ")");
                return null;
            }
            case "CMDRES": {
                Cfg c = cfg.get(r.tag);
                addSoe(r, null, r.tag, c == null ? "" : c.substation, r.action.toUpperCase() + (r.ok ? " executed" : " FAILED: " + r.err) + " [" + r.user + "]");
                return null;
            }
            case "ACK": return ack(r);
            case "SHELVE": {
                shelf.put(r.tag, r.ts + r.minutes * 60_000);
                for (Alarm a : alarms.values()) if (a.tag.equals(r.tag)) a.shelved = true;
                Cfg c = cfg.get(r.tag);
                addSoe(r, null, r.tag, c == null ? "" : c.substation, "SHELVED " + r.minutes + " min by " + r.user + " (" + r.reason + ")");
                alarmVersion++;
                return Boolean.TRUE;
            }
            case "UNSHELVE": {
                boolean had = shelf.remove(r.tag) != null;
                for (Alarm a : alarms.values()) if (a.tag.equals(r.tag)) a.shelved = false;
                if (had) { Cfg c = cfg.get(r.tag); addSoe(r, null, r.tag, c == null ? "" : c.substation, "UNSHELVED by " + r.user); alarmVersion++; }
                return had;
            }
            case "OUT": { applyOutage(r); return null; }
            case "PAR": {
                parallelRun = r.ok;
                addSoe(r, null, "", "", "PARALLEL-RUN MODE " + (r.ok ? "ON: remote control disabled" : "OFF") + " by " + r.user + " (" + r.reason + ")");
                return null;
            }
            case "AI": {
                aiEnabled = r.ok;
                addSoe(r, null, "", "", "AI/ML ADVISORY " + (r.ok ? "ENABLED" : "DISABLED") + " by " + r.user + " (" + r.reason + ")");
                return null;
            }
            case "AIUSE": {
                addSoe(r, null, "", "", "AI/ML advisory used by " + r.user + ": " + r.action + " " + r.tag + (r.reason == null ? "" : " (" + r.reason + ")"));
                return null;
            }
            case "LOG": applyLog(r); return null;
            case "HAND": applyHand(r); return null;
            default: return null;
        }
    }

    // ---- planned outages: request -> (study) -> approve/reject by a second person -> start -> complete, every step in the event log
    public final Map<String, Map<String, Object>> outages = new LinkedHashMap<>();
    private long nextOutage = 1;

    private synchronized void applyOutage(Rec r) {
        if ("create".equals(r.action)) {
            String id = String.format("OUT-%04d", nextOutage++);
            Map<String, Object> o = new LinkedHashMap<>();
            o.put("id", id); o.put("n", nextOutage - 1);
            o.put("equipment", new ArrayList<>(Arrays.asList(r.tag.split(","))));
            o.put("start", r.t1); o.put("end", r.t2); o.put("reason", r.reason); o.put("requester", r.user); o.put("status", "requested");
            o.put("study", r.data == null ? null : Json.parse(r.data)); o.put("created", r.ts); o.put("updated", r.ts);
            outages.put(id, o);
            addSoe(r, null, "", "", "OUTAGE " + id + " requested by " + r.user + " for " + r.tag + " (" + r.reason + ")");
            return;
        }
        String id = String.format("OUT-%04d", r.id);
        Map<String, Object> o = outages.get(id);
        if (o == null) return;
        String st = (String) o.get("status"), to = null;
        switch (r.action) {
            case "approve": if (st.equals("requested") && !r.user.equals(o.get("requester"))) to = "approved"; break;
            case "reject": if (st.equals("requested") && !r.user.equals(o.get("requester"))) to = "rejected"; break;
            case "cancel": if (st.equals("requested") || st.equals("approved")) to = "cancelled"; break;
            case "start": if (st.equals("approved")) to = "active"; break;
            case "complete": if (st.equals("active")) to = "completed"; break;
            default: break;
        }
        if (to == null) return;
        o.put("status", to); o.put("updated", r.ts);
        if (r.action.equals("approve") || r.action.equals("reject")) { o.put("decidedBy", r.user); o.put("decisionNote", r.reason); }
        addSoe(r, null, "", "", "OUTAGE " + id + " " + to.toUpperCase() + " by " + r.user + (r.reason == null ? "" : " (" + r.reason + ")"));
    }


    // ---- communications loss: points keep their last value but are marked "stale"; one alarm per substation
    static final double LOST_FRAC = 0.8, RECOVER_FRAC = 0.2;
    private final Map<String, Integer> staleN = new HashMap<>();
    private long commsGroupTs = -1, commsGroupId;
    private Map<String, Integer> totalN;

    private Map<String, Integer> totals() {
        if (totalN == null) { Map<String, Integer> t = new HashMap<>(); for (Cfg c : cfg.values()) t.merge(c.substation(), 1, Integer::sum); totalN = t; }
        return totalN;
    }

    private void commsUpdate(Rec r, Cfg c, boolean nowStale) {
        String sub = c.substation();
        int n = staleN.merge(sub, nowStale ? 1 : -1, Integer::sum);
        int tot = Math.max(1, totals().getOrDefault(sub, 1));
        double f = (double) n / tot;
        Alarm a = slot.get("SUB|" + sub + "|C");
        if (a == null && f >= LOST_FRAC) {
            Cfg sc = new Cfg("COMMS." + sub, "comms", "", "", sub, "Substation " + sub, null, null);
            Alarm na = raise(r.ts, sc, "COMMS", 2, "Communications lost: " + sub + " (" + n + " of " + tot + " points stale)", Double.NaN, false, 0);
            // a failure of the shared FEP/link takes many substations at once: the first is the "first out", the rest are consequential
            if (commsGroupTs >= 0 && r.ts - commsGroupTs <= GROUP_WINDOW_MS) { na.group = commsGroupId; na.consequential = true; na.firstOut = false; }
            else { commsGroupTs = r.ts; commsGroupId = nextGroup++; na.group = commsGroupId; na.firstOut = true; na.consequential = false; }
            slot.put("SUB|" + sub + "|C", na);
            addSoe(r, null, "COMMS." + sub, sub, "COMMUNICATIONS LOST (" + n + " of " + tot + " points stale)");
        } else if (a != null && f < RECOVER_FRAC) {
            rtn(a, r.ts); slot.remove("SUB|" + sub + "|C");
            addSoe(r, null, "COMMS." + sub, sub, "COMMUNICATIONS RESTORED (" + n + " of " + tot + " points still stale)");
        }
    }

    public synchronized List<Map<String, Object>> commsList() {
        Map<String, long[]> agg = new TreeMap<>();        // sub -> {stale, since(min ts)}
        for (PtState p : state.values()) {
            Cfg c = cfg.get(p.tag());
            if (c == null || !"stale".equals(p.q())) continue;
            long[] a = agg.computeIfAbsent(c.substation(), k -> new long[]{0, Long.MAX_VALUE});
            a[0]++; a[1] = Math.min(a[1], p.ts());
        }
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map.Entry<String, Integer> e : new TreeMap<>(totals()).entrySet()) {
            long[] a = agg.get(e.getKey());
            long st = a == null ? 0 : a[0];
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("substation", e.getKey()); m.put("points", e.getValue()); m.put("stale", st);
            m.put("state", slot.containsKey("SUB|" + e.getKey() + "|C") ? "lost" : st > 0 ? "degraded" : "ok");
            if (a != null) m.put("since", a[1]);
            out.add(m);
        }
        return out;
    }

    // ---- operator log (append-only; a correction is a new entry that references the one it amends) and shift handover
    static final int LOG_CAP = 100_000;
    private final ArrayList<Map<String, Object>> logs = new ArrayList<>();
    private long nextLog = 1;
    private final Map<Long, Map<String, Object>> hands = new LinkedHashMap<>();
    private long nextHand = 1;

    private synchronized void applyLog(Rec r) {
        Map<String, Object> e = new LinkedHashMap<>();
        e.put("n", nextLog++); e.put("ts", r.ts); e.put("user", r.user); e.put("category", r.tag); e.put("text", r.reason);
        if (r.id > 0) e.put("amends", r.id);
        if (r.data != null) e.put("ref", r.data);
        logs.add(e);
        while (logs.size() > LOG_CAP) logs.remove(0);
        String t = r.reason.length() > 80 ? r.reason.substring(0, 80) + "…" : r.reason;
        addSoe(r, null, "", r.data == null ? "" : r.data, "LOG #" + e.get("n") + " [" + r.tag + "] " + r.user + ": " + t);
    }

    public synchronized List<Map<String, Object>> logList(int limit, String cat, String q) {
        List<Map<String, Object>> out = new ArrayList<>();
        String needle = q == null ? null : q.toLowerCase(Locale.ROOT);
        for (int i = logs.size() - 1; i >= 0 && out.size() < limit; i--) {
            Map<String, Object> e = logs.get(i);
            if (cat != null && !cat.equals(e.get("category"))) continue;
            if (needle != null && !(((String) e.get("text")).toLowerCase(Locale.ROOT).contains(needle) || ((String) e.get("user")).contains(needle))) continue;
            out.add(e);
        }
        return out;
    }

    public synchronized Map<String, Object> pendingHandover() {
        for (Map<String, Object> h : hands.values()) if ("pending".equals(h.get("status"))) return h;
        return null;
    }

    public synchronized List<Map<String, Object>> handoverList(int limit) {
        List<Map<String, Object>> all = new ArrayList<>(hands.values());
        Collections.reverse(all);
        return all.size() > limit ? all.subList(0, limit) : all;
    }

    private synchronized void applyHand(Rec r) {
        if ("create".equals(r.action)) {
            if (pendingHandover() != null) return;                     // one open handover at a time
            Map<String, Object> h = new LinkedHashMap<>();
            h.put("n", nextHand); h.put("ts", r.ts); h.put("from", r.user); if (r.tag != null && !r.tag.isEmpty()) h.put("to", r.tag);
            h.put("notes", r.reason); h.put("snapshot", r.data == null ? null : Json.parse(r.data)); h.put("status", "pending");
            hands.put(nextHand, h);
            addSoe(r, null, "", "", "SHIFT HANDOVER #" + nextHand + " issued by " + r.user + (h.containsKey("to") ? " to " + h.get("to") : ""));
            nextHand++;
        } else if ("accept".equals(r.action)) {
            Map<String, Object> h = hands.get(r.id);
            if (h == null || !"pending".equals(h.get("status")) || r.user.equals(h.get("from"))) return;
            if (h.containsKey("to") && !r.user.equals(h.get("to"))) return;
            h.put("status", "accepted"); h.put("acceptedBy", r.user); h.put("acceptedTs", r.ts); h.put("acceptNote", r.reason);
            addSoe(r, null, "", "", "SHIFT HANDOVER #" + r.id + " accepted by " + r.user + " (from " + h.get("from") + ")");
        }
    }

    public synchronized List<Map<String, Object>> outageList() { return new ArrayList<>(outages.values()); }

    /** Global switch for every AI/ML advisory feature; part of the deterministic state, changed only by an audited admin record. */
    public volatile boolean aiEnabled = true;
    /** Parallel-run (shadow) mode: this system observes while a legacy system is in control; commands are refused. */
    public volatile boolean parallelRun = false;

    private void sample(Rec r) {
        Cfg c = cfg.get(r.tag);
        if (c == null) { unknown++; return; }
        PtState prev = state.get(r.tag);
        PtState ns = new PtState(r.tag, c.status() ? Double.NaN : r.v, c.status() ? r.st : -1, r.q, r.src, r.ts, r.seq);
        state.put(r.tag, ns);
        boolean bad = !"good".equals(r.q), stale = "stale".equals(r.q);
        boolean wasStale = prev != null && "stale".equals(prev.q);
        // quality alarm (slot Q); stale data is reported once per substation (communications-loss alarm), never once per point
        Alarm qa = slot.get(r.tag + "|Q");
        if (bad && !stale && qa == null) slot.put(r.tag + "|Q", raise(r.ts, c, "QUALITY", 3, "Data quality " + r.q, Double.NaN, false, 0));
        else if (!bad && qa != null) { rtn(qa, r.ts); slot.remove(r.tag + "|Q"); }
        if (stale != wasStale) commsUpdate(r, c, stale);
        if (c.status()) statusChange(r, c, prev, ns);
        else if (!bad) analog(r, c, ns);
    }

    private static String pos(int s) { return switch (s) { case 1 -> "OPEN"; case 2 -> "CLOSED"; case 3 -> "FAULT"; default -> "INDETERMINATE"; }; }

    private void statusChange(Rec r, Cfg c, PtState prev, PtState ns) {
        if (prev == null || prev.st == ns.st) return;
        long[] e = expect.get(c.tag());
        boolean commanded = e != null && r.ts - e[0] <= CMD_WINDOW_MS && e[1] == ns.st;
        if (commanded) expect.remove(c.tag());
        addSoe(r, r.src, c.tag(), c.substation(), pos(prev.st) + " → " + pos(ns.st) + (commanded ? " (commanded)" : ""));
        if (commanded) return;
        if (ns.st == 1) raise(r.ts, c, "TRIP", 1, c.element() + " OPENED (uncommanded)", Double.NaN, true, 0);
        else if (ns.st == 2) raise(r.ts, c, "STATE", 3, c.element() + " CLOSED (uncommanded)", Double.NaN, true, 0);
        else raise(r.ts, c, "STATE", 2, c.element() + " position " + pos(ns.st), Double.NaN, true, 0);
    }

    private static final int DEAD = 99;

    private void analog(Rec r, Cfg c, PtState ns) {
        Alarm cur = slot.get(c.tag() + "|L");
        int curLvl = cur == null ? 0 : cur.level;
        int target = level(c, ns.v, curLvl);
        if (target == curLvl) { if (cur != null) cur.value = ns.v; return; }
        if (cur != null) { rtn(cur, r.ts); slot.remove(c.tag() + "|L"); }
        if (target == 0) return;
        String type; int prio; String text;
        boolean flow = "P".equals(c.qty()) || "Q".equals(c.qty());
        if (target == DEAD) { type = "DEADBUS"; prio = 1; text = "Bus de-energised (" + fmt(ns.v) + " " + c.unit() + ")"; }
        else {
            type = switch (target) { case 2 -> "HIHI"; case 1 -> "HI"; case -1 -> "LO"; default -> "LOLO"; };
            prio = Math.abs(target) == 2 ? ("V".equals(c.qty()) ? 2 : 1) : (flow ? 2 : 3);
            text = c.element() + " " + c.qty() + " " + type + " " + fmt(ns.v) + " " + c.unit();
        }
        Alarm a = raise(r.ts, c, type, prio, text, ns.v, false, target);
        slot.put(c.tag() + "|L", a);
    }

    static String fmt(double v) { return String.format(Locale.ROOT, "%.2f", v); }

    private static int level(Engine.Cfg c, double v, int cur) {
        Double hi = c.high(), lo = c.low();
        if ("V".equals(c.qty()) && hi != null && lo != null && v < 0.2 * ((hi + lo) / 2)) return DEAD;
        final double mg = "V".equals(c.qty()) ? 0.03 : 0.10;       // second-tier margin beyond the limit
        int t = 0;
        // alarms trigger a small tolerance beyond the limit (1 %) so sensor noise on a value pinned AT its limit cannot chatter
        if (hi != null) { double tol = 0.01 * Math.max(Math.abs(hi), 1); if (v > hi + mg * Math.abs(hi) + tol) t = 2; else if (v > hi + tol) t = 1; }
        if (t == 0 && lo != null) { double tol = 0.01 * Math.max(Math.abs(lo), 1); if (v < lo - mg * Math.abs(lo) - tol) t = -2; else if (v < lo - tol) t = -1; }
        if (cur != DEAD && cur > 0 && t < cur && hi != null) {      // improving: each threshold has its own hysteresis band
            double db = 0.02 * Math.max(Math.abs(hi), 1);
            int h = 0;
            if (cur == 2 && v > hi + mg * Math.abs(hi) - db) h = 2; else if (v > hi - db) h = 1;
            t = Math.max(t, h);
        }
        if (cur != DEAD && cur < 0 && t > cur && lo != null) {
            double db = 0.02 * Math.max(Math.abs(lo), 1);
            int h = 0;
            if (cur == -2 && v < lo - mg * Math.abs(lo) + db) h = -2; else if (v < lo + db) h = -1;
            t = Math.min(t, h);
        }
        return t;
    }

    private Alarm raise(long ts, Cfg c, String type, int prio, String text, double v, boolean latching, int level) {
        Alarm a = new Alarm();
        a.id = nextId++; a.tag = c.tag(); a.type = type; a.prio = prio; a.text = text; a.substation = c.substation();
        a.raiseTs = ts; a.value = v; a.latching = latching; a.level = level;
        if (groupStart >= 0 && ts - groupStart > GROUP_WINDOW_MS) groupStart = -1;
        if ("TRIP".equals(type)) {
            if (groupStart < 0) { groupStart = ts; groupId = nextGroup++; a.firstOut = true; } else a.consequential = true;
            a.group = groupId;
        } else if (groupStart >= 0) { a.group = groupId; a.consequential = true; }
        Long sh = shelf.get(c.tag());
        a.shelved = sh != null && sh > ts;
        alarms.put(a.id, a);
        totalRaised++;
        raiseTimes.addLast(ts);
        while (!raiseTimes.isEmpty() && ts - raiseTimes.peekFirst() > 600_000) raiseTimes.pollFirst();
        peak10 = Math.max(peak10, raiseTimes.size());
        alarmVersion++;
        return a;
    }

    private void rtn(Alarm a, long ts) {
        if (a.latching) return;
        a.rtnTs = ts;
        if (Alarm.ACTIVE_ACK.equals(a.state)) close(a); else if (Alarm.ACTIVE_UNACK.equals(a.state)) a.state = Alarm.RTN_UNACK;
        alarmVersion++;
    }

    private void close(Alarm a) {
        a.state = Alarm.CLOSED;
        alarms.remove(a.id);
        history.addLast(a);
        while (history.size() > HIST_CAP) history.pollFirst();
        for (String k : List.of(a.tag + "|L", a.tag + "|Q")) if (slot.get(k) == a) slot.remove(k);
    }

    private Object ack(Rec r) {
        int n = 0;
        for (Alarm a : new ArrayList<>(alarms.values())) {
            if (!r.all && a.id != r.id) continue;
            if (r.all && a.shelved) continue;
            if (Alarm.ACTIVE_UNACK.equals(a.state)) {
                a.ackBy = r.user; a.ackTs = r.ts;
                if (a.latching) close(a); else a.state = Alarm.ACTIVE_ACK;
                n++;
            } else if (Alarm.RTN_UNACK.equals(a.state)) { a.ackBy = r.user; a.ackTs = r.ts; close(a); n++; }
        }
        if (n > 0) { alarmVersion++; addSoe(r, null, "-", "", "ACK " + (r.all ? "all (" + n + ")" : "#" + r.id) + " by " + r.user); }
        return n;
    }

    private void expireShelves(long ts) {
        if (shelf.isEmpty()) return;
        List<String> gone = new ArrayList<>();
        for (Map.Entry<String, Long> e : shelf.entrySet()) if (e.getValue() <= ts) gone.add(e.getKey());
        for (String t : gone) {
            shelf.remove(t);
            for (Alarm a : alarms.values()) if (a.tag.equals(t)) a.shelved = false;
            alarmVersion++;
        }
    }

    private void addSoe(Rec r, String src, String tag, String sub, String text) {
        soe.addLast(new Soe(r.seq, r.ts, src, tag, sub, text));
        while (soe.size() > SOE_CAP) soe.pollFirst();
    }

    // ------------------------------------------------------------------ queries
    public synchronized boolean hasAlarm(long id) { return alarms.containsKey(id); }

    public synchronized List<Map<String, Object>> activeAlarms() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Alarm a : alarms.values()) out.add(a.toMap());
        return out;
    }

    public synchronized List<Map<String, Object>> alarmHistory(int limit) {
        List<Map<String, Object>> out = new ArrayList<>();
        Iterator<Alarm> it = history.descendingIterator();
        while (it.hasNext() && out.size() < limit) out.add(it.next().toMap());
        return out;
    }

    public synchronized List<Map<String, Object>> recentSoe(int limit) {
        List<Map<String, Object>> out = new ArrayList<>();
        Iterator<Soe> it = soe.descendingIterator();
        while (it.hasNext() && out.size() < limit) out.add(it.next().toMap());
        return out;
    }

    public synchronized Map<String, Object> alarmStats() {
        Map<String, Object> m = new LinkedHashMap<>();
        long[] byPrio = new long[5]; long unack = 0, shelved = 0;
        for (Alarm a : alarms.values()) {
            if (a.shelved) { shelved++; continue; }
            byPrio[a.prio]++;
            if (Alarm.ACTIVE_UNACK.equals(a.state) || Alarm.RTN_UNACK.equals(a.state)) unack++;
        }
        m.put("active", alarms.size() - shelved); m.put("unacked", unack); m.put("shelved", shelved);
        m.put("p1", byPrio[1]); m.put("p2", byPrio[2]); m.put("p3", byPrio[3]); m.put("p4", byPrio[4]);
        m.put("raised_total", totalRaised); m.put("raised_last_10min", (long) raiseTimes.size()); m.put("peak_10min", peak10);
        m.put("unknown_tags", unknown);
        return m;
    }

    public String stateHash() {
        List<String> tags = new ArrayList<>(state.keySet());
        Collections.sort(tags);
        StringBuilder b = new StringBuilder();
        for (String t : tags) { PtState p = state.get(t); b.append(t).append('|').append(p.v).append('|').append(p.st).append('|').append(p.q).append('\n'); }
        b.append("AI|").append(aiEnabled).append('\n');
        b.append("PAR|").append(parallelRun).append('\n');
        synchronized (this) {
            b.append("LOG|").append(logs.size()).append('|').append(nextLog).append('\n');
            for (Map<String, Object> h : hands.values()) b.append("HAND|").append(h.get("n")).append('|').append(h.get("status")).append('|').append(h.get("from")).append('|').append(h.get("acceptedBy")).append('\n');
        }
        synchronized (this) { for (Map<String, Object> o : outages.values()) b.append("OUT|").append(o.get("id")).append('|').append(o.get("status")).append('|').append(o.get("equipment")).append('|').append(o.get("start")).append('|').append(o.get("end")).append('|').append(o.get("requester")).append('|').append(o.get("decidedBy")).append('\n'); }
        return sha(b.toString());
    }

    public synchronized String alarmHash() {
        StringBuilder b = new StringBuilder();
        for (Alarm a : alarms.values()) b.append(a.toMap()).append('\n');
        b.append("H").append(history.size()).append('|');
        for (Alarm a : history) b.append(a.id).append(a.state).append(a.ackBy).append(a.rtnTs).append(',');
        b.append("S").append(shelf).append("|G").append(nextGroup).append('|').append(nextId);
        return sha(b.toString());
    }

    static String sha(String s) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8))); }
        catch (Exception e) { throw new IllegalStateException(e); }
    }
}
