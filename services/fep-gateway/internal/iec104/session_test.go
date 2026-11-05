package iec104

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"io"
	"math/big"
	"net"
	"sort"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type memH struct {
	mu      sync.Mutex
	pts     map[[2]uint32]*Point
	cmds    [][3]uint32
	failCmd bool
}

func newMemH() *memH {
	h := &memH{pts: map[[2]uint32]*Point{}}
	add := func(ca uint16, ioa uint32, status bool, f float32, dpi uint8) {
		h.pts[[2]uint32{uint32(ca), ioa}] = &Point{CA: ca, IOA: ioa, Status: status, Float: f, DPI: dpi, TS: time.Now()}
	}
	add(1, 1, true, 0, DPIOn)
	add(1, 2, true, 0, DPIOff)
	add(1, 1001, false, 123.5, 0)
	add(1, 1002, false, -7.25, 0)
	add(2, 1, true, 0, DPIOn)
	add(2, 1001, false, 400.0, 0)
	return h
}
func (h *memH) Snapshot(ca uint16) []Point {
	h.mu.Lock()
	defer h.mu.Unlock()
	var out []Point
	for _, p := range h.pts {
		if ca == 0xFFFF || p.CA == ca {
			out = append(out, *p)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].CA < out[j].CA || (out[i].CA == out[j].CA && out[i].IOA < out[j].IOA)
	})
	return out
}
func (h *memH) IsStatus(ca uint16, ioa uint32) (bool, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	p, ok := h.pts[[2]uint32{uint32(ca), ioa}]
	if !ok {
		return false, false
	}
	return p.Status, true
}
func (h *memH) Command(ca uint16, ioa uint32, dcs uint8) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.failCmd {
		return errors.New("interlock")
	}
	h.cmds = append(h.cmds, [3]uint32{uint32(ca), ioa, uint32(dcs)})
	return nil
}

type rig struct {
	srv *Server
	h   *memH
	cl  *Client
	got chan ASDU
	up  chan bool
}

func start(t *testing.T, serverTLS, clientTLS *tls.Config) *rig {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	if serverTLS != nil {
		ln = tls.NewListener(ln, serverTLS)
	}
	h := newMemH()
	srv := &Server{H: h}
	go srv.Serve(ln)
	r := &rig{srv: srv, h: h, got: make(chan ASDU, 100000), up: make(chan bool, 8)}
	r.cl = &Client{Addr: ln.Addr().String(), TLS: clientTLS,
		OnASDU: func(a ASDU) { r.got <- a }, OnState: func(u bool) {
			select {
			case r.up <- u:
			default:
			}
		}}
	ctx, cancel := context.WithCancel(context.Background())
	go r.cl.Run(ctx)
	t.Cleanup(func() { cancel(); srv.Close() })
	return r
}

