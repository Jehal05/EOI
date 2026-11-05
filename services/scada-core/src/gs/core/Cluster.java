package gs.core;

import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;

/**
 * Cluster coordination and leadership.
 *
 * <p>With three or more nodes leadership is decided by a majority (Raft-style election, leadership part only): terms that
 * only grow, one persisted vote per term, randomised election timeouts, a leader that must keep hearing from a majority
 * (its lease) or it stops being leader. The term doubles as a fencing token: control commands carry it to the front-end
 * processor, and replicated operator records carry it to the peers, so a deposed leader that has not noticed yet is
 * refused downstream. Two nodes cannot form a majority that survives one failure, so a pair keeps the older
 * lowest-id-alive rule, reported as mode "pair" (it can split the brain if the link between the two fails).
 *
 * <p>Operator records (commands, acks, shelves) replicate leader -> peers; telemetry reaches every node from the FEP.
 */
public final class Cluster {
    public record Peer(String id, String url) {}

    /** Message transport; the default is HTTP to the peer's /api/cluster/* endpoints. Tests inject an in-memory network. */
    public interface Transport { Map<String, Object> call(Peer p, String path, Map<String, Object> body) throws Exception; }

    public enum Role { FOLLOWER, CANDIDATE, LEADER }

    public static final long HEARTBEAT_MS = 400, LEASE_MS = 1200, ELECTION_MIN_MS = 1500, ELECTION_SPAN_MS = 1500;

