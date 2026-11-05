// Package iec104 implements the IEC 60870-5-104 application/link framing used between a
// control-centre front-end processor (master) and outstations. Written from the standard's
// frame layout; standard library only. All decoders are bounds-checked and never panic on
// malformed input (see fuzz tests).
package iec104

import (
	"errors"
	"fmt"
	"io"
)

const (
	startByte  = 0x68
	maxAPDULen = 253 // length octet value limit (control field + ASDU)
	// U-format function codes (first control octet)
	UStartDTAct = 0x07
	UStartDTCon = 0x0B
	UStopDTAct  = 0x13
	UStopDTCon  = 0x23
	UTestFRAct  = 0x43
	UTestFRCon  = 0x83
)

type FrameKind int

const (
	KindI FrameKind = iota
	KindS
	KindU
)

// Frame is one APDU. For I-frames ASDU holds the raw ASDU bytes.
type Frame struct {
	Kind    FrameKind
	SendSeq uint16 // N(S), I-frames
	RecvSeq uint16 // N(R), I- and S-frames
	U       byte   // U-format function octet
	ASDU    []byte
}

var (
	ErrBadStart  = errors.New("iec104: bad start byte")
	ErrBadLength = errors.New("iec104: bad APDU length")
	ErrBadFrame  = errors.New("iec104: malformed frame")
)

// Encode serialises the frame (including start and length octets).
func (f Frame) Encode() ([]byte, error) {
	var ctl [4]byte
	switch f.Kind {
	case KindI:
		if f.SendSeq > 0x7FFF || f.RecvSeq > 0x7FFF {
			return nil, fmt.Errorf("iec104: sequence number out of range")
		}
		if len(f.ASDU) == 0 || len(f.ASDU) > maxAPDULen-4 {
			return nil, ErrBadLength
		}
		ctl[0] = byte(f.SendSeq << 1)
		ctl[1] = byte(f.SendSeq >> 7)
		ctl[2] = byte(f.RecvSeq << 1)
		ctl[3] = byte(f.RecvSeq >> 7)
	case KindS:
		if f.RecvSeq > 0x7FFF {
			return nil, fmt.Errorf("iec104: sequence number out of range")
		}
		ctl[0] = 0x01
		ctl[2] = byte(f.RecvSeq << 1)
		ctl[3] = byte(f.RecvSeq >> 7)
	case KindU:
		switch f.U {
		case UStartDTAct, UStartDTCon, UStopDTAct, UStopDTCon, UTestFRAct, UTestFRCon:
		default:
			return nil, fmt.Errorf("iec104: invalid U function 0x%02x", f.U)
		}
		ctl[0] = f.U
	default:
		return nil, ErrBadFrame
	}
	body := len(ctl)
	if f.Kind == KindI {
		body += len(f.ASDU)
	}
	out := make([]byte, 0, 2+body)
	out = append(out, startByte, byte(body))
	out = append(out, ctl[:]...)
	if f.Kind == KindI {
		out = append(out, f.ASDU...)
	}
	return out, nil
}

// DecodeFrame parses a complete APDU (starting at the 0x68 octet).
func DecodeFrame(b []byte) (Frame, error) {
	if len(b) < 6 {
		return Frame{}, ErrBadLength
	}
	if b[0] != startByte {
		return Frame{}, ErrBadStart
	}
	n := int(b[1])
	if n < 4 || n > maxAPDULen || len(b) != 2+n {
		return Frame{}, ErrBadLength
	}
	c := b[2:6]
	switch {
	case c[0]&0x01 == 0: // I-format
		if n == 4 || c[2]&0x01 != 0 {
			return Frame{}, ErrBadFrame // must carry an ASDU; bit 0 of octet 3 is reserved (0)
		}
		return Frame{Kind: KindI, SendSeq: uint16(c[0])>>1 | uint16(c[1])<<7, RecvSeq: uint16(c[2])>>1 | uint16(c[3])<<7,
			ASDU: append([]byte(nil), b[6:]...)}, nil
	case c[0]&0x03 == 0x01: // S-format
		if n != 4 || c[0] != 0x01 || c[1] != 0 || c[2]&0x01 != 0 {
			return Frame{}, ErrBadFrame
		}
		return Frame{Kind: KindS, RecvSeq: uint16(c[2])>>1 | uint16(c[3])<<7}, nil
	default: // U-format (low bits 11)
		if n != 4 || c[1] != 0 || c[2] != 0 || c[3] != 0 {
			return Frame{}, ErrBadFrame
		}
		switch c[0] {
		case UStartDTAct, UStartDTCon, UStopDTAct, UStopDTCon, UTestFRAct, UTestFRCon:
			return Frame{Kind: KindU, U: c[0]}, nil
		}
		return Frame{}, ErrBadFrame
	}
}

// ReadFrame reads exactly one APDU from r.
func ReadFrame(r io.Reader) (Frame, error) {
	var hdr [2]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return Frame{}, err
	}
	if hdr[0] != startByte {
		return Frame{}, ErrBadStart
	}
	n := int(hdr[1])
	if n < 4 || n > maxAPDULen {
		return Frame{}, ErrBadLength
	}
	buf := make([]byte, 2+n)
	copy(buf, hdr[:])
	if _, err := io.ReadFull(r, buf[2:]); err != nil {
		return Frame{}, err
	}
	return DecodeFrame(buf)
}
