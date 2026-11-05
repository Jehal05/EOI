// Package token verifies and mints the platform's HMAC-SHA256 bearer tokens
// (identical format across cim-pds, ems-analytics, scada-core and fep-gateway):
//
//	token = base64url(payload_json) "." hex(hmac_sha256(secret, base64url(payload_json)))
package token

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

type Claims struct {
	Sub  string   `json:"sub"`
	Role string   `json:"role"`
	Jur  []string `json:"jur"`
	Exp  int64    `json:"exp"` // unix seconds
}

var rank = map[string]int{"viewer": 1, "operator": 2, "engineer": 3, "admin": 4}

func (c Claims) AtLeast(role string) bool { return rank[c.Role] >= rank[role] && rank[role] > 0 }

func (c Claims) Covers(region string) bool {
	for _, j := range c.Jur {
		if j == "*" || j == region {
			return true
		}
	}
	return false
}

var enc = base64.RawURLEncoding

func sign(secret []byte, payload string) string {
	m := hmac.New(sha256.New, secret)
	m.Write([]byte(payload))
	return hex.EncodeToString(m.Sum(nil))
}

func Mint(secret []byte, c Claims) string {
	if Asymmetric() {
		return mintV2(c)
	}
	b, _ := json.Marshal(c)
	p := enc.EncodeToString(b)
	return p + "." + sign(secret, p)
}

var (
	ErrFormat  = errors.New("malformed token")
	ErrSig     = errors.New("bad signature")
	ErrExpired = errors.New("token expired")
)

func Verify(secret []byte, tok string, now time.Time) (Claims, error) {
	if Asymmetric() { // trust list installed: v2 only, the shared-secret format is not accepted
		if !strings.HasPrefix(tok, "v2.") {
			return Claims{}, ErrFormat
		}
		return verifyV2(tok, now)
	}
	p, sig, ok := strings.Cut(tok, ".")
	if !ok {
		return Claims{}, ErrFormat
	}
	got, err := hex.DecodeString(sig)
	if err != nil || hex.EncodeToString(got) != sig { // lower-case canonical form only
		return Claims{}, ErrFormat
	}
	want, _ := hex.DecodeString(sign(secret, p))
	if !hmac.Equal(got, want) { // constant time
		return Claims{}, ErrSig
	}
	raw, err := enc.DecodeString(p)
	if err != nil {
		return Claims{}, ErrFormat
	}
	var c Claims
	if json.Unmarshal(raw, &c) != nil {
		return Claims{}, ErrFormat
	}
	if c.Exp < now.Unix() {
		return Claims{}, ErrExpired
	}
	return c, nil
}