    public final String id;
    public final List<Peer> peers;
    private final byte[] secret;
    private final java.nio.file.Path stateFile;
    private final Random rnd;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(600)).executor(Executors.newVirtualThreadPerTaskExecutor()).build();
    private final ConcurrentHashMap<String, Long> lastSeen = new ConcurrentHashMap<>();
    private Transport transport = this::httpCall;
    private java.util.function.LongSupplier clock = System::currentTimeMillis;
    public void setClock(java.util.function.LongSupplier c) { this.clock = c; long now = c.getAsLong(); synchronized (this) { lastHeard = now; electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS) + 500; } }
    // election state (guarded by this)
    private long term; private String votedFor; private Role role = Role.FOLLOWER; private String leaderId;
    private long lastHeard, electionDeadline, lastBeat, leaseUntil;
    private final Map<String, Long> ackAt = new HashMap<>();
    private volatile boolean legacyLeader = true;
    public volatile long failovers, replFail, replOk, staleRejected, stepDowns;
    public volatile long becameLeaderAt;
    private final ScheduledExecutorService sched = Executors.newSingleThreadScheduledExecutor(r -> { Thread t = new Thread(r, "cluster"); t.setDaemon(true); return t; });

    public Cluster(String id, List<Peer> peers, byte[] secret) { this(id, peers, secret, null, new Random()); }

    public Cluster(String id, List<Peer> peers, byte[] secret, java.nio.file.Path dataDir, Random rnd) {
        this.id = id; this.peers = peers; this.secret = secret; this.rnd = rnd;
        this.stateFile = dataDir == null ? null : dataDir.resolve("cluster.json");
        legacyLeader = peers.isEmpty();
        loadState();
        long now = clock.getAsLong();
        lastHeard = now; electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS) + 500;
    }

    public void setTransport(Transport t) { this.transport = t; }
    /** Quorum mode needs at least three nodes. */
    public boolean quorumMode() { return peers.size() >= 2; }
    private int quorum() { return (peers.size() + 1) / 2 + 1; }

    public void start() {
        if (peers.isEmpty()) return;
        if (quorumMode()) sched.scheduleAtFixedRate(() -> { try { tick(clock.getAsLong()); } catch (Throwable ignore) { } }, 100, 100, TimeUnit.MILLISECONDS);
        else sched.scheduleAtFixedRate(this::probe, 0, 500, TimeUnit.MILLISECONDS);
    }

    // ---------------------------------------------------------------- persistence (term and vote must survive a restart)
    private void loadState() {
        if (stateFile == null || !java.nio.file.Files.exists(stateFile)) return;
        try {
            Map<String, Object> m = Json.map(Json.parse(java.nio.file.Files.readString(stateFile)));
            term = (long) Json.d(m, "term", 0); votedFor = Json.s(m, "votedFor");
        } catch (Exception e) { throw new IllegalStateException("unreadable cluster state " + stateFile + ": " + e); }
    }
    private void saveState() {
        if (stateFile == null) return;
        try {
            Map<String, Object> m = new LinkedHashMap<>(); m.put("term", term); m.put("votedFor", votedFor);
            java.nio.file.Path tmp = stateFile.resolveSibling("cluster.json.tmp");
            try (var ch = java.nio.channels.FileChannel.open(tmp, java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.WRITE, java.nio.file.StandardOpenOption.TRUNCATE_EXISTING)) {
                ch.write(java.nio.ByteBuffer.wrap(Json.str(m).getBytes(java.nio.charset.StandardCharsets.UTF_8))); ch.force(true);
            }
            java.nio.file.Files.move(tmp, stateFile, java.nio.file.StandardCopyOption.REPLACE_EXISTING, java.nio.file.StandardCopyOption.ATOMIC_MOVE);
        } catch (java.io.IOException e) { throw new IllegalStateException("cannot persist cluster state: " + e); }
    }

    // ---------------------------------------------------------------- election
    /** One step of the state machine at time `now` (ms). Public so tests can drive it with a synthetic clock. */
    public void tick(long now) {
        Role r; long t;
        synchronized (this) {
            r = role; t = term;
            if (r == Role.LEADER && now > leaseUntil) { role = Role.FOLLOWER; leaderId = null; stepDowns++; lastHeard = now; electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS); r = role; }
        }
        if (r == Role.LEADER) { if (now - lastBeat >= HEARTBEAT_MS) heartbeat(now); return; }
        boolean due; synchronized (this) { due = now >= electionDeadline; }
        if (due) startElection(now);
    }

    /** Send `path` to all peers in parallel; return as soon as `need` positive answers are in (or all have answered/timed out).
     *  Waiting for a dead peer's timeout must not delay a decision a majority has already made. Returns the highest term seen. */
    private long gather(String path, Map<String, Object> req, String flag, int need, int[] yes) {
        ExecutorService ex = Executors.newVirtualThreadPerTaskExecutor();
        ExecutorCompletionService<Map<String, Object>> cs = new ExecutorCompletionService<>(ex);
        for (Peer p : peers) cs.submit(() -> { Map<String, Object> r = transport.call(p, path, req); if (r != null) lastSeen.put(p.id, clock.getAsLong()); return r; });
        long highest = 0; long deadline = System.nanoTime() + 1_200_000_000L;
        for (int i = 0; i < peers.size() && yes[0] < need; i++) {
            try {
                Future<Map<String, Object>> f = cs.poll(Math.max(1, (deadline - System.nanoTime()) / 1_000_000), TimeUnit.MILLISECONDS);
                if (f == null) break;
                Map<String, Object> r = f.get();
                if (r == null) continue;
                highest = Math.max(highest, (long) Json.d(r, "term", 0));
                if (Boolean.TRUE.equals(r.get(flag))) yes[0]++;
            } catch (Exception ignore) { }
        }
        ex.shutdown();
        return highest;
    }

    /** Would a majority vote for us? Asked before a real election so that a node that is merely cut off never inflates the term. */
    private boolean preVote() {
        long t; synchronized (this) { t = term + 1; }
        Map<String, Object> req = new LinkedHashMap<>(); req.put("term", t); req.put("candidate", id);
        int[] yes = {1};
        gather("/api/cluster/prevote", req, "granted", quorum(), yes);
        return yes[0] >= quorum();
    }

    /** Pre-vote and vote requests are refused while we have heard a leader recently (leader stickiness): this is what keeps
     *  a partitioned or restarting node from deposing a healthy leader, and what keeps leases and elections from overlapping. */
    private boolean leaderRecentlyHeard() { return leaderId != null && clock.getAsLong() - lastHeard < ELECTION_MIN_MS; }

    public synchronized Map<String, Object> onPreVote(Map<String, Object> req) {
        long t = (long) Json.d(req, "term", 0); String cand = Json.s(req, "candidate");
        if (cand == null || peers.stream().noneMatch(p -> p.id.equals(cand))) throw new IllegalArgumentException("unknown candidate");
        boolean grant = t > term && !leaderRecentlyHeard() && role != Role.LEADER;
        Map<String, Object> m = new LinkedHashMap<>(); m.put("term", term); m.put("granted", grant); return m;
    }

    private void startElection(long now) {
        if (!preVote()) { synchronized (this) { electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS); } return; }
        long myTerm;
        synchronized (this) {
            term++; myTerm = term; votedFor = id; role = Role.CANDIDATE; leaderId = null; saveState();
            electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS);
        }
        Map<String, Object> req = new LinkedHashMap<>(); req.put("term", myTerm); req.put("candidate", id);
        int[] yes = {1};
        long highest = Math.max(myTerm, gather("/api/cluster/vote", req, "granted", quorum(), yes));
        int votes = yes[0];
        synchronized (this) {
            if (highest > term) { term = highest; votedFor = null; role = Role.FOLLOWER; saveState(); return; }
            if (role == Role.CANDIDATE && term == myTerm && votes >= quorum()) {
                role = Role.LEADER; leaderId = id; leaseUntil = clock.getAsLong() + LEASE_MS; ackAt.clear(); lastBeat = 0;
                becameLeaderAt = clock.getAsLong(); failovers++;
            }
        }
        if (isLeader()) heartbeat(clock.getAsLong());
    }

    private void heartbeat(long now) {
        long myTerm; synchronized (this) { myTerm = term; lastBeat = now; }
        Map<String, Object> req = new LinkedHashMap<>(); req.put("term", myTerm); req.put("leader", id);
        int[] yes = {1};
        long highest = Math.max(myTerm, gather("/api/cluster/heartbeat", req, "ok", quorum(), yes));
        int acks = yes[0];
        synchronized (this) {
            if (highest > term) { term = highest; votedFor = null; if (role == Role.LEADER) stepDowns++; role = Role.FOLLOWER; leaderId = null; saveState(); return; }
            // the lease is only extended from the time the heartbeat was SENT, and only when a majority answered
            if (role == Role.LEADER && term == myTerm && acks >= quorum()) leaseUntil = now + LEASE_MS;
        }
    }

    /** Vote request from a candidate. */
    public synchronized Map<String, Object> onVote(Map<String, Object> req) {
        long t = (long) Json.d(req, "term", 0); String cand = Json.s(req, "candidate");
        if (cand == null || peers.stream().noneMatch(p -> p.id.equals(cand))) throw new IllegalArgumentException("unknown candidate");
        if (t > term && (leaderRecentlyHeard() || role == Role.LEADER)) { Map<String, Object> m = new LinkedHashMap<>(); m.put("term", term); m.put("granted", false); return m; }
        if (t > term) { term = t; votedFor = null; if (role == Role.LEADER) stepDowns++; role = Role.FOLLOWER; leaderId = null; saveState(); }
        boolean grant = t == term && (votedFor == null || votedFor.equals(cand)) && role != Role.LEADER;
        if (grant) { votedFor = cand; saveState(); long now = clock.getAsLong(); electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS); }
        Map<String, Object> m = new LinkedHashMap<>(); m.put("term", term); m.put("granted", grant); return m;
    }

    /** Heartbeat from a leader. */
    public synchronized Map<String, Object> onHeartbeat(Map<String, Object> req) {
        long t = (long) Json.d(req, "term", 0); String ld = Json.s(req, "leader");
        if (ld == null || peers.stream().noneMatch(p -> p.id.equals(ld))) throw new IllegalArgumentException("unknown leader");
        boolean ok = false;
        if (t >= term) {
            if (t > term) { term = t; votedFor = null; saveState(); }
            if (role == Role.LEADER) stepDowns++;
            role = Role.FOLLOWER; leaderId = ld; ok = true;
            long now = clock.getAsLong(); lastHeard = now; electionDeadline = now + ELECTION_MIN_MS + rnd.nextInt((int) ELECTION_SPAN_MS);
            lastSeen.put(ld, now);
        }
        Map<String, Object> m = new LinkedHashMap<>(); m.put("term", term); m.put("ok", ok); return m;
    }

    /** True when a record or command stamped with `t` may be accepted (not from a deposed leader). */
    public synchronized boolean acceptTerm(long t) {
        if (!quorumMode()) return true;
        if (t < term) { staleRejected++; return false; }
        return true;
    }

    public synchronized long term() { return term; }

    private Map<String, Object> httpCall(Peer p, String path, Map<String, Object> body) throws Exception {
        String tok = Token.mint(secret, "core-" + id, "admin", List.of("*"), System.currentTimeMillis() / 1000 + 30);
        HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(p.url + path)).timeout(Duration.ofMillis(900)).header("Authorization", "Bearer " + tok)
                .POST(HttpRequest.BodyPublishers.ofString(Json.str(body))).build(), HttpResponse.BodyHandlers.ofString());
        if (r.statusCode() != 200) throw new java.io.IOException("peer answered " + r.statusCode());
        return Json.map(Json.parse(r.body()));
    }

    // ---------------------------------------------------------------- legacy pair mode
    private void probe() {
        long now = clock.getAsLong();
        for (Peer p : peers) {
            try {
                HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(p.url + "/health")).timeout(Duration.ofMillis(700)).GET().build(), HttpResponse.BodyHandlers.ofString());
                if (r.statusCode() == 200) lastSeen.put(p.id, now);
            } catch (Exception ignore) { }
        }
        // a peer is considered alive if it answered within the last 1.6 s (3 missed probes)
        boolean lowerAlive = false;
        for (Peer p : peers) if (p.id.compareTo(id) < 0 && now - lastSeen.getOrDefault(p.id, 0L) < 1600) lowerAlive = true;
        boolean was = legacyLeader;
        legacyLeader = !lowerAlive;
        if (legacyLeader && !was) { becameLeaderAt = now; failovers++; }
    }

    /** Leader now? In quorum mode this is true only while the majority lease is valid. */
    public boolean isLeader() {
        if (!quorumMode()) return legacyLeader;
        synchronized (this) { return role == Role.LEADER && clock.getAsLong() < leaseUntil; }
    }

    public String leaderUrl() {
        if (quorumMode()) {
            synchronized (this) { if (leaderId != null) for (Peer p : peers) if (p.id.equals(leaderId)) return p.url; }
            return null;
        }
        long now = clock.getAsLong();
        for (Peer p : peers) if (p.id.compareTo(id) < 0 && now - lastSeen.getOrDefault(p.id, 0L) < 1600) return p.url;
        return null;
    }

    public Map<String, Object> status() {
        Map<String, Object> m = new LinkedHashMap<>();
        long now = clock.getAsLong();
        boolean ld = isLeader();
        m.put("node", id); m.put("role", ld ? "leader" : "standby"); m.put("mode", quorumMode() ? "quorum" : peers.isEmpty() ? "single" : "pair");
        if (quorumMode()) synchronized (this) { m.put("term", term); m.put("raft_role", role.name().toLowerCase()); m.put("leader", leaderId); m.put("quorum", quorum()); m.put("lease_ms", ld ? Math.max(0, leaseUntil - now) : 0); }
        List<Object> ps = new ArrayList<>();
        for (Peer p : peers) {
            Map<String, Object> x = new LinkedHashMap<>();
            x.put("id", p.id); x.put("alive", now - lastSeen.getOrDefault(p.id, 0L) < 1600);
            ps.add(x);
        }
        m.put("peers", ps); m.put("failovers", failovers); m.put("step_downs", stepDowns); m.put("stale_rejected", staleRejected);
        m.put("replicated_ok", replOk); m.put("replicated_failed", replFail);
        return m;
    }

    /** Best-effort async replication of an operator record to every peer (3 attempts). */
    public void replicate(Rec r) {
        if (peers.isEmpty()) return;
        String body = Json.str(r.toMap());
        String tok = Token.mint(secret, "core-" + id, "admin", List.of("*"), System.currentTimeMillis() / 1000 + 60);
        for (Peer p : peers) Thread.startVirtualThread(() -> {
            for (int a = 0; a < 3; a++) {
                try {
                    HttpResponse<String> resp = http.send(HttpRequest.newBuilder(URI.create(p.url + "/api/replicate")).timeout(Duration.ofSeconds(2))
                            .header("Authorization", "Bearer " + tok).header("X-Term", Long.toString(term())).POST(HttpRequest.BodyPublishers.ofString(body)).build(), HttpResponse.BodyHandlers.ofString());
                    if (resp.statusCode() == 200) { replOk++; return; }
                    if (resp.statusCode() == 409) { replFail++; return; }   // the peer knows a newer term: we are not the leader, do not retry
                } catch (Exception ignore) { }
                try { Thread.sleep(200L << a); } catch (InterruptedException e) { return; }
            }
            replFail++;
        });
    }

    /** On startup: fetch operator records newer than `sinceTs` from any peer and apply them. */
    public List<Rec> catchUp(long sinceTs) {
        String tok = Token.mint(secret, "core-" + id, "admin", List.of("*"), System.currentTimeMillis() / 1000 + 60);
        for (Peer p : peers) {
            try {
                HttpResponse<String> r = http.send(HttpRequest.newBuilder(URI.create(p.url + "/api/oplog?since=" + sinceTs)).timeout(Duration.ofSeconds(3))
                        .header("Authorization", "Bearer " + tok).GET().build(), HttpResponse.BodyHandlers.ofString());
                if (r.statusCode() != 200) continue;
                List<Rec> out = new ArrayList<>();
                for (Object o : (List<?>) Json.parse(r.body())) out.add(Rec.fromMap(Json.map(o)));
                return out;
            } catch (Exception ignore) { }
        }
        return List.of();
    }
}
