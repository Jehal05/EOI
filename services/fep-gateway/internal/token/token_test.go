package token

import (
	"testing"
	"time"
)

// Cross-language contract: this exact token was produced by an independent Python implementation
// (see tools/token_vector.py) and is also verified by the Rust and Java implementations in their tests.
const vectorSecret = "contract-test-secret-0123456789"
const vectorToken = "eyJzdWIiOiJ2ZWN0b3IiLCJyb2xlIjoib3BlcmF0b3IiLCJqdXIiOlsiTm9ydGhlcm4iXSwiZXhwIjo0MTAyNDQ0ODAwfQ.aa49339aa8cae099180f44323c5b6130884b98651ab521cf52d3da63a8c1ba38"

func TestCrossLanguageVector(t *testing.T) {
	c, err := Verify([]byte(vectorSecret), vectorToken, time.Date(2026, 11, 3, 0, 0, 0, 0, time.UTC))
	if err != nil || c.Sub != "vector" || c.Role != "operator" || !c.Covers("Northern") || c.Covers("Southern") {
		t.Fatalf("vector rejected: %+v %v", c, err)
	}
	if _, err := Verify([]byte("contract-test-secret-0123456780"), vectorToken, time.Now()); err == nil {
		t.Fatal("wrong secret must fail")
	}
}

func TestRoundTripAndTamper(t *testing.T) {
	s := []byte("k-0123456789abcdef")
	tok := Mint(s, Claims{Sub: "a", Role: "operator", Jur: []string{"Northern"}, Exp: time.Now().Add(time.Hour).Unix()})
	c, err := Verify(s, tok, time.Now())
	if err != nil || c.Sub != "a" || !c.AtLeast("operator") || c.AtLeast("engineer") || !c.Covers("Northern") || c.Covers("Southern") {
		t.Fatalf("%+v %v", c, err)
	}
	if _, err := Verify([]byte("other-secret-0123456"), tok, time.Now()); err != ErrSig {
		t.Fatal("wrong secret must fail")
	}
	if _, err := Verify(s, tok[:len(tok)-2]+"00", time.Now()); err == nil {
		t.Fatal("tampered signature must fail")
	}
	if _, err := Verify(s, tok, time.Now().Add(2*time.Hour)); err != ErrExpired {
		t.Fatal("expired token must fail")
	}
	for _, bad := range []string{"", ".", "a.b", "x", "abc.zz"} {
		if _, err := Verify(s, bad, time.Now()); err == nil {
			t.Fatalf("%q must fail", bad)
		}
	}
}
