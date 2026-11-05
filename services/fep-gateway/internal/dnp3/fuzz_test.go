package dnp3

import (
	"bytes"
	"testing"
)

func FuzzParse(f *testing.F) {
	f.Add([]byte{0x05, 0x64, 0x05, 0xC0, 0x01, 0x00, 0x0A, 0x00, 0xE0, 0x8C})
	f.Add([]byte{0xC0, 0x01, 0x3C, 0x01, 0x06})
	f.Add([]byte{0xC1, 0x81, 0x00, 0x00, 0x01, 0x02, 0x00, 0x00, 0x02, 0x01})
	f.Fuzz(func(t *testing.T, b []byte) {
		_, _ = ReadFrame(bytes.NewReader(b))
		if h, rest, err := parseHeader(b); err == nil {
			_ = h.ctrl()
			if len(rest) > 0 {
				for off := 0; off+3 <= len(rest) && off < 64; off += 3 {
					_, _ = parseQual(rest[off+2], rest[off+3:])
				}
			}
		}
		if len(b) > 0 {
			_, _ = parseQual(b[0], b[1:])
		}
	})
}
