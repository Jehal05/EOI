package gs.core;

import java.nio.file.*;
import java.util.*;

/** Quorum election and fencing, driven with a synthetic clock and an in-memory network that can partition and crash nodes. */
public final class ClusterTests {
    static final class Net {
        long now = 1_000_000;
        final Map<String, Cluster> nodes = new LinkedHashMap<>();
        final Set<String> down = new HashSet<>();
        final Set<String> cut = new HashSet<>();   // "A>B" blocks messages from A to B (and the reply)
        void cutBoth(String a, String b) { cut.add(a + ">" + b); cut.add(b + ">" + a); }
        Cluster make(String id, List<String> all, Path dir, long seed) {
            List<Cluster.Peer> ps = new ArrayList<>();
            for (String o : all) if (!o.equals(id)) ps.add(new Cluster.Peer(o, "http://" + o));
            Cluster c = new Cluster(id, ps, new byte[32], dir, new Random(seed));
            c.setClock(() -> now);
            c.setTransport((p, path, body) -> {
                if (down.contains(id) || down.contains(p.id()) || cut.contains(id + ">" + p.id())) throw new java.io.IOException("unreachable");
                Cluster t = nodes.get(p.id());
                return path.endsWith("prevote") ? t.onPreVote(body) : path.endsWith("vote") ? t.onVote(body) : t.onHeartbeat(body);
            });
            nodes.put(id, c);
            return c;
        }
        /** advance 100 ms: every live node ticks; returns leaders at this instant */
        List<String> step() {
            now += 100;
            for (var e : nodes.entrySet()) if (!down.contains(e.getKey())) e.getValue().tick(now);
            List<String> ld = new ArrayList<>();
            for (var e : nodes.entrySet()) if (!down.contains(e.getKey()) && e.getValue().isLeader()) ld.add(e.getKey());
            return ld;
        }
        String settle(int maxSteps) { for (int i = 0; i < maxSteps; i++) { var l = step(); if (l.size() == 1) return l.get(0); } return null; }
    }

    static int overlaps;
    static void stepChecked(Net n) { if (n.step().size() > 1) overlaps++; }

