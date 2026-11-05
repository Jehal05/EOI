package opcua

import (
	"encoding/binary"
	"io"
	"math/rand"
	"net"
	"testing"
	"time"
)

type fakeSrc struct {
	q uint8
	v float64
}

func (f *fakeSrc) Points() []Point {
	return []Point{{Tag: "G1.P", Group: "SS1", Unit: "MW", Quantity: "P"}, {Tag: "BRK1.STATE", Group: "SS1", Status: true}}
}
func (f *fakeSrc) Value(tag string) (float64, uint8, time.Time, bool) {
	if tag == "BRK1.STATE" {
		return 2, 0, time.Unix(1700000000, 0), true
	}
	if tag == "G1.P" {
		return f.v, f.q, time.Unix(1700000000, 0), true
	}
	return 0, 2, time.Time{}, false
}

type tc struct {
	t    *testing.T
	c    net.Conn
	seq  uint32
	ch   uint32
	tok  uint32
	auth nodeID
}

func start(t *testing.T, src Source) *tc {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{Src: src, EndpointURL: "opc.tcp://test"}
	go s.Serve(ln)
	t.Cleanup(func() { ln.Close() })
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return &tc{t: t, c: c}
}

func (x *tc) send(typ string, body []byte) {
	h := make([]byte, 8)
	copy(h, typ)
	h[3] = 'F'
	binary.LittleEndian.PutUint32(h[4:], uint32(8+len(body)))
	x.c.Write(append(h, body...))
}

func (x *tc) recv() (string, []byte) {
	x.c.SetReadDeadline(time.Now().Add(3 * time.Second))
	var h [8]byte
	if _, err := io.ReadFull(x.c, h[:]); err != nil {
		return "", nil
	}
	b := make([]byte, binary.LittleEndian.Uint32(h[4:])-8)
	io.ReadFull(x.c, b)
	return string(h[:3]), b
}

func (x *tc) hello() {
	w := &writer{}
	for _, v := range []uint32{0, 65536, 65536, 0, 0} {
		w.u32(v)
	}
	w.str("opc.tcp://test")
	x.send("HEL", w.b)
	if typ, _ := x.recv(); typ != "ACK" {
		x.t.Fatalf("expected ACK, got %q", typ)
	}
}

func reqHeader(w *writer, auth nodeID, handle uint32) {
	w.nodeID(auth)
	w.i64(0)
	w.u32(handle)
	w.u32(0)
	w.str("")
	w.u32(0)
	w.u8(0)
	w.u8(0)
	w.u8(0)
}

func (x *tc) opn(policy string, mode uint32) (string, []byte) {
	w := &writer{}
	w.u32(0)
	w.str(policy)
	w.bytes(nil)
	w.bytes(nil)
	x.seq++
	w.u32(x.seq)
	w.u32(1)
	w.nodeID(nodeID{num: 446})
	reqHeader(w, nodeID{}, 1)
	w.u32(0)
	w.u32(0)
	w.u32(mode)
	w.bytes(nil)
	w.u32(60000)
	x.send("OPN", w.b)
	typ, b := x.recv()
	if typ == "OPN" {
		r := &reader{b: b}
		x.ch = r.u32()
		r.str()
		r.bytes()
		r.bytes()
		r.u32()
		r.u32()
		r.nodeID()
		r.i64()
		r.u32()
		r.u32()
		r.u8()
		r.i32()
		r.u16()
		r.u8()
		r.u32() // version
		r.u32() // channel
		x.tok = r.u32()
	}
	return typ, b
}

