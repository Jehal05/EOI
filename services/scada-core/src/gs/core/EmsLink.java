package gs.core;

import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;

/**
 * Periodic bridge to ems-analytics: builds the real-time network from SCADA breaker states (operational overlay on the CIM model),
 * runs state estimation on the live RTDB measurements and an N-1 contingency screen, and publishes a summary.
 * Results are advisory displays; the deterministic alarm engine never depends on them.
 */
public final class EmsLink {
    private final Core core;
    private final byte[] secret;
    private final String ems, model;
    private final Api api;
    private final long periodS;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();

    public EmsLink(Core core, byte[] secret, String ems, String model, Api api, long periodS) {
        this.core = core; this.secret = secret; this.ems = ems; this.model = model; this.api = api; this.periodS = periodS;
    }

    public void start() {
        Thread t = new Thread(() -> {
            try { Thread.sleep(8000); } catch (InterruptedException e) { return; }
            while (true) {
                long wait = periodS;
                try { cycle(); } catch (Exception e) { api.emsSummary = Map.of("error", String.valueOf(e.getMessage()), "ts", System.currentTimeMillis()); wait = Math.min(periodS, 8); }
                try { Thread.sleep(wait * 1000); } catch (InterruptedException e) { return; }
            }
        }, "ems-link");
        t.setDaemon(true);
        t.start();
    }

