package token

import (
	"crypto/ed25519"
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func setup(t *testing.T) (dir string) {
	t.Helper()
	dir = t.TempDir()
	mu.Lock()
	trust, signKy, signKi = nil, nil, ""
	mu.Unlock()
	t.Cleanup(func() { mu.Lock(); trust, signKy, signKi = nil, nil, ""; mu.Unlock() })
	return dir
}

func newKey(t *testing.T, dir, kid, maxRole, prefix string) string {
	kf, tk := Generate()
	kf.Kid, tk.Kid, tk.MaxRole, tk.SubPrefix = kid, kid, maxRole, prefix
	b, _ := jsonMarshal(kf)
	p := filepath.Join(dir, kid+".tokkey")
	if err := os.WriteFile(p, b, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := AddToTrustFile(filepath.Join(dir, "trust.json"), tk); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestAsymRoundTripAndPolicy(t *testing.T) {
	dir := setup(t)
	core := newKey(t, dir, "core-A", "admin", "")
	ems := newKey(t, dir, "ems", "viewer", "ems")
	if err := LoadTrust(filepath.Join(dir, "trust.json")); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	c := Claims{Sub: "alice", Role: "operator", Jur: []string{"North"}, Exp: now.Add(time.Minute).Unix()}
	if err := LoadSigner(core); err != nil {
		t.Fatal(err)
	}
	tok := Mint(nil, c)
	if !strings.HasPrefix(tok, "v2.") {
		t.Fatal("not a v2 token")
	}
	got, err := Verify(nil, tok, now)
	if err != nil || got.Sub != "alice" || got.Role != "operator" || !got.Covers("North") || got.Covers("South") {
		t.Fatalf("round trip: %+v %v", got, err)
	}
	// a restricted key cannot issue above its cap or with the wrong subject, even though its signature is genuine
	if err := LoadSigner(ems); err != nil {
		t.Fatal(err)
	}
	if _, err := Verify(nil, Mint(nil, Claims{Sub: "ems-analytics", Role: "viewer", Jur: []string{"*"}, Exp: now.Add(time.Minute).Unix()}), now); err != nil {
		t.Fatalf("legit service token refused: %v", err)
	}
	if _, err := Verify(nil, Mint(nil, Claims{Sub: "ems-analytics", Role: "admin", Jur: []string{"*"}, Exp: now.Add(time.Minute).Unix()}), now); err == nil {
		t.Fatal("role above the key's cap accepted")
	}
	if _, err := Verify(nil, Mint(nil, Claims{Sub: "alice", Role: "viewer", Jur: []string{"*"}, Exp: now.Add(time.Minute).Unix()}), now); err == nil {
		t.Fatal("subject outside the key's prefix accepted")
	}
	// expiry
	LoadSigner(core)
	if _, err := Verify(nil, Mint(nil, Claims{Sub: "a", Role: "viewer", Jur: nil, Exp: now.Add(-time.Second).Unix()}), now); err != ErrExpired {
		t.Fatalf("expired: %v", err)
	}
}

func TestAsymRefusesForgeriesAndDowngrade(t *testing.T) {
	dir := setup(t)
	core := newKey(t, dir, "core-A", "admin", "")
	LoadTrust(filepath.Join(dir, "trust.json"))
	LoadSigner(core)
	now := time.Now()
	good := Mint(nil, Claims{Sub: "a", Role: "viewer", Jur: []string{"*"}, Exp: now.Add(time.Minute).Unix()})
	parts := strings.Split(good, ".")
	// payload swapped to admin, signature kept
	evil := base64.RawURLEncoding.EncodeToString([]byte(`{"sub":"a","role":"admin","jur":["*"],"exp":99999999999,"kid":"core-A"}`))
	for name, tok := range map[string]string{
		"payload swapped":   "v2." + evil + "." + parts[2],
		"signature flipped": "v2." + parts[1] + "." + parts[2][:len(parts[2])-2] + "AA",
		"empty":             "",
		"only prefix":       "v2.",
		"no signature":      "v2." + parts[1],
		"huge":              "v2." + strings.Repeat("A", 5000) + ".x",
		"unknown key":       "v2." + base64.RawURLEncoding.EncodeToString([]byte(`{"sub":"a","role":"viewer","jur":[],"exp":99999999999,"kid":"nope"}`)) + "." + parts[2],
	} {
		if _, err := Verify(nil, tok, now); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	// a key that is not in the trust list cannot make a valid token
	pub, priv, _ := ed25519.GenerateKey(nil)
	_ = pub
	body := base64.RawURLEncoding.EncodeToString([]byte(`{"sub":"a","role":"admin","jur":["*"],"exp":99999999999,"kid":"core-A"}`))
	forged := "v2." + body + "." + base64.RawURLEncoding.EncodeToString(ed25519.Sign(priv, []byte(domain+body)))
	if _, err := Verify(nil, forged, now); err == nil {
		t.Error("token signed by an untrusted key accepted under a trusted key id")
	}
	// shared-secret tokens are refused once a trust list is installed
	setup(t)
	hm := func() string {
		mu.Lock()
		trust = nil
		mu.Unlock()
		return Mint([]byte("0123456789abcdef"), Claims{Sub: "a", Role: "admin", Jur: []string{"*"}, Exp: now.Add(time.Minute).Unix()})
	}()
	LoadTrust(filepath.Join(dir, "trust.json"))
	if _, err := Verify([]byte("0123456789abcdef"), hm, now); err == nil {
		t.Error("HMAC token accepted in asymmetric mode (downgrade)")
	}
	// and v2 is not mistaken for HMAC when no trust list is installed
	mu.Lock()
	trust = nil
	mu.Unlock()
	if _, err := Verify([]byte("0123456789abcdef"), good, now); err == nil {
		t.Error("v2 token accepted in shared-secret mode")
	}
}

func TestTrustListValidation(t *testing.T) {
	dir := setup(t)
	p := filepath.Join(dir, "bad.json")
	for _, content := range []string{`{}`, `{"keys":[{"kid":"x","pub":"AAAA","maxRole":"admin"}]}`, `{"keys":[{"kid":"x","pub":"` + base64.StdEncoding.EncodeToString(make([]byte, 32)) + `","maxRole":"root"}]}`, `not json`} {
		os.WriteFile(p, []byte(content), 0o600)
		if LoadTrust(p) == nil {
			t.Errorf("accepted bad trust list %q", content)
		}
	}
	if Asymmetric() {
		t.Error("a failed load must not switch the process into asymmetric mode")
	}
}