// call sends a service request and returns the response type id and a reader positioned after the response header.
func (x *tc) call(id uint32, auth nodeID, body func(w *writer)) (uint32, uint32, *reader) {
	w := &writer{}
	w.u32(x.ch)
	w.u32(x.tok)
	x.seq++
	w.u32(x.seq)
	w.u32(x.seq)
	w.nodeID(nodeID{num: id})
	reqHeader(w, auth, 7)
	body(w)
	x.send("MSG", w.b)
	var all []byte
	for {
		x.c.SetReadDeadline(time.Now().Add(3 * time.Second))
		var h [8]byte
		if _, err := io.ReadFull(x.c, h[:]); err != nil {
			return 0, 0xdead, &reader{}
		}
		b := make([]byte, binary.LittleEndian.Uint32(h[4:])-8)
		io.ReadFull(x.c, b)
		all = append(all, b[16:]...)
		if h[3] == 'F' {
			break
		}
	}
	r := &reader{b: all}
	tid := r.nodeID()
	r.i64()
	r.u32()
	st := r.u32()
	r.u8()
	r.i32()
	r.u8()
	r.u8()
	r.u8()
	return tid.num, st, r
}

func (x *tc) session(anonymous bool) (nodeID, uint32) {
	_, st, r := x.call(461, nodeID{}, func(w *writer) {
		w.str("urn:c")
		w.str("")
		w.u8(0)
		w.u32(1)
		w.str("")
		w.str("")
		w.i32(0)
		w.str("")
		w.str("opc.tcp://test")
		w.str("s")
		w.bytes(make([]byte, 32))
		w.bytes(nil)
		w.f64(60000)
		w.u32(0)
	})
	if st != 0 {
		x.t.Fatalf("CreateSession status %#x", st)
	}
	r.nodeID()
	x.auth = r.nodeID()
	tokenType := uint32(321)
	if !anonymous {
		tokenType = 324 // UserNameIdentityToken
	}
	_, st, _ = x.call(467, x.auth, func(w *writer) {
		w.str("")
		w.bytes(nil)
		w.i32(0)
		w.i32(0)
		w.nodeID(nodeID{num: tokenType})
		w.u8(1)
		b := &writer{}
		b.str("anonymous")
		w.bytes(b.b)
		w.str("")
		w.bytes(nil)
	})
	return x.auth, st
}

func TestSecurityPolicyOtherThanNoneIsRefused(t *testing.T) {
	x := start(t, &fakeSrc{})
	x.hello()
	typ, b := x.opn("http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256", 3)
	if typ != "ERR" || binary.LittleEndian.Uint32(b) != scBadPolicy {
		t.Fatalf("expected ERR BadSecurityPolicyRejected, got %q % x", typ, b)
	}
}

func TestUserNameTokenRefusedAndReadRequiresActivation(t *testing.T) {
	x := start(t, &fakeSrc{})
	x.hello()
	if typ, _ := x.opn(policyNone, 1); typ != "OPN" {
		t.Fatal("OPN failed")
	}
	if _, st := x.session(false); st != scBadIdentity {
		t.Fatalf("user-name token must be refused, status %#x", st)
	}
	_, st, _ := x.call(631, x.auth, func(w *writer) { w.f64(0); w.u32(0); w.i32(0) })
	if st != scBadSession {
		t.Fatalf("Read before activation must be refused, status %#x", st)
	}
}

func readOne(x *tc, id nodeID, attr uint32) (*reader, uint32) {
	_, st, r := x.call(631, x.auth, func(w *writer) {
		w.f64(0)
		w.u32(0)
		w.i32(1)
		w.nodeID(id)
		w.u32(attr)
		w.str("")
		w.u16(0)
		w.str("")
	})
	return r, st
}

