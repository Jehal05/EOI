package gs.core;

import java.util.*;

/** One input to the deterministic core. Everything that changes state is a Rec, and every Rec is logged. */
public final class Rec {
    public long seq, ts;
    public String k;                       // S sample | CMD accepted command | CMDRES result | ACK | SHELVE | UNSHELVE
    public String tag, q = "good", src;
    public double v = Double.NaN;
    public int st = -1;
    public String user, action, reason, err;
    public long id, minutes, t1, t2;
    public String data;                    // OUT: compact JSON of the server-side study
    public boolean all, ok;

    public static Rec sample(String tag, double v, int st, String q, String src) {
        Rec r = new Rec(); r.k = "S"; r.tag = tag; r.v = v; r.st = st; r.q = q; r.src = src; return r;
    }

    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("seq", seq); m.put("ts", ts); m.put("k", k);
        switch (k) {
            case "S" -> {
                m.put("tag", tag);
                if (st >= 0) m.put("st", (long) st); else m.put("v", v);
                if (!"good".equals(q)) m.put("q", q);
                if (src != null) m.put("src", src);
            }
            case "CMD" -> { m.put("tag", tag); m.put("action", action); m.put("user", user); m.put("reason", reason); }
            case "CMDRES" -> { m.put("tag", tag); m.put("action", action); m.put("user", user); m.put("ok", ok); if (err != null) m.put("err", err); }
            case "ACK" -> { m.put("user", user); if (all) m.put("all", true); else m.put("id", id); }
            case "SHELVE" -> { m.put("tag", tag); m.put("user", user); m.put("minutes", minutes); m.put("reason", reason); }
            case "UNSHELVE" -> { m.put("tag", tag); m.put("user", user); }
            case "PAR" -> { m.put("user", user); m.put("ok", ok); m.put("reason", reason); }
            case "AI" -> { m.put("user", user); m.put("ok", ok); m.put("reason", reason); }
            case "AIUSE" -> { m.put("user", user); m.put("tag", tag); m.put("action", action); m.put("reason", reason); }
            case "LOG" -> { m.put("user", user); m.put("tag", tag); m.put("reason", reason); if (id > 0) m.put("id", id); if (data != null) m.put("data", data); }
            case "HAND" -> { m.put("user", user); m.put("action", action); m.put("reason", reason); if (tag != null) m.put("tag", tag); if (id > 0) m.put("id", id); if (data != null) m.put("data", data); }
            case "OUT" -> { m.put("id", id); m.put("user", user); m.put("action", action); m.put("reason", reason); if (tag != null) m.put("tag", tag); if (t1 != 0) m.put("t1", t1); if (t2 != 0) m.put("t2", t2); if (data != null) m.put("data", data); }
            default -> throw new IllegalStateException("bad kind " + k);
        }
        return m;
    }

    public static Rec fromMap(Map<String, Object> m) {
        Rec r = new Rec();
        r.seq = (long) Json.d(m, "seq", 0); r.ts = (long) Json.d(m, "ts", 0); r.k = Json.s(m, "k");
        r.tag = Json.s(m, "tag"); r.user = Json.s(m, "user"); r.action = Json.s(m, "action"); r.reason = Json.s(m, "reason"); r.err = Json.s(m, "err");
        r.src = Json.s(m, "src");
        String q = Json.s(m, "q"); if (q != null) r.q = q;
        if (m.containsKey("st")) r.st = (int) Json.d(m, "st", -1);
        if (m.containsKey("v")) r.v = Json.d(m, "v", Double.NaN);
        r.id = (long) Json.d(m, "id", 0); r.minutes = (long) Json.d(m, "minutes", 0);
        r.all = Json.b(m, "all"); r.ok = Json.b(m, "ok");
        r.t1 = (long) Json.d(m, "t1", 0); r.t2 = (long) Json.d(m, "t2", 0); r.data = Json.s(m, "data");
        return r;
    }
}
