package gs.core;

import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;

public final class Main {
    /** Environment value; NAME_FILE names a file holding the value (container/Kubernetes secrets), which wins over NAME. */
    static String env(String k, String d) {
        String f = System.getenv(k + "_FILE");
        if (f != null && !f.isEmpty()) {
            try { return java.nio.file.Files.readString(java.nio.file.Path.of(f)).strip(); } catch (java.io.IOException e) { throw new IllegalStateException("cannot read " + k + "_FILE: " + e.getMessage()); }
        }
        String v = System.getenv(k); return v == null || v.isEmpty() ? d : v;
    }

    public static void main(String[] args) throws Exception {
        if (args.length > 0 && args[0].equals("adduser")) { adduser(args); return; }
        Map<String, String> a = new HashMap<>();
        List<Cluster.Peer> peers = new ArrayList<>();
        for (int i = 0; i + 1 < args.length; i += 2) {
            if (args[i].equals("-peer")) { String[] kv = args[i + 1].split("=", 2); peers.add(new Cluster.Peer(kv[0], kv[1])); }
            else a.put(args[i], args[i + 1]);
        }
        String node = a.getOrDefault("-id", "A");
        String listen = a.getOrDefault("-listen", "127.0.0.1:8083");
        Token.loadFromEnv();
        Path data = Path.of(a.getOrDefault("-data", "data-" + node));
        String cim = a.getOrDefault("-cim", env("CIM_URL", "http://127.0.0.1:8081"));
        String ems = a.getOrDefault("-ems", env("EMS_URL", "http://127.0.0.1:8082"));
        String fep = a.getOrDefault("-fep", env("FEP_URL", "http://127.0.0.1:8084"));
        String model = a.getOrDefault("-model", "ieee118");
        Path ui = a.containsKey("-ui") ? Path.of(a.get("-ui")) : null;

        byte[] secret = env("GS_SECRET", "").getBytes(StandardCharsets.UTF_8);
        boolean dev = "1".equals(System.getenv("GS_DEV"));
        if (secret.length < 16) {
            if (!dev) { System.err.println("GS_SECRET (>=16 chars) required; GS_DEV=1 for local development only"); System.exit(2); }
            secret = "dev-secret-do-not-use-in-prod".getBytes(StandardCharsets.UTF_8);
            System.err.println("WARNING: using insecure development secret (GS_DEV=1)");
        }
        final byte[] sec = secret;
        byte[] auditKey = Token.hmacHex(secret, "gridsovereign/audit-log/v1").getBytes(StandardCharsets.US_ASCII);   // domain-separated key
        Files.createDirectories(data);

        Users users = Users.load(data.resolve("users.json"));
        if (users.isEmpty() && dev) seedDev(data);
        users = Users.load(data.resolve("users.json"));

        HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        String svc = Token.mint(sec, "scada-core-" + node, "viewer", List.of("*"), System.currentTimeMillis() / 1000 + 600);
        Map<String, Engine.Cfg> cfg = new LinkedHashMap<>();
        Map<String, String> region = new HashMap<>();
        for (int attempt = 0; ; attempt++) {
            try {
                Map<String, Object> pl = Json.map(Json.parse(get(http, cim + "/models/" + model + "/pointlist", svc)));
                for (Object o : (List<?>) pl.get("points")) {
                    Map<String, Object> p = Json.map(o);
                    cfg.put(Json.s(p, "tag"), new Engine.Cfg(Json.s(p, "tag"), Json.s(p, "kind"), Json.s(p, "quantity"), Json.s(p, "unit"), Json.s(p, "substation"), Json.s(p, "element"),
                            p.get("low") instanceof Number n ? n.doubleValue() : null, p.get("high") instanceof Number n ? n.doubleValue() : null));
                }
                Map<String, Object> mdl = Json.map(Json.parse(get(http, cim + "/models/" + model, svc)));
                for (Object o : (List<?>) mdl.get("substations")) { Map<String, Object> s = Json.map(o); region.put(Json.s(s, "mrid"), Json.s(s, "region")); }
                break;
            } catch (Exception e) {
                if (attempt > 60) { System.err.println("cannot load model from cim-pds: " + e); System.exit(1); }
                Thread.sleep(500);
            }
        }
        System.out.println("model " + model + ": " + cfg.size() + " points, " + region.size() + " substations");

        Engine eng = new Engine(cfg);
        Path logFile = data.resolve("events.log");
        EventLog log = new EventLog(logFile, auditKey);
        List<Rec> restoredOps = new ArrayList<>();
        long t0 = System.nanoTime();
        EventLog.Result res = log.open(r -> { eng.apply(r); if (!"S".equals(r.k)) restoredOps.add(r); });
        System.out.printf("event log: %d records restored and chain verified in %d ms%s%n", res.records(), (System.nanoTime() - t0) / 1_000_000,
                res.truncatedBytes() > 0 ? " (repaired " + res.truncatedBytes() + " torn bytes)" : "");
        Core core = new Core(eng, log, res.lastSeq());
        for (Rec r : restoredOps) core.primeOps(r);

        Cluster cluster = new Cluster(node, peers, sec, data, new java.util.Random());
        core.replicator = cluster::replicate;
        Api api = new Api(core, users, cluster, sec, auditKey, node, fep, cim, ems, logFile, ui, region, Long.parseLong(env("GS_TOKEN_TTL", "3600")));
        core.start();
        cluster.start();
        if (a.containsKey("-bus")) api.startBus(new Bus(a.get("-bus"), a.getOrDefault("-bus-stream", "gs:telemetry"), Long.parseLong(a.getOrDefault("-bus-maxlen", "200000"))));
        String[] hp = listen.split(":");
        api.start(new InetSocketAddress(hp[0], Integer.parseInt(hp[1])));
        System.out.println("scada-core " + node + " listening on " + listen + " peers=" + peers);

        // catch up on operator actions we missed while down, then ask the FEP for a fresh general interrogation
        Thread.startVirtualThread(() -> {
            try {
                Thread.sleep(1500);
                long since = restoredOps.stream().mapToLong(r -> r.ts).max().orElse(0);
                int n = 0;
                for (Rec r : cluster.catchUp(since)) { core.applyReplicated(r); n++; }
                if (n > 0) System.out.println("caught up " + n + " operator records from peers");
                String tok = Token.mint(sec, "scada-core-" + node, "operator", List.of("*"), System.currentTimeMillis() / 1000 + 60);
                http.send(HttpRequest.newBuilder(URI.create(fep + "/gi")).header("Authorization", "Bearer " + tok).POST(HttpRequest.BodyPublishers.noBody()).timeout(Duration.ofSeconds(5)).build(), HttpResponse.BodyHandlers.discarding());
            } catch (Exception e) { System.err.println("startup sync: " + e); }
        });
        if (env("GS_EMS", "1").equals("1")) { EmsLink el = new EmsLink(core, sec, ems, model, api, Long.parseLong(env("GS_EMS_PERIOD_S", "30"))); api.emsLink = el; el.start(); }

        Runtime.getRuntime().addShutdownHook(new Thread(() -> { try { api.stop(); core.stop(); } catch (Exception ignore) { } }));
        Thread.currentThread().join();
    }

