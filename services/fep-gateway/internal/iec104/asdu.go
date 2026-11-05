package iec104

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"time"
)

// ASDU type identifiers implemented.
const (
	M_SP_NA_1 = 1  // single point
	M_DP_NA_1 = 3  // double point
	M_ME_NC_1 = 13 // measured value, short float
	M_SP_TB_1 = 30 // single point with CP56Time2a
	M_DP_TB_1 = 31 // double point with CP56Time2a
	M_ME_TF_1 = 36 // measured value, short float with CP56Time2a
	C_SC_NA_1 = 45 // single command
	C_DC_NA_1 = 46 // double command
	C_IC_NA_1 = 100
	S_CH_NA_1 = 81 // authentication challenge (IEC 60870-5-7 / 62351-5 subset)
	S_RP_NA_1 = 82 // authentication reply
	S_ER_NA_1 = 87 // authentication error
)

// Causes of transmission.
const (
	CotPeriodic     = 1
	CotBackground   = 2
	CotSpont        = 3
	CotRequest      = 5
	CotActivation   = 6
	CotActCon       = 7
	CotDeactivation = 8
	CotDeactCon     = 9
	CotActTerm      = 10
	CotInrogen      = 20
	CotUnknownType  = 44
	CotUnknownCause = 45
	CotUnknownCA    = 46
	CotUnknownIOA   = 47
)

// Quality descriptor bits (QDS / upper nibble of DIQ).
const (
	QOverflow   = 0x01
	QBlocked    = 0x10
	QSubstitute = 0x20
	QNotTopical = 0x40
	QInvalid    = 0x80
)

// Double-point information values.
const (
	DPIIndeterminate = 0
	DPIOff           = 1 // open
	DPIOn            = 2 // closed
	DPIFault         = 3
)

// Object is one information object. Which fields are meaningful depends on ASDU.Type.
type Object struct {
	IOA     uint32
	Float   float32 // M_ME_*
	DPI     uint8   // M_DP_*, and DCS for C_DC_NA_1 (1=off, 2=on); SCS (0/1) for C_SC_NA_1
	Quality uint8   // QDS flags (0 = good)
	Select  bool    // S/E bit of commands: true = select, false = execute
	QU      uint8   // qualifier of command (0 = unspecified)
	QOI     uint8   // qualifier of interrogation (20 = station)
	Time    *time.Time
	Data    []byte // raw information element of the authentication types
}

// ASDU is a decoded application service data unit.
type ASDU struct {
	Type    uint8
	SQ      bool
	Cause   uint8
	Neg     bool // P/N bit
	Test    bool
	Orig    uint8
	CA      uint16
	Objects []Object
}

var ErrBadASDU = errors.New("iec104: malformed ASDU")

const headerLen = 6 // type, vsq, cot(2), ca(2)

func objLen(t uint8) (int, bool) { // information element length without IOA
	switch t {
	case M_SP_NA_1, M_DP_NA_1:
		return 1, true
	case M_ME_NC_1:
		return 5, true
	case M_SP_TB_1, M_DP_TB_1:
		return 8, true
	case M_ME_TF_1:
		return 12, true
	case C_SC_NA_1, C_DC_NA_1, C_IC_NA_1:
		return 1, true
	case S_CH_NA_1:
		return 16, true
	case S_RP_NA_1:
		return 22, true
	case S_ER_NA_1:
		return 8, true
	}
	return 0, false
}

// EncodeTime / DecodeTime handle CP56Time2a (UTC, millisecond resolution).
func EncodeTime(t time.Time) [7]byte {
	t = t.UTC()
	var b [7]byte
	ms := uint16(t.Second()*1000 + t.Nanosecond()/1e6)
	binary.LittleEndian.PutUint16(b[0:2], ms)
	b[2] = byte(t.Minute()) & 0x3F
	b[3] = byte(t.Hour()) & 0x1F
	wd := byte(t.Weekday())
	if wd == 0 {
		wd = 7
	}
	b[4] = byte(t.Day())&0x1F | wd<<5
	b[5] = byte(t.Month()) & 0x0F
	b[6] = byte(t.Year()-2000) & 0x7F
	return b
}

func DecodeTime(b []byte) (time.Time, bool) { // returns (time, valid)
	ms := int(binary.LittleEndian.Uint16(b[0:2]))
	iv := b[2]&0x80 != 0
	min, hr, day, mon, yr := int(b[2]&0x3F), int(b[3]&0x1F), int(b[4]&0x1F), int(b[5]&0x0F), int(b[6]&0x7F)
	if ms > 59999 || min > 59 || hr > 23 || day < 1 || mon < 1 || mon > 12 {
		return time.Time{}, false
	}
	return time.Date(2000+yr, time.Month(mon), day, hr, min, ms/1000, (ms%1000)*1e6, time.UTC), !iv
}