    public static void run() throws Exception {
        Path d = Files.createTempDirectory("clu");
        // ---- election, single leader
        Net n = new Net();
        for (String id : List.of("A", "B", "C")) n.make(id, List.of("A", "B", "C"), d.resolve(id), 7);
        for (String id : List.of("A", "B", "C")) Files.createDirectories(d.resolve(id));
        String l1 = n.settle(100);
        T.ok("a leader is elected from a cold start", l1 != null);
        T.eq("quorum mode with 3 nodes needs 2", 2, n.nodes.get("A").status().get("quorum"));
        long t1 = n.nodes.get(l1).term();
        for (int i = 0; i < 200; i++) stepChecked(n);
        T.eq("the leader is stable while healthy", List.of(l1), n.step());
        T.eq("term does not change while healthy", t1, n.nodes.get(l1).term());

        // ---- the leader is cut off alone: it must stop leading within its lease; the majority elects another
        for (String o : List.of("A", "B", "C")) if (!o.equals(l1)) n.cutBoth(l1, o);
        int stepsToStepDown = 0;
        while (n.nodes.get(l1).isLeader() && stepsToStepDown < 100) { stepChecked(n); stepsToStepDown++; }
        T.ok("isolated leader stops leading within its lease (<= 1.3 s)", stepsToStepDown * 100 <= 1300);
        String l2 = null;
        for (int i = 0; i < 100 && l2 == null; i++) { stepChecked(n); for (var e : n.nodes.entrySet()) if (!e.getKey().equals(l1) && e.getValue().isLeader()) l2 = e.getKey(); }
        T.ok("the connected majority elects a new leader", l2 != null && !l2.equals(l1));
        T.ok("the new leader has a higher term", n.nodes.get(l2).term() > t1);
        T.ok("a record from the old term is refused by the new side (fenced)", !n.nodes.get(l2).acceptTerm(t1));
        T.ok("records from the current term are accepted", n.nodes.get(l2).acceptTerm(n.nodes.get(l2).term()));
        for (int i = 0; i < 40; i++) stepChecked(n);
        T.ok("the isolated node did not lead again alone", !n.nodes.get(l1).isLeader());
        // heal
        n.cut.clear();
        for (int i = 0; i < 60; i++) stepChecked(n);
        T.eq("after healing there is exactly one leader", 1, (int) n.nodes.values().stream().filter(Cluster::isLeader).count());
        T.eq("and it is the one elected during the partition (no disruption by the returning node)", l2, n.nodes.values().stream().filter(Cluster::isLeader).findFirst().get().id);
        T.eq("all nodes agree on the term", 1, (int) n.nodes.values().stream().map(Cluster::term).distinct().count());
        T.eq("never two leaders at the same instant so far", 0, overlaps);

        // ---- no majority, no leader
        n.down.add("B"); n.down.add("C");
        for (int i = 0; i < 60; i++) stepChecked(n);
        T.ok("a single survivor of three cannot lead", n.nodes.values().stream().noneMatch(Cluster::isLeader));
        n.down.clear();
        T.ok("a leader returns when a majority is back", n.settle(100) != null);

        // ---- pair mode keeps the old rule and says so
        Net p = new Net();
        Cluster pa = p.make("A", List.of("A", "B"), null, 1);
        T.ok("two nodes are not a quorum cluster", !pa.quorumMode() && "pair".equals(pa.status().get("mode")));
        Cluster solo = new Cluster("A", List.of(), new byte[32]);
        T.ok("a single node leads", solo.isLeader() && "single".equals(solo.status().get("mode")));

        // ---- vote safety: one vote per term, persisted across a restart
        Net v = new Net();
        Path vd = Files.createTempDirectory("vote");
        Cluster va = v.make("A", List.of("A", "B", "C"), vd, 3);
        Map<String, Object> r1 = va.onVote(Map.of("term", 5.0, "candidate", "B"));
        Map<String, Object> r2 = va.onVote(Map.of("term", 5.0, "candidate", "C"));
        T.ok("first candidate in a term gets the vote", Boolean.TRUE.equals(r1.get("granted")));
        T.ok("a second candidate in the same term does not", Boolean.FALSE.equals(r2.get("granted")));
        Cluster vb = new Cluster("A", List.of(new Cluster.Peer("B", "x"), new Cluster.Peer("C", "y")), new byte[32], vd, new Random(1));
        T.eq("term survives a restart", 5L, vb.term());
        T.ok("the vote survives a restart (no double vote after a crash)", Boolean.FALSE.equals(vb.onVote(Map.of("term", 5.0, "candidate", "C")).get("granted")));
        T.ok("a lower term is never granted", Boolean.FALSE.equals(vb.onVote(Map.of("term", 4.0, "candidate", "B")).get("granted")));
        T.throwsA("a vote request from a stranger is refused", () -> vb.onVote(Map.of("term", 9.0, "candidate", "Z")));
        T.throwsA("a heartbeat from a stranger is refused", () -> vb.onHeartbeat(Map.of("term", 9.0, "leader", "Z")));

        // ---- chaos: random crashes and partitions, five nodes; at no instant two leaders
        Net c = new Net();
        List<String> ids = List.of("A", "B", "C", "D", "E");
        Path cd = Files.createTempDirectory("chaos");
        for (String id : ids) { Files.createDirectories(cd.resolve(id)); c.make(id, ids, cd.resolve(id), id.charAt(0)); }
        Random rr = new Random(42);
        int chaosOverlap = 0, leaderSteps = 0;
        for (int round = 0; round < 400; round++) {
            if (rr.nextInt(12) == 0) { c.down.clear(); c.cut.clear(); }
            if (rr.nextInt(6) == 0) { String x = ids.get(rr.nextInt(5)); if (c.down.size() < 2) c.down.add(x); }
            if (rr.nextInt(6) == 0) c.cutBoth(ids.get(rr.nextInt(5)), ids.get(rr.nextInt(5)));
            for (int k = 0; k < 10; k++) { var ld = c.step(); if (ld.size() > 1) chaosOverlap++; if (ld.size() == 1) leaderSteps++; }
        }
        T.eq("chaos (5 nodes, 400 random faults): never two leaders at once", 0, chaosOverlap);
        T.ok("chaos: a leader existed for a substantial part of the time", leaderSteps > 1500);
        c.down.clear(); c.cut.clear();
        T.ok("chaos: after all faults heal there is one leader", c.settle(200) != null);
        System.out.println("  cluster: leader present in " + leaderSteps + " of 4000 steps under chaos");
    }
}
