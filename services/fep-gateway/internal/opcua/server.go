package opcua

import (
	"bufio"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Point describes one variable of the address space.
type Point struct {
	Tag, Group, Unit, Quantity string
	Status                     bool // double-point state (Int32), otherwise analog (Double)
}

// Source supplies the address space and live values. Quality: 0 good, 1 uncertain (stale / not topical), 2 bad.
type Source interface {
	Points() []Point
	Value(tag string) (v float64, quality uint8, ts time.Time, ok bool)
}

const (
	scGood          = 0x00000000
	scUncertain     = 0x40900000 // UncertainLastUsableValue
	scBad           = 0x80000000
	scBadNodeID     = 0x80340000
	scBadAttr       = 0x80350000
	scBadService    = 0x800B0000
	scBadIdentity   = 0x80210000
	scBadPolicy     = 0x80550000
	scBadSession    = 0x80250000
	scBadDecoding   = 0x80070000
	scBadTooMany    = 0x80100000
	scBadChannelID  = 0x80220000
	policyNone      = "http://opcfoundation.org/UA/SecurityPolicy#None"
	anonymousPolicy = "anonymous"
	maxSessions     = 64
	maxChunk        = 1 << 20
)

type Server struct {
	Src         Source
	Log         *slog.Logger
	EndpointURL string // advertised, e.g. opc.tcp://127.0.0.1:4840
	AppURI      string
	Stats       struct{ Accepted, Reads, Browses, Rejected atomic.Int64 }

	once sync.Once
	// address space built from Src on first use and refreshed when the point count changes
	mu     sync.RWMutex
	built  int
	groups []string
	byGrp  map[string][]Point
	byTag  map[string]Point
	chanID atomic.Uint32
	sessID atomic.Uint32
	nsess  atomic.Int32
}

func (s *Server) log() *slog.Logger {
	if s.Log != nil {
		return s.Log
	}
	return slog.Default()
}

func (s *Server) space() {
	pts := s.Src.Points()
	s.mu.RLock()
	ok := s.built == len(pts) && s.byTag != nil
	s.mu.RUnlock()
	if ok {
		return
	}
	g := map[string][]Point{}
	t := map[string]Point{}
	for _, p := range pts {
		g[p.Group] = append(g[p.Group], p)
		t[p.Tag] = p
	}
	names := make([]string, 0, len(g))
	for k := range g {
		names = append(names, k)
		sort.Slice(g[k], func(i, j int) bool { return g[k][i].Tag < g[k][j].Tag })
	}
	sort.Strings(names)
	s.mu.Lock()
	s.built, s.groups, s.byGrp, s.byTag = len(pts), names, g, t
	s.mu.Unlock()
}

func (s *Server) Serve(ln net.Listener) error {
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		s.Stats.Accepted.Add(1)
		go s.handle(c)
	}
}

type conn struct {
	s       *Server
	c       net.Conn
	rd      *bufio.Reader
	wmu     sync.Mutex
	chanID  uint32
	tokenID uint32
	seq     uint32
	sendMax int
	session nodeID // authentication token of the activated session, zero if none
	hasSess bool
	active  bool
	sessID  uint32
}

func (s *Server) handle(c net.Conn) {
	defer c.Close()
	cn := &conn{s: s, c: c, rd: bufio.NewReader(c), sendMax: 8192}
	defer func() {
		if cn.hasSess {
			s.nsess.Add(-1)
		}
		if r := recover(); r != nil {
			s.log().Error("opcua connection panic", "err", r)
		}
	}()
	c.SetReadDeadline(time.Now().Add(10 * time.Second))
	typ, body, err := cn.readMsg()
	if err != nil || typ != "HEL" {
		return
	}
	r := &reader{b: body}
	r.u32()
	recvBuf := r.u32()
	r.u32()
	r.u32()
	r.u32()
	r.str()
	if r.err != nil {
		cn.errMsg(scBadDecoding, "bad Hello")
		return
	}
	cn.sendMax = int(minU(maxU(recvBuf, 8192), 1<<16))
	w := &writer{}
	w.u32(0)
	w.u32(65536)
	w.u32(uint32(cn.sendMax))
	w.u32(0)
	w.u32(0)
	cn.writeRaw("ACK", 'F', w.b)
	for {
		c.SetReadDeadline(time.Now().Add(5 * time.Minute))
		typ, body, err := cn.readMsg()
		if err != nil {
			return
		}
		switch typ {
		case "OPN":
			if !cn.opn(body) {
				return
			}
		case "MSG":
			if !cn.msg(body) {
				return
			}
		case "CLO":
			return
		default:
			return
		}
	}
}

