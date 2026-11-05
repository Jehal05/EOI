package iec104

import (
	"bytes"
	"testing"
)

// FuzzDecode feeds arbitrary bytes to the frame and ASDU decoders: they must never panic, and what decodes must re-encode
// and decode to the same value.
func FuzzDecode(f *testing.F) {
	f.Add([]byte{0x68, 0x04, 0x07, 0x00, 0x00, 0x00})
	f.Add([]byte{0x68, 0x0E, 0x00, 0x00, 0x00, 0x00, 0x0D, 0x01, 0x03, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00})
	f.Add([]byte{0x0D, 0x81, 0x03, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00})
	f.Fuzz(func(t *testing.T, b []byte) {
		if fr, err := DecodeFrame(b); err == nil {
			_ = fr
		}
		_, _ = ReadFrame(bytes.NewReader(b))
		a, err := DecodeASDU(b)
		if err != nil {
			return
		}
		enc, err := a.Encode()
		if err != nil {
			return
		}
		a2, err := DecodeASDU(enc)
		if err != nil {
			t.Fatalf("own encoding does not decode: %v", err)
		}
		// compare encodings, not structs: SQ is written canonically and NaN payloads never compare equal
		enc2, err := a2.Encode()
		if err != nil || !bytes.Equal(enc, enc2) {
			t.Fatalf("encoding is not stable: % x vs % x (%v)", enc, enc2, err)
		}
	})
}