    static String get(HttpClient http, String url, String tok) throws Exception {
        HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(url)).header("Authorization", "Bearer " + tok).timeout(Duration.ofSeconds(20)).build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) throw new IllegalStateException(url + " -> " + r.statusCode());
        return r.body();
    }

    static void adduser(String[] a) throws Exception {
        if (a.length < 5) { System.err.println("usage: GS_NEW_PASSWORD=... adduser <users.json> <name> <viewer|operator|engineer|admin> <region,region|*>"); System.exit(2); }
        String pw = System.getenv("GS_NEW_PASSWORD");
        if (pw == null) { System.err.println("set GS_NEW_PASSWORD (>=12 chars)"); System.exit(2); }
        byte[] totp = Users.add(Path.of(a[1]), a[2], pw, a[3], Arrays.asList(a[4].split(",")));
        System.out.println("user " + a[2] + " created. TOTP secret (base32, enrol in an authenticator app): " + Totp.b32(totp));
    }

    /** Development-only demo accounts. Credentials are written to data/dev-credentials.txt and are NOT secrets. */
    static void seedDev(Path data) throws Exception {
        StringBuilder sb = new StringBuilder("# DEVELOPMENT ONLY demo accounts (GS_DEV=1)\n# user password role jurisdiction totp-secret-base32\n");
        String[][] u = {{"alice", "operator", "*"}, {"north", "operator", "Northern"}, {"bob", "viewer", "*"}, {"carol", "engineer", "*"}, {"admin", "admin", "*"}};
        for (String[] x : u) {
            String pw = "demo-password-" + x[0];
            byte[] t = Users.add(data.resolve("users.json"), x[0], pw, x[1], Arrays.asList(x[2].split(",")));
            sb.append(x[0]).append(' ').append(pw).append(' ').append(x[1]).append(' ').append(x[2]).append(' ').append(Totp.b32(t)).append('\n');
        }
        Files.writeString(data.resolve("dev-credentials.txt"), sb.toString());
        System.err.println("WARNING: seeded demo accounts into " + data.resolve("dev-credentials.txt") + " (development only)");
    }
}
