package gs.core;

import java.util.*;

/** Deterministic mutation fuzzing of the parsers that read network input: JSON bodies / ingest lines, record maps (replication),
 *  and token verification (shared-secret and Ed25519 formats). Only RuntimeException/AuthException may escape; an Error
 *  (StackOverflow, OutOfMemory), another exception type, a hang, or an accepted forged token fails the run. */
public final class FuzzTests {
    static final String[] FRAGS = {"[", "]", "{", "}", "\"", "\\", ":", ",", "null", "NaN", "1e999", "-0", "\\u0000", "\\ud800", "\u0000", "99999999999999999999", "{\"tag\":", "[[[[[[[[[[[[[[[[[[[[", "\"\\"};

    static String mutate(Random r, String seed, String other) {
        StringBuilder b = new StringBuilder(seed);
        int n = 1 + r.nextInt(8);
        for (int k = 0; k < n; k++) {
            if (b.length() == 0) b.append('{');
            switch (r.nextInt(7)) {
                case 0 -> { int i = r.nextInt(b.length()); b.setCharAt(i, (char) (b.charAt(i) ^ (1 << r.nextInt(7)))); }
                case 1 -> { int i = r.nextInt(b.length()); b.setCharAt(i, (char) r.nextInt(128)); }
                case 2 -> { int i = r.nextInt(b.length()); b.delete(i, Math.min(b.length(), i + 1 + r.nextInt(32))); }
                case 3 -> { int i = r.nextInt(b.length()); String s = b.substring(i, Math.min(b.length(), i + 1 + r.nextInt(64))); b.insert(r.nextInt(b.length() + 1), s); }
                case 4 -> b.setLength(r.nextInt(b.length()));
                case 5 -> { if (!other.isEmpty()) { int i = r.nextInt(other.length()); b.insert(r.nextInt(b.length() + 1), other.substring(i, Math.min(other.length(), i + 1 + r.nextInt(64)))); } }
                default -> b.insert(r.nextInt(b.length() + 1), FRAGS[r.nextInt(FRAGS.length)]);
            }
            if (b.length() > 200_000) b.setLength(200_000);
        }
        return b.toString();
    }

    public static void run() throws Exception {
        Random r = new Random(0xC0FFEE);
        int iters = Integer.parseInt(System.getenv().getOrDefault("FUZZ_ITERS", "30000"));
        String[] json = {
            "{\"tag\":\"G001.P\",\"v\":12.5,\"quality\":\"good\",\"ts\":\"2026-11-05T10:00:00Z\"}", "{\"tag\":\"BRK1.STATE\",\"st\":2}", "{\"hb\":true}",
            "{\"seq\":5,\"ts\":1700000000000,\"k\":\"CMD\",\"user\":\"alice\",\"action\":\"open\",\"tag\":\"BRK1.STATE\",\"reason\":\"test\",\"id\":7,\"ok\":true}",
            "{\"a\":[1,2,{\"b\":[true,false,null,\"x\\u00e9\\n\"]}],\"c\":-1.5e-3}"};
        long slow = 0; int errs = 0, parsed = 0;
        long t0 = System.nanoTime();
        for (int i = 0; i < iters; i++) {
            String s = mutate(r, json[r.nextInt(json.length)], json[r.nextInt(json.length)]);
            long t = System.nanoTime();
            try {
                Object o = Json.parse(s); parsed++;
                try { Rec.fromMap(Json.map(o)); } catch (RuntimeException ignore) { }
            } catch (RuntimeException ignore) { errs++;
            } catch (Throwable e) { T.ok("JSON input #" + i + " escaped as " + e.getClass().getName() + ": " + s.substring(0, Math.min(80, s.length())).replace("\n", " "), false); return; }
            slow = Math.max(slow, System.nanoTime() - t);
        }
        T.ok("JSON/record parsing: " + iters + " mutated inputs (" + parsed + " parsed, " + errs + " rejected), slowest " + slow / 1_000_000 + " ms, no Error or foreign exception", slow < 2_000_000_000L);
        // deep nesting must be rejected cleanly, not blow the stack
        String deep = "[".repeat(200_000);
        boolean clean;
        try { Json.parse(deep); clean = false; } catch (RuntimeException e) { clean = true; } catch (Throwable e) { clean = false; }
        T.ok("200,000-deep nesting is rejected with a normal exception", clean);

        byte[] sec = "0123456789abcdef-fuzz".getBytes();
        long now = System.currentTimeMillis() / 1000;
        String good = Token.mint(sec, "a", "admin", List.of("*"), now + 600);
        int forged = 0, other = 0;
        for (int i = 0; i < iters; i++) {
            String m = mutate(r, good, "v2.eyJzdWIiOiJhIn0.AAAA");
            try { Token.verify(sec, m, now); if (!m.equals(good)) forged++; } catch (Token.AuthException e) { } catch (Throwable e) { other++; }
        }
        T.ok("token verification: " + iters + " mutated tokens, none accepted, only AuthException thrown", forged == 0 && other == 0);
    }
}
