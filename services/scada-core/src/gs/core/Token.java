package gs.core;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.nio.file.*;
import java.security.*;
import java.security.spec.*;
import java.util.*;

/** Bearer tokens: HMAC-SHA256 (shared secret) or, when a trust list is loaded, Ed25519 "v2" tokens; wire-compatible with cim-pds (Rust), fep-gateway (Go) and ems-analytics (Python). */
public final class Token {
    private Token() {}

    public static final Map<String, Integer> RANK = Map.of("viewer", 1, "operator", 2, "engineer", 3, "admin", 4);

    public record Claims(String sub, String role, List<String> jur, long exp) {
        public boolean atLeast(String r) { return RANK.getOrDefault(role, 0) >= RANK.getOrDefault(r, 99); }
        public boolean covers(String region) { return jur.contains("*") || (region != null && jur.contains(region)); }
    }

    public static class AuthException extends Exception {
        private static final long serialVersionUID = 1L;
        public AuthException(String m) { super(m); }
    }


    // ------------------------------------------------------------------ asymmetric (Ed25519) tokens, "v2"
    // v2 token = "v2." b64url(payload) "." b64url(sig); signed message = "gridsovereign-token-v2\n" + b64url(payload).
    // Once a trust list is loaded this process accepts v2 tokens only; mint() then needs a signing key.
    private static final String DOMAIN = "gridsovereign-token-v2\n";
    private static final byte[] PUB_PREFIX = HexFormat.of().parseHex("302a300506032b6570032100");
    private static final byte[] PRIV_PREFIX = HexFormat.of().parseHex("302e020100300506032b657004220420");
    private record TrustKey(String kid, PublicKey pub, String maxRole, String subPrefix) {}
    private static volatile Map<String, TrustKey> trust;
    private static volatile String signKid;
    private static volatile PrivateKey signKey;

    public static boolean asymmetric() { return trust != null; }
    static synchronized void resetForTest() { trust = null; signKey = null; signKid = null; }

    public static synchronized void loadTrust(Path file) throws Exception {
        Map<String, Object> root = Json.map(Json.parse(Files.readString(file)));
        Map<String, TrustKey> m = new HashMap<>();
        if (!(root.get("keys") instanceof List<?> ks) || ks.isEmpty()) throw new IllegalArgumentException("trust list is empty");
        KeyFactory kf = KeyFactory.getInstance("Ed25519");
        for (Object o : ks) {
            Map<String, Object> k = Json.map(o);
            byte[] raw = Base64.getDecoder().decode(Json.s(k, "pub"));
            String kid = Json.s(k, "kid"), max = Json.s(k, "maxRole");
            if (raw.length != 32 || kid == null || kid.isEmpty() || !RANK.containsKey(max)) throw new IllegalArgumentException("invalid trust list entry");
            byte[] spki = new byte[PUB_PREFIX.length + 32];
            System.arraycopy(PUB_PREFIX, 0, spki, 0, PUB_PREFIX.length); System.arraycopy(raw, 0, spki, PUB_PREFIX.length, 32);
            m.put(kid, new TrustKey(kid, kf.generatePublic(new X509EncodedKeySpec(spki)), max, Json.s(k, "subPrefix") == null ? "" : Json.s(k, "subPrefix")));
        }
        trust = m;
    }

    public static synchronized void loadSigner(Path file) throws Exception {
        Map<String, Object> k = Json.map(Json.parse(Files.readString(file)));
        byte[] seed = Base64.getDecoder().decode(Json.s(k, "seed"));
        if (seed.length != 32 || Json.s(k, "kid") == null) throw new IllegalArgumentException("invalid signing key file");
        byte[] pkcs8 = new byte[PRIV_PREFIX.length + 32];
        System.arraycopy(PRIV_PREFIX, 0, pkcs8, 0, PRIV_PREFIX.length); System.arraycopy(seed, 0, pkcs8, PRIV_PREFIX.length, 32);
        signKey = KeyFactory.getInstance("Ed25519").generatePrivate(new PKCS8EncodedKeySpec(pkcs8));
        signKid = Json.s(k, "kid");
    }

    /** Install trust list / signing key from GS_TOKEN_TRUST / GS_TOKEN_KEY when set. */
    public static void loadFromEnv() throws Exception {
        String t = System.getenv("GS_TOKEN_TRUST"), k = System.getenv("GS_TOKEN_KEY");
        if (t != null && !t.isEmpty()) loadTrust(Path.of(t));
        if (k != null && !k.isEmpty()) loadSigner(Path.of(k));
    }