func (r *rig) waitUp(t *testing.T) {
	t.Helper()
	select {
	case u := <-r.up:
		if !u {
			t.Fatal("link went down")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("STARTDT not confirmed")
	}
}

func TestGeneralInterrogationAndSpontaneous(t *testing.T) {
	r := start(t, nil, nil)
	r.waitUp(t)
	if err := r.cl.Interrogate(1); err != nil {
		t.Fatal(err)
	}
	var analog, status int
	deadline := time.After(3 * time.Second)
	for analog < 2 || status < 2 {
		select {
		case a := <-r.got:
			if a.Cause != CotInrogen || a.CA != 1 {
				t.Fatalf("unexpected %+v", a)
			}
			switch a.Type {
			case M_ME_NC_1:
				analog += len(a.Objects)
				if a.Objects[0].IOA != 1001 || a.Objects[0].Float != 123.5 || a.Objects[1].Float != -7.25 {
					t.Fatalf("analog %+v", a.Objects)
				}
			case M_DP_NA_1:
				status += len(a.Objects)
				if a.Objects[0].DPI != DPIOn || a.Objects[1].DPI != DPIOff {
					t.Fatalf("status %+v", a.Objects)
				}
			}
		case <-deadline:
			t.Fatalf("GI incomplete: analog=%d status=%d", analog, status)
		}
	}
	if err := r.cl.Interrogate(99); err != nil { // unknown CA: nothing delivered to OnASDU (negative handled at link)
		t.Fatal(err)
	}
	now := time.Now().UTC().Truncate(time.Millisecond)
	r.srv.Publish([]Point{{CA: 1, IOA: 1001, Float: 130.25, TS: now}, {CA: 1, IOA: 1, Status: true, DPI: DPIOff, TS: now}})
	seen := map[uint8]bool{}
	for len(seen) < 2 {
		select {
		case a := <-r.got:
			if a.Cause == CotSpont {
				seen[a.Type] = true
				if a.Objects[0].Time == nil || !a.Objects[0].Time.Equal(now) {
					t.Fatalf("timestamp lost: %+v", a.Objects[0])
				}
			}
		case <-time.After(3 * time.Second):
			t.Fatalf("spontaneous updates missing: %v", seen)
		}
	}
	if !seen[M_ME_TF_1] || !seen[M_DP_TB_1] {
		t.Fatalf("expected timed float + timed double point, got %v", seen)
	}
}

func TestSelectBeforeOperate(t *testing.T) {
	r := start(t, nil, nil)
	r.waitUp(t)
	ctx := context.Background()
	if err := r.cl.SelectExecute(ctx, 1, 1, 1); err != nil {
		t.Fatalf("valid SBO failed: %v", err)
	}
	r.h.mu.Lock()
	if len(r.h.cmds) != 1 || r.h.cmds[0] != [3]uint32{1, 1, 1} {
		t.Fatalf("backend got %v", r.h.cmds)
	}
	r.h.mu.Unlock()
	// execute without a prior select must be refused and must NOT reach the backend
	if err := r.cl.ExecuteWithoutSelect(ctx, 1, 1, 1); err == nil {
		t.Fatal("bare execute must be rejected")
	}
	// select(open) then execute(close): mismatched command must be refused
	l, _ := r.cl.session()
	ch := r.cl.wait(waitKey{1, 2, true, CotActCon})
	ch2 := r.cl.wait(waitKey{1, 2, false, CotActCon})
	r.cl.sendCmd(l, 1, 2, 1, true)
	if _, err := recv(ctx, ch, 2*time.Second, "select"); err != nil {
		t.Fatal(err)
	}
	r.cl.sendCmd(l, 1, 2, 2, false)
	if _, err := recv(ctx, ch2, 2*time.Second, "execute"); err == nil {
		t.Fatal("mismatched execute must be rejected")
	}
	// analog point is not controllable; unknown IOA rejected; backend failure surfaces
	if err := r.cl.SelectExecute(ctx, 1, 1001, 1); err == nil {
		t.Fatal("command to analog point must fail")
	}
	if err := r.cl.SelectExecute(ctx, 1, 4242, 1); err == nil {
		t.Fatal("command to unknown IOA must fail")
	}
	r.h.mu.Lock()
	r.h.failCmd = true
	r.h.mu.Unlock()
	if err := r.cl.SelectExecute(ctx, 1, 2, 2); err == nil {
		t.Fatal("backend interlock failure must be reported")
	}
	r.h.mu.Lock()
	n := len(r.h.cmds)
	r.h.mu.Unlock()
	if n != 1 {
		t.Fatalf("only the first valid command may reach the backend, got %d", n)
	}
}

func TestSequenceWrapAndFlowControl(t *testing.T) {
	r := start(t, nil, nil)
	r.waitUp(t)
	const frames = 34000 // > 2^15 so N(S) wraps; far more than k=12 unacknowledged
	var next atomic.Uint32
	done := make(chan struct{})
	var total, bad atomic.Int64
	go func() {
		for a := range r.got {
			for _, o := range a.Objects {
				if o.IOA != uint32(next.Add(1)) {
					bad.Add(1)
				}
				total.Add(1)
			}
			if total.Load() >= frames*16 {
				close(done)
				return
			}
		}
	}()
	go func() {
		ioa := uint32(0)
		for i := 0; i < frames; i++ {
			pts := make([]Point, 16)
			for j := range pts {
				ioa++
				pts[j] = Point{CA: 5, IOA: ioa, Float: float32(ioa), TS: time.Now()}
			}
			r.srv.Publish(pts)
		}
	}()
	select {
	case <-done:
	case <-time.After(60 * time.Second):
		t.Fatalf("received %d of %d objects", total.Load(), frames*16)
	}
	if bad.Load() != 0 {
		t.Fatalf("%d objects out of order or lost", bad.Load())
	}
}

func TestIgnoresDataBeforeStartDTAndRejectsSequenceError(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	srv := &Server{H: newMemH()}
	go srv.Serve(ln)
	defer srv.Close()
	c, _ := net.Dial("tcp", ln.Addr().String())
	defer c.Close()
	gi, _ := ASDU{Type: C_IC_NA_1, Cause: CotActivation, CA: 1, Objects: []Object{{QOI: 20}}}.Encode()
	f, _ := Frame{Kind: KindI, SendSeq: 0, RecvSeq: 0, ASDU: gi}.Encode()
	c.Write(f) // before STARTDT
	c.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
	if _, err := ReadFrame(c); err == nil {
		t.Fatal("outstation must not answer before STARTDT")
	}
	u, _ := Frame{Kind: KindU, U: UStartDTAct}.Encode()
	c.Write(u)
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	fr, err := ReadFrame(c)
	if err != nil || fr.Kind != KindU || fr.U != UStartDTCon {
		t.Fatalf("expected STARTDT con, got %+v %v", fr, err)
	}
	// wrong N(S) (expected 0, send 5) -> session must be terminated
	bad, _ := Frame{Kind: KindI, SendSeq: 5, RecvSeq: 0, ASDU: gi}.Encode()
	c.Write(bad)
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	for {
		if _, err := ReadFrame(c); err != nil {
			if errors.Is(err, io.EOF) || isClosed(err) {
				return
			}
			t.Fatalf("expected connection close, got %v", err)
		}
	}
}

func isClosed(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && !ne.Timeout()
}

// ---- TLS (IEC 62351-3 style) with mutual authentication ----
func pki(t *testing.T) (ca *x509.Certificate, caKey *ecdsa.PrivateKey, mk func(cn string, server bool) tls.Certificate) {
	t.Helper()
	caKey, _ = ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test-ca"}, NotBefore: time.Now().Add(-time.Hour),
		NotAfter: time.Now().Add(time.Hour), IsCA: true, KeyUsage: x509.KeyUsageCertSign, BasicConstraintsValid: true}
	der, _ := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &caKey.PublicKey, caKey)
	ca, _ = x509.ParseCertificate(der)
	n := int64(10)
	mk = func(cn string, server bool) tls.Certificate {
		n++
		k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		tp := &x509.Certificate{SerialNumber: big.NewInt(n), Subject: pkix.Name{CommonName: cn}, NotBefore: time.Now().Add(-time.Hour),
			NotAfter: time.Now().Add(time.Hour), IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
		if server {
			tp.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		} else {
			tp.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
		}
		d, _ := x509.CreateCertificate(rand.Reader, tp, ca, &k.PublicKey, caKey)
		return tls.Certificate{Certificate: [][]byte{d}, PrivateKey: k}
	}
	return
}

