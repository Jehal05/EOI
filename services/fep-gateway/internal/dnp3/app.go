package dnp3

import (
	"encoding/binary"
	"errors"
	"io"
	"math"
	"time"

	"gridsovereign/fep/internal/iec104"
)

// Application function codes.
const (
	FnConfirm   = 0
	FnRead      = 1
	FnWrite     = 2
	FnSelect    = 3
	FnOperate   = 4
	FnDirectOp  = 5
	FnResponse  = 129
	FnUnsolResp = 130
)

// IIN bits (IIN1 in the low byte, IIN2 in the high byte).
const (
	IINClass1     = 0x0002
	IINClass2     = 0x0004
	IINClass3     = 0x0008
	IINRestart    = 0x0080
	IINNoFunc     = 0x0100
	IINObjUnknown = 0x0200
	IINParamErr   = 0x0400
	IINOverflow   = 0x0800
)

// Object groups/variations used.
const (
	gBinIn, vBinFlags   = 1, 2
	gBinEv, vBinEvTime  = 2, 2
	gCROB, vCROB        = 12, 1
	gAnaIn, vAnaFloat   = 30, 5
	gAnaEv, vAnaEvFTime = 32, 7
	gClass, gIIN        = 60, 80
)

// CROB status codes.
const (
	StSuccess  = 0
	StNoSelect = 2
	StFormat   = 3
	StNotSupp  = 4
	StTooMany  = 6
)

const maxFragment = 2048

var ErrApp = errors.New("dnp3: malformed application message")

// header is the application layer header.
type header struct {
	fir, fin, con, uns bool
	seq                uint8
	fn                 uint8
	iin                uint16 // responses only
}

func (h header) ctrl() byte {
	c := h.seq & 0x0F
	if h.fir {
		c |= 0x80
	}
	if h.fin {
		c |= 0x40
	}
	if h.con {
		c |= 0x20
	}
	if h.uns {
		c |= 0x10
	}
	return c
}

func parseHeader(b []byte) (header, []byte, error) {
	if len(b) < 2 {
		return header{}, nil, ErrApp
	}
	h := header{fir: b[0]&0x80 != 0, fin: b[0]&0x40 != 0, con: b[0]&0x20 != 0, uns: b[0]&0x10 != 0, seq: b[0] & 0x0F, fn: b[1]}
	if h.fn >= 129 {
		if len(b) < 4 {
			return h, nil, ErrApp
		}
		h.iin = uint16(b[2]) | uint16(b[3])<<8
		return h, b[4:], nil
	}
	return h, b[2:], nil
}

func buildMsg(h header, body []byte) []byte {
	out := []byte{h.ctrl(), h.fn}
	if h.fn >= 129 {
		out = append(out, byte(h.iin), byte(h.iin>>8))
	}
	return append(out, body...)
}

// ---- transport layer ----

type transport struct {
	rw    io.ReadWriter
	local uint16
	peer  uint16
	dir   byte // ctrlDIR for masters, 0 for outstations
	tseq  byte
	rseq  int
	buf   []byte
	open  bool
}

func (t *transport) send(msg []byte) error {
	for i := 0; i < len(msg); i += 249 {
		j := i + 249
		if j > len(msg) {
			j = len(msg)
		}
		h := t.tseq & 0x3F
		t.tseq++
		if i == 0 {
			h |= 0x40
		}
		if j == len(msg) {
			h |= 0x80
		}
		f := Frame{Ctrl: t.dir | ctrlPRM | fcUserNoConfirm, Dst: t.peer, Src: t.local, Data: append([]byte{h}, msg[i:j]...)}
		b, err := f.Encode()
		if err != nil {
			return err
		}
		if _, err := t.rw.Write(b); err != nil {
			return err
		}
	}
	return nil
}

// recv returns the next complete application message addressed to us (or broadcast), with the source address.
func (t *transport) recv() ([]byte, uint16, error) {
	for {
		f, err := ReadFrame(t.rw)
		if err != nil {
			return nil, 0, err
		}
		if f.Ctrl&ctrlPRM == 0 || f.Ctrl&0x0F != fcUserNoConfirm || len(f.Data) < 1 {
			continue // link-status and other link services are not implemented
		}
		if f.Dst != t.local && f.Dst < 0xFFFD {
			continue
		}
		if t.peer != 0xFFFF && f.Src != t.peer {
			continue
		}
		h := f.Data[0]
		first, fin, seq := h&0x40 != 0, h&0x80 != 0, int(h&0x3F)
		if first {
			t.buf, t.rseq = nil, seq
		} else if t.rseq < 0 || seq != (t.rseq+1)&0x3F {
			t.buf, t.rseq = nil, -1 // lost segment: drop the partial message
			continue
		} else {
			t.rseq = seq
		}
		t.buf = append(t.buf, f.Data[1:]...)
		if len(t.buf) > 4*maxFragment {
			t.buf, t.rseq = nil, -1
			continue
		}
		if fin {
			m := t.buf
			t.buf, t.rseq = nil, -1
			return m, f.Src, nil
		}
	}
}

// ---- object encoding helpers ----

func objHdr(g, v, q byte) []byte { return []byte{g, v, q} }

