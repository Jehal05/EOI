package gs.core;

import java.util.*;

/** Minimal strict JSON (RFC 8259) parser/serialiser. Own code: no third-party parser on the network edge. */
public final class Json {
    private final String s;
    private int i;

    private Json(String s) { this.s = s; }

    public static Object parse(String s) {
        Json p = new Json(s);
        p.ws();
        Object v = p.val(0);
        p.ws();
        if (p.i != s.length()) throw p.err("trailing data");
        return v;
    }

    private IllegalArgumentException err(String m) { return new IllegalArgumentException("json: " + m + " at " + i); }

    private void ws() {
        while (i < s.length()) {
            char c = s.charAt(i);
            if (c == ' ' || c == '\n' || c == '\t' || c == '\r') i++; else break;
        }
    }

    private Object val(int d) {
        if (d > 64) throw err("nesting too deep");
        if (i >= s.length()) throw err("unexpected end");
        char c = s.charAt(i);
        switch (c) {
            case '{': return obj(d);
            case '[': return arr(d);
            case '"': return str();
            case 't': lit("true"); return Boolean.TRUE;
            case 'f': lit("false"); return Boolean.FALSE;
            case 'n': lit("null"); return null;
            default: return num();
        }
    }

    private void lit(String w) {
        if (!s.startsWith(w, i)) throw err("bad literal");
        i += w.length();
    }

    private Map<String, Object> obj(int d) {
        Map<String, Object> m = new LinkedHashMap<>();
        i++; ws();
        if (i < s.length() && s.charAt(i) == '}') { i++; return m; }
        while (true) {
            ws();
            if (i >= s.length() || s.charAt(i) != '"') throw err("expected key");
            String k = str();
            ws();
            if (i >= s.length() || s.charAt(i) != ':') throw err("expected ':'");
            i++; ws();
            m.put(k, val(d + 1));
            ws();
            if (i >= s.length()) throw err("unterminated object");
            char c = s.charAt(i++);
            if (c == '}') return m;
            if (c != ',') throw err("expected ',' or '}'");
        }
    }

    private List<Object> arr(int d) {
        List<Object> l = new ArrayList<>();
        i++; ws();
        if (i < s.length() && s.charAt(i) == ']') { i++; return l; }
        while (true) {
            ws();
            l.add(val(d + 1));
            ws();
            if (i >= s.length()) throw err("unterminated array");
            char c = s.charAt(i++);
            if (c == ']') return l;
            if (c != ',') throw err("expected ',' or ']'");
        }
    }

    private String str() {
        StringBuilder b = new StringBuilder();
        i++;
        while (true) {
            if (i >= s.length()) throw err("unterminated string");
            char c = s.charAt(i++);
            if (c == '"') return b.toString();
            if (c < 0x20) throw err("control char in string");
            if (c != '\\') { b.append(c); continue; }
            if (i >= s.length()) throw err("bad escape");
            char e = s.charAt(i++);
            switch (e) {
                case '"': b.append('"'); break;
                case '\\': b.append('\\'); break;
                case '/': b.append('/'); break;
                case 'b': b.append('\b'); break;
                case 'f': b.append('\f'); break;
                case 'n': b.append('\n'); break;
                case 'r': b.append('\r'); break;
                case 't': b.append('\t'); break;
                case 'u':
                    if (i + 4 > s.length()) throw err("bad \\u escape");
                    try { b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); }
                    catch (NumberFormatException ex) { throw err("bad \\u escape"); }
                    i += 4;
                    break;
                default: throw err("bad escape");
            }
        }
    }

    private Double num() {
        int st = i;
        if (i < s.length() && s.charAt(i) == '-') i++;
        while (i < s.length() && "0123456789.eE+-".indexOf(s.charAt(i)) >= 0) i++;
        String t = s.substring(st, i);
        if (t.isEmpty() || !(t.charAt(0) == '-' || Character.isDigit(t.charAt(0)))) throw err("unexpected character");
        try {
            double v = Double.parseDouble(t);
            if (Double.isNaN(v) || Double.isInfinite(v)) throw err("number out of range");
            return v;
        } catch (NumberFormatException ex) { throw err("bad number"); }
    }

    // ------------------------------------------------------------------ serialise
    public static String str(Object o) {
        StringBuilder b = new StringBuilder(128);
        write(b, o);
        return b.toString();
    }

    @SuppressWarnings("unchecked")
    public static void write(StringBuilder b, Object o) {
        if (o == null) { b.append("null"); return; }
        if (o instanceof String t) { quote(b, t); return; }
        if (o instanceof Boolean x) { b.append(x); return; }
        if (o instanceof Double || o instanceof Float) {
            double v = ((Number) o).doubleValue();
            if (Double.isNaN(v) || Double.isInfinite(v)) { b.append("null"); return; }
            if (v == Math.rint(v) && Math.abs(v) < 1e15) b.append((long) v); else b.append(v);
            return;
        }
        if (o instanceof Number n) { b.append(n.longValue()); return; }
        if (o instanceof Map<?, ?> m) {
            b.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) b.append(',');
                first = false;
                quote(b, String.valueOf(e.getKey()));
                b.append(':');
                write(b, e.getValue());
            }
            b.append('}');
            return;
        }
        if (o instanceof Iterable<?> it) {
            b.append('[');
            boolean first = true;
            for (Object x : it) { if (!first) b.append(','); first = false; write(b, x); }
            b.append(']');
            return;
        }
        quote(b, o.toString());
    }

    private static void quote(StringBuilder b, String t) {
        b.append('"');
        for (int k = 0; k < t.length(); k++) {
            char c = t.charAt(k);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c)); else b.append(c);
            }
        }
        b.append('"');
    }

    // ------------------------------------------------------------------ typed accessors
    @SuppressWarnings("unchecked")
    public static Map<String, Object> map(Object o) { return o instanceof Map ? (Map<String, Object>) o : null; }
    public static String s(Map<String, Object> m, String k) { Object v = m.get(k); return v instanceof String t ? t : null; }
    public static double d(Map<String, Object> m, String k, double def) { Object v = m.get(k); return v instanceof Number n ? n.doubleValue() : def; }
    public static boolean b(Map<String, Object> m, String k) { return Boolean.TRUE.equals(m.get(k)); }
}