func TestReadValueQualityMapsToStatusCode(t *testing.T) {
	src := &fakeSrc{v: 123.5}
	x := start(t, src)
	x.hello()
	x.opn(policyNone, 1)
	if _, st := x.session(true); st != 0 {
		t.Fatalf("anonymous activation failed: %#x", st)
	}
	// populate the address space by browsing
	x.call(527, x.auth, func(w *writer) { w.nodeID(nodeID{}); w.i64(0); w.u32(0); w.u32(0); w.i32(0) })
	for _, c := range []struct {
		q    uint8
		want uint32
	}{{0, scGood}, {1, scUncertain}, {2, scBad}} {
		src.q = c.q
		r, st := readOne(x, tagID("G1.P"), 13)
		if st != 0 || r.i32() != 1 {
			t.Fatalf("read status %#x", st)
		}
		mask := r.u8()
		if mask&1 == 0 || r.u8() != 11 {
			t.Fatalf("expected a Double variant, mask %#x", mask)
		}
		v := r.f64()
		code := r.u32()
		if v != 123.5 || code != c.want {
			t.Fatalf("quality %d: value %v status %#x want %#x", c.q, v, code, c.want)
		}
	}
	// breaker state: Int32 2
	r, _ := readOne(x, tagID("BRK1.STATE"), 13)
	r.i32()
	r.u8()
	if r.u8() != 6 || r.i32() != 2 {
		t.Fatal("double-point state must be an Int32")
	}
	// unknown node
	r, _ = readOne(x, tagID("NOPE"), 13)
	r.i32()
	if r.u8() != 2 || r.u32() != scBadNodeID {
		t.Fatal("unknown node must give BadNodeIdUnknown")
	}
	// writes are an unsupported service
	if id, st, _ := x.call(673, x.auth, func(w *writer) { w.i32(0) }); id != 397 || st != scBadService {
		t.Fatalf("Write must be refused with a ServiceFault, got %d %#x", id, st)
	}
}

func TestBrowseReturnsTheHierarchy(t *testing.T) {
	x := start(t, &fakeSrc{})
	x.hello()
	x.opn(policyNone, 1)
	x.session(true)
	browse := func(id nodeID) []string {
		_, st, r := x.call(527, x.auth, func(w *writer) {
			w.nodeID(nodeID{})
			w.i64(0)
			w.u32(0)
			w.u32(0)
			w.i32(1)
			w.nodeID(id)
			w.u32(0)
			w.nodeID(nodeID{})
			w.u8(1)
			w.u32(0)
			w.u32(63)
		})
		if st != 0 {
			t.Fatalf("browse status %#x", st)
		}
		r.i32()
		r.u32()
		r.bytes()
		var names []string
		for i, n := 0, int(r.i32()); i < n; i++ {
			r.nodeID()
			r.u8()
			r.expandedNodeID()
			_, nm := r.qualifiedName()
			r.localizedText()
			r.u32()
			r.expandedNodeID()
			names = append(names, nm)
		}
		if r.err != nil {
			t.Fatal(r.err)
		}
		return names
	}
	if got := browse(idObjects); len(got) != 2 || got[1] != "GridSovereign" {
		t.Fatalf("Objects children %v", got)
	}
	if got := browse(idGS); len(got) != 1 || got[0] != "SS1" {
		t.Fatalf("GS children %v", got)
	}
	if got := browse(grpID("SS1")); len(got) != 2 || got[0] != "BRK1.STATE" {
		t.Fatalf("group children %v", got)
	}
}

func TestLargeBrowseIsChunked(t *testing.T) {
	big := &bigSrc{}
	x := start(t, big)
	x.hello()
	x.opn(policyNone, 1)
	x.session(true)
	_, st, r := x.call(527, x.auth, func(w *writer) {
		w.nodeID(nodeID{})
		w.i64(0)
		w.u32(0)
		w.u32(0)
		w.i32(1)
		w.nodeID(grpID("SS"))
		w.u32(0)
		w.nodeID(nodeID{})
		w.u8(1)
		w.u32(0)
		w.u32(63)
	})
	if st != 0 {
		t.Fatalf("status %#x", st)
	}
	r.i32()
	r.u32()
	r.bytes()
	if n := r.i32(); n != 3000 || r.err != nil {
		t.Fatalf("expected 3000 references reassembled from chunks, got %d (%v)", n, r.err)
	}
}

type bigSrc struct{}

