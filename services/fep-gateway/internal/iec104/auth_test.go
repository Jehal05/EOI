package iec104

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"
)

var key16 = []byte("0123456789abcdef-test-key")

func startAuth(t *testing.T, srvAuth, cliAuth *AuthConfig) *rig {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	h := newMemH()
	srv := &Server{H: h, Auth: srvAuth}
	go srv.Serve(ln)
	r := &rig{srv: srv, h: h, got: make(chan ASDU, 100000), up: make(chan bool, 8)}
	r.cl = &Client{Addr: ln.Addr().String(), Auth: cliAuth, OnASDU: func(a ASDU) { r.got <- a }, OnState: func(u bool) {
		select {
		case r.up <- u:
		default:
		}
	}}
	ctx, cancel := context.WithCancel(context.Background())
	go r.cl.Run(ctx)
	t.Cleanup(func() { cancel(); srv.Close() })
	r.waitUp(t)
	return r
}

func (r *rig) cmdCount() int { r.h.mu.Lock(); defer r.h.mu.Unlock(); return len(r.h.cmds) }

func TestAuthenticatedCommandSucceeds(t *testing.T) {
	r := startAuth(t, &AuthConfig{Key: key16}, &AuthConfig{Key: key16})
	if err := r.cl.SelectExecute(context.Background(), 1, 1, 1); err != nil {
		t.Fatalf("authenticated SBO failed: %v", err)
	}
	if r.cmdCount() != 1 {
		t.Fatal("backend did not get the command")
	}
}

func TestWrongKeyIsRefusedAndNeverReachesTheBackend(t *testing.T) {
	r := startAuth(t, &AuthConfig{Key: key16}, &AuthConfig{Key: []byte("a-completely-different-key!!")})
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	if err := r.cl.SelectExecute(ctx, 1, 1, 1); err == nil {
		t.Fatal("command with the wrong key succeeded")
	}
	if r.cmdCount() != 0 {
		t.Fatal("unauthenticated command reached the backend")
	}
}

func TestMasterWithoutAuthCannotOperateAnAuthenticatedOutstation(t *testing.T) {
	r := startAuth(t, &AuthConfig{Key: key16}, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := r.cl.SelectExecute(ctx, 1, 1, 1); err == nil || r.cmdCount() != 0 {
		t.Fatal("command accepted without authentication")
	}
}

func TestInterrogationDoesNotNeedAuthentication(t *testing.T) {
	r := startAuth(t, &AuthConfig{Key: key16}, &AuthConfig{Key: key16})
	if err := r.cl.Interrogate(1); err != nil {
		t.Fatal(err)
	}
	select {
	case <-r.got:
	case <-time.After(3 * time.Second):
		t.Fatal("no data")
	}
}

// rawMaster speaks the protocol by hand so a test can replay or forge replies.
type rawMaster struct {
	l   *link
	in  chan ASDU
	mu  sync.Mutex
	end chan error
}

func newRaw(t *testing.T, addr string) *rawMaster {
	c, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	m := &rawMaster{l: newLink(c), in: make(chan ASDU, 100), end: make(chan error, 1)}
	go func() { m.end <- m.l.run(func(a ASDU) { m.in <- a }, func(byte) {}) }()
	m.l.sendU(UStartDTAct)
	time.Sleep(200 * time.Millisecond)
	t.Cleanup(m.l.close)
	return m
}

func (m *rawMaster) send(t *testing.T, a ASDU) {
	b, err := a.Encode()
	if err != nil {
		t.Fatal(err)
	}
	if err := m.l.sendI(b, time.Second); err != nil {
		t.Fatal(err)
	}
}

func (m *rawMaster) next(t *testing.T, typ uint8) ASDU {
	for {
		select {
		case a := <-m.in:
			if a.Type == typ {
				return a
			}
		case <-time.After(3 * time.Second):
			t.Fatalf("no ASDU of type %d", typ)
		}
	}
}

func cmd(ioa uint32, dcs uint8, sel bool) ASDU {
	return ASDU{Type: C_DC_NA_1, Cause: CotActivation, CA: 1, Objects: []Object{{IOA: ioa, DPI: dcs, Select: sel}}}
}

func startRawServer(t *testing.T, cfg *AuthConfig) (*Server, *memH, string) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	h := newMemH()
	srv := &Server{H: h, Auth: cfg}
	go srv.Serve(ln)
	t.Cleanup(srv.Close)
	return srv, h, ln.Addr().String()
}

