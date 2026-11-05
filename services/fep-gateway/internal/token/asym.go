package token

// Asymmetric (Ed25519) bearer tokens, "v2".
//
//	token   = "v2." base64url(payload_json) "." base64url(signature)
//	payload = {"sub":..,"role":..,"jur":[..],"exp":..,"kid":..}
//	signed  = "gridsovereign-token-v2\n" base64url(payload_json)         (domain-separated message)
//
// Every verifier holds only a trust list (key id -> public key, the highest role that key may issue, an optional required
// subject prefix). A service that only verifies never holds a secret that could forge a token; a service that issues holds
// only its own private key, and what it may issue is capped by the trust list that every verifier applies. Once a trust
// list is installed the verifier accepts v2 tokens ONLY (the shared-secret format is refused: no downgrade).

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"
)

const domain = "gridsovereign-token-v2\n"

type TrustKey struct {
	Kid       string `json:"kid"`
	Pub       string `json:"pub"` // base64 (standard) of the 32-byte public key
	MaxRole   string `json:"maxRole"`
	SubPrefix string `json:"subPrefix,omitempty"`
	pub       ed25519.PublicKey
}

type trustFile struct {
	Keys []TrustKey `json:"keys"`
}

// KeyFile is a service's private signing key.
type KeyFile struct {
	Kid  string `json:"kid"`
	Seed string `json:"seed"` // base64 (standard) of the 32-byte seed
}

var (
	mu     sync.RWMutex
	trust  map[string]*TrustKey // nil: shared-secret mode
	signKi string
	signKy ed25519.PrivateKey
)

// LoadTrust reads a trust list and switches this process to asymmetric mode.
func LoadTrust(path string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var tf trustFile
	if err := json.Unmarshal(b, &tf); err != nil {
		return err
	}
	m := map[string]*TrustKey{}
	for i := range tf.Keys {
		k := tf.Keys[i]
		raw, err := base64.StdEncoding.DecodeString(k.Pub)
		if err != nil || len(raw) != ed25519.PublicKeySize || rank[k.MaxRole] == 0 || k.Kid == "" {
			return fmt.Errorf("trust list entry %d is invalid", i)
		}
		k.pub = raw
		m[k.Kid] = &k
	}
	if len(m) == 0 {
		return errors.New("trust list is empty")
	}
	mu.Lock()
	trust = m
	mu.Unlock()
	return nil
}

// LoadSigner installs this process's signing key; Mint then issues v2 tokens.
func LoadSigner(path string) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var kf KeyFile
	if err := json.Unmarshal(b, &kf); err != nil {
		return err
	}
	seed, err := base64.StdEncoding.DecodeString(kf.Seed)
	if err != nil || len(seed) != ed25519.SeedSize || kf.Kid == "" {
		return errors.New("signing key file is invalid")
	}
	mu.Lock()
	signKi, signKy = kf.Kid, ed25519.NewKeyFromSeed(seed)
	mu.Unlock()
	return nil
}

// Asymmetric reports whether this process verifies v2 tokens only.
func Asymmetric() bool { mu.RLock(); defer mu.RUnlock(); return trust != nil }

// Generate makes a new key pair; the caller stores the seed privately and adds the public part to the trust list.
func Generate() (KeyFile, TrustKey) {
	pub, priv, _ := ed25519.GenerateKey(nil)
	return KeyFile{Seed: base64.StdEncoding.EncodeToString(priv.Seed())}, TrustKey{Pub: base64.StdEncoding.EncodeToString(pub)}
}

func mintV2(c Claims) string {
	mu.RLock()
	kid, key := signKi, signKy
	mu.RUnlock()
	if key == nil {
		panic("token: asymmetric mode without a signing key")
	}
	body := struct {
		Claims
		Kid string `json:"kid"`
	}{c, kid}
	b, _ := json.Marshal(body)
	p := enc.EncodeToString(b)
	return "v2." + p + "." + enc.EncodeToString(ed25519.Sign(key, []byte(domain+p)))
}

func verifyV2(tok string, now time.Time) (Claims, error) {
	rest := strings.TrimPrefix(tok, "v2.")
	p, sig, ok := strings.Cut(rest, ".")
	if !ok || len(tok) > 4096 {
		return Claims{}, ErrFormat
	}
	raw, err := enc.DecodeString(p)
	if err != nil {
		return Claims{}, ErrFormat
	}
	sb, err := enc.DecodeString(sig)
	if err != nil || len(sb) != ed25519.SignatureSize {
		return Claims{}, ErrFormat
	}
	var body struct {
		Claims
		Kid string `json:"kid"`
	}
	if json.Unmarshal(raw, &body) != nil || body.Sub == "" || body.Role == "" {
		return Claims{}, ErrFormat
	}
	mu.RLock()
	k := trust[body.Kid]
	mu.RUnlock()
	if k == nil {
		return Claims{}, errors.New("unknown signing key")
	}
	if !ed25519.Verify(k.pub, []byte(domain+p), sb) {
		return Claims{}, ErrSig
	}
	if rank[body.Role] == 0 || rank[body.Role] > rank[k.MaxRole] {
		return Claims{}, errors.New("role exceeds what the signing key may issue")
	}
	if k.SubPrefix != "" && !strings.HasPrefix(body.Sub, k.SubPrefix) {
		return Claims{}, errors.New("subject not allowed for the signing key")
	}
	if body.Exp < now.Unix() {
		return Claims{}, ErrExpired
	}
	return body.Claims, nil
}

// LoadFromEnv installs the trust list from GS_TOKEN_TRUST and the signing key from GS_TOKEN_KEY, when set.
func LoadFromEnv() error {
	if p := os.Getenv("GS_TOKEN_TRUST"); p != "" {
		if err := LoadTrust(p); err != nil {
			return fmt.Errorf("GS_TOKEN_TRUST: %w", err)
		}
	}
	if p := os.Getenv("GS_TOKEN_KEY"); p != "" {
		if err := LoadSigner(p); err != nil {
			return fmt.Errorf("GS_TOKEN_KEY: %w", err)
		}
	}
	return nil
}

// AddToTrustFile appends (or replaces) a key in the trust list file, creating it if needed.
func AddToTrustFile(path string, k TrustKey) error {
	var tf trustFile
	if b, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(b, &tf); err != nil {
			return err
		}
	}
	out := tf.Keys[:0]
	for _, e := range tf.Keys {
		if e.Kid != k.Kid {
			out = append(out, e)
		}
	}
	tf.Keys = append(out, k)
	b, _ := json.MarshalIndent(tf, "", "  ")
	return os.WriteFile(path, b, 0o644)
}

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }
