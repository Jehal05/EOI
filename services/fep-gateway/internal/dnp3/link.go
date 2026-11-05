// Package dnp3 implements a subset of IEEE 1815 (DNP3) over TCP/TLS: data link framing with CRCs,
// transport segmentation, and an application layer with class/integrity polls, static and event
// objects, and select-before-operate CROB control. Subset only: see docs/STATUS.md for the exact scope.
package dnp3

import (
	"bytes"
	"errors"
	"io"
)

// crcTable: CRC-16/DNP (poly 0x3D65 reflected = 0xA6BC, init 0, output complemented).
var crcTable [256]uint16

func init() {
	for i := 0; i < 256; i++ {
		c := uint16(i)
		for j := 0; j < 8; j++ {
			if c&1 != 0 {
				c = c>>1 ^ 0xA6BC
			} else {
				c >>= 1
			}
		}
		crcTable[i] = c
	}
}

// CRC computes the DNP3 CRC of b.
func CRC(b []byte) uint16 {
	var c uint16
	for _, x := range b {
		c = c>>8 ^ crcTable[byte(c)^x]
	}
	return ^c
}

// Link control bits.
const (
	ctrlDIR         = 0x80
	ctrlPRM         = 0x40
	fcUserNoConfirm = 4 // primary: unconfirmed user data
)

// Frame is one link frame.
type Frame struct {
	Ctrl     byte
	Dst, Src uint16
	Data     []byte // user data (transport segment), max 250
}

var ErrFrame = errors.New("dnp3: bad frame")

func appendCRC(dst, blk []byte) []byte {
	c := CRC(blk)
	return append(append(dst, blk...), byte(c), byte(c>>8))
}

// Encode serialises the frame with header and per-16-byte-block CRCs.
func (f Frame) Encode() ([]byte, error) {
	if len(f.Data) > 250 {
		return nil, ErrFrame
	}
	h := []byte{0x05, 0x64, byte(5 + len(f.Data)), f.Ctrl, byte(f.Dst), byte(f.Dst >> 8), byte(f.Src), byte(f.Src >> 8)}
	out := appendCRC(nil, h)
	for i := 0; i < len(f.Data); i += 16 {
		j := i + 16
		if j > len(f.Data) {
			j = len(f.Data)
		}
		out = appendCRC(out, f.Data[i:j])
	}
	return out, nil
}

// ReadFrame reads one frame, resynchronising on 0x05 0x64 after noise or a CRC failure.
// Frames with a bad CRC are discarded (the caller's timeout/retry logic recovers).
func ReadFrame(r io.Reader) (Frame, error) {
	var b [1]byte
	prev := byte(0)
	for {
		if _, err := io.ReadFull(r, b[:]); err != nil {
			return Frame{}, err
		}
		if prev == 0x05 && b[0] == 0x64 {
			rest := make([]byte, 8)
			rest[0], rest[1] = 0x05, 0x64
			if _, err := io.ReadFull(r, rest[2:]); err != nil {
				return Frame{}, err
			}
			var crc [2]byte
			if _, err := io.ReadFull(r, crc[:]); err != nil {
				return Frame{}, err
			}
			if uint16(crc[0])|uint16(crc[1])<<8 != CRC(rest) {
				// false start: re-scan the bytes just read, a real frame may begin inside them
				r = io.MultiReader(bytes.NewReader(append(append([]byte(nil), rest[2:]...), crc[:]...)), r)
				prev = 0
				continue
			}
			ln := int(rest[2])
			if ln < 5 {
				prev = 0
				continue
			}
			f := Frame{Ctrl: rest[3], Dst: uint16(rest[4]) | uint16(rest[5])<<8, Src: uint16(rest[6]) | uint16(rest[7])<<8}
			n := ln - 5
			bad := false
			for n > 0 {
				k := n
				if k > 16 {
					k = 16
				}
				blk := make([]byte, k+2)
				if _, err := io.ReadFull(r, blk); err != nil {
					return Frame{}, err
				}
				if uint16(blk[k])|uint16(blk[k+1])<<8 != CRC(blk[:k]) {
					bad = true
				}
				f.Data = append(f.Data, blk[:k]...)
				n -= k
			}
			if bad {
				prev = 0
				continue
			}
			return f, nil
		}
		prev = b[0]
	}
}
