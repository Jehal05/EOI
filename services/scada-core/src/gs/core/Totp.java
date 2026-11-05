package gs.core;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.ByteBuffer;
import java.security.MessageDigest;

/** RFC 6238 time-based one-time passwords (HMAC-SHA1, 30 s step) with RFC 4648 base32 helpers. */
public final class Totp {
    private Totp() {}

    private static final String B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

    public static String code(byte[] key, long unixSec, int digits) {
        try {
            Mac m = Mac.getInstance("HmacSHA1");
            m.init(new SecretKeySpec(key, "HmacSHA1"));
            byte[] h = m.doFinal(ByteBuffer.allocate(8).putLong(unixSec / 30).array());
            int o = h[h.length - 1] & 0xf;
            int bin = ((h[o] & 0x7f) << 24) | ((h[o + 1] & 0xff) << 16) | ((h[o + 2] & 0xff) << 8) | (h[o + 3] & 0xff);
            long mod = 1;
            for (int k = 0; k < digits; k++) mod *= 10;
            return String.format("%0" + digits + "d", bin % mod);
        } catch (Exception e) { throw new IllegalStateException(e); }
    }

    /** Returns the matched time step (> lastUsedStep, ±1 step tolerance) or -1. A step can be used once. */
    public static long verify(byte[] key, String submitted, long nowSec, long lastUsedStep) {
        if (submitted == null || submitted.length() != 6) return -1;
        long cur = nowSec / 30;
        long found = -1;
        for (long st = cur - 1; st <= cur + 1; st++) {      // constant work regardless of match
            boolean eq = MessageDigest.isEqual(code(key, st * 30, 6).getBytes(), submitted.getBytes());
            if (eq && st > lastUsedStep) found = st;
        }
        return found;
    }

    public static String b32(byte[] d) {
        StringBuilder b = new StringBuilder();
        int buf = 0, bits = 0;
        for (byte x : d) {
            buf = (buf << 8) | (x & 0xff); bits += 8;
            while (bits >= 5) { b.append(B32.charAt((buf >> (bits - 5)) & 31)); bits -= 5; }
        }
        if (bits > 0) b.append(B32.charAt((buf << (5 - bits)) & 31));
        return b.toString();
    }

    public static byte[] unb32(String s) {
        java.io.ByteArrayOutputStream o = new java.io.ByteArrayOutputStream();
        int buf = 0, bits = 0;
        for (char c : s.toUpperCase().replace("=", "").replace(" ", "").toCharArray()) {
            int v = B32.indexOf(c);
            if (v < 0) throw new IllegalArgumentException("bad base32");
            buf = (buf << 5) | v; bits += 5;
            if (bits >= 8) { o.write((buf >> (bits - 8)) & 0xff); bits -= 8; }
        }
        return o.toByteArray();
    }
}
