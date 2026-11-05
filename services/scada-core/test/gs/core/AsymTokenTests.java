package gs.core;

import java.nio.file.*;
import java.security.*;
import java.util.*;

/** Ed25519 "v2" tokens: round trip, policy caps, forgeries, downgrade. */
public final class AsymTokenTests {
    static byte[] seedOf(PrivateKey k) { byte[] e = k.getEncoded(); return Arrays.copyOfRange(e, e.length - 32, e.length); }
    static byte[] pubOf(PublicKey k) { byte[] e = k.getEncoded(); return Arrays.copyOfRange(e, e.length - 32, e.length); }
    static String key(Path dir, String kid, String max, String prefix, StringBuilder trust) throws Exception {
        KeyPair kp = KeyPairGenerator.getInstance("Ed25519").generateKeyPair();
        Files.writeString(dir.resolve(kid + ".key"), "{\"kid\":\"" + kid + "\",\"seed\":\"" + Base64.getEncoder().encodeToString(seedOf(kp.getPrivate())) + "\"}");
        if (trust.length() > 0) trust.append(',');
        trust.append("{\"kid\":\"").append(kid).append("\",\"pub\":\"").append(Base64.getEncoder().encodeToString(pubOf(kp.getPublic()))).append("\",\"maxRole\":\"").append(max).append("\"");
        if (!prefix.isEmpty()) trust.append(",\"subPrefix\":\"").append(prefix).append("\"");
        trust.append('}');
        return kid;
    }

    public static void run() throws Exception {
        Path d = Files.createTempDirectory("tok");
        StringBuilder tr = new StringBuilder();
        key(d, "core-A", "admin", "", tr); key(d, "ems", "viewer", "ems", tr);
        Files.writeString(d.resolve("trust.json"), "{\"keys\":[" + tr + "]}");
        byte[] hmacSecret = "0123456789abcdef-secret".getBytes();
        long now = System.currentTimeMillis() / 1000;
        String hm = Token.mint(hmacSecret, "a", "admin", List.of("*"), now + 60);       // shared-secret token, minted before the switch
        T.ok("shared-secret mode is the default", !Token.asymmetric());
        Token.loadTrust(d.resolve("trust.json"));
        try {
            T.ok("trust list switches the process to asymmetric mode", Token.asymmetric());
            Token.loadSigner(d.resolve("core-A.key"));
            String t = Token.mint(null, "alice", "operator", List.of("North"), now + 60);
            T.ok("minted token is v2", t.startsWith("v2."));
            Token.Claims c = Token.verify(null, t, now);
            T.ok("round trip keeps subject, role and jurisdiction", c.sub().equals("alice") && c.role().equals("operator") && c.covers("North") && !c.covers("South"));
            T.throwsA("shared-secret token refused in asymmetric mode (no downgrade)", () -> { try { Token.verify(hmacSecret, hm, now); } catch (Token.AuthException e) { throw new RuntimeException(e); } });
            String[] parts = t.split("\\.");
            String swapped = "v2." + Base64.getUrlEncoder().withoutPadding().encodeToString("{\"sub\":\"alice\",\"role\":\"admin\",\"jur\":[\"*\"],\"exp\":99999999999,\"kid\":\"core-A\"}".getBytes()) + "." + parts[2];
            T.throwsA("payload swapped to admin with the old signature is refused", () -> { try { Token.verify(null, swapped, now); } catch (Token.AuthException e) { throw new RuntimeException(e); } });
            T.throwsA("expired token refused", () -> { try { Token.verify(null, Token.mint(null, "a", "viewer", List.of("*"), now - 5), now); } catch (Token.AuthException e) { throw new RuntimeException(e); } });
            for (String bad : new String[]{"", "v2.", "v2.x", "v2..", "v2.a.b.c", "x".repeat(5000)}) T.throwsA("malformed token refused: " + bad.length(), () -> { try { Token.verify(null, bad, now); } catch (Token.AuthException e) { throw new RuntimeException(e); } });
            Token.loadSigner(d.resolve("ems.key"));
            Token.verify(null, Token.mint(null, "ems-analytics", "viewer", List.of("*"), now + 60), now);
            T.throwsA("a restricted key cannot issue above its role cap", () -> { try { Token.verify(null, Token.mint(null, "ems-analytics", "admin", List.of("*"), now + 60), now); } catch (Token.AuthException e) { throw new RuntimeException(e); } });
            T.throwsA("a restricted key cannot use another subject", () -> { try { Token.verify(null, Token.mint(null, "alice", "viewer", List.of("*"), now + 60), now); } catch (Token.AuthException e) { throw new RuntimeException(e); } });
        } finally { Token.resetForTest(); }
        T.ok("reset returns to shared-secret mode and old HMAC tokens verify again", verifies(hmacSecret, hm, now));
    }
    static boolean verifies(byte[] s, String t, long now) { try { Token.verify(s, t, now); return true; } catch (Token.AuthException e) { return false; } }
}