func minU(a, b uint32) uint32 {
	if a < b {
		return a
	}
	return b
}
func maxU(a, b uint32) uint32 {
	if a > b {
		return a
	}
	return b
}

// readMsg returns one complete (final) message; intermediate chunks are not supported and end the connection.
func (c *conn) readMsg() (string, []byte, error) {
	var h [8]byte
	if _, err := io.ReadFull(c.rd, h[:]); err != nil {
		return "", nil, err
	}
	n := binary.LittleEndian.Uint32(h[4:])
	if n < 8 || n > maxChunk {
		return "", nil, errors.New("bad message size")
	}
	typ := string(h[:3])
	if h[3] != 'F' && typ != "ERR" {
		c.errMsg(scBadTooMany, "chunked requests are not supported")
		return "", nil, errors.New("chunked request")
	}
	b := make([]byte, n-8)
	if _, err := io.ReadFull(c.rd, b); err != nil {
		return "", nil, err
	}
	return typ, b, nil
}

func (c *conn) writeRaw(typ string, chunk byte, body []byte) {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	h := make([]byte, 8, 8+len(body))
	copy(h, typ)
	h[3] = chunk
	binary.LittleEndian.PutUint32(h[4:], uint32(8+len(body)))
	c.c.SetWriteDeadline(time.Now().Add(10 * time.Second))
	c.c.Write(append(h, body...))
}

func (c *conn) errMsg(code uint32, reason string) {
	w := &writer{}
	w.u32(code)
	w.str(reason)
	c.writeRaw("ERR", 'F', w.b)
}

// opn handles OpenSecureChannel (SecurityPolicy None only).
func (c *conn) opn(body []byte) bool {
	r := &reader{b: body}
	r.u32() // channel id (0 for a new channel)
	policy := r.str()
	r.bytes()
	r.bytes()
	r.u32()
	reqID := r.u32()
	tid := r.nodeID()
	h := r.requestHeader()
	r.u32()
	reqType := r.u32()
	mode := r.u32()
	r.bytes()
	life := r.u32()
	if r.err != nil || tid.num != 446 {
		c.errMsg(scBadDecoding, "bad OpenSecureChannel")
		return false
	}
	if policy != policyNone || mode != 1 {
		c.errMsg(scBadPolicy, "only SecurityPolicy None / MessageSecurityMode None is implemented")
		c.s.Stats.Rejected.Add(1)
		return false
	}
	if reqType == 0 {
		c.chanID = c.s.chanID.Add(1)
	} else if c.chanID == 0 {
		c.errMsg(scBadChannelID, "renew without a channel")
		return false
	}
	c.tokenID++
	if life == 0 || life > 3600000 {
		life = 3600000
	}
	w := &writer{}
	w.u32(c.chanID)
	w.str(policyNone)
	w.bytes(nil)
	w.bytes(nil)
	c.seq++
	w.u32(c.seq)
	w.u32(reqID)
	w.nodeID(nodeID{num: 449})
	w.responseHeader(h.handle, scGood)
	w.u32(0)
	w.u32(c.chanID)
	w.u32(c.tokenID)
	w.i64(dateTime(time.Now()))
	w.u32(life)
	w.bytes(nil)
	c.writeRaw("OPN", 'F', w.b)
	return true
}

func (c *conn) msg(body []byte) bool {
	r := &reader{b: body}
	ch := r.u32()
	tok := r.u32()
	r.u32()
	reqID := r.u32()
	if r.err != nil || ch != c.chanID || c.chanID == 0 || tok != c.tokenID {
		c.errMsg(scBadChannelID, "unknown secure channel or token")
		return false
	}
	tid := r.nodeID()
	if r.err != nil {
		return false
	}
	if tid.num == 452 { // CloseSecureChannelRequest
		return false
	}
	h := r.requestHeader()
	if r.err != nil {
		c.fault(reqID, 0, scBadDecoding)
		return true
	}
	out := &writer{}
	var status uint32
	switch tid.num {
	case 428:
		status = c.getEndpoints(r, out, h)
	case 422:
		status = c.findServers(r, out, h)
	case 461:
		status = c.createSession(r, out, h)
	case 467:
		status = c.activateSession(r, out, h)
	case 473:
		status = c.closeSession(r, out, h)
	case 631:
		status = c.read(r, out, h)
	case 527:
		status = c.browse(r, out, h)
	default:
		status = scBadService
	}
	if status != scGood || r.err != nil {
		if r.err != nil && status == scGood {
			status = scBadDecoding
		}
		c.fault(reqID, h.handle, status)
		return true
	}
	c.send(reqID, out.b)
	return true
}

