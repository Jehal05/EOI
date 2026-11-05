package gs.core;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public final class FoundationTests {
    public static void run() throws Exception {
        // ---- JSON
        T.eq("json num", 1.5, ((Map<?, ?>) Json.parse("{\"a\":1.5}")).get("a"));
        T.eq("json nested", "[1, 2]", ((List<?>) ((Map<?, ?>) Json.parse(" {\"a\" : [1,2] } ")).get("a")).toString().replace(".0", ""));
        T.eq("json unicode", "é", Json.parse("\"\\u00e9\""));
        for (String bad : new String[]{"", "{", "[1,]", "{\"a\":}", "\"abc", "tru", "01x", "{\"a\":1}x", "[1 2]", "{'a':1}", "\"\\x\"", "\"a\nb\"", "NaN", "[".repeat(100)})
            T.throwsA("json reject " + bad.replace("\n", "\\n"), () -> Json.parse(bad));
        T.eq("json roundtrip", "{\"a\":[1,2.5,\"x\\\"y\",null,true],\"b\":{}}", Json.str(Json.parse("{\"a\":[1,2.5,\"x\\\"y\",null,true],\"b\":{}}")));
        T.eq("json NaN -> null", "null", Json.str(Double.NaN));

        // ---- token: cross-language contract vector produced by the independent Python reference
        byte[] sec = "contract-test-secret-0123456789".getBytes(StandardCharsets.UTF_8);
        String vec = "eyJzdWIiOiJ2ZWN0b3IiLCJyb2xlIjoib3BlcmF0b3IiLCJqdXIiOlsiTm9ydGhlcm4iXSwiZXhwIjo0MTAyNDQ0ODAwfQ.aa49339aa8cae099180f44323c5b6130884b98651ab521cf52d3da63a8c1ba38";
        Token.Claims c = Token.verify(sec, vec, 1_000);
        T.eq("vector sub", "vector", c.sub());
        T.eq("vector role", "operator", c.role());
        T.eq("vector jur", List.of("Northern"), c.jur());
        T.eq("mint == vector", vec, Token.mint(sec, "vector", "operator", List.of("Northern"), 4102444800L));
        T.ok("covers", c.covers("Northern") && !c.covers("Southern"));
        T.ok("atLeast", c.atLeast("viewer") && c.atLeast("operator") && !c.atLeast("engineer"));
        String tampered = vec.substring(0, 5) + (vec.charAt(5) == 'A' ? 'B' : 'A') + vec.substring(6);
        for (String bad : new String[]{tampered, vec + "0", "x", "", "a.b.c", ".abc", vec.replace('.', ':'), vec.substring(0, vec.length() - 1) + "0"})
            try { Token.verify(sec, bad, 1_000); T.ok("token reject " + bad, false); } catch (Token.AuthException e) { T.ok("token reject", true); }
        try { Token.verify(sec, vec, 4102444801L); T.ok("expired", false); } catch (Token.AuthException e) { T.eq("expired", "token expired", e.getMessage()); }
        try { Token.verify("another-secret-000000".getBytes(), vec, 1); T.ok("wrong key", false); } catch (Token.AuthException e) { T.ok("wrong key", true); }

        // ---- TOTP: RFC 6238 Appendix B (SHA-1, 8 digits) known answers
        byte[] k = "12345678901234567890".getBytes(StandardCharsets.US_ASCII);
        T.eq("rfc6238 t=59", "94287082", Totp.code(k, 59, 8));
        T.eq("rfc6238 t=1111111109", "07081804", Totp.code(k, 1111111109L, 8));
        T.eq("rfc6238 t=1111111111", "14050471", Totp.code(k, 1111111111L, 8));
        T.eq("rfc6238 t=1234567890", "89005924", Totp.code(k, 1234567890L, 8));
        T.eq("rfc6238 t=2000000000", "69279037", Totp.code(k, 2000000000L, 8));
        T.eq("b32 roundtrip", Arrays.toString(k), Arrays.toString(Totp.unb32(Totp.b32(k))));
        T.eq("b32 known", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", Totp.b32(k));
        long now = 1_700_000_000L;
        String good = Totp.code(k, now, 6);
        long st = Totp.verify(k, good, now, -1);
        T.ok("totp accepts", st == now / 30);
        T.eq("totp replay rejected", -1L, Totp.verify(k, good, now, st));
        T.ok("totp +30s tolerated", Totp.verify(k, good, now + 30, -1) >= 0);
        T.eq("totp +90s rejected", -1L, Totp.verify(k, good, now + 90, -1));
        T.eq("totp wrong code", -1L, Totp.verify(k, "000000".equals(good) ? "000001" : "000000", now, -1));

        // ---- users: hashing, lockout, TOTP single use
        Path f = Files.createTempFile("users", ".json");
        Files.delete(f);
        byte[] tk = Users.add(f, "alice", "correct horse battery", "operator", List.of("Northern"));
        T.throwsA("short password refused", () -> { try { Users.add(f, "bob", "short", "viewer", List.of("*")); } catch (java.io.IOException e) { throw new RuntimeException(e); } });
        Users u = Users.load(f);
        T.ok("auth ok", u.authenticate("alice", "correct horse battery", 0) != null);
        T.ok("auth wrong pw", u.authenticate("alice", "wrong password!!", 0) == null);
        T.ok("auth unknown", u.authenticate("nobody", "correct horse battery", 0) == null);
        for (int i = 0; i < Users.MAX_FAILS; i++) u.authenticate("alice", "bad-bad-bad-bad", 1000);
        T.ok("locked after repeated failures", u.authenticate("alice", "correct horse battery", 2000) == null);
        T.ok("unlocked after window", u.authenticate("alice", "correct horse battery", 1000 + Users.LOCK_MS + 1) != null);
        long t = 1_700_000_000L;
        T.ok("user totp ok", u.verifyTotp("alice", Totp.code(tk, t, 6), t));
        T.ok("user totp replay refused", !u.verifyTotp("alice", Totp.code(tk, t, 6), t));
        T.ok("user totp next step ok", u.verifyTotp("alice", Totp.code(tk, t + 30, 6), t + 30));
        Files.delete(f);
    }
}
