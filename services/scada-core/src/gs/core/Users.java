package gs.core;

import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

/** Local user store: PBKDF2-HMAC-SHA256 password hashes, per-user TOTP key, lockout, one-time TOTP steps. */
public final class Users {
    public static final int ITER = 210_000;
    public static final int MAX_FAILS = 5;
    public static final long LOCK_MS = 60_000;

    public record User(String name, String role, List<String> jur, byte[] salt, byte[] hash, int iter, byte[] totp) {}

    private final Map<String, User> users = new LinkedHashMap<>();
    private final ConcurrentHashMap<String, long[]> fails = new ConcurrentHashMap<>();   // user -> {count, lockedUntilMs}
    private final ConcurrentHashMap<String, Long> lastStep = new ConcurrentHashMap<>();
    private static final SecureRandom RNG = new SecureRandom();
    private static final byte[] DUMMY_SALT = new byte[16];

    static byte[] pbkdf2(String pw, byte[] salt, int iter) {
        try {
            return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
                    .generateSecret(new PBEKeySpec(pw.toCharArray(), salt, iter, 256)).getEncoded();
        } catch (Exception e) { throw new IllegalStateException(e); }
    }

    @SuppressWarnings("unchecked")
    public static Users load(Path p) throws IOException {
        Users u = new Users();
        if (!Files.exists(p)) return u;
        Map<String, Object> root = Json.map(Json.parse(Files.readString(p)));
        HexFormat h = HexFormat.of();
        for (Object o : (List<Object>) root.get("users")) {
            Map<String, Object> m = Json.map(o);
            List<String> jur = new ArrayList<>();
            for (Object x : (List<Object>) m.get("jur")) jur.add((String) x);
            u.users.put(Json.s(m, "name"), new User(Json.s(m, "name"), Json.s(m, "role"), jur,
                    h.parseHex(Json.s(m, "salt")), h.parseHex(Json.s(m, "hash")), (int) Json.d(m, "iter", ITER), h.parseHex(Json.s(m, "totp"))));
        }
        return u;
    }

    public static byte[] add(Path p, String name, String pw, String role, List<String> jur) throws IOException {
        if (!Token.RANK.containsKey(role)) throw new IllegalArgumentException("bad role");
        if (pw.length() < 12) throw new IllegalArgumentException("password must be at least 12 characters");
        Users u = load(p);
        byte[] salt = new byte[16], totp = new byte[20];
        RNG.nextBytes(salt); RNG.nextBytes(totp);
        u.users.put(name, new User(name, role, jur, salt, pbkdf2(pw, salt, ITER), ITER, totp));
        HexFormat h = HexFormat.of();
        List<Object> arr = new ArrayList<>();
        for (User x : u.users.values()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", x.name); m.put("role", x.role); m.put("jur", x.jur);
            m.put("salt", h.formatHex(x.salt)); m.put("hash", h.formatHex(x.hash)); m.put("iter", (long) x.iter); m.put("totp", h.formatHex(x.totp));
            arr.add(m);
        }
        Path tmp = p.resolveSibling(p.getFileName() + ".tmp");
        Files.writeString(tmp, Json.str(Map.of("users", arr)));
        try { Files.setPosixFilePermissions(tmp, PosixFilePermissions.fromString("rw-------")); } catch (Exception ignore) { }
        Files.move(tmp, p, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        return totp;
    }

    public synchronized boolean isEmpty() { return users.isEmpty(); }

    /** Returns the user on success. Unknown users cost the same hashing time as known ones. */
    public User authenticate(String name, String pw, long nowMs) {
        if (name == null || pw == null || name.length() > 64 || pw.length() > 256) return null;
        long[] f = fails.get(name);
        if (f != null && f[1] > nowMs) return null;            // locked out
        User u;
        synchronized (this) { u = users.get(name); }
        byte[] got = pbkdf2(pw, u != null ? u.salt : DUMMY_SALT, u != null ? u.iter : ITER);
        boolean ok = u != null && MessageDigest.isEqual(got, u.hash);
        if (!ok) {
            fails.compute(name, (k, v) -> {
                long c = (v == null ? 0 : v[0]) + 1;
                return c >= MAX_FAILS ? new long[]{0, nowMs + LOCK_MS} : new long[]{c, 0};
            });
            return null;
        }
        fails.remove(name);
        return u;
    }

    public boolean verifyTotp(String name, String code, long nowSec) {
        User u;
        synchronized (this) { u = users.get(name); }
        if (u == null) return false;
        boolean[] ok = {false};
        lastStep.compute(name, (k, last) -> {
            long st = Totp.verify(u.totp, code, nowSec, last == null ? -1 : last);
            if (st >= 0) { ok[0] = true; return st; }
            return last;
        });
        return ok[0];
    }

    public synchronized User get(String name) { return users.get(name); }
}