func (c *conn) fault(reqID, handle, status uint32) {
	w := &writer{}
	w.nodeID(nodeID{num: 397})
	w.responseHeader(handle, status)
	c.send(reqID, w.b)
}

// send writes a service response, split into chunks that fit the client's receive buffer.
func (c *conn) send(reqID uint32, payload []byte) {
	room := c.sendMax - 8 - 16
	for len(payload) > 0 {
		n := len(payload)
		kind := byte('F')
		if n > room {
			n, kind = room, 'C'
		}
		c.seq++
		w := &writer{}
		w.u32(c.chanID)
		w.u32(c.tokenID)
		w.u32(c.seq)
		w.u32(reqID)
		w.b = append(w.b, payload[:n]...)
		c.writeRaw("MSG", kind, w.b)
		payload = payload[n:]
	}
}

func (c *conn) endpointDesc(w *writer) {
	w.str(c.s.EndpointURL)
	c.appDesc(w)
	w.bytes(nil)
	w.u32(1) // MessageSecurityMode None
	w.str(policyNone)
	w.i32(1)
	w.str(anonymousPolicy)
	w.u32(0) // Anonymous
	w.str("")
	w.str("")
	w.str("")
	w.str("http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary")
	w.u8(0)
}

func (c *conn) appDesc(w *writer) {
	uri := c.s.AppURI
	if uri == "" {
		uri = "urn:gridsovereign:fep"
	}
	w.str(uri)
	w.str("urn:gridsovereign")
	w.u8(2)
	w.str("GridSovereign FEP")
	w.u32(0) // Server
	w.str("")
	w.str("")
	w.i32(1)
	w.str(c.s.EndpointURL)
}

func (c *conn) getEndpoints(r *reader, w *writer, h requestHeader) uint32 {
	r.str()
	for i, n := 0, r.count(4); i < n; i++ {
		r.str()
	}
	for i, n := 0, r.count(4); i < n; i++ {
		r.str()
	}
	w.nodeID(nodeID{num: 431})
	w.responseHeader(h.handle, scGood)
	w.i32(1)
	c.endpointDesc(w)
	return scGood
}

func (c *conn) findServers(r *reader, w *writer, h requestHeader) uint32 {
	w.nodeID(nodeID{num: 425})
	w.responseHeader(h.handle, scGood)
	w.i32(1)
	c.appDesc(w)
	return scGood
}

func (c *conn) createSession(r *reader, w *writer, h requestHeader) uint32 {
	r.str()
	r.str()
	r.localizedText()
	r.u32()
	r.str()
	r.str()
	for i, n := 0, r.count(4); i < n; i++ {
		r.str()
	}
	r.str()
	r.str()
	r.str()
	r.bytes()
	r.bytes()
	r.f64()
	r.u32()
	if r.err != nil {
		return scBadDecoding
	}
	if c.hasSess {
		return scBadTooMany
	}
	if c.s.nsess.Add(1) > maxSessions {
		c.s.nsess.Add(-1)
		return scBadTooMany
	}
	c.hasSess = true
	c.sessID = c.s.sessID.Add(1)
	c.session = nodeID{ns: 1, num: 0x10000 + c.sessID}
	w.nodeID(nodeID{num: 464})
	w.responseHeader(h.handle, scGood)
	w.nodeID(nodeID{ns: 1, num: 0x20000 + c.sessID})
	w.nodeID(c.session)
	w.f64(60000)
	nonce := make([]byte, 32)
	for i := range nonce {
		nonce[i] = byte(time.Now().UnixNano() >> (i % 8))
	}
	w.bytes(nonce)
	w.bytes(nil)
	w.i32(1)
	c.endpointDesc(w)
	w.i32(0)
	w.str("")
	w.bytes(nil)
	w.u32(0)
	return scGood
}

func (c *conn) activateSession(r *reader, w *writer, h requestHeader) uint32 {
	if !c.hasSess || h.token != c.session {
		return scBadSession
	}
	r.str()
	r.bytes()
	for i, n := 0, r.count(8); i < n; i++ {
		r.bytes()
		r.bytes()
	}
	for i, n := 0, r.count(4); i < n; i++ {
		r.str()
	}
	tid, tbody := r.extensionObject()
	r.str()
	r.bytes()
	if r.err != nil {
		return scBadDecoding
	}
	if tid.num != 321 && tid.num != 0 { // AnonymousIdentityToken (binary encoding id 321); a null token also means anonymous
		c.s.Stats.Rejected.Add(1)
		return scBadIdentity
	}
	_ = tbody
	c.active = true
	w.nodeID(nodeID{num: 470})
	w.responseHeader(h.handle, scGood)
	w.bytes(make([]byte, 32))
	w.i32(0)
	w.i32(0)
	return scGood
}

