// Package iec101 implements IEC 60870-5-101 over an FT1.2 link (unbalanced master/slave) on a
// byte stream: a TCP connection or a serial device. ASDU parameter sizes are configurable.
// Scope: single-char, fixed and variable frames, FCB/FCV duplicate detection, ACD class-1/2 polling,
// retries. Not implemented: balanced mode, file transfer, clock sync, per-vendor companion profiles.
package iec101

import (
	"errors"
	"fmt"
	"io"
)

const (
	startFixed = 0x10
	startVar   = 0x68
	endByte    = 0x16
	singleAck  = 0xE5
)

// Primary (master->slave) function codes.
const (
	FcResetLink   = 0
	FcUserConfirm = 3
	FcUserNoReply = 4
	FcReqStatus   = 9
	FcReqClass1   = 10
	FcReqClass2   = 11
)

// Secondary (slave->master) function codes.
const (
	FcAck       = 0
	FcNack      = 1
	FcUserData  = 8
	FcNoData    = 9
	FcLinkState = 11
)

// Frame is one decoded FT1.2 frame.
type Frame struct {
	Kind   byte // 'S' single char, 'F' fixed, 'V' variable
	Ctrl   byte
	Addr   uint16
	Data   []byte
	AddrSz int
}

var ErrFrame = errors.New("iec101: malformed frame")

func cks(b []byte) byte {
	var s byte
	for _, x := range b {
		s += x
	}
	return s
}

func putAddr(b []byte, a uint16, sz int) []byte {
	b = append(b, byte(a))
	if sz == 2 {
		b = append(b, byte(a>>8))
	}
	return b
}

// Encode builds the frame bytes.
func (f Frame) Encode() ([]byte, error) {
	sz := f.AddrSz
	if sz == 0 {
		sz = 1
	}
	switch f.Kind {
	case 'S':
		return []byte{singleAck}, nil
	case 'F':
		body := putAddr([]byte{f.Ctrl}, f.Addr, sz)
		return append(append([]byte{startFixed}, body...), cks(body), endByte), nil
	case 'V':
		body := putAddr([]byte{f.Ctrl}, f.Addr, sz)
		body = append(body, f.Data...)
		if len(body) > 255 || len(body) < 3 {
			return nil, fmt.Errorf("iec101: variable frame body %d bytes", len(body))
		}
		l := byte(len(body))
		out := []byte{startVar, l, l, startVar}
		out = append(out, body...)
		return append(out, cks(body), endByte), nil
	}
	return nil, ErrFrame
}

// Read reads one frame from r, resynchronising on garbage between frames. It never panics on bad input.
func Read(r io.Reader, addrSz int) (Frame, error) {
	if addrSz == 0 {
		addrSz = 1
	}
	var one [1]byte
	for {
		if _, err := io.ReadFull(r, one[:]); err != nil {
			return Frame{}, err
		}
		switch one[0] {
		case singleAck:
			return Frame{Kind: 'S'}, nil
		case startFixed:
			b := make([]byte, 1+addrSz+2)
			if _, err := io.ReadFull(r, b); err != nil {
				return Frame{}, err
			}
			if b[len(b)-1] != endByte || cks(b[:len(b)-2]) != b[len(b)-2] {
				continue // bad frame: resynchronise
			}
			f := Frame{Kind: 'F', Ctrl: b[0], AddrSz: addrSz, Addr: uint16(b[1])}
			if addrSz == 2 {
				f.Addr |= uint16(b[2]) << 8
			}
			return f, nil
		case startVar:
			var h [3]byte
			if _, err := io.ReadFull(r, h[:]); err != nil {
				return Frame{}, err
			}
			if h[0] != h[1] || h[2] != startVar || int(h[0]) < 1+addrSz {
				continue
			}
			b := make([]byte, int(h[0])+2)
			if _, err := io.ReadFull(r, b); err != nil {
				return Frame{}, err
			}
			n := int(h[0])
			if b[n+1] != endByte || cks(b[:n]) != b[n] {
				continue
			}
			f := Frame{Kind: 'V', Ctrl: b[0], AddrSz: addrSz, Addr: uint16(b[1])}
			if addrSz == 2 {
				f.Addr |= uint16(b[2]) << 8
			}
			f.Data = append([]byte(nil), b[1+addrSz:n]...)
			return f, nil
		}
	}
}

// Control-field helpers. Primary: PRM=1, FCB(5), FCV(4). Secondary: PRM=0, ACD(5), DFC(4).
func primary(fc byte, fcb, fcv bool) byte {
	c := byte(0x40) | fc&0x0F
	if fcb {
		c |= 0x20
	}
	if fcv {
		c |= 0x10
	}
	return c
}

func secondary(fc byte, acd bool) byte {
	c := fc & 0x0F
	if acd {
		c |= 0x20
	}
	return c
}

func isPrimary(c byte) bool { return c&0x40 != 0 }
func fcOf(c byte) byte      { return c & 0x0F }
func fcbOf(c byte) bool     { return c&0x20 != 0 }
func fcvOf(c byte) bool     { return c&0x10 != 0 }
func acdOf(c byte) bool     { return c&0x20 != 0 }
