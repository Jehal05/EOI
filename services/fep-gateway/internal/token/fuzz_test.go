package token

import (
	"testing"
	"time"
)

// FuzzVerify: arbitrary strings must never verify, never panic, in either token mode.
func FuzzVerify(f *testing.F) {
	f.Add("v2.eyJzdWIiOiJhIn0.AAAA")
	f.Add("eyJzdWIiOiJhIn0.00ff")
	f.Add("")
	f.Add("...")
	f.Fuzz(func(t *testing.T, tok string) {
		if _, err := Verify([]byte("0123456789abcdef-test-secret"), tok, time.Now()); err == nil {
			t.Fatalf("random string verified: %q", tok)
		}
	})
}
