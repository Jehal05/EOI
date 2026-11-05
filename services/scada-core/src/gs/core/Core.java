package gs.core;

import java.io.IOException;
import java.nio.file.Path;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.function.Consumer;

/**
 * Single-writer actor around the Engine. Inputs arrive on a bounded queue; the actor stamps time and sequence,
 * appends to the event log BEFORE applying, and then applies. One thread owns all state mutation.
 */
public final class Core {
    public final Engine eng;
    public final EventLog log;
    public final BlockingQueue<Object> q = new ArrayBlockingQueue<>(200_000);
    public final LongAdder offered = new LongAdder(), noop = new LongAdder(), dropped = new LongAdder(), applied = new LongAdder(), logErrors = new LongAdder();
    public final AtomicLong seq = new AtomicLong();
    private final long[] lat = new long[8192];
    private int latN;
    private final List<Rec> ops = Collections.synchronizedList(new ArrayList<>());   // operator-originated records (for peer catch-up)
    private volatile boolean running = true, degraded;
    private final Thread actor;
    private final ScheduledExecutorService flusher = Executors.newSingleThreadScheduledExecutor(r -> { Thread t = new Thread(r, "log-flush"); t.setDaemon(true); return t; });
    public volatile Consumer<Rec> replicator = r -> { };
    private long lastTs;

    public Core(Engine eng, EventLog log, long startSeq) {
        this.eng = eng; this.log = log; this.seq.set(startSeq);
        this.lastTs = 0;
        actor = new Thread(this::loop, "core-actor");
        actor.setDaemon(false);
    }

    public void start() {
        actor.start();
        long[] n = {0};
        flusher.scheduleAtFixedRate(() -> {
            try { if (++n[0] % 10 == 0) log.sync(); else log.flush(); } catch (IOException e) { degraded = true; logErrors.increment(); }
        }, 100, 100, TimeUnit.MILLISECONDS);
        flusher.scheduleAtFixedRate(() -> q.offer((Runnable) this::sweepHeartbeat), 2, 2, TimeUnit.SECONDS);
    }

    /** Wall-clock arrival of the last FEP heartbeat (0 = no FEP has ever reported). Runtime fact only: its consequences are logged as samples. */
    public volatile long lastHb;
    public final long hbTimeoutMs = Long.getLong("gs.hb.timeout.ms", Long.parseLong(System.getenv().getOrDefault("GS_HB_TIMEOUT_MS", "20000")));
    public volatile boolean feedLost;

    public void heartbeat() { lastHb = System.currentTimeMillis(); }

    /** Runs on the actor. If the FEP stops sending heartbeats (it, or the path to it, is down) every good point is marked stale. */
    private void sweepHeartbeat() {
        long hb = lastHb;
        feedLost = hb != 0 && System.currentTimeMillis() - hb > hbTimeoutMs;
        if (!feedLost) return;
        for (Engine.PtState p : new ArrayList<>(eng.state.values()))
            if ("good".equals(p.q())) sample(Rec.sample(p.tag(), p.v(), p.st(), "stale", "hb-timeout"));
    }

    public void stop() throws IOException, InterruptedException {
        running = false; actor.join(2000); flusher.shutdownNow(); log.close();
    }

    public boolean degraded() { return degraded; }

    /** Non-blocking: telemetry must never stall the field side. */
    public boolean offer(Rec r) {
        offered.increment();
        if (q.offer(r)) return true;
        dropped.increment();
        return false;
    }

    /** Run on the actor thread and wait for the result (operator actions, verification). */
    public <T> T call(Callable<T> c) throws Exception {
        CompletableFuture<T> f = new CompletableFuture<>();
        q.put((Runnable) () -> { try { f.complete(c.call()); } catch (Throwable t) { f.completeExceptionally(t); } });
        try { return f.get(30, TimeUnit.SECONDS); } catch (ExecutionException e) { throw e.getCause() instanceof Exception x ? x : e; }
    }

    private void loop() {
        while (running) {
            try {
                Object o = q.poll(100, TimeUnit.MILLISECONDS);
                if (o == null) continue;
                if (o instanceof Rec r) sample(r); else ((Runnable) o).run();
            } catch (InterruptedException e) { return; }
            catch (Throwable t) { logErrors.increment(); System.err.println("actor error: " + t); }
        }
    }