    private static String mintV2(String sub, String role, List<String> jur, long exp) {
        if (signKey == null) throw new IllegalStateException("asymmetric mode without a signing key");
        try {
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("sub", sub); c.put("role", role); c.put("jur", jur); c.put("exp", exp); c.put("kid", signKid);
            String p = Base64.getUrlEncoder().withoutPadding().encodeToString(Json.str(c).getBytes(StandardCharsets.UTF_8));
            Signature sg = Signature.getInstance("Ed25519");
            sg.initSign(signKey); sg.update((DOMAIN + p).getBytes(StandardCharsets.UTF_8));
            return "v2." + p + "." + Base64.getUrlEncoder().withoutPadding().encodeToString(sg.sign());
        } catch (GeneralSecurityException e) { throw new IllegalStateException(e); }
    }

    private static Claims verifyV2(String tok, long nowSec) throws AuthException {
        if (tok == null || tok.length() > 4096 || !tok.startsWith("v2.")) throw new AuthException("malformed token");
        String rest = tok.substring(3);
        int dot = rest.indexOf('.');
        if (dot <= 0 || dot != rest.lastIndexOf('.')) throw new AuthException("malformed token");
        String p = rest.substring(0, dot);
        Map<String, Object> m;
        byte[] sig;
        try {
            sig = Base64.getUrlDecoder().decode(rest.substring(dot + 1));
            m = Json.map(Json.parse(new String(Base64.getUrlDecoder().decode(p), StandardCharsets.UTF_8)));
        } catch (RuntimeException e) { throw new AuthException("malformed token"); }
        if (m == null || sig.length != 64) throw new AuthException("malformed token");
        TrustKey k = trust.get(Json.s(m, "kid"));
        if (k == null) throw new AuthException("unknown signing key");
        try {
            Signature sg = Signature.getInstance("Ed25519");
            sg.initVerify(k.pub()); sg.update((DOMAIN + p).getBytes(StandardCharsets.UTF_8));
            if (!sg.verify(sig)) throw new AuthException("bad signature");
        } catch (GeneralSecurityException e) { throw new AuthException("bad signature"); }
        if (Json.s(m, "sub") == null || Json.s(m, "role") == null || !(m.get("jur") instanceof List<?> jl) || !(m.get("exp") instanceof Number en)) throw new AuthException("malformed token");
        String role = Json.s(m, "role");
        if (!RANK.containsKey(role) || RANK.get(role) > RANK.get(k.maxRole())) throw new AuthException("role exceeds what the signing key may issue");
        if (!k.subPrefix().isEmpty() && !Json.s(m, "sub").startsWith(k.subPrefix())) throw new AuthException("subject not allowed for the signing key");
        List<String> jur = new ArrayList<>();
        for (Object o : jl) if (o instanceof String t) jur.add(t); else throw new AuthException("malformed token");
        if (en.longValue() < nowSec) throw new AuthException("token expired");
        return new Claims(Json.s(m, "sub"), role, jur, en.longValue());
    }

    public static String hmacHex(byte[] key, String data) {
        try {
            Mac m = Mac.getInstance("HmacSHA256");
            m.init(new SecretKeySpec(key, "HmacSHA256"));
            return HexFormat.of().formatHex(m.doFinal(data.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) { throw new IllegalStateException(e); }
    }

    public static String mint(byte[] secret, String sub, String role, List<String> jur, long exp) {
        if (trust != null) return mintV2(sub, role, jur, exp);
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("sub", sub); c.put("role", role); c.put("jur", jur); c.put("exp", exp);
        String payload = Base64.getUrlEncoder().withoutPadding().encodeToString(Json.str(c).getBytes(StandardCharsets.UTF_8));
        return payload + "." + hmacHex(secret, payload);
    }

    public static Claims verify(byte[] secret, String tok, long nowSec) throws AuthException {
        if (trust != null) return verifyV2(tok, nowSec);   // trust list installed: v2 only, no shared-secret fallback
        if (tok == null || tok.length() > 4096) throw new AuthException("malformed token");
        int dot = tok.indexOf('.');
        if (dot <= 0 || dot != tok.lastIndexOf('.')) throw new AuthException("malformed token");
        String payload = tok.substring(0, dot), sig = tok.substring(dot + 1);
        byte[] expect = hmacHex(secret, payload).getBytes(StandardCharsets.US_ASCII);
        if (!MessageDigest.isEqual(expect, sig.getBytes(StandardCharsets.US_ASCII))) throw new AuthException("bad signature");
        Map<String, Object> m;
        try {
            m = Json.map(Json.parse(new String(Base64.getUrlDecoder().decode(payload), StandardCharsets.UTF_8)));
        } catch (RuntimeException e) { throw new AuthException("malformed token"); }
        if (m == null || Json.s(m, "sub") == null || Json.s(m, "role") == null || !(m.get("jur") instanceof List<?> jl) || !(m.get("exp") instanceof Number en))
            throw new AuthException("malformed token");
        List<String> jur = new ArrayList<>();
        for (Object o : jl) if (o instanceof String t) jur.add(t); else throw new AuthException("malformed token");
        long exp = en.longValue();
        if (exp < nowSec) throw new AuthException("token expired");
        return new Claims(Json.s(m, "sub"), Json.s(m, "role"), jur, exp);
    }
}
