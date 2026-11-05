package gs.core;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public final class EngineTests {
    static Map<String, Engine.Cfg> cfg() {
        Map<String, Engine.Cfg> m = new LinkedHashMap<>();
        m.put("BRK-1.STATE", new Engine.Cfg("BRK-1.STATE", "status", "STATE", "", "SS-1", "BRK-1", null, null));
        m.put("BRK-2.STATE", new Engine.Cfg("BRK-2.STATE", "status", "STATE", "", "SS-2", "BRK-2", null, null));
        m.put("BR1-F.P", new Engine.Cfg("BR1-F.P", "analog", "P", "MW", "SS-1", "BR1", -100.0, 100.0));
        m.put("CN-1-BB.V", new Engine.Cfg("CN-1-BB.V", "analog", "V", "kV", "SS-1", "CN-1-BB", 125.4, 138.6));
        m.put("CN-2-BB.V", new Engine.Cfg("CN-2-BB.V", "analog", "V", "kV", "SS-2", "CN-2-BB", 125.4, 138.6));
        return m;
    }
    static long seq = 0;
    static Rec an(long ts, String tag, double v) { Rec r = Rec.sample(tag, v, -1, "good", null); r.ts = ts; r.seq = ++seq; return r; }
    static Rec st(long ts, String tag, int s) { Rec r = Rec.sample(tag, Double.NaN, s, "good", null); r.ts = ts; r.seq = ++seq; return r; }
    static Rec op(String k, long ts) { Rec r = new Rec(); r.k = k; r.ts = ts; r.seq = ++seq; return r; }

    @SuppressWarnings("unchecked")
    static String stateOf(Engine e, long id) {
        for (Map<String, Object> a : e.activeAlarms()) if (((Number) a.get("id")).longValue() == id) return (String) a.get("state");
        return "GONE";
    }

    public static void run() throws Exception {
        // ---- planned outages: lifecycle, two-person rule in the state machine, deterministic replay
        Engine o1 = new Engine(cfg()), o2 = new Engine(cfg());
        Rec c1 = op("OUT", 1000); c1.action = "create"; c1.user = "alice"; c1.tag = "BR0001,BR0002"; c1.t1 = 2000; c1.t2 = 9000; c1.reason = "relay test"; c1.data = "{\"verdict\":\"secure\"}";
        Rec self = op("OUT", 1100); self.action = "approve"; self.user = "alice"; self.id = 1; self.reason = "self";
        Rec ap = op("OUT", 1200); ap.action = "approve"; ap.user = "carol"; ap.id = 1; ap.reason = "checked";
        Rec st = op("OUT", 1300); st.action = "start"; st.user = "alice"; st.id = 1;
        Rec cm = op("OUT", 1400); cm.action = "complete"; cm.user = "alice"; cm.id = 1;
        Rec bad = op("OUT", 1500); bad.action = "start"; bad.user = "alice"; bad.id = 1;
        for (Rec r : new Rec[]{c1, self}) { o1.apply(r); o2.apply(Rec.fromMap(r.toMap())); }
        T.eq("requester cannot approve own outage", "requested", o1.outages.get("OUT-0001").get("status"));
        for (Rec r : new Rec[]{ap}) { o1.apply(r); o2.apply(Rec.fromMap(r.toMap())); }
        T.eq("second person approves", "approved", o1.outages.get("OUT-0001").get("status"));
        T.eq("decision is recorded", "carol", o1.outages.get("OUT-0001").get("decidedBy"));
        for (Rec r : new Rec[]{st, cm, bad}) { o1.apply(r); o2.apply(Rec.fromMap(r.toMap())); }
        T.eq("start then complete", "completed", o1.outages.get("OUT-0001").get("status"));
        T.eq("illegal transition ignored", "completed", o1.outages.get("OUT-0001").get("status"));
        T.ok("outage events in the sequence of events", o1.recentSoe(20).stream().anyMatch(e -> ((String) e.get("text")).contains("OUT-0001 APPROVED by carol")));
        T.eq("replay reproduces the same state hash", o1.stateHash(), o2.stateHash());

        // ---- communications loss: stale marking keeps values, one alarm per substation, recovery, replay
        Engine cm1 = new Engine(cfg()), cm2 = new Engine(cfg());
        List<Rec> feed = new ArrayList<>();
        feed.add(an(100, "BR1-F.P", 10)); feed.add(an(100, "CN-1-BB.V", 132)); feed.add(st(100, "BRK-1.STATE", 2)); feed.add(an(100, "CN-2-BB.V", 132)); feed.add(st(100, "BRK-2.STATE", 2));
        for (Rec r : feed) { cm1.apply(r); cm2.apply(Rec.fromMap(r.toMap())); }
        java.util.function.BiFunction<Long, String, Rec> stale = (ts, tag) -> { Engine.PtState p = cm1.state.get(tag); Rec r = Rec.sample(tag, p.v(), p.st(), "stale", "link-down"); r.ts = ts; r.seq = ++seq; return r; };
        Rec s1 = stale.apply(200L, "BR1-F.P");
        cm1.apply(s1); cm2.apply(Rec.fromMap(s1.toMap()));
        T.eq("a single stale point keeps its value", 10.0, cm1.state.get("BR1-F.P").v());
        T.eq("a single stale point is marked stale", "stale", cm1.state.get("BR1-F.P").q());
        T.ok("one stale point of three raises no alarm", cm1.activeAlarms().isEmpty());
        for (String t : new String[]{"CN-1-BB.V", "BRK-1.STATE"}) { Rec r = stale.apply(300L, t); cm1.apply(r); cm2.apply(Rec.fromMap(r.toMap())); }
        T.eq("all points of the substation stale: exactly one alarm, not one per point", 1, cm1.activeAlarms().size());
        T.eq("it is a communications alarm", "COMMS", cm1.activeAlarms().get(0).get("type"));
        T.eq("status of the other substation is unaffected", "ok", cm1.commsList().stream().filter(m -> m.get("substation").equals("SS-2")).findFirst().get().get("state"));
        T.eq("the lost substation is reported lost", "lost", cm1.commsList().stream().filter(m -> m.get("substation").equals("SS-1")).findFirst().get().get("state"));
        T.eq("replay reproduces comms state", cm1.stateHash() + cm1.alarmHash(), cm2.stateHash() + cm2.alarmHash());
        Rec back1 = an(400, "BR1-F.P", 10), back2 = an(400, "CN-1-BB.V", 132), back3 = st(400, "BRK-1.STATE", 2);
        T.ok("a fresh sample with an unchanged value still counts as a change after stale", cm1.wouldChange(back1));
        for (Rec r : new Rec[]{back1, back2, back3}) cm1.apply(r);
        T.eq("data restored to good", "good", cm1.state.get("BR1-F.P").q());
        T.ok("comms alarm returns to normal", cm1.activeAlarms().stream().noneMatch(a -> "COMMS".equals(a.get("type")) && "ACTIVE_UNACK".equals(a.get("state"))));
        T.ok("loss and restoration are both in the SOE", cm1.recentSoe(40).stream().anyMatch(e -> ((String) e.get("text")).contains("COMMUNICATIONS LOST")) && cm1.recentSoe(40).stream().anyMatch(e -> ((String) e.get("text")).contains("COMMUNICATIONS RESTORED")));

        // ---- measured (not asserted): marking a whole network stale and recovering it, 118-bus size and 9x tiled size
        for (int tiles : new int[]{1, 9}) {
            Map<String, Engine.Cfg> big = new HashMap<>();
            int subs = 118 * tiles, per = 14;
            for (int sIdx = 0; sIdx < subs; sIdx++) for (int k = 0; k < per; k++) { String tg = "T" + sIdx + "." + k; big.put(tg, new Engine.Cfg(tg, "analog", "P", "MW", "SS-" + sIdx, "E" + sIdx, -1e9, 1e9)); }
            Engine be = new Engine(big);
            long ts = 1000; long q = 0;
            for (String tg : big.keySet()) be.apply(an(ts, tg, 1.0));
            long t0 = System.nanoTime();
            for (String tg : big.keySet()) { Rec r = Rec.sample(tg, 1.0, -1, "stale", "link-down"); r.ts = 5000; r.seq = ++seq; be.apply(r); }
            long t1 = System.nanoTime();
            int lost = (int) be.commsList().stream().filter(m -> "lost".equals(m.get("state"))).count();
            for (String tg : big.keySet()) be.apply(an(9000, tg, 1.0));
            long t2 = System.nanoTime();
            System.out.printf("  bench comms: %d points, %d substations: mark stale %.0f ms (%d substations lost, %d alarms), recover %.0f ms%n", big.size(), subs, (t1 - t0) / 1e6, lost, be.activeAlarms().size(), (t2 - t1) / 1e6);
        }

        // ---- operator log (append-only, amendments reference the original) and shift handover (two-person)
        Engine lg = new Engine(cfg()), lg2 = new Engine(cfg());
        Rec l1 = op("LOG", 1000); l1.user = "alice"; l1.tag = "abnormal"; l1.reason = "Relay alarm at SS-1 under investigation"; l1.data = "SS-1";
        Rec l2 = op("LOG", 1100); l2.user = "alice"; l2.tag = "general"; l2.reason = "Correction: SS-2 not SS-1"; l2.id = 1;
        for (Rec r : new Rec[]{l1, l2}) { lg.apply(r); lg2.apply(Rec.fromMap(r.toMap())); }
        T.eq("log has two entries", 2, lg.logList(10, null, null).size());
        T.eq("newest first", 2L, ((Number) lg.logList(10, null, null).get(0).get("n")).longValue());
        T.eq("amendment references the original", 1L, ((Number) lg.logList(10, null, null).get(0).get("amends")).longValue());
        T.eq("original is unchanged", "Relay alarm at SS-1 under investigation", lg.logList(10, "abnormal", null).get(0).get("text"));
        T.eq("search by text", 1, lg.logList(10, null, "correction").size());
        Rec h1 = op("HAND", 2000); h1.user = "alice"; h1.action = "create"; h1.reason = "Quiet shift; relay test pending"; h1.tag = ""; h1.data = "{\"alarms\":{\"active\":3}}";
        Rec h1b = op("HAND", 2100); h1b.user = "bob"; h1b.action = "create"; h1b.reason = "second"; h1b.tag = ""; h1b.data = "{}";
        Rec ha = op("HAND", 2200); ha.user = "alice"; ha.action = "accept"; ha.id = 1; ha.reason = "self";
        Rec hb = op("HAND", 2300); hb.user = "bob"; hb.action = "accept"; hb.id = 1; hb.reason = "got it";
        for (Rec r : new Rec[]{h1, h1b, ha}) { lg.apply(r); lg2.apply(Rec.fromMap(r.toMap())); }
        T.eq("only one open handover at a time", 1, lg.handoverList(10).size());
        T.eq("outgoing operator cannot accept own handover", "pending", lg.pendingHandover().get("status"));
        for (Rec r : new Rec[]{hb}) { lg.apply(r); lg2.apply(Rec.fromMap(r.toMap())); }
        T.eq("incoming operator accepts", "accepted", lg.handoverList(1).get(0).get("status"));
        T.eq("acceptor recorded", "bob", lg.handoverList(1).get(0).get("acceptedBy"));
        T.ok("snapshot stored with the handover", lg.handoverList(1).get(0).get("snapshot") != null);
        T.eq("log and handover replay to the same state hash", lg.stateHash(), lg2.stateHash());

        // ---- global AI/ML switch: audited, part of the deterministic state, survives a record round trip
        Engine ai = new Engine(cfg()), ai2 = new Engine(cfg());
        T.ok("AI advisory enabled by default", ai.aiEnabled);
        String h0 = ai.stateHash();
        Rec off = op("AI", 5000); off.user = "root"; off.ok = false; off.reason = "regulator review";
        Rec use = op("AIUSE", 6000); use.user = "op1"; use.action = "forecast"; use.tag = "load:total"; use.reason = "h=60";
        Rec off2 = Rec.fromMap(off.toMap()), use2 = Rec.fromMap(use.toMap());
        T.eq("AI record round trip", off.toMap(), off2.toMap());
        T.eq("AIUSE record round trip", use.toMap(), use2.toMap());
        ai.apply(off); ai.apply(use);
        T.ok("switch off takes effect", !ai.aiEnabled);
        T.ok("switch changes the state hash", !h0.equals(ai.stateHash()));
        T.ok("both are in the sequence of events", ai.recentSoe(5).stream().anyMatch(x -> ((String) x.get("text")).contains("DISABLED by root")) && ai.recentSoe(5).stream().anyMatch(x -> ((String) x.get("text")).contains("used by op1")));
        ai2.apply(off2); ai2.apply(use2);
        T.eq("replay reproduces the same state hash", ai.stateHash(), ai2.stateHash());
        Rec on = op("AI", 7000); on.user = "root"; on.ok = true; on.reason = "back on"; ai.apply(on);
        T.ok("switch back on", ai.aiEnabled);


        // ---- parallel-run mode and legacy comparison
        Engine pr = new Engine(cfg()), pr2 = new Engine(cfg());
        pr.apply(an(1000, "BR1-F.P", 50)); pr2.apply(an(1000, "BR1-F.P", 50));
        T.ok("parallel-run off by default", !pr.parallelRun);
        String ph0 = pr.stateHash();
        Rec pon = op("PAR", 2000); pon.user = "root"; pon.ok = true; pon.reason = "shadowing legacy EMS";
        T.eq("PAR record round trip", pon.toMap(), Rec.fromMap(pon.toMap()).toMap());
        pr.apply(pon); pr2.apply(Rec.fromMap(pon.toMap()));
        T.ok("mode on", pr.parallelRun);
        T.ok("mode changes the state hash", !ph0.equals(pr.stateHash()));
        T.eq("replay reproduces the hash", pr.stateHash(), pr2.stateHash());
        T.ok("mode switch is in the event sequence", pr.recentSoe(5).stream().anyMatch(x -> ((String) x.get("text")).contains("PARALLEL-RUN MODE ON")));
        Parallel par = new Parallel();
        par.tolerance(0.5, 0.0, "test");
        T.ok("known tag accepted", par.ingest(pr, "BR1-F.P", 50.2, null, 1));
        par.ingest(pr, "BR1-F.P", 52.0, null, 2);
        T.ok("unknown tag is reported, not compared", !par.ingest(pr, "NOPE.P", 1.0, null, 3));
        par.ingest(pr, "BRK-1.STATE", null, 2, 4);
        Map<String, Object> rep = par.report(pr, true, 99.0);
        T.eq("two tags compared", 2, rep.get("tagsCompared"));
        T.eq("comparisons counted (breaker has no good value yet, so it is not compared)", 2L, rep.get("comparisons"));
        double ag = (Double) rep.get("agreementPct");
        T.ok("agreement is 1 of 2 (second analog off by 2.0 > 0.5)", Math.abs(ag - 50.0) < 1e-9);
        T.ok("criterion not met with unknown tag and a miss", !(Boolean) ((Map<?, ?>) rep.get("criterion")).get("met"));
        T.eq("one unknown legacy tag", 1, rep.get("legacyTagsUnknownHere"));

        // ---- limit alarm lifecycle with hysteresis (ISA-18.2 states)
        Engine e = new Engine(cfg());
        e.apply(an(1000, "BR1-F.P", 50));
        T.eq("no alarm in range", 0, e.activeAlarms().size());
        e.apply(an(2000, "BR1-F.P", 105));
        T.eq("HI raised", 1, e.activeAlarms().size());
        T.eq("HI type", "HI", e.activeAlarms().get(0).get("type"));
        T.eq("HI prio (flow=major)", 2L, e.activeAlarms().get(0).get("prio"));
        e.apply(an(3000, "BR1-F.P", 115));
        T.eq("escalate to HIHI", "HIHI", e.activeAlarms().stream().filter(a -> !"RTN_UNACK".equals(a.get("state"))).findFirst().get().get("type"));
        e.apply(an(4000, "BR1-F.P", 99));                      // inside hysteresis band (hi - 2) -> still HI? 99 > 98 -> yes
        T.ok("hysteresis holds HI at 99", e.activeAlarms().stream().anyMatch(a -> "HI".equals(a.get("type")) && !"RTN_UNACK".equals(a.get("state"))));
        e.apply(an(5000, "BR1-F.P", 90));
        T.ok("returns to normal below band", e.activeAlarms().stream().noneMatch(a -> !"RTN_UNACK".equals(a.get("state")) && !"ACTIVE_ACK".equals(a.get("state")) && ((String) a.get("type")).startsWith("H") && a.get("rtn") == null));
        Rec ack = op("ACK", 6000); ack.all = true; ack.user = "alice"; e.apply(ack);
        T.eq("all closed after ack-all of RTN alarms", 0, e.activeAlarms().size());
        T.ok("history kept", e.alarmHistory(10).size() >= 2);

        // ---- uncommanded trip: first-out + consequential grouping
        e = new Engine(cfg());
        e.apply(st(1000, "BRK-1.STATE", 2)); e.apply(st(1000, "BRK-2.STATE", 2));
        e.apply(an(1000, "CN-2-BB.V", 132));
        e.apply(st(10_000, "BRK-1.STATE", 1));
        e.apply(an(10_500, "CN-2-BB.V", 100));               // voltage collapse a moment later
        e.apply(st(12_000, "BRK-2.STATE", 1));               // second trip in window
        List<Map<String, Object>> al = e.activeAlarms();
        Map<String, Object> first = al.stream().filter(a -> "TRIP".equals(a.get("type"))).findFirst().get();
        T.eq("first trip is first-out", Boolean.TRUE, first.get("firstOut"));
        T.ok("later alarms are consequential", al.stream().filter(a -> a != first).allMatch(a -> Boolean.TRUE.equals(a.get("consequential"))));
        T.eq("same group", 1, al.stream().map(a -> a.get("group")).distinct().count() == 1 ? 1 : 0);
        e.apply(st(40_000, "BRK-2.STATE", 2));
        e.apply(st(60_000, "BRK-1.STATE", 2));
        e.apply(st(70_000, "BRK-1.STATE", 1));
        T.ok("new incident after window gets its own first-out", e.activeAlarms().stream().anyMatch(a -> Boolean.TRUE.equals(a.get("firstOut")) && ((Number) a.get("group")).longValue() == 2));

        // ---- commanded operation raises no alarm, uncommanded does
        e = new Engine(cfg());
        e.apply(st(1000, "BRK-1.STATE", 2));
        Rec cmd = op("CMD", 2000); cmd.tag = "BRK-1.STATE"; cmd.action = "open"; cmd.user = "alice"; cmd.reason = "maintenance"; e.apply(cmd);
        e.apply(st(2500, "BRK-1.STATE", 1));
        T.eq("commanded open: no alarm", 0, e.activeAlarms().size());
        T.ok("commanded open: SOE says commanded", e.recentSoe(5).stream().anyMatch(s -> ((String) s.get("text")).contains("(commanded)")));
        e.apply(st(3000, "BRK-1.STATE", 2));
        T.eq("uncommanded close alarms", 1, e.activeAlarms().size());
        cmd = op("CMD", 100_000); cmd.tag = "BRK-1.STATE"; cmd.action = "open"; cmd.user = "alice"; cmd.reason = "x"; e.apply(cmd);
        e.apply(st(130_000, "BRK-1.STATE", 1));             // 30 s later: outside the command window
        T.ok("late change after command window is an alarm", e.activeAlarms().stream().anyMatch(a -> "TRIP".equals(a.get("type"))));

        // ---- dead bus replaces voltage-low flood
        e = new Engine(cfg());
        e.apply(an(1000, "CN-1-BB.V", 132));
        e.apply(an(2000, "CN-1-BB.V", 0));
        T.eq("dead bus type", "DEADBUS", e.activeAlarms().get(0).get("type"));
        T.eq("dead bus is one alarm", 1, e.activeAlarms().size());
        e.apply(an(3000, "CN-1-BB.V", 132));
        T.ok("recovers", e.activeAlarms().stream().allMatch(a -> "RTN_UNACK".equals(a.get("state"))));

        // ---- shelving suppresses counts and expires deterministically
        e = new Engine(cfg());
        e.apply(an(1000, "BR1-F.P", 150));
        Rec sh = op("SHELVE", 2000); sh.tag = "BR1-F.P"; sh.minutes = 1; sh.user = "bob"; sh.reason = "nuisance"; e.apply(sh);
        T.eq("shelved not counted", 0L, e.alarmStats().get("active"));
        e.apply(an(2500, "CN-1-BB.V", 132));
        T.eq("still shelved before expiry", 1L, e.alarmStats().get("shelved"));
        e.apply(an(70_000, "CN-1-BB.V", 133));
        T.eq("shelf expired by record time", 0L, e.alarmStats().get("shelved"));

        // ---- unknown tags ignored; no-op detection
        e = new Engine(cfg());
        e.apply(an(1, "NOPE", 1));
        T.eq("unknown ignored", 0, e.state.size());
        e.apply(an(2, "BR1-F.P", 5));
        T.ok("identical sample is a no-op", !e.wouldChange(an(3, "BR1-F.P", 5)));
        T.ok("changed sample detected", e.wouldChange(an(3, "BR1-F.P", 6)));

        determinism();
        tamper();
    }

    /** Replaying the log into a fresh engine must reproduce state and alarms exactly. */
    static void determinism() throws Exception {
        Path f = Files.createTempFile("evt", ".log"); Files.delete(f);
        byte[] key = "log-key-0123456789abcdef".getBytes(StandardCharsets.UTF_8);
        Engine live = new Engine(cfg());
        EventLog log = new EventLog(f, key);
        log.open(r -> { });
        Random rnd = new Random(7);
        long ts = 1_000_000, sq = 0;
        String[] tags = {"BR1-F.P", "CN-1-BB.V", "CN-2-BB.V"};
        for (int i = 0; i < 20_000; i++) {
            ts += 1 + rnd.nextInt(400);
            Rec r;
            int c = rnd.nextInt(100);
            if (c < 6) { r = Rec.sample(rnd.nextBoolean() ? "BRK-1.STATE" : "BRK-2.STATE", Double.NaN, 1 + rnd.nextInt(2), "good", null); }
            else if (c < 7) { r = new Rec(); r.k = "ACK"; r.all = true; r.user = "u"; }
            else if (c < 8) { r = new Rec(); r.k = "CMD"; r.tag = "BRK-1.STATE"; r.action = rnd.nextBoolean() ? "open" : "close"; r.user = "u"; r.reason = "t"; }
            else if (c < 9) { r = new Rec(); r.k = "SHELVE"; r.tag = tags[rnd.nextInt(3)]; r.minutes = 1 + rnd.nextInt(3); r.user = "u"; r.reason = "t"; }
            else {
                String t = tags[rnd.nextInt(3)];
                double v = t.startsWith("BR") ? rnd.nextDouble() * 160 - 20 : (rnd.nextInt(30) == 0 ? 0 : 118 + rnd.nextDouble() * 30);
                r = Rec.sample(t, Math.round(v * 1e4) / 1e4, -1, rnd.nextInt(50) == 0 ? "stale" : "good", null);
            }
            r.ts = ts; r.seq = ++sq;
            if ("S".equals(r.k) && !live.wouldChange(r)) { sq--; continue; }
            log.append(r); live.apply(r);
        }
        log.close();
        Engine re = new Engine(cfg());
        EventLog.Result res = EventLog.read(f, key, re::apply, false);
        T.ok("replayed many records", res.records() > 10_000);
        T.eq("replay: state hash identical", live.stateHash(), re.stateHash());
        T.eq("replay: alarm hash identical", live.alarmHash(), re.alarmHash());
        T.eq("replay: last seq", live.lastSeq, re.lastSeq);
        System.out.println("  determinism: " + res.records() + " records replayed, state+alarm hashes identical");
        Files.delete(f);
    }

    static void tamper() throws Exception {
        Path f = Files.createTempFile("evt", ".log"); Files.delete(f);
        byte[] key = "log-key-0123456789abcdef".getBytes(StandardCharsets.UTF_8);
        EventLog log = new EventLog(f, key);
        log.open(r -> { });
        for (int i = 1; i <= 50; i++) { Rec r = an(i * 1000L, "BR1-F.P", i); log.append(r); }
        log.close();
        T.eq("clean chain verifies", 50L, EventLog.read(f, key, r -> { }, false).records());
        T.throwsA("wrong key rejected", () -> { try { EventLog.read(f, "another-key-0123456789".getBytes(), r -> { }, false); } catch (java.io.IOException x) { throw new RuntimeException(x); } });
        List<String> lines = new ArrayList<>(Files.readAllLines(f));
        List<String> edited = new ArrayList<>(lines);
        edited.set(20, lines.get(20).replace("\"v\":21", "\"v\":22"));
        Path g = Files.createTempFile("evt", ".log");
        Files.write(g, edited);
        T.throwsA("edited record detected", () -> { try { EventLog.read(g, key, r -> { }, false); } catch (java.io.IOException x) { throw new RuntimeException(x); } });
        List<String> deleted = new ArrayList<>(lines); deleted.remove(10);
        Files.write(g, deleted);
        T.throwsA("deleted record detected", () -> { try { EventLog.read(g, key, r -> { }, false); } catch (java.io.IOException x) { throw new RuntimeException(x); } });
        List<String> swapped = new ArrayList<>(lines); Collections.swap(swapped, 5, 6);
        Files.write(g, swapped);
        T.throwsA("reordered records detected", () -> { try { EventLog.read(g, key, r -> { }, false); } catch (java.io.IOException x) { throw new RuntimeException(x); } });
        // torn tail (crash during write) is repaired, earlier records survive
        Files.writeString(g, String.join("\n", lines) + "\n{\"seq\":99,\"ts\":1,\"k\":\"S\",\"tag\":\"BR1");
        EventLog.Result rr = EventLog.read(g, key, r -> { }, true);
        T.eq("torn tail: records kept", 50L, rr.records());
        T.ok("torn tail: truncated", rr.truncatedBytes() > 0);
        T.eq("after repair verifies", 50L, EventLog.read(g, key, r -> { }, false).records());
        Files.delete(f); Files.delete(g);
    }
}