    private long stamp(Rec r) {
        long now = System.currentTimeMillis();
        if (r.ts == 0) r.ts = Math.max(now, lastTs); // monotone even if the wall clock steps back
        lastTs = Math.max(lastTs, r.ts);
        return now;
    }

    private void sample(Rec r) {
        long now = stamp(r);
        if ("stale".equals(r.q)) {                                // keep the last value; only the quality changes
            Engine.PtState p = eng.state.get(r.tag);
            if (p == null) return;
            r.v = p.v(); r.st = p.st();
        }
        if (r.st < 0 && !Double.isNaN(r.v)) r.v = Math.round(r.v * 1e4) / 1e4;    // log exactly what is stored
        if (!eng.wouldChange(r)) { noop.increment(); return; }
        if (!commit(r)) return;
        if (r.src != null) try {
            long l = now - Instant.parse(r.src).toEpochMilli();
            synchronized (lat) { lat[latN++ & (lat.length - 1)] = l; }
        } catch (RuntimeException ignore) { }
    }

    /** Log first, then apply. If the log cannot be written the core refuses further changes (fail-safe). */
    private boolean commit(Rec r) {
        if (degraded) { dropped.increment(); return false; }
        r.seq = seq.incrementAndGet();
        try { log.append(r); } catch (IOException e) { degraded = true; logErrors.increment(); return false; }
        eng.apply(r);
        applied.increment();
        return true;
    }

    /** Operator action on the actor. Returns the engine result (ACK count / SHELVE flag); an ACK for an unknown id is rejected unlogged. */
    public Object actResult(Rec r) throws Exception {
        return call(() -> {
            stamp(r);
            if ("ACK".equals(r.k) && !r.all && !eng.hasAlarm(r.id)) return 0;
            if (degraded) throw new IOException("event log unavailable: core is degraded");
            r.seq = seq.incrementAndGet();
            try { log.append(r); } catch (IOException e) { degraded = true; logErrors.increment(); throw e; }
            Object res = eng.apply(r);
            applied.increment();
            ops.add(r);
            replicator.accept(r);
            return res;
        });
    }

    /** Apply an operator record replicated from the leader (keeps leader's timestamp for deterministic windows). */
    public void applyReplicated(Rec in) throws Exception {
        call(() -> {
            lastTs = Math.max(lastTs, in.ts);
            if (degraded) throw new IOException("degraded");
            in.seq = seq.incrementAndGet();
            log.append(in);
            eng.apply(in);
            ops.add(in);
            return null;
        });
    }

    public long syncNow() throws Exception { return call(() -> { log.sync(); return seq.get(); }); }

    public List<Rec> opsSince(long ts) {
        synchronized (ops) { List<Rec> o = new ArrayList<>(); for (Rec r : ops) if (r.ts > ts) o.add(r); return o; }
    }

    public void primeOps(Rec r) { ops.add(r); }

    public long[] latencyPercentiles() {
        long[] c;
        synchronized (lat) { c = Arrays.copyOf(lat, Math.min(latN, lat.length)); }
        if (c.length == 0) return new long[]{0, 0, 0, 0, 0};
        Arrays.sort(c);
        return new long[]{c[c.length / 2], c[(int) (c.length * 0.95)], c[(int) (c.length * 0.99)], c[c.length - 1], c.length};
    }

    /** Deterministic replay check: rebuild a fresh engine from the on-disk log and compare with live state. */
    public Map<String, Object> verifyReplay(byte[] key, Path file) throws Exception {
        return call(() -> {
            long t0 = System.nanoTime();
            log.sync();
            Engine fresh = new Engine(eng.cfg);
            EventLog.Result r = EventLog.read(file, key, fresh::apply, false);
            Map<String, Object> m = new LinkedHashMap<>();
            String s1 = eng.stateHash(), s2 = fresh.stateHash(), a1 = eng.alarmHash(), a2 = fresh.alarmHash();
            m.put("records", r.records()); m.put("chain_ok", true);
            m.put("state_hash_live", s1); m.put("state_hash_replay", s2); m.put("alarm_hash_live", a1); m.put("alarm_hash_replay", a2);
            m.put("identical", s1.equals(s2) && a1.equals(a2));
            m.put("replay_ms", (System.nanoTime() - t0) / 1_000_000);
            return m;
        });
    }
}