func flagsFromQuality(q uint8) byte {
	f := byte(0x01) // ONLINE
	if q&iec104.QInvalid != 0 {
		f &^= 0x01
	}
	if q&iec104.QNotTopical != 0 {
		f |= 0x04 // COMM_LOST
	}
	if q&iec104.QSubstitute != 0 {
		f |= 0x08 // REMOTE_FORCED
	}
	if q&iec104.QBlocked != 0 {
		f |= 0x10 // LOCAL_FORCED
	}
	if q&iec104.QOverflow != 0 {
		f |= 0x20 // OVER_RANGE (analog)
	}
	return f
}

func qualityFromFlags(f byte) uint8 {
	var q uint8
	if f&0x01 == 0 {
		q |= iec104.QInvalid
	}
	if f&0x04 != 0 {
		q |= iec104.QNotTopical
	}
	if f&0x08 != 0 {
		q |= iec104.QSubstitute
	}
	if f&0x10 != 0 {
		q |= iec104.QBlocked
	}
	return q
}

// binFlags maps a double-point to one binary input: state bit 7; chatter (bit 5) marks the two
// non-determinate values (0 -> state 0, 3 -> state 1).
func binFlags(dpi, q uint8) byte {
	f := flagsFromQuality(q) &^ 0x20
	switch dpi {
	case iec104.DPIOn:
		f |= 0x80
	case iec104.DPIFault:
		f |= 0x80 | 0x20
	case iec104.DPIIndeterminate:
		f |= 0x20
	}
	return f
}

func dpiFromBin(f byte) (dpi, q uint8) {
	q = qualityFromFlags(f)
	switch {
	case f&0x20 != 0 && f&0x80 != 0:
		dpi = iec104.DPIFault
	case f&0x20 != 0:
		dpi = iec104.DPIIndeterminate
	case f&0x80 != 0:
		dpi = iec104.DPIOn
	default:
		dpi = iec104.DPIOff
	}
	return
}

func timeMs(t time.Time) []byte {
	var b [8]byte
	binary.LittleEndian.PutUint64(b[:], uint64(t.UnixMilli()))
	return b[:6]
}

func msTime(b []byte) time.Time {
	var x [8]byte
	copy(x[:], b[:6])
	return time.UnixMilli(int64(binary.LittleEndian.Uint64(x[:]))).UTC()
}

func putF32(f float32) []byte {
	var b [4]byte
	binary.LittleEndian.PutUint32(b[:], math.Float32bits(f))
	return b[:]
}

// qualifier parsing: returns object count, index width (0 = none), range width, and header length consumed.
type qual struct {
	prefix int // bytes of index prefix per object
	start  uint32
	stop   uint32
	count  uint32
	isRng  bool
	all    bool
	size   int // header bytes after the 3-byte object header
}

func parseQual(q byte, b []byte) (qual, error) {
	var r qual
	pc, rc := int(q>>4)&7, q&0x0F
	switch pc {
	case 0:
	case 1:
		r.prefix = 1
	case 2:
		r.prefix = 2
	case 3:
		r.prefix = 4
	default:
		return r, ErrApp
	}
	need := func(n int) error {
		if len(b) < n {
			return ErrApp
		}
		return nil
	}
	switch rc {
	case 0:
		if r.prefix != 0 || need(2) != nil {
			return r, ErrApp
		}
		r.start, r.stop, r.isRng, r.size = uint32(b[0]), uint32(b[1]), true, 2
	case 1:
		if r.prefix != 0 || need(4) != nil {
			return r, ErrApp
		}
		r.start, r.stop, r.isRng, r.size = uint32(binary.LittleEndian.Uint16(b)), uint32(binary.LittleEndian.Uint16(b[2:])), true, 4
	case 2:
		if r.prefix != 0 || need(8) != nil {
			return r, ErrApp
		}
		r.start, r.stop, r.isRng, r.size = binary.LittleEndian.Uint32(b), binary.LittleEndian.Uint32(b[4:]), true, 8
	case 6:
		if r.prefix != 0 {
			return r, ErrApp
		}
		r.all = true
	case 7:
		if need(1) != nil {
			return r, ErrApp
		}
		r.count, r.size = uint32(b[0]), 1
	case 8:
		if need(2) != nil {
			return r, ErrApp
		}
		r.count, r.size = uint32(binary.LittleEndian.Uint16(b)), 2
	case 9:
		if need(4) != nil {
			return r, ErrApp
		}
		r.count, r.size = binary.LittleEndian.Uint32(b), 4
	default:
		return r, ErrApp
	}
	if r.isRng {
		if r.stop < r.start || r.stop-r.start > 65535 {
			return r, ErrApp
		}
		r.count = r.stop - r.start + 1
	}
	if r.count > 65535 {
		return r, ErrApp
	}
	return r, nil
}

func getIdx(b []byte, w int) uint32 {
	switch w {
	case 1:
		return uint32(b[0])
	case 2:
		return uint32(binary.LittleEndian.Uint16(b))
	}
	return binary.LittleEndian.Uint32(b)
}

func putIdx(i uint32) []byte {
	var b [4]byte
	binary.LittleEndian.PutUint32(b[:], i)
	return b[:]
}

func iec104Float(u uint32) float32 { return math.Float32frombits(u) }
