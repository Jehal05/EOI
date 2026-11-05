package c37118

import (
	"bytes"
	"testing"
)

func FuzzDecode(f *testing.F) {
	f.Add([]byte{0xAA, 0x31, 0x00, 0x14, 0x00, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0})
	f.Fuzz(func(t *testing.T, b []byte) {
		_, body, _ := Read(bytes.NewReader(b))
		cfg, err := DecodeConfig(b)
		if err == nil {
			_, _ = DecodeData(cfg, b)
		}
		_, _ = DecodeConfig(body)
		// a configuration that decodes must also survive data decoding of the same bytes without panicking
		if cfg, err := DecodeConfig(body); err == nil {
			_, _ = DecodeData(cfg, body)
		}
	})
}