func (bigSrc) Points() []Point {
	var ps []Point
	for i := 0; i < 3000; i++ {
		ps = append(ps, Point{Tag: "T" + string(rune('A'+i%26)) + "." + itoa(i), Group: "SS"})
	}
	return ps
}
func (bigSrc) Value(string) (float64, uint8, time.Time, bool) { return 0, 2, time.Time{}, false }
func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	s := ""
	for ; i > 0; i /= 10 {
		s = string(rune('0'+i%10)) + s
	}
	return s
}

// Random and mutated bodies must never crash the server or leak into other connections.
func TestMalformedMessagesNeverPanic(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	x := start(t, &fakeSrc{})
	x.hello()
	x.opn(policyNone, 1)
	x.session(true)
	valid := &writer{}
	valid.nodeID(nodeID{num: 631})
	reqHeader(valid, x.auth, 1)
	valid.f64(0)
	valid.u32(0)
	valid.i32(1)
	valid.nodeID(tagID("G1.P"))
	valid.u32(13)
	valid.str("")
	valid.u16(0)
	valid.str("")
	for i := 0; i < 150; i++ {
		b := append([]byte(nil), valid.b...)
		switch i % 3 {
		case 0:
			b = b[:rng.Intn(len(b))]
		case 1:
			for j := 0; j < 1+rng.Intn(4); j++ {
				b[rng.Intn(len(b))] = byte(rng.Intn(256))
			}
		default:
			b = make([]byte, rng.Intn(80))
			rng.Read(b)
		}
		y := start(t, &fakeSrc{})
		y.hello()
		y.opn(policyNone, 1)
		y.session(true)
		w := &writer{}
		w.u32(y.ch)
		w.u32(y.tok)
		w.u32(99)
		w.u32(99)
		w.b = append(w.b, b...)
		y.send("MSG", w.b)
		y.c.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		io.Copy(io.Discard, y.c)
	}
	z := start(t, &fakeSrc{})
	z.hello()
	if typ, _ := z.opn(policyNone, 1); typ != "OPN" {
		t.Fatal("server unusable after malformed input")
	}
}

func FuzzReaderNeverPanics(f *testing.F) {
	f.Add([]byte{3, 2, 0, 3, 0, 0, 0, 'a', 'b', 'c'})
	f.Fuzz(func(t *testing.T, b []byte) {
		r := &reader{b: b}
		r.nodeID()
		r.expandedNodeID()
		r.localizedText()
		r.extensionObject()
		r.requestHeader()
		for i, n := 0, r.count(4); i < n; i++ {
			r.str()
		}
	})
}

// BenchmarkRead1000: one Read service call for 1,000 variables over loopback (server work plus the test client's decoding).
func BenchmarkRead1000(b *testing.B) {
	t := &testing.T{}
	x := start(t, bigValSrc{})
	x.hello()
	x.opn(policyNone, 1)
	x.session(true)
	x.call(527, x.auth, func(w *writer) { w.nodeID(nodeID{}); w.i64(0); w.u32(0); w.u32(0); w.i32(0) })
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_, st, r := x.call(631, x.auth, func(w *writer) {
			w.f64(0)
			w.u32(0)
			w.i32(1000)
			for j := 0; j < 1000; j++ {
				w.nodeID(tagID("T" + itoa(j)))
				w.u32(13)
				w.str("")
				w.u16(0)
				w.str("")
			}
		})
		if st != 0 || r.i32() != 1000 {
			b.Fatalf("status %#x", st)
		}
	}
}

type bigValSrc struct{}

func (bigValSrc) Points() []Point {
	var ps []Point
	for i := 0; i < 14868; i++ {
		ps = append(ps, Point{Tag: "T" + itoa(i), Group: "SS" + itoa(i/14)})
	}
	return ps
}
func (bigValSrc) Value(string) (float64, uint8, time.Time, bool) {
	return 1.5, 0, time.Unix(1700000000, 0), true
}
