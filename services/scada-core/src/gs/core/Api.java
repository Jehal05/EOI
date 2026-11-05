package gs.core;

import com.sun.net.httpserver.*;

import java.io.*;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

/** HTTP surface of scada-core. Every route authenticates; commands additionally need MFA, jurisdiction and leadership. */
public final class Api {
    static final class HttpErr extends RuntimeException {
        private static final long serialVersionUID = 1L;
        final int code;
        HttpErr(int code, String m) { super(m); this.code = code; }
    }

    private final Core core;
    private final Users users;
    private final Cluster cluster;
    private final byte[] secret, auditKey;
    private final String node, fepUrl, cimUrl, emsUrl;
    private final Path logFile, uiDir;
    private final Map<String, String> regionOfSub;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).executor(Executors.newVirtualThreadPerTaskExecutor()).build();
    private final long ttlSec;
    private final ConcurrentHashMap<String, ArrayDeque<Long>> cmdTimes = new ConcurrentHashMap<>();
    private final Set<String> inflight = ConcurrentHashMap.newKeySet();
    private final AtomicInteger streams = new AtomicInteger();
    public final LongAdder ingestBatches = new LongAdder(), ingestLines = new LongAdder(), ingestBad = new LongAdder(), cmds = new LongAdder(), cmdFail = new LongAdder(), authFail = new LongAdder();
    public volatile Map<String, Object> emsSummary = Map.of();
    public volatile EmsLink emsLink;
    private HttpServer server;
    private final long startMs = System.currentTimeMillis();

    public Api(Core core, Users users, Cluster cluster, byte[] secret, byte[] auditKey, String node, String fepUrl, String cimUrl, String emsUrl,
               Path logFile, Path uiDir, Map<String, String> regionOfSub, long ttlSec) {
        this.core = core; this.users = users; this.cluster = cluster; this.secret = secret; this.auditKey = auditKey; this.node = node;
        this.fepUrl = fepUrl; this.cimUrl = cimUrl; this.emsUrl = emsUrl; this.logFile = logFile; this.uiDir = uiDir; this.regionOfSub = regionOfSub; this.ttlSec = ttlSec;
    }

    public void start(InetSocketAddress addr) throws IOException {
        server = HttpServer.create(addr, 1024);
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        server.createContext("/", this::dispatch);
        server.start();
    }

    public void stop() { if (server != null) server.stop(0); }

    // ------------------------------------------------------------------ plumbing
    private interface H { void run(HttpExchange x) throws Exception; }

    private void dispatch(HttpExchange x) throws IOException {
        String path = x.getRequestURI().getPath(), m = x.getRequestMethod();
        try {
            x.getResponseHeaders().set("X-Content-Type-Options", "nosniff");
            x.getResponseHeaders().set("X-Frame-Options", "DENY");
            x.getResponseHeaders().set("Referrer-Policy", "no-referrer");
            if (path.startsWith("/api/") || path.equals("/health")) x.getResponseHeaders().set("Cache-Control", "no-store");
            switch (m + " " + path) {
                case "GET /health" -> health(x);
                case "POST /api/login" -> login(x);
                case "POST /api/ingest" -> ingest(x);
                case "GET /api/snapshot" -> snapshot(x);
                case "GET /api/stream" -> stream(x);
                case "GET /api/alarms" -> alarms(x);
                case "POST /api/alarms/ack" -> ack(x);
                case "POST /api/alarms/shelve" -> shelve(x, true);
                case "POST /api/alarms/unshelve" -> shelve(x, false);
                case "GET /api/outages" -> { auth(x, "viewer"); send(x, 200, Map.of("outages", core.eng.outageList(), "now", System.currentTimeMillis())); }
                case "POST /api/outages" -> outageCreate(x);
                case "POST /api/outages/decision" -> outageDecision(x);
                case "POST /api/outages/cancel" -> outageLifecycle(x, "cancel");
                case "POST /api/outages/start" -> outageLifecycle(x, "start");
                case "POST /api/outages/complete" -> outageLifecycle(x, "complete");
                case "POST /api/outages/plan" -> outagePlan(x);
                case "GET /api/log" -> logGet(x);
                case "POST /api/log" -> logPost(x);
                case "GET /api/handover" -> { auth(x, "viewer"); Map<String, Object> pend = core.eng.pendingHandover(); Map<String, Object> o = new LinkedHashMap<>(); o.put("pending", pend); o.put("history", core.eng.handoverList(20)); send(x, 200, o); }
                case "GET /api/handover/draft" -> { auth(x, "operator"); send(x, 200, handoverSnapshot()); }
                case "POST /api/handover" -> handoverCreate(x);
                case "POST /api/handover/accept" -> handoverAccept(x);
                case "GET /api/comms" -> commsGet(x);
                case "GET /api/ai" -> { auth(x, "viewer"); send(x, 200, Map.of("enabled", core.eng.aiEnabled)); }
                case "POST /api/ai/switch" -> aiSwitch(x);
                case "GET /api/parallel" -> parallelGet(x);
                case "POST /api/parallel/switch" -> parallelSwitch(x);
                case "POST /api/parallel/samples" -> parallelSamples(x);
                case "POST /api/parallel/reset" -> { auth(x, "admin"); parallel.reset(); send(x, 200, Map.of("reset", true)); }
                case "POST /api/ai/use" -> aiUse(x);
                case "GET /api/soe" -> soe(x);
                case "POST /api/command" -> command(x);
                case "GET /api/metrics" -> metrics(x);
                case "GET /api/verify-replay" -> verify(x);
                case "POST /api/replicate" -> replicate(x);
                case "POST /api/cluster/vote" -> clusterMsg(x, true);
                case "POST /api/cluster/heartbeat" -> clusterMsg(x, false);
                case "POST /api/cluster/prevote" -> clusterMsg(x, null);
                case "GET /api/oplog" -> oplog(x);
                case "GET /api/ems" -> send(x, 200, auth(x, "viewer") == null ? null : emsSummary);
                case "GET /api/me" -> { Token.Claims c = auth(x, "viewer"); send(x, 200, Map.of("user", c.sub(), "role", c.role(), "jur", c.jur(), "node", node, "leader", cluster.isLeader())); }
                default -> {
                    if (path.startsWith("/cim/") || path.startsWith("/ems/") || path.startsWith("/hist/")) proxy(x, path);
                    else if (path.startsWith("/api/")) throw new HttpErr(404, "not found");      // unknown API routes are JSON 404s, never the single-page app
                    else if (m.equals("GET")) staticFile(x, path);
                    else throw new HttpErr(404, "not found");
                }
            }
        } catch (HttpErr e) {
            if (e.code == 401) authFail.increment();
            try { send(x, e.code, Map.of("error", e.getMessage())); } catch (IOException ignore) { }
        } catch (Exception e) {
            System.err.println("500 " + m + " " + path + ": " + e);
            try { send(x, 500, Map.of("error", "internal error")); } catch (IOException ignore) { }
        } finally { x.close(); }
    }

    private Token.Claims auth(HttpExchange x, String min) {
        String h = x.getRequestHeaders().getFirst("Authorization");
        if (h == null || !h.startsWith("Bearer ")) throw new HttpErr(401, "authentication required");
        try {
            Token.Claims c = Token.verify(secret, h.substring(7), System.currentTimeMillis() / 1000);
            if (!c.atLeast(min)) throw new HttpErr(403, min + " role required");
            return c;
        } catch (Token.AuthException e) { throw new HttpErr(401, e.getMessage()); }
    }

    private static byte[] body(HttpExchange x, int max) throws IOException {
        try (InputStream in = x.getRequestBody()) {
            byte[] b = in.readNBytes(max + 1);
            if (b.length > max) throw new HttpErr(413, "request too large");
            return b;
        }
    }

    private static Map<String, Object> jsonBody(HttpExchange x) throws IOException {
        try { Map<String, Object> m = Json.map(Json.parse(new String(body(x, 65536), StandardCharsets.UTF_8))); if (m == null) throw new HttpErr(400, "object expected"); return m; }
        catch (IllegalArgumentException e) { throw new HttpErr(400, "invalid JSON"); }
    }

    private static Map<String, String> query(HttpExchange x) {
        Map<String, String> q = new HashMap<>();
        String s = x.getRequestURI().getRawQuery();
        if (s == null) return q;
        for (String kv : s.split("&")) { int i = kv.indexOf('='); if (i > 0) q.put(kv.substring(0, i), java.net.URLDecoder.decode(kv.substring(i + 1), StandardCharsets.UTF_8)); }
        return q;
    }

    static void send(HttpExchange x, int code, Object o) throws IOException {
        byte[] b = Json.str(o).getBytes(StandardCharsets.UTF_8);
        x.getResponseHeaders().set("Content-Type", "application/json");
        x.sendResponseHeaders(code, b.length);
        try (OutputStream os = x.getResponseBody()) { os.write(b); }
    }

    // ------------------------------------------------------------------ routes
    private void health(HttpExchange x) throws IOException {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("status", core.degraded() ? "degraded" : "ok"); m.put("service", "scada-core");
        m.put("node", node); m.put("role", cluster.isLeader() ? "leader" : "standby"); m.put("seq", core.seq.get());
        send(x, 200, m);
    }

    private void login(HttpExchange x) throws IOException {
        Map<String, Object> b = jsonBody(x);
        Users.User u = users.authenticate(Json.s(b, "user"), Json.s(b, "password"), System.currentTimeMillis());
        if (u == null) { authFail.increment(); throw new HttpErr(401, "invalid credentials or account locked"); }
        long exp = System.currentTimeMillis() / 1000 + ttlSec;
        send(x, 200, Map.of("token", Token.mint(secret, u.name(), u.role(), u.jur(), exp), "role", u.role(), "jur", u.jur(), "exp", exp, "user", u.name()));
    }

    private void ingest(HttpExchange x) throws IOException {
        auth(x, "operator");
        byte[] raw = body(x, 32 << 20);
        core.heartbeat();                                      // any authenticated batch proves the feed is alive (a GI can arrive before the first heartbeat)
        int ok = 0, bad = 0;
        int p = 0;
        while (p < raw.length) {
            int e = p;
            while (e < raw.length && raw[e] != '\n') e++;
            if (e > p) {
                try {
                    Map<String, Object> m = Json.map(Json.parse(new String(raw, p, e - p, StandardCharsets.UTF_8)));
                    if (Json.b(m, "hb")) { core.heartbeat(); ok++; p = e + 1; continue; }
                    String tag = Json.s(m, "tag");
                    if (Json.b(m, "stale") && tag != null && tag.length() <= 128) { if (core.offer(Rec.sample(tag, Double.NaN, -1, "stale", "link-down"))) ok++; else bad++; p = e + 1; continue; }
                    if (tag == null || tag.length() > 128) throw new IllegalArgumentException("tag");
                    boolean status = m.containsKey("st");
                    double v = status ? Double.NaN : Json.d(m, "v", Double.NaN);
                    if (!status && Double.isNaN(v)) throw new IllegalArgumentException("value");
                    int st = status ? (int) Json.d(m, "st", -1) : -1;
                    if (status && (st < 0 || st > 3)) throw new IllegalArgumentException("st");
                    String q = Json.s(m, "quality");
                    Rec r = Rec.sample(tag, v, st, q == null ? "good" : (q.length() > 16 ? "invalid" : q), Json.s(m, "ts"));
                    if (core.offer(r)) ok++; else bad++;
                } catch (RuntimeException ex) { bad++; }
            }
            p = e + 1;
        }
        ingestBatches.increment(); ingestLines.add(ok); ingestBad.add(bad);
        send(x, 200, Map.of("accepted", ok, "rejected", bad));
    }

    /** Publish telemetry deltas to the message bus while this node is the leader (same content as /api/stream). */
    public void startBus(Bus bus) {
        Thread.startVirtualThread(() -> {
            long since = 0, av = -1;
            while (true) {
                try {
                    Thread.sleep(250);
                    if (!cluster.isLeader()) { since = 0; av = -1; continue; }     // a new leader starts with a full snapshot
                    long seq = core.seq.get(), ver = core.eng.alarmVersion;
                    if (seq == since && ver == av) continue;
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("seq", seq); m.put("now", System.currentTimeMillis()); m.put("node", node); m.put("term", cluster.term());
                    if (seq != since) m.put("pts", points(since));
                    if (ver != av) { m.put("alarms", core.eng.activeAlarms()); m.put("stats", core.eng.alarmStats()); }
                    if (bus.publish(Json.str(m))) { since = seq; av = ver; }       // on failure keep the old cursor: the next message re-covers the gap
                } catch (InterruptedException e) { return; } catch (Exception ignore) { }
            }
        });
    }

    private List<Object> points(long since) {
        List<Object> pts = new ArrayList<>();
        for (Engine.PtState s : core.eng.state.values()) {
            if (s.seq() <= since) continue;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("t", s.tag());
            if (s.st() >= 0) m.put("s", (long) s.st()); else m.put("v", s.v());
            if (!"good".equals(s.q())) m.put("q", s.q());
            m.put("n", s.seq()); m.put("m", s.ts());
            pts.add(m);
        }
        return pts;
    }

    private void snapshot(HttpExchange x) throws IOException {
        auth(x, "viewer");
        long since = Long.parseLong(query(x).getOrDefault("since", "0"));
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("seq", core.seq.get()); m.put("now", System.currentTimeMillis()); m.put("node", node); m.put("pts", points(since));
        m.put("alarms", core.eng.activeAlarms()); m.put("stats", core.eng.alarmStats());
        send(x, 200, m);
    }

    private void stream(HttpExchange x) throws IOException {
        auth(x, "viewer");
        if (streams.incrementAndGet() > 256) { streams.decrementAndGet(); throw new HttpErr(503, "too many streams"); }
        try {
            x.getResponseHeaders().set("Content-Type", "application/x-ndjson");
            x.sendResponseHeaders(200, 0);
            OutputStream os = x.getResponseBody();
            long since = 0, av = -1, lastWrite = 0;
            while (true) {
                Thread.sleep(250);
                long seq = core.seq.get(), ver = core.eng.alarmVersion, now = System.currentTimeMillis();
                if (seq == since && ver == av && now - lastWrite < 5000) continue;
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("seq", seq); m.put("now", now); m.put("leader", cluster.isLeader());
                if (seq != since) m.put("pts", points(since));
                if (ver != av) { m.put("alarms", core.eng.activeAlarms()); m.put("stats", core.eng.alarmStats()); }
                since = seq; av = ver; lastWrite = now;
                os.write((Json.str(m) + "\n").getBytes(StandardCharsets.UTF_8));
                os.flush();                                   // a slow client blocks only its own virtual thread
            }
        } catch (IOException | InterruptedException e) {
            // client went away
        } finally { streams.decrementAndGet(); }
    }

    private void alarms(HttpExchange x) throws IOException {
        auth(x, "viewer");
        Map<String, String> q = query(x);
        send(x, 200, Map.of("active", core.eng.activeAlarms(), "history", "1".equals(q.get("history")) ? core.eng.alarmHistory(500) : List.of(), "stats", core.eng.alarmStats()));
    }

    private void ack(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        Rec r = new Rec(); r.k = "ACK"; r.user = c.sub(); r.all = Json.b(b, "all"); r.id = (long) Json.d(b, "id", 0);
        if (!r.all && r.id <= 0) throw new HttpErr(400, "id or all required");
        requireLeader();
        Object n = core.actResult(r);
        send(x, 200, Map.of("acked", n == null ? 0 : n));
    }

    private void shelve(HttpExchange x, boolean on) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        String tag = Json.s(b, "tag");
        if (tag == null || !core.eng.cfg.containsKey(tag)) throw new HttpErr(400, "unknown tag");
        requireLeader();
        Rec r = new Rec(); r.k = on ? "SHELVE" : "UNSHELVE"; r.tag = tag; r.user = c.sub();
        if (on) {
            r.minutes = (long) Json.d(b, "minutes", 0); r.reason = Json.s(b, "reason");
            if (r.minutes < 1 || r.minutes > 24 * 60) throw new HttpErr(400, "minutes must be 1..1440");
            if (r.reason == null || r.reason.trim().length() < 3) throw new HttpErr(400, "a reason is required to shelve an alarm");
        }
        core.actResult(r);
        send(x, 200, Map.of("ok", true));
    }

    private static final java.util.regex.Pattern EQ = java.util.regex.Pattern.compile("^[A-Za-z0-9._-]{1,32}$");

    @SuppressWarnings("unchecked")
    private Map<String, Object> studyOf(List<String> eq, long t1, long t2, String exceptId) {
        EmsLink l = emsLink;
        if (l == null) return null;
        try {
            List<Object> conc = new ArrayList<>();
            for (Map<String, Object> o : core.eng.outageList()) {
                String st = (String) o.get("status");
                if (!(st.equals("requested") || st.equals("approved") || st.equals("active")) || o.get("id").equals(exceptId)) continue;
                if (((Number) o.get("start")).longValue() < t2 && ((Number) o.get("end")).longValue() > t1) conc.add(o.get("equipment"));
            }
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("equipment", eq); body.put("concurrent", conc); body.put("open_switches", l.openBreakers());
            Map<String, Object> r = l.call("/v1/outage-study", body, 60);
            Map<String, Object> c = new LinkedHashMap<>();
            for (String k : List.of("verdict", "islanded_load_mw", "base_violations", "new_critical", "worst_loading_pct", "isolating_breakers", "notes", "unknown_equipment", "n_concurrent", "wall_seconds")) if (r.containsKey(k)) c.put(k, r.get(k));
            return c;
        } catch (Exception e) {
            return Map.of("verdict", "unavailable", "error", String.valueOf(e.getMessage()));
        }
    }

    private void outageCreate(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        List<String> eq = new ArrayList<>();
        Object e = b.get("equipment");
        if (!(e instanceof List<?> l) || l.isEmpty() || l.size() > 10) throw new HttpErr(400, "1 to 10 equipment ids required");
        for (Object o : l) { if (!(o instanceof String s) || !EQ.matcher(s).matches()) throw new HttpErr(400, "bad equipment id"); eq.add((String) o); }
        long t1 = (long) Json.d(b, "start", 0), t2 = (long) Json.d(b, "end", 0), now = System.currentTimeMillis();
        if (t1 < now - 3_600_000L || t2 <= t1 || t2 - t1 > 30L * 86_400_000L) throw new HttpErr(400, "window must start no earlier than 1 h ago and last at most 30 days");
        String reason = Json.s(b, "reason");
        if (reason == null || reason.trim().length() < 3 || reason.length() > 300) throw new HttpErr(400, "a reason (3 to 300 characters) is required");
        requireLeader();
        Map<String, Object> study = studyOf(eq, t1, t2, null);
        Rec r = new Rec(); r.k = "OUT"; r.action = "create"; r.user = c.sub(); r.tag = String.join(",", eq); r.t1 = t1; r.t2 = t2; r.reason = reason;
        r.data = Json.str(study);
        core.actResult(r);
        List<Map<String, Object>> all = core.eng.outageList();
        Map<String, Object> last = all.get(all.size() - 1);
        send(x, 200, Map.of("ok", true, "id", last.get("id"), "n", last.get("n"), "study", study));
    }

    // ------------------------------------------------------------------ operator log, shift handover, communications status
    static final Set<String> LOG_CATS = Set.of("general", "operation", "abnormal", "safety", "equipment", "handover");

    private void logGet(HttpExchange x) throws IOException {
        auth(x, "viewer");
        Map<String, String> q = query(x);
        int lim = Math.max(1, Math.min(1000, Integer.parseInt(q.getOrDefault("limit", "200"))));
        String cat = q.get("category"), text = q.get("q");
        if (cat != null && !LOG_CATS.contains(cat)) throw new HttpErr(400, "unknown category");
        send(x, 200, Map.of("entries", core.eng.logList(lim, cat, text != null && text.length() > 100 ? text.substring(0, 100) : text), "categories", new TreeSet<>(LOG_CATS)));
    }

    private void logPost(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        String cat = Json.s(b, "category"), text = Json.s(b, "text"), ref = Json.s(b, "ref");
        if (cat == null || !LOG_CATS.contains(cat) || "handover".equals(cat)) throw new HttpErr(400, "category must be one of general, operation, abnormal, safety, equipment");
        if (text == null || text.trim().length() < 3 || text.length() > 1000) throw new HttpErr(400, "text of 3 to 1000 characters required");
        if (ref != null && !ref.matches("^[A-Za-z0-9._ -]{1,64}$")) throw new HttpErr(400, "bad reference");
        long amends = (long) Json.d(b, "amends", 0);
        if (amends != 0 && core.eng.logList(100_000, null, null).stream().noneMatch(e -> ((Number) e.get("n")).longValue() == amends)) throw new HttpErr(404, "no such log entry to amend");
        requireLeader();
        Rec r = new Rec(); r.k = "LOG"; r.user = c.sub(); r.tag = cat; r.reason = text.trim(); r.id = Math.max(0, amends); r.data = ref;
        core.actResult(r);
        List<Map<String, Object>> last = core.eng.logList(1, null, null);
        send(x, 200, Map.of("ok", true, "n", last.get(0).get("n")));
    }

    private void commsGet(HttpExchange x) throws IOException {
        auth(x, "viewer");
        Map<String, Object> o = new LinkedHashMap<>();
        o.put("substations", core.eng.commsList());
        long hb = core.lastHb;
        o.put("feed", Map.of("monitored", hb != 0, "lost", core.feedLost, "heartbeat_age_ms", hb == 0 ? -1 : System.currentTimeMillis() - hb, "timeout_ms", core.hbTimeoutMs));
        send(x, 200, o);
    }

    /** Everything the incoming operator needs, taken from the server's own state (the outgoing operator cannot edit it). */
    private Map<String, Object> handoverSnapshot() {
        Map<String, Object> s = new LinkedHashMap<>();
        s.put("taken", System.currentTimeMillis());
        s.put("alarms", core.eng.alarmStats());
        List<Map<String, Object>> top = new ArrayList<>();
        for (Map<String, Object> a : core.eng.activeAlarms()) { if (top.size() >= 10) break; if (!Boolean.TRUE.equals(a.get("shelved")) && ((Number) a.getOrDefault("prio", 4)).intValue() <= 2) top.add(Map.of("id", a.get("id"), "text", String.valueOf(a.get("text")), "prio", a.get("prio"), "state", String.valueOf(a.get("state")))); }
        s.put("priority_alarms", top);
        List<Map<String, Object>> out = new ArrayList<>();
        for (Map<String, Object> o : core.eng.outageList()) {
            String st = String.valueOf(o.get("status"));
            if (st.equals("requested") || st.equals("approved") || st.equals("active")) out.add(Map.of("id", o.get("id"), "status", st, "equipment", o.get("equipment"), "start", o.get("start"), "end", o.get("end")));
        }
        s.put("outages", out);
        List<Map<String, Object>> bad = new ArrayList<>();
        for (Map<String, Object> m : core.eng.commsList()) if (!"ok".equals(m.get("state"))) bad.add(m);
        s.put("comms", bad);
        s.put("feed_lost", core.feedLost);
        s.put("ai_enabled", core.eng.aiEnabled);
        List<Map<String, Object>> notable = new ArrayList<>();
        for (Map<String, Object> e : core.eng.logList(200, null, null)) { String cat = String.valueOf(e.get("category")); if ((cat.equals("abnormal") || cat.equals("safety") || cat.equals("equipment")) && notable.size() < 10) notable.add(e); }
        s.put("notable_log", notable);
        return s;
    }

    private void handoverCreate(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        String notes = Json.s(b, "notes"), to = Json.s(b, "to");
        if (notes == null || notes.trim().length() < 3 || notes.length() > 2000) throw new HttpErr(400, "handover notes of 3 to 2000 characters required");
        if (to != null && !to.isEmpty() && !to.matches("^[A-Za-z0-9._-]{1,32}$")) throw new HttpErr(400, "bad recipient");
        if (to != null && to.equals(c.sub())) throw new HttpErr(400, "cannot hand over to yourself");
        requireLeader();
        if (core.eng.pendingHandover() != null) throw new HttpErr(409, "a handover is already waiting to be accepted");
        Rec r = new Rec(); r.k = "HAND"; r.action = "create"; r.user = c.sub(); r.reason = notes.trim(); r.tag = to == null ? "" : to; r.data = Json.str(handoverSnapshot());
        core.actResult(r);
        send(x, 200, Map.of("ok", true, "handover", core.eng.handoverList(1).get(0)));
    }

    private void handoverAccept(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        long n = (long) Json.d(b, "n", 0);
        String note = Json.s(b, "note");
        Map<String, Object> h = core.eng.pendingHandover();
        if (h == null || ((Number) h.get("n")).longValue() != n) throw new HttpErr(409, "no such pending handover");
        if (c.sub().equals(h.get("from"))) throw new HttpErr(403, "the outgoing operator cannot accept their own handover");
        if (h.containsKey("to") && !c.sub().equals(h.get("to"))) throw new HttpErr(403, "this handover is addressed to " + h.get("to"));
        if (note != null && note.length() > 500) throw new HttpErr(400, "note too long");
        requireLeader();
        Rec r = new Rec(); r.k = "HAND"; r.action = "accept"; r.user = c.sub(); r.id = n; r.reason = note == null ? "" : note;
        core.actResult(r);
        send(x, 200, Map.of("ok", true));
    }

    private void outageDecision(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "engineer");
        Map<String, Object> b = jsonBody(x);
        long n = (long) Json.d(b, "n", 0);
        String d = Json.s(b, "decision"), note = Json.s(b, "note");
        if (!("approve".equals(d) || "reject".equals(d))) throw new HttpErr(400, "decision must be approve or reject");
        if (note == null || note.trim().length() < 3) throw new HttpErr(400, "a note is required");
        Map<String, Object> o = core.eng.outages.get(String.format("OUT-%04d", n));
        if (o == null) throw new HttpErr(404, "no such outage");
        if (!"requested".equals(o.get("status"))) throw new HttpErr(409, "outage is " + o.get("status"));
        if (c.sub().equals(o.get("requester"))) throw new HttpErr(403, "two-person rule: the requester cannot decide their own outage");
        Map<String, Object> st = Json.map(o.get("study"));
        String verdict = st == null ? "unavailable" : String.valueOf(st.get("verdict"));
        if ("approve".equals(d) && !verdict.equals("secure")) {
            boolean override = Json.b(b, "override");
            if (!override) throw new HttpErr(409, "study verdict is " + verdict + ": approval needs an explicit override by an administrator");
            if (!c.atLeast("admin")) throw new HttpErr(403, "only an administrator can override a non-secure study");
            note = "OVERRIDE of " + verdict + " study: " + note;
        }
        requireLeader();
        Rec r = new Rec(); r.k = "OUT"; r.action = d; r.user = c.sub(); r.id = n; r.reason = note;
        core.actResult(r);
        send(x, 200, Map.of("ok", true));
    }

    private void outageLifecycle(HttpExchange x, String action) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        long n = (long) Json.d(b, "n", 0);
        Map<String, Object> o = core.eng.outages.get(String.format("OUT-%04d", n));
        if (o == null) throw new HttpErr(404, "no such outage");
        String st = (String) o.get("status");
        boolean okState = switch (action) { case "cancel" -> st.equals("requested") || st.equals("approved"); case "start" -> st.equals("approved"); default -> st.equals("active"); };
        if (!okState) throw new HttpErr(409, "outage is " + st);
        if (action.equals("cancel") && !c.sub().equals(o.get("requester")) && !c.atLeast("admin")) throw new HttpErr(403, "only the requester or an administrator can cancel");
        requireLeader();
        Rec r = new Rec(); r.k = "OUT"; r.action = action; r.user = c.sub(); r.id = n; r.reason = Json.s(b, "note");
        core.actResult(r);
        send(x, 200, Map.of("ok", true));
    }

    /** Switching plan for an outage (isolate before work, restore after), computed by the EMS and never executed here. */
    @SuppressWarnings("unchecked")
    private void outagePlan(HttpExchange x) throws Exception {
        auth(x, "viewer");
        Map<String, Object> b = jsonBody(x);
        Map<String, Object> o = core.eng.outages.get(String.format("OUT-%04d", (long) Json.d(b, "n", 0)));
        if (o == null) throw new HttpErr(404, "no such outage");
        EmsLink l = emsLink;
        if (l == null) throw new HttpErr(503, "EMS not available");
        String mode = "restore".equals(Json.s(b, "mode")) ? "restore" : "isolate";
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("equipment", o.get("equipment")); body.put("mode", mode); body.put("open_switches", l.openBreakers());
        try { send(x, 200, l.call("/v1/switching-plan", body, 90)); } catch (Exception e) { throw new HttpErr(503, "plan unavailable: " + e.getMessage()); }
    }

    private final Parallel parallel = new Parallel();

    /** Parallel-run mode switch: admin only, reason mandatory, audited and replicated. While on, every remote command is refused. */
    private void parallelSwitch(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "admin");
        Map<String, Object> b = jsonBody(x);
        if (!b.containsKey("enabled")) throw new HttpErr(400, "enabled required");
        String reason = Json.s(b, "reason");
        if (reason == null || reason.trim().length() < 3 || reason.length() > 200) throw new HttpErr(400, "a reason (3-200 chars) is required");
        requireLeader();
        Rec r = new Rec(); r.k = "PAR"; r.user = c.sub(); r.ok = Json.b(b, "enabled"); r.reason = reason;
        core.actResult(r);
        send(x, 200, Map.of("enabled", core.eng.parallelRun));
    }

    /** Legacy samples to compare: {"source":..,"tolAbs":..,"tolRel":..,"samples":[{"tag":..,"v":..|"st":..,"ts":..}]} (max 5000 per call). */
    private void parallelSamples(HttpExchange x) throws Exception {
        auth(x, "engineer");
        Map<String, Object> b = jsonBody(x);
        if (b.containsKey("tolAbs") || b.containsKey("tolRel")) {
            try { parallel.tolerance(Json.d(b, "tolAbs", 0.5), Json.d(b, "tolRel", 0.005), Json.s(b, "source")); } catch (IllegalArgumentException e) { throw new HttpErr(400, e.getMessage()); }
        }
        Object so = b.get("samples");
        if (!(so instanceof List<?> l)) throw new HttpErr(400, "samples must be an array");
        if (l.size() > 5000) throw new HttpErr(413, "at most 5000 samples per call");
        int known = 0, unknown = 0;
        for (Object o : l) {
            if (!(o instanceof Map<?, ?> m0)) throw new HttpErr(400, "each sample must be an object");
            @SuppressWarnings("unchecked") Map<String, Object> m = (Map<String, Object>) m0;
            String tag = Json.s(m, "tag");
            if (tag == null || tag.isEmpty() || tag.length() > 120) throw new HttpErr(400, "sample without a valid tag");
            Double v = m.get("v") instanceof Number n ? n.doubleValue() : null;
            Integer st = m.get("st") instanceof Number n ? n.intValue() : null;
            if (v == null && st == null) throw new HttpErr(400, "sample for " + tag + " needs v or st");
            if (parallel.ingest(core.eng, tag, v, st, (long) Json.d(m, "ts", System.currentTimeMillis()))) known++; else unknown++;
        }
        send(x, 200, Map.of("accepted", known, "unknownTags", unknown));
    }

    private void parallelGet(HttpExchange x) throws Exception {
        auth(x, "viewer");
        String q = x.getRequestURI().getQuery();
        Double minPct = null;
        if (q != null) for (String kv : q.split("&")) if (kv.startsWith("minPct=")) { try { minPct = Double.parseDouble(kv.substring(7)); } catch (NumberFormatException e) { throw new HttpErr(400, "minPct must be a number"); } }
        send(x, 200, parallel.report(core.eng, core.eng.parallelRun, minPct));
    }

    /** Global AI/ML off switch: admin only, reason mandatory, audited and replicated like any other operator action. */
    private void aiSwitch(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "admin");
        Map<String, Object> b = jsonBody(x);
        if (!b.containsKey("enabled")) throw new HttpErr(400, "enabled required");
        String reason = Json.s(b, "reason");
        if (reason == null || reason.trim().length() < 3) throw new HttpErr(400, "a reason is required");
        requireLeader();
        Rec r = new Rec(); r.k = "AI"; r.user = c.sub(); r.ok = Json.b(b, "enabled"); r.reason = reason;
        core.actResult(r);
        send(x, 200, Map.of("enabled", core.eng.aiEnabled));
    }

    /** An AI/ML feature asks permission and records its use in one step. Disabled means 409 and nothing is recorded. */
    private void aiUse(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "viewer");
        Map<String, Object> b = jsonBody(x);
        if (!core.eng.aiEnabled) throw new HttpErr(409, "AI/ML advisory is switched off");
        String what = Json.s(b, "feature"), target = Json.s(b, "target");
        if (what == null || target == null || what.length() > 64 || target.length() > 128) throw new HttpErr(400, "feature and target required");
        requireLeader();
        Rec r = new Rec(); r.k = "AIUSE"; r.user = c.sub(); r.action = what; r.tag = target; r.reason = Json.s(b, "detail");
        if (r.reason != null && r.reason.length() > 200) r.reason = r.reason.substring(0, 200);
        core.actResult(r);
        send(x, 200, Map.of("enabled", true));
    }

    private void soe(HttpExchange x) throws IOException {
        auth(x, "viewer");
        int lim = Math.min(2000, Integer.parseInt(query(x).getOrDefault("limit", "200")));
        send(x, 200, Map.of("events", core.eng.recentSoe(lim)));
    }

    private void requireLeader() {
        if (!cluster.isLeader()) throw new HttpErr(409, "standby node: send operator actions to the leader " + cluster.leaderUrl());
    }

    private void command(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "operator");
        Map<String, Object> b = jsonBody(x);
        String tag = Json.s(b, "tag"), action = Json.s(b, "action"), reason = Json.s(b, "reason"), totp = Json.s(b, "totp");
        cmds.increment();
        try {
            requireLeader();
            if (core.eng.parallelRun) throw new HttpErr(409, "parallel-run mode: remote control is disabled while the legacy system is in control");
            Engine.Cfg cfg = tag == null ? null : core.eng.cfg.get(tag);
            if (cfg == null || !cfg.status() || cfg.element() == null || !cfg.element().startsWith("BRK-")) throw new HttpErr(404, "not a controllable breaker point");
            if (!"open".equals(action) && !"close".equals(action)) throw new HttpErr(400, "action must be open or close");
            if (reason == null || reason.trim().length() < 3 || reason.length() > 200) throw new HttpErr(400, "a reason (3-200 chars) is required");
            if (!c.covers(regionOfSub.get(cfg.substation()))) throw new HttpErr(403, "outside your jurisdiction");
            if (!users.verifyTotp(c.sub(), totp, System.currentTimeMillis() / 1000)) throw new HttpErr(403, "second factor required: invalid or reused one-time code");
            ArrayDeque<Long> times = cmdTimes.computeIfAbsent(c.sub(), k -> new ArrayDeque<>());
            synchronized (times) {
                long now = System.currentTimeMillis();
                while (!times.isEmpty() && now - times.peekFirst() > 60_000) times.pollFirst();
                if (times.size() >= 10) throw new HttpErr(429, "command rate limit: 10 per minute");
                times.addLast(now);
            }
            Engine.PtState cur = core.eng.state.get(tag);
            if (cur == null || cur.st() < 1 || cur.st() > 2 || !"good".equals(cur.q())) throw new HttpErr(409, "interlock: breaker position unknown or quality not good");
            if ((cur.st() == 1) == "open".equals(action)) throw new HttpErr(409, "interlock: breaker already " + action);
            if (!inflight.add(tag)) throw new HttpErr(409, "a command for this point is already in progress");
            try {
                Rec cmd = new Rec(); cmd.k = "CMD"; cmd.tag = tag; cmd.action = action; cmd.user = c.sub(); cmd.reason = reason;
                core.actResult(cmd);
                core.syncNow();                                  // audit record is durable BEFORE the field is touched
                long t0 = System.currentTimeMillis();
                boolean ok; String err = null;
                try {
                    String ctok = Token.mint(secret, c.sub(), "operator", c.jur(), System.currentTimeMillis() / 1000 + 60);
                    HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(fepUrl + "/command")).timeout(Duration.ofSeconds(25)).header("Authorization", "Bearer " + ctok);
                    if (cluster.quorumMode()) rb.header("X-Fence-Term", Long.toString(cluster.term())); // only a quorum cluster has a term to fence with
                    HttpResponse<String> r = http.send(rb.POST(HttpRequest.BodyPublishers.ofString(Json.str(Map.of("tag", tag, "action", action)))).build(), HttpResponse.BodyHandlers.ofString());
                    ok = r.statusCode() == 200;
                    if (!ok) err = "FEP: " + r.body();
                } catch (Exception e) { ok = false; err = "FEP unreachable"; }
                Rec res = new Rec(); res.k = "CMDRES"; res.tag = tag; res.action = action; res.user = c.sub(); res.ok = ok; res.err = err;
                core.actResult(res);
                if (!ok) { cmdFail.increment(); throw new HttpErr(502, err); }
                send(x, 200, Map.of("ok", true, "tag", tag, "action", action, "executed_ms", System.currentTimeMillis() - t0, "note", "execution confirmed by outstation; position change will arrive via telemetry"));
            } finally { inflight.remove(tag); }
        } catch (HttpErr e) { if (e.code != 502) cmdFail.increment(); throw e; }
    }

    private void metrics(HttpExchange x) throws IOException {
        auth(x, "viewer");
        long[] l = core.latencyPercentiles();
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("node", node); m.put("uptime_s", (System.currentTimeMillis() - startMs) / 1000);
        m.put("ingest_batches", ingestBatches.sum()); m.put("ingest_lines_accepted", ingestLines.sum()); m.put("ingest_lines_rejected", ingestBad.sum());
        m.put("queue_depth", (long) core.q.size()); m.put("offered", core.offered.sum()); m.put("applied", core.applied.sum()); m.put("no_change_filtered", core.noop.sum()); m.put("dropped", core.dropped.sum());
        m.put("latency_ms_p50", l[0]); m.put("latency_ms_p95", l[1]); m.put("latency_ms_p99", l[2]); m.put("latency_ms_max", l[3]); m.put("latency_samples", l[4]);
        m.put("points", (long) core.eng.state.size()); m.put("log_seq", core.seq.get()); m.put("log_errors", core.logErrors.sum()); m.put("degraded", core.degraded());
        m.put("commands", cmds.sum()); m.put("commands_failed", cmdFail.sum()); m.put("auth_failures", authFail.sum());
        m.put("alarms", core.eng.alarmStats()); m.put("cluster", cluster.status());
        try { m.put("log_bytes", Files.size(logFile)); } catch (IOException ignore) { }
        send(x, 200, m);
    }

    private void verify(HttpExchange x) throws Exception {
        auth(x, "engineer");
        send(x, 200, core.verifyReplay(auditKey, logFile));
    }

    private void clusterMsg(HttpExchange x, Boolean vote) throws Exception {
        Token.Claims c = auth(x, "admin");
        if (!c.sub().startsWith("core-")) throw new HttpErr(403, "service identity required");
        if (!cluster.quorumMode()) throw new HttpErr(404, "not a quorum cluster");
        Map<String, Object> b = Json.map(Json.parse(new String(body(x, 4096), StandardCharsets.UTF_8)));
        try { send(x, 200, vote == null ? cluster.onPreVote(b) : vote ? cluster.onVote(b) : cluster.onHeartbeat(b)); } catch (IllegalArgumentException e) { throw new HttpErr(403, e.getMessage()); }
    }

    private void replicate(HttpExchange x) throws Exception {
        Token.Claims c = auth(x, "admin");
        if (!c.sub().startsWith("core-")) throw new HttpErr(403, "service identity required");
        String xt = x.getRequestHeaders().getFirst("X-Term");
        if (cluster.quorumMode() && (xt == null || !xt.matches("\\d{1,15}") || !cluster.acceptTerm(Long.parseLong(xt)))) throw new HttpErr(409, "stale or missing leader term: record refused (fenced)");
        Rec r = Rec.fromMap(Json.map(Json.parse(new String(body(x, 65536), StandardCharsets.UTF_8))));
        if (!List.of("CMD", "CMDRES", "ACK", "SHELVE", "UNSHELVE", "AI", "AIUSE", "PAR", "OUT", "LOG", "HAND").contains(r.k)) throw new HttpErr(400, "bad record kind");
        core.applyReplicated(r);
        send(x, 200, Map.of("ok", true));
    }

    private void oplog(HttpExchange x) throws IOException {
        Token.Claims c = auth(x, "admin");
        if (!c.sub().startsWith("core-")) throw new HttpErr(403, "service identity required");
        List<Object> out = new ArrayList<>();
        for (Rec r : core.opsSince(Long.parseLong(query(x).getOrDefault("since", "0")))) out.add(r.toMap());
        send(x, 200, out);
    }

    // ------------------------------------------------------------------ proxy to cim-pds / ems-analytics (same-origin for the UI)
    private void proxy(HttpExchange x, String path) throws Exception {
        String base = path.startsWith("/cim/") ? cimUrl : path.startsWith("/ems/") ? emsUrl : System.getenv().getOrDefault("HIST_URL", "http://127.0.0.1:8085");
        String rest = path.substring(path.indexOf('/', 1));
        String q = x.getRequestURI().getRawQuery();
        String m = x.getRequestMethod();
        if (!List.of("GET", "POST", "PUT").contains(m)) throw new HttpErr(405, "method not allowed");
        auth(x, "viewer");                                     // the upstream re-verifies the same token and enforces RBAC itself
        HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(base + rest + (q == null ? "" : "?" + q))).timeout(Duration.ofSeconds(60))
                .header("Authorization", x.getRequestHeaders().getFirst("Authorization"));
        byte[] b = m.equals("GET") ? new byte[0] : body(x, 8 << 20);
        if (!m.equals("GET")) rb.header("Content-Type", "application/json");
        rb.method(m, b.length == 0 ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofByteArray(b));
        HttpResponse<byte[]> r = http.send(rb.build(), HttpResponse.BodyHandlers.ofByteArray());
        x.getResponseHeaders().set("Content-Type", r.headers().firstValue("Content-Type").orElse("application/json"));
        x.sendResponseHeaders(r.statusCode(), r.body().length == 0 ? -1 : r.body().length);
        if (r.body().length > 0) try (OutputStream os = x.getResponseBody()) { os.write(r.body()); }
    }

    // ------------------------------------------------------------------ static UI
    private static final Map<String, String> MIME = Map.of(".html", "text/html; charset=utf-8", ".js", "text/javascript", ".css", "text/css", ".svg", "image/svg+xml",
            ".json", "application/json", ".woff2", "font/woff2", ".png", "image/png", ".ico", "image/x-icon", ".map", "application/json");

    private void staticFile(HttpExchange x, String path) throws IOException {
        if (uiDir == null || !Files.isDirectory(uiDir)) throw new HttpErr(404, "UI not installed");
        Path root = uiDir.toAbsolutePath().normalize();
        Path f = root.resolve(path.equals("/") ? "index.html" : path.substring(1)).normalize();
        if (!f.startsWith(root)) throw new HttpErr(403, "forbidden");
        if (!Files.isRegularFile(f)) f = root.resolve("index.html");         // SPA fallback
        if (!Files.isRegularFile(f)) throw new HttpErr(404, "not found");
        String name = f.getFileName().toString();
        String ext = name.contains(".") ? name.substring(name.lastIndexOf('.')) : "";
        Headers h = x.getResponseHeaders();
        h.set("Content-Type", MIME.getOrDefault(ext, "application/octet-stream"));
        h.set("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'");
        h.set("Cache-Control", name.equals("index.html") ? "no-cache" : "public, max-age=3600");
        byte[] b = Files.readAllBytes(f);
        x.sendResponseHeaders(200, b.length);
        try (OutputStream os = x.getResponseBody()) { os.write(b); }
    }
}