func TestMutualTLS(t *testing.T) {
	ca, _, mk := pki(t)
	pool := x509.NewCertPool()
	pool.AddCert(ca)
	srvCfg := &tls.Config{Certificates: []tls.Certificate{mk("outstation", true)}, ClientCAs: pool, ClientAuth: tls.RequireAndVerifyClientCert, MinVersion: tls.VersionTLS12}
	good := &tls.Config{Certificates: []tls.Certificate{mk("fep", false)}, RootCAs: pool, MinVersion: tls.VersionTLS12}
	r := start(t, srvCfg, good)
	r.waitUp(t)
	if err := r.cl.Interrogate(1); err != nil {
		t.Fatalf("interrogation over TLS: %v", err)
	}
	select {
	case a := <-r.got:
		if a.CA != 1 {
			t.Fatal("unexpected CA")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no data over TLS")
	}
	// client without a certificate must never get a session
	noCert := &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	r2 := start(t, srvCfg, noCert)
	select {
	case u := <-r2.up:
		if u {
			t.Fatal("client without certificate was accepted")
		}
	case <-time.After(1500 * time.Millisecond):
	}
	if err := r2.cl.Interrogate(1); err == nil {
		t.Fatal("no session expected without a client certificate")
	}
	// plain-TCP client against TLS server must not obtain a session either
	r3 := start(t, srvCfg, nil)
	select {
	case u := <-r3.up:
		if u {
			t.Fatal("plaintext client was accepted by TLS outstation")
		}
	case <-time.After(1500 * time.Millisecond):
	}
}
