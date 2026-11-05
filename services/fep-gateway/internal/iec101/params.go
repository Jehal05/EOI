package iec101

import (
	"fmt"

	"gridsovereign/fep/internal/iec104"
)

// Params are the configurable field sizes of IEC 101 (fixed to 1/2... in IEC 104).
type Params struct {
	LinkAddr int // 1 or 2
	COT      int // 1 (no originator) or 2
	CA       int // 1 or 2
	IOA      int // 1, 2 or 3
}

// Default is the common profile: 1-byte link address, 2-byte COT, 2-byte CA, 3-byte IOA.
var Default = Params{LinkAddr: 1, COT: 2, CA: 2, IOA: 3}

func (p Params) Valid() error {
	if p.LinkAddr < 1 || p.LinkAddr > 2 || p.COT < 1 || p.COT > 2 || p.CA < 1 || p.CA > 2 || p.IOA < 1 || p.IOA > 3 {
		return fmt.Errorf("iec101: invalid parameter sizes %+v", p)
	}
	return nil
}

func put(b []byte, v uint32, n int) []byte {
	for i := 0; i < n; i++ {
		b = append(b, byte(v>>(8*i)))
	}
	return b
}

func get(b []byte, n int) uint32 {
	var v uint32
	for i := 0; i < n; i++ {
		v |= uint32(b[i]) << (8 * i)
	}
	return v
}

// Encode converts an ASDU to its IEC 101 wire form with the configured field sizes.
func (p Params) Encode(a iec104.ASDU) ([]byte, error) {
	if err := p.Valid(); err != nil {
		return nil, err
	}
	el, ok := iec104.ObjectLen(a.Type)
	if !ok {
		return nil, fmt.Errorf("iec101: unsupported type %d", a.Type)
	}
	if p.CA == 1 && a.CA > 0xFF && a.CA != 0xFFFF {
		return nil, fmt.Errorf("iec101: CA %d does not fit 1 byte", a.CA)
	}
	if p.IOA < 3 && a.Objects != nil {
		for _, o := range a.Objects {
			if o.IOA >= 1<<(8*uint(p.IOA)) {
				return nil, fmt.Errorf("iec101: IOA %d does not fit %d bytes", o.IOA, p.IOA)
			}
		}
	}
	full, err := a.Encode() // 104 layout: type vsq cot(2) ca(2) then objects of (ioa3 + el)
	if err != nil {
		return nil, err
	}
	out := []byte{full[0], full[1]}
	cot := full[2]
	out = append(out, cot)
	if p.COT == 2 {
		out = append(out, full[3])
	}
	ca := uint32(a.CA)
	if p.CA == 1 && a.CA == 0xFFFF {
		ca = 0xFF
	}
	out = put(out, ca, p.CA)
	pos := 6
	for i := 0; i < len(a.Objects); i++ {
		ioa := get(full[pos:], 3)
		out = put(out, ioa, p.IOA)
		out = append(out, full[pos+3:pos+3+el]...)
		pos += 3 + el
	}
	return out, nil
}

// Decode converts IEC 101 wire bytes to an ASDU (sequence encoding is expanded).
func (p Params) Decode(b []byte) (iec104.ASDU, error) {
	if err := p.Valid(); err != nil {
		return iec104.ASDU{}, err
	}
	hdr := 2 + p.COT + p.CA
	if len(b) < hdr {
		return iec104.ASDU{}, iec104.ErrBadASDU
	}
	typ, vsq := b[0], b[1]
	el, ok := iec104.ObjectLen(typ)
	if !ok {
		return iec104.ASDU{}, fmt.Errorf("iec101: unsupported type %d", typ)
	}
	n := int(vsq & 0x7F)
	sq := vsq&0x80 != 0
	if n == 0 {
		return iec104.ASDU{}, iec104.ErrBadASDU
	}
	cot := b[2]
	var orig byte
	if p.COT == 2 {
		orig = b[3]
	}
	ca := get(b[2+p.COT:], p.CA)
	if p.CA == 1 && ca == 0xFF {
		ca = 0xFFFF
	}
	// re-encode in the 104 layout (non-SQ) and let the 104 decoder validate the elements
	out := []byte{typ, byte(n), cot, orig, byte(ca), byte(ca >> 8)}
	pos := hdr
	var ioa uint32
	for i := 0; i < n; i++ {
		if !sq || i == 0 {
			if pos+p.IOA > len(b) {
				return iec104.ASDU{}, iec104.ErrBadASDU
			}
			ioa = get(b[pos:], p.IOA)
			pos += p.IOA
		} else {
			ioa++
		}
		if pos+el > len(b) {
			return iec104.ASDU{}, iec104.ErrBadASDU
		}
		out = put(out, ioa, 3)
		out = append(out, b[pos:pos+el]...)
		pos += el
	}
	if pos != len(b) {
		return iec104.ASDU{}, iec104.ErrBadASDU
	}
	return iec104.DecodeASDU(out)
}