func (c *conn) closeSession(r *reader, w *writer, h requestHeader) uint32 {
	if !c.hasSess || h.token != c.session {
		return scBadSession
	}
	c.hasSess, c.active = false, false
	c.s.nsess.Add(-1)
	w.nodeID(nodeID{num: 476})
	w.responseHeader(h.handle, scGood)
	return scGood
}

// ---- address space

const (
	nsGS        = 2
	clsObject   = 1
	clsVariable = 2
)

var (
	idRoot, idObjects, idServer, idNsArray = nodeID{num: 84}, nodeID{num: 85}, nodeID{num: 2253}, nodeID{num: 2255}
	idGS                                   = nodeID{ns: nsGS, str: "GS", isS: true}
)

func grpID(g string) nodeID { return nodeID{ns: nsGS, str: "GS/" + g, isS: true} }
func tagID(t string) nodeID { return nodeID{ns: nsGS, str: t, isS: true} }

func statusFor(q uint8) uint32 {
	switch q {
	case 0:
		return scGood
	case 1:
		return scUncertain
	}
	return scBad
}

// readAttr writes the DataValue of one attribute.
func (c *conn) readAttr(w *writer, id nodeID, attr uint32) {
	dvStatus := func(code uint32) { w.u8(2); w.u32(code) }
	name, class, isVar := "", uint32(0), false
	var p Point
	c.s.mu.RLock()
	switch {
	case id == idRoot:
		name, class = "Root", 1
	case id == idObjects:
		name, class = "Objects", 1
	case id == idServer:
		name, class = "Server", 1
	case id == idNsArray:
		name, class, isVar = "NamespaceArray", clsVariable, true
	case id == idGS:
		name, class = "GridSovereign", clsObject
	case id.isS && id.ns == nsGS && strings.HasPrefix(id.str, "GS/"):
		if _, ok := c.s.byGrp[strings.TrimPrefix(id.str, "GS/")]; ok {
			name, class = strings.TrimPrefix(id.str, "GS/"), clsObject
		}
	case id.isS && id.ns == nsGS:
		if q, ok := c.s.byTag[id.str]; ok {
			p, name, class, isVar = q, q.Tag, clsVariable, true
		}
	}
	c.s.mu.RUnlock()
	if class == 0 {
		dvStatus(scBadNodeID)
		return
	}
	switch attr {
	case 1:
		w.u8(1)
		w.u8(17)
		w.nodeID(id)
	case 2:
		w.u8(1)
		w.u8(6)
		w.i32(int32(class))
	case 3:
		w.u8(1)
		w.u8(20)
		w.qualifiedName(map[bool]uint16{true: nsGS, false: 0}[id.ns == nsGS], name)
	case 4:
		w.u8(1)
		w.u8(21)
		w.localizedText(name)
	case 5:
		d := ""
		if p.Tag != "" {
			d = p.Quantity + " [" + p.Unit + "]"
		}
		w.u8(1)
		w.u8(21)
		w.localizedText(d)
	case 13:
		if !isVar {
			dvStatus(scBadAttr)
			return
		}
		if id == idNsArray {
			w.u8(1)
			w.u8(0x8c)
			w.i32(3)
			w.str("http://opcfoundation.org/UA/")
			w.str("urn:gridsovereign:fep")
			w.str("urn:gridsovereign:model")
			return
		}
		v, q, ts, ok := c.s.Src.Value(p.Tag)
		c.s.Stats.Reads.Add(1)
		if !ok { // never received: bad, no value
			dvStatus(scBad)
			return
		}
		mask := byte(1 | 2)
		if !ts.IsZero() {
			mask |= 4
		}
		w.u8(mask)
		if p.Status {
			w.u8(6)
			w.i32(int32(v))
		} else {
			w.u8(11)
			w.f64(v)
		}
		w.u32(statusFor(q))
		if !ts.IsZero() {
			w.i64(dateTime(ts))
		}
	case 14:
		if !isVar {
			dvStatus(scBadAttr)
			return
		}
		dt := uint32(11) // Double
		if p.Status {
			dt = 6 // Int32
		}
		if id == idNsArray {
			dt = 12
		}
		w.u8(1)
		w.u8(17)
		w.nodeID(nodeID{num: dt})
	case 15:
		if !isVar {
			dvStatus(scBadAttr)
			return
		}
		w.u8(1)
		w.u8(6)
		if id == idNsArray {
			w.i32(1)
		} else {
			w.i32(-1)
		}
	case 17, 18:
		if !isVar {
			dvStatus(scBadAttr)
			return
		}
		w.u8(1)
		w.u8(3)
		w.u8(1) // CurrentRead only
	default:
		dvStatus(scBadAttr)
	}
}