func TestReplayedReplyIsRefused(t *testing.T) {
	cfg := &AuthConfig{Key: key16}
	_, h, addr := startRawServer(t, cfg)
	m := newRaw(t, addr)
	sel := cmd(1, 1, true)
	raw, _ := sel.Encode()
	m.send(t, sel)
	ch := m.next(t, S_CH_NA_1)
	rep, ok := answer(cfg, ch, raw)
	if !ok {
		t.Fatal("cannot answer")
	}
	m.send(t, rep)
	if a := m.next(t, C_DC_NA_1); a.Neg || a.Cause != CotActCon {
		t.Fatalf("valid reply refused: %+v", a)
	}
	m.send(t, rep) // replay of the very same reply
	e := m.next(t, S_ER_NA_1)
	if e.Objects[0].Data[6] != AuthErrExpired {
		t.Fatalf("replay not reported as expired: %v", e.Objects[0].Data)
	}
	_ = h
}

func TestReplyForAnotherCommandIsRefused(t *testing.T) {
	cfg := &AuthConfig{Key: key16}
	_, h, addr := startRawServer(t, cfg)
	m := newRaw(t, addr)
	harmless := cmd(1, 1, true)
	hr, _ := harmless.Encode()
	m.send(t, cmd(2, 1, true)) // the command the server will actually hold
	ch := m.next(t, S_CH_NA_1)
	rep, _ := answer(cfg, ch, hr) // MAC computed over a different command
	m.send(t, rep)
	e := m.next(t, S_ER_NA_1)
	if e.Objects[0].Data[6] != AuthErrFailed {
		t.Fatal("MAC over another command accepted")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.cmds) != 0 {
		t.Fatal("command executed")
	}
}

func TestExpiredChallengeIsRefused(t *testing.T) {
	cfg := &AuthConfig{Key: key16, Window: 200 * time.Millisecond}
	_, _, addr := startRawServer(t, cfg)
	m := newRaw(t, addr)
	sel := cmd(1, 1, true)
	raw, _ := sel.Encode()
	m.send(t, sel)
	ch := m.next(t, S_CH_NA_1)
	time.Sleep(400 * time.Millisecond)
	rep, _ := answer(cfg, ch, raw)
	m.send(t, rep)
	if e := m.next(t, S_ER_NA_1); e.Objects[0].Data[6] != AuthErrExpired {
		t.Fatal("late reply accepted")
	}
}

func TestRepeatedFailuresDropTheAssociation(t *testing.T) {
	cfg := &AuthConfig{Key: key16, MaxFail: 3}
	_, _, addr := startRawServer(t, cfg)
	m := newRaw(t, addr)
	for i := 0; i < 3; i++ {
		m.send(t, cmd(1, 1, true))
		ch := m.next(t, S_CH_NA_1)
		bad, _ := answer(&AuthConfig{Key: []byte("wrong-wrong-wrong-wrong")}, ch, mustRaw(cmd(1, 1, true)))
		m.send(t, bad)
		m.next(t, S_ER_NA_1)
	}
	select {
	case <-m.end:
	case <-time.After(3 * time.Second):
		t.Fatal("association not dropped after repeated failures")
	}
}

func mustRaw(a ASDU) []byte { b, _ := a.Encode(); return b }

func TestAuthASDURoundTrip(t *testing.T) {
	var ch [8]byte
	copy(ch[:], "12345678")
	for _, a := range []ASDU{challengeASDU(3, 7, 1, ch), replyASDU(3, 7, 1, make([]byte, 16)), errorASDU(3, 7, 1, 2)} {
		b, err := a.Encode()
		if err != nil {
			t.Fatal(err)
		}
		d, err := DecodeASDU(b)
		if err != nil || d.Type != a.Type || string(d.Objects[0].Data) != string(a.Objects[0].Data) {
			t.Fatalf("round trip failed for type %d: %v", a.Type, err)
		}
	}
	bad := challengeASDU(3, 7, 1, ch)
	bad.Objects[0].Data = bad.Objects[0].Data[:5]
	if _, err := bad.Encode(); err == nil {
		t.Fatal("short challenge encoded")
	}
}
