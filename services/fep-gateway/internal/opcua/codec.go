// Package opcua is a REDUCED OPC UA server: UA-TCP + SecureChannel with SecurityPolicy None, anonymous sessions, and the services
// GetEndpoints, FindServers, CreateSession, ActivateSession, CloseSession, Read, Browse (no continuation points) and
// CloseSecureChannel over a read-only address space built from the model's point list. No signing or encryption, no subscriptions,
// no method calls, no writes. See docs/STATUS.md for what that means for use.
package opcua

import (
	"encoding/binary"
	"errors"
	"math"
	"time"
)

var errShort = errors.New("opcua: truncated or malformed message")

// reader decodes the OPC UA binary encoding; the first error sticks and every later read returns zero values.
type reader struct {
	b   []byte
	err error
}

func (r *reader) take(n int) []byte {
	if r.err != nil || n < 0 || n > len(r.b) {
		r.err = errShort
		return make([]byte, maxInt(n, 0)&0xff)
	}
	x := r.b[:n]
	r.b = r.b[n:]
	return x
}
func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
func (r *reader) u8() uint8 { return r.take(1)[0] }
func (r *reader) u16() uint16 {
	b := r.take(2)
	return binary.LittleEndian.Uint16(b)
}
func (r *reader) u32() uint32 {
	b := r.take(4)
	return binary.LittleEndian.Uint32(b)
}
func (r *reader) i32() int32   { return int32(r.u32()) }
func (r *reader) i64() int64   { b := r.take(8); return int64(binary.LittleEndian.Uint64(b)) }
func (r *reader) f64() float64 { return math.Float64frombits(uint64(r.i64())) }
func (r *reader) bytes() []byte {
	n := r.i32()
	if n <= 0 {
		return nil
	}
	if int(n) > len(r.b) {
		r.err = errShort
		return nil
	}
	return append([]byte(nil), r.take(int(n))...)
}
func (r *reader) str() string { return string(r.bytes()) }

// count reads an array length and refuses lengths the remaining bytes cannot hold (each element needs at least min bytes).
func (r *reader) count(min int) int {
	n := r.i32()
	if n <= 0 {
		return 0
	}
	if r.err != nil || int(n) > len(r.b)/maxInt(min, 1) {
		r.err = errShort
		return 0
	}
	return int(n)
}

type nodeID struct {
	ns  uint16
	num uint32
	str string
	isS bool
}

func (r *reader) nodeID() nodeID {
	enc := r.u8()
	switch enc & 0x3f {
	case 0:
		return nodeID{num: uint32(r.u8())}
	case 1:
		ns := uint16(r.u8())
		return nodeID{ns: ns, num: uint32(r.u16())}
	case 2:
		ns := r.u16()
		return nodeID{ns: ns, num: r.u32()}
	case 3:
		ns := r.u16()
		return nodeID{ns: ns, str: r.str(), isS: true}
	case 4: // GUID: carried opaquely, never matches a node
		ns := r.u16()
		r.take(16)
		return nodeID{ns: ns, num: 0xffffffff}
	case 5:
		ns := r.u16()
		r.bytes()
		return nodeID{ns: ns, num: 0xffffffff}
	}
	r.err = errShort
	return nodeID{}
}

func (r *reader) expandedNodeID() nodeID {
	save := r.b
	if len(save) == 0 {
		r.err = errShort
		return nodeID{}
	}
	flags := save[0] & 0xc0
	r.b = append([]byte{save[0] &^ 0xc0}, save[1:]...)
	n := r.nodeID()
	if flags&0x80 != 0 {
		r.str()
	}
	if flags&0x40 != 0 {
		r.u32()
	}
	return n
}

func (r *reader) qualifiedName() (uint16, string) { return r.u16(), r.str() }
func (r *reader) localizedText() string {
	m := r.u8()
	if m&1 != 0 {
		r.str()
	}
	if m&2 != 0 {
		return r.str()
	}
	return ""
}
func (r *reader) extensionObject() (typeID nodeID, body []byte) {
	typeID = r.nodeID()
	enc := r.u8()
	if enc == 1 || enc == 2 {
		body = r.bytes()
	}
	return
}

type requestHeader struct {
	token  nodeID
	handle uint32
}

func (r *reader) requestHeader() requestHeader {
	var h requestHeader
	h.token = r.nodeID()
	r.i64()
	h.handle = r.u32()
	r.u32()
	r.str()
	r.u32()
	r.extensionObject()
	return h
}

// writer encodes the binary format.
type writer struct{ b []byte }

func (w *writer) u8(v uint8)    { w.b = append(w.b, v) }
func (w *writer) u16(v uint16)  { w.b = binary.LittleEndian.AppendUint16(w.b, v) }
func (w *writer) u32(v uint32)  { w.b = binary.LittleEndian.AppendUint32(w.b, v) }
func (w *writer) i32(v int32)   { w.u32(uint32(v)) }
func (w *writer) i64(v int64)   { w.b = binary.LittleEndian.AppendUint64(w.b, uint64(v)) }
func (w *writer) f64(v float64) { w.i64(int64(math.Float64bits(v))) }
func (w *writer) bytes(v []byte) {
	if v == nil {
		w.i32(-1)
		return
	}
	w.i32(int32(len(v)))
	w.b = append(w.b, v...)
}
func (w *writer) str(s string) {
	if s == "" {
		w.i32(-1)
		return
	}
	w.bytes([]byte(s))
}
func (w *writer) nodeID(n nodeID) {
	switch {
	case n.isS:
		w.u8(3)
		w.u16(n.ns)
		w.str(n.str)
	case n.ns == 0 && n.num < 256:
		w.u8(0)
		w.u8(uint8(n.num))
	case n.ns < 256 && n.num < 65536:
		w.u8(1)
		w.u8(uint8(n.ns))
		w.u16(uint16(n.num))
	default:
		w.u8(2)
		w.u16(n.ns)
		w.u32(n.num)
	}
}
func (w *writer) expandedNodeID(n nodeID)           { w.nodeID(n) }
func (w *writer) qualifiedName(ns uint16, s string) { w.u16(ns); w.str(s) }
func (w *writer) localizedText(s string) {
	if s == "" {
		w.u8(0)
		return
	}
	w.u8(2)
	w.str(s)
}

// ua DateTime: 100 ns ticks since 1601-01-01 UTC.
const ticksToUnix = 116444736000000000

func dateTime(t time.Time) int64 {
	if t.IsZero() {
		return 0
	}
	return t.UnixNano()/100 + ticksToUnix
}

func (w *writer) responseHeader(handle, status uint32) {
	w.i64(dateTime(time.Now()))
	w.u32(handle)
	w.u32(status)
	w.u8(0)   // no diagnostics
	w.i32(-1) // string table
	w.u8(0)   // additional header: null NodeId, two-byte form (encoding byte, id byte)
	w.u8(0)
	w.u8(0) // no body
}