func (c *conn) read(r *reader, w *writer, h requestHeader) uint32 {
	if !c.active || h.token != c.session {
		return scBadSession
	}
	c.s.space()
	r.f64()
	r.u32()
	n := r.count(10)
	type rv struct {
		id   nodeID
		attr uint32
	}
	rs := make([]rv, 0, n)
	for i := 0; i < n && r.err == nil; i++ {
		id := r.nodeID()
		a := r.u32()
		r.str()
		r.qualifiedName()
		rs = append(rs, rv{id, a})
	}
	if r.err != nil {
		return scBadDecoding
	}
	if len(rs) > 10000 {
		return scBadTooMany
	}
	w.nodeID(nodeID{num: 634})
	w.responseHeader(h.handle, scGood)
	w.i32(int32(len(rs)))
	for _, x := range rs {
		c.readAttr(w, x.id, x.attr)
	}
	w.i32(0)
	return scGood
}

type ref struct {
	id    nodeID
	name  string
	class uint32
	ns    uint16
}

func (c *conn) children(id nodeID) (refs []ref, ok bool) {
	c.s.mu.RLock()
	defer c.s.mu.RUnlock()
	switch {
	case id == idRoot:
		return []ref{{idObjects, "Objects", 1, 0}}, true
	case id == idObjects:
		return []ref{{idServer, "Server", 1, 0}, {idGS, "GridSovereign", 1, nsGS}}, true
	case id == idServer:
		return []ref{{idNsArray, "NamespaceArray", 2, 0}}, true
	case id == idGS:
		for _, g := range c.s.groups {
			refs = append(refs, ref{grpID(g), g, 1, nsGS})
		}
		return refs, true
	case id.isS && id.ns == nsGS && strings.HasPrefix(id.str, "GS/"):
		ps, found := c.s.byGrp[strings.TrimPrefix(id.str, "GS/")]
		for _, p := range ps {
			refs = append(refs, ref{tagID(p.Tag), p.Tag, 2, nsGS})
		}
		return refs, found
	case id == idNsArray || (id.isS && id.ns == nsGS):
		return nil, true // variables have no children (an unknown string id is handled by the caller via readAttr)
	}
	return nil, false
}

func (c *conn) browse(r *reader, w *writer, h requestHeader) uint32 {
	if !c.active || h.token != c.session {
		return scBadSession
	}
	c.s.space()
	r.nodeID()
	r.i64()
	r.u32()
	r.u32() // requested max references per node: ignored, no continuation points; we return everything
	n := r.count(12)
	type bd struct {
		id  nodeID
		dir uint32
	}
	bs := make([]bd, 0, n)
	for i := 0; i < n && r.err == nil; i++ {
		id := r.nodeID()
		dir := r.u32()
		r.nodeID()
		r.u8()
		r.u32()
		r.u32()
		bs = append(bs, bd{id, dir})
	}
	if r.err != nil {
		return scBadDecoding
	}
	if len(bs) > 1000 {
		return scBadTooMany
	}
	c.s.Stats.Browses.Add(1)
	w.nodeID(nodeID{num: 530})
	w.responseHeader(h.handle, scGood)
	w.i32(int32(len(bs)))
	for _, b := range bs {
		refs, ok := c.children(b.id)
		if !ok {
			w.u32(scBadNodeID)
			w.bytes(nil)
			w.i32(0)
			continue
		}
		if b.dir == 1 { // inverse only: nothing is offered
			refs = nil
		}
		w.u32(scGood)
		w.bytes(nil)
		w.i32(int32(len(refs)))
		for _, x := range refs {
			w.nodeID(nodeID{num: 35}) // Organizes
			w.u8(1)
			w.expandedNodeID(x.id)
			w.qualifiedName(x.ns, x.name)
			w.localizedText(x.name)
			w.u32(x.class)
			if x.class == 2 {
				w.expandedNodeID(nodeID{num: 63}) // BaseDataVariableType
			} else {
				w.expandedNodeID(nodeID{num: 61}) // FolderType
			}
		}
	}
	w.i32(0)
	return scGood
}

// ListenAndServe runs until ctx ends.
func (s *Server) ListenAndServe(ctx context.Context, addr string) error {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	go func() { <-ctx.Done(); ln.Close() }()
	if s.EndpointURL == "" {
		s.EndpointURL = fmt.Sprintf("opc.tcp://%s", ln.Addr().String())
	}
	return s.Serve(ln)
}