// Encode builds the ASDU bytes. Sequence (SQ) encoding is not produced; every object carries its IOA.
func (a ASDU) Encode() ([]byte, error) {
	el, ok := objLen(a.Type)
	if !ok {
		return nil, fmt.Errorf("iec104: unsupported type %d", a.Type)
	}
	if len(a.Objects) == 0 || len(a.Objects) > 127 {
		return nil, ErrBadASDU
	}
	size := headerLen + len(a.Objects)*(3+el)
	if size > maxAPDULen-4 {
		return nil, fmt.Errorf("iec104: ASDU too large (%d bytes)", size)
	}
	b := make([]byte, headerLen, size)
	b[0] = a.Type
	b[1] = byte(len(a.Objects))
	b[2] = a.Cause & 0x3F
	if a.Neg {
		b[2] |= 0x40
	}
	if a.Test {
		b[2] |= 0x80
	}
	b[3] = a.Orig
	binary.LittleEndian.PutUint16(b[4:6], a.CA)
	for _, o := range a.Objects {
		if o.IOA > 0xFFFFFF {
			return nil, ErrBadASDU
		}
		b = append(b, byte(o.IOA), byte(o.IOA>>8), byte(o.IOA>>16))
		switch a.Type {
		case M_SP_NA_1, M_SP_TB_1:
			b = append(b, o.DPI&0x01|o.Quality&0xF0)
		case M_DP_NA_1, M_DP_TB_1:
			b = append(b, o.DPI&0x03|o.Quality&0xF0)
		case M_ME_NC_1, M_ME_TF_1:
			var f [4]byte
			binary.LittleEndian.PutUint32(f[:], math.Float32bits(o.Float))
			b = append(b, f[:]...)
			b = append(b, o.Quality)
		case C_SC_NA_1:
			v := o.DPI&0x01 | (o.QU&0x1F)<<2
			if o.Select {
				v |= 0x80
			}
			b = append(b, v)
		case C_DC_NA_1:
			v := o.DPI&0x03 | (o.QU&0x1F)<<2
			if o.Select {
				v |= 0x80
			}
			b = append(b, v)
		case C_IC_NA_1:
			b = append(b, o.QOI)
		case S_CH_NA_1, S_RP_NA_1, S_ER_NA_1:
			if len(o.Data) != el {
				return nil, ErrBadASDU
			}
			b = append(b, o.Data...)
		}
		if a.Type == M_SP_TB_1 || a.Type == M_DP_TB_1 || a.Type == M_ME_TF_1 {
			t := time.Now()
			if o.Time != nil {
				t = *o.Time
			}
			tb := EncodeTime(t)
			b = append(b, tb[:]...)
		}
	}
	return b, nil
}

// DecodeASDU parses raw ASDU bytes; it never panics on malformed input.
func DecodeASDU(b []byte) (ASDU, error) {
	if len(b) < headerLen || len(b) > maxAPDULen-4 {
		return ASDU{}, ErrBadASDU // an ASDU can never exceed 249 bytes inside one APDU
	}
	a := ASDU{Type: b[0], SQ: b[1]&0x80 != 0, Cause: b[2] & 0x3F, Neg: b[2]&0x40 != 0, Test: b[2]&0x80 != 0,
		Orig: b[3], CA: binary.LittleEndian.Uint16(b[4:6])}
	n := int(b[1] & 0x7F)
	el, ok := objLen(a.Type)
	if !ok {
		return a, fmt.Errorf("iec104: unsupported type %d", a.Type)
	}
	if n == 0 {
		return a, ErrBadASDU
	}
	pos := headerLen
	var ioa uint32
	for i := 0; i < n; i++ {
		if !a.SQ || i == 0 {
			if pos+3 > len(b) {
				return a, ErrBadASDU
			}
			ioa = uint32(b[pos]) | uint32(b[pos+1])<<8 | uint32(b[pos+2])<<16
			pos += 3
		} else {
			ioa++
		}
		if pos+el > len(b) {
			return a, ErrBadASDU
		}
		e := b[pos : pos+el]
		o := Object{IOA: ioa}
		switch a.Type {
		case M_SP_NA_1, M_SP_TB_1:
			o.DPI, o.Quality = e[0]&0x01, e[0]&0xF0
		case M_DP_NA_1, M_DP_TB_1:
			o.DPI, o.Quality = e[0]&0x03, e[0]&0xF0
		case M_ME_NC_1, M_ME_TF_1:
			o.Float = math.Float32frombits(binary.LittleEndian.Uint32(e[0:4]))
			o.Quality = e[4]
		case C_SC_NA_1:
			o.DPI, o.QU, o.Select = e[0]&0x01, (e[0]>>2)&0x1F, e[0]&0x80 != 0
		case C_DC_NA_1:
			o.DPI, o.QU, o.Select = e[0]&0x03, (e[0]>>2)&0x1F, e[0]&0x80 != 0
		case C_IC_NA_1:
			o.QOI = e[0]
		case S_CH_NA_1, S_RP_NA_1, S_ER_NA_1:
			o.Data = append([]byte(nil), e...)
		}
		switch a.Type {
		case M_SP_TB_1, M_DP_TB_1:
			if t, ok := DecodeTime(e[1:8]); ok {
				o.Time = &t
			} else {
				o.Quality |= QInvalid
			}
		case M_ME_TF_1:
			if t, ok := DecodeTime(e[5:12]); ok {
				o.Time = &t
			} else {
				o.Quality |= QInvalid
			}
		}
		a.Objects = append(a.Objects, o)
		pos += el
	}
	if pos != len(b) {
		return a, ErrBadASDU // trailing bytes
	}
	return a, nil
}

// ObjectLen is the information-element length (without the IOA) of an implemented ASDU type.
func ObjectLen(t uint8) (int, bool) { return objLen(t) }