    /** Server-side call to the EMS for studies the core must not take from a client (outage studies, switching plans). */
    public Map<String, Object> call(String path, Map<String, Object> body, int timeoutS) throws Exception {
        String tok = Token.mint(secret, "scada-core-ems", "viewer", List.of("*"), System.currentTimeMillis() / 1000 + 120);
        Map<String, Object> b = new LinkedHashMap<>(body); b.put("model", model);
        HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(ems + path)).timeout(Duration.ofSeconds(timeoutS)).header("Authorization", "Bearer " + tok)
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(Json.str(b))).build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) throw new IllegalStateException(path + " -> " + r.statusCode());
        return Json.map(Json.parse(r.body()));
    }

    /** Breakers currently open in the field model (the live overlay). */
    public List<String> openBreakers() {
        List<String> open = new ArrayList<>();
        for (Engine.Cfg c : core.eng.cfg.values()) {
            if (!c.status() || c.element() == null || !c.element().startsWith("BRK-")) continue;
            Engine.PtState s = core.eng.state.get(c.tag());
            if (s != null && s.st() == 1) open.add(c.element());
        }
        Collections.sort(open);
        return open;
    }

    private String post(String path, Object body, String tok) throws Exception {
        HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(ems + path)).timeout(Duration.ofSeconds(120)).header("Authorization", "Bearer " + tok)
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(Json.str(body))).build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) throw new IllegalStateException(path + " -> " + r.statusCode() + " " + r.body().substring(0, Math.min(200, r.body().length())));
        return r.body();
    }

    private Engine.PtState good(String tag) {
        Engine.PtState s = core.eng.state.get(tag);
        return s != null && "good".equals(s.q()) ? s : null;
    }

    @SuppressWarnings("unchecked")
    void cycle() throws Exception {
        long t0 = System.nanoTime();
        String tok = Token.mint(secret, "scada-core-ems", "viewer", List.of("*"), System.currentTimeMillis() / 1000 + 300);
        List<String> open = new ArrayList<>();
        for (Engine.Cfg c : core.eng.cfg.values()) {
            if (!c.status() || c.element() == null || !c.element().startsWith("BRK-")) continue;
            Engine.PtState s = core.eng.state.get(c.tag());
            if (s != null && s.st() == 1) open.add(c.element());
        }
        Collections.sort(open);
        // the same overlay the EMS will use, to map SCADA tags to estimator elements
        HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(cimBusbranch(open))).header("Authorization", "Bearer " + tok).timeout(Duration.ofSeconds(30)).build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) throw new IllegalStateException("busbranch " + r.statusCode());
        Map<String, Object> bb = Json.map(Json.parse(r.body()));
        Set<Integer> dead = new HashSet<>();
        for (Object o : (List<?>) bb.get("islands")) { Map<String, Object> is = Json.map(o); if (Json.b(is, "dead")) for (Object b : (List<?>) is.get("buses")) dead.add(((Number) b).intValue()); }
        List<Object> meas = new ArrayList<>();
        int nBus = 0;
        double[] pinj, qinj, pVar, qVar, pDb, qDb; int nb = ((List<?>) bb.get("buses")).size();
        pinj = new double[nb]; qinj = new double[nb]; pVar = new double[nb]; qVar = new double[nb]; pDb = new double[nb]; qDb = new double[nb];   // Var: sum of squared component magnitudes
        boolean[] pOk = new boolean[nb], qOk = new boolean[nb];
        Arrays.fill(pOk, true); Arrays.fill(qOk, true);
        for (Object o : (List<?>) bb.get("buses")) {
            Map<String, Object> b = Json.map(o);
            int idx = (int) Json.d(b, "idx", -1);
            if (dead.contains(idx)) { pOk[idx] = qOk[idx] = false; continue; }
            Engine.PtState v = good(((List<String>) (List<?>) b.get("nodes")).get(0) + ".V");
            if (v == null) { pOk[idx] = qOk[idx] = false; continue; }
            meas.add(m("vm", Json.s(b, "name"), v.v() / Json.d(b, "baseKV", 1), SIG_V)); nBus++;
        }
        for (Object o : (List<?>) bb.get("gens")) {
            Map<String, Object> g = Json.map(o); int bi = (int) Json.d(g, "bus", -1); String id = Json.s(g, "mrid");
            Engine.PtState p = good(id + ".P"), q = good(id + ".Q");
            if (p == null) pOk[bi] = false; else { pinj[bi] += p.v(); pVar[bi] += p.v() * p.v(); pDb[bi] += sq(dbStd(id + ".P")); }
            if (q == null) qOk[bi] = false; else { qinj[bi] += q.v(); qVar[bi] += q.v() * q.v(); qDb[bi] += sq(dbStd(id + ".Q")); }
        }
        for (Object o : (List<?>) bb.get("loads")) {
            Map<String, Object> l = Json.map(o); int bi = (int) Json.d(l, "bus", -1); String id = Json.s(l, "mrid");
            Engine.PtState p = good(id + ".P"), q = good(id + ".Q");
            if (p == null) pOk[bi] = false; else { pinj[bi] -= p.v(); pVar[bi] += p.v() * p.v(); pDb[bi] += sq(dbStd(id + ".P")); }
            if (q == null) qOk[bi] = false; else { qinj[bi] -= q.v(); qVar[bi] += q.v() * q.v(); qDb[bi] += sq(dbStd(id + ".Q")); }
        }
        for (Object o : (List<?>) bb.get("buses")) {
            Map<String, Object> b = Json.map(o); int idx = (int) Json.d(b, "idx", -1);
            if (dead.contains(idx)) continue;
            if (pOk[idx]) meas.add(m("pinj", Json.s(b, "name"), pinj[idx], Math.hypot(sigPQ(Math.sqrt(pVar[idx]), 1.5), Math.sqrt(pDb[idx]))));
            if (qOk[idx]) meas.add(m("qinj", Json.s(b, "name"), qinj[idx], Math.hypot(sigPQ(Math.sqrt(qVar[idx]), 1.5), Math.sqrt(qDb[idx]))));
        }
        for (Object o : (List<?>) bb.get("branches")) {
            String id = Json.s(Json.map(o), "mrid");
            for (String[] k : new String[][]{{"pf", "-F.P"}, {"qf", "-F.Q"}, {"pt", "-T.P"}, {"qt", "-T.Q"}}) {
                Engine.PtState s = good(id + k[1]);
                if (s != null) meas.add(m(k[0], id, s.v(), Math.hypot(sigPQ(s.v(), 1.0), dbStd(id + k[1]))));
            }
        }
        Map<String, Object> req = new LinkedHashMap<>();
        req.put("model", model); req.put("open_switches", open); req.put("measurements", meas); req.put("bad_data", true);
        long ts0 = System.nanoTime();
        Map<String, Object> se = Json.map(Json.parse(post("/v1/state-estimation", req, tok)));
        double seMs = (System.nanoTime() - ts0) / 1e6;
        se.remove("buses");
        Map<String, Object> creq = new LinkedHashMap<>();
        creq.put("model", model); creq.put("open_switches", open); creq.put("workers", 2); creq.put("top", 10);
        // operating voltage band: same defaults as the model data (VoltageLevel vMin/vMax), overridable per site
        creq.put("vmin", Double.parseDouble(System.getenv().getOrDefault("GS_VMIN", "0.93")));
        creq.put("vmax", Double.parseDouble(System.getenv().getOrDefault("GS_VMAX", "1.07")));
        long tc0 = System.nanoTime();
        Map<String, Object> ca = Json.map(Json.parse(post("/v1/contingency", creq, tok)));
        double caMs = (System.nanoTime() - tc0) / 1e6;
        Map<String, Object> n2 = null;
        double n2Ms = 0;
        try {   // N-2 is best-effort: a failure here must never take down the SE / N-1 cycle
            Map<String, Object> n2req = new LinkedHashMap<>(creq);
            n2req.put("top", 15); n2req.put("k_top", 40); n2req.put("verify", 40);
            long tn0 = System.nanoTime();
            n2 = Json.map(Json.parse(post("/v1/contingency-n2", n2req, tok)));
            n2Ms = (System.nanoTime() - tn0) / 1e6;
        } catch (Exception e) {
            n2 = new LinkedHashMap<>(); n2.put("error", String.valueOf(e.getMessage()));
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("ts", System.currentTimeMillis()); out.put("open_breakers", open.size());
        out.put("measurements_used", (long) meas.size()); out.put("se", se); out.put("se_roundtrip_ms", seMs);
        out.put("contingency", ca); out.put("contingency_roundtrip_ms", caMs);
        out.put("contingency_n2", n2); out.put("contingency_n2_roundtrip_ms", n2Ms);
        out.put("cycle_ms", (System.nanoTime() - t0) / 1e6);
        api.emsSummary = out;
    }

    private String cimBusbranch(List<String> open) {
        String base = System.getenv().getOrDefault("CIM_URL", "http://127.0.0.1:8081");
        return base + "/models/" + model + "/busbranch" + (open.isEmpty() ? "" : "?open=" + String.join(",", open));
    }

    /** Measurement accuracy model: 0.1 pu-% on voltage; power: absolute floor + 0.2 % of reading (transducer noise + report-by-exception deadband). */
    /** Report-by-exception deadband: a value is only re-sent after it moved by DEADBAND x point range, so the held value can be
     *  stale by up to that much (uniform, std = limit/sqrt3). Range rule and default equal outstation-sim's (-deadband 0.002); override GS_DEADBAND for other RTUs. */
    static final double DEADBAND = Double.parseDouble(System.getenv().getOrDefault("GS_DEADBAND", "0.002"));
    private double dbStd(String tag) {
        Engine.Cfg c = core.eng.cfg.get(tag);
        double rng = 100.0;
        if (c != null && c.high() != null && c.low() != null) rng = Math.max(Math.abs(c.high() - c.low()), 1);
        else if (c != null && c.high() != null) rng = Math.max(Math.abs(c.high()), 1);
        return rng * DEADBAND / Math.sqrt(3);
    }
    private static double sq(double x) { return x * x; }
    static final double SIG_V = 0.001;
    static double sigPQ(double v, double floorMul) { return floorMul * 0.12 + 0.002 * Math.abs(v); }

    private static Map<String, Object> m(String kind, String ref, double v, double sigma) {
        Map<String, Object> x = new LinkedHashMap<>();
        x.put("kind", kind); x.put("ref", ref); x.put("value", v); x.put("sigma", sigma);
        return x;
    }
}
