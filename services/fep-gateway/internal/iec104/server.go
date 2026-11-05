package iec104

import (
	"log/slog"
	"net"
	"sync"
	"time"
)

// Point is one value held by an outstation.
type Point struct {
	CA      uint16
	IOA     uint32
	Status  bool // true: double point (breaker); false: analog (short float)
	Float   float32
	DPI     uint8
	Quality uint8
	TS      time.Time
}

// Handler is the outstation's data/command backend.
type Handler interface {
	Snapshot(ca uint16) []Point // all points of a common address (0xFFFF = all)
	IsStatus(ca uint16, ioa uint32) (status bool, exists bool)
	Command(ca uint16, ioa uint32, dcs uint8) error // execute a double command (1=open, 2=close)
}

const selectTimeout = 10 * time.Second

type Server struct {
	H     Handler
	Log   *slog.Logger
	Auth  *AuthConfig // non-nil: every control command must be answered with a valid challenge reply
	mu    sync.Mutex
	conns map[*srvConn]struct{}
	ln    net.Listener
}

type srvConn struct {
	s        *Server
	l        *link
	out      chan []byte
	started  bool
	smu      sync.Mutex
	selected map[[2]uint32]selection
	auth     authState
}

type selection struct {
	dcs uint8
	exp time.Time
}

func (s *Server) logger() *slog.Logger {
	if s.Log != nil {
		return s.Log
	}
	return slog.Default()
}

// Serve accepts connections until the listener is closed. Wrap the listener with tls.NewListener for IEC 62351-3 style TLS.
func (s *Server) Serve(ln net.Listener) error {
	s.mu.Lock()
	s.ln = ln
	if s.conns == nil {
		s.conns = map[*srvConn]struct{}{}
	}
	s.mu.Unlock()
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go s.handle(c)
	}
}

func (s *Server) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ln != nil {
		s.ln.Close()
	}
	for c := range s.conns {
		c.l.close()
	}
}

func (s *Server) handle(c net.Conn) {
	sc := &srvConn{s: s, l: newLink(c), out: make(chan []byte, 2048), selected: map[[2]uint32]selection{}}
	s.mu.Lock()
	s.conns[sc] = struct{}{}
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { // single writer keeps I-frame order and isolates slow consumers
		for {
			select {
			case b := <-sc.out:
				if err := sc.l.sendI(b, 5*time.Second); err != nil {
					sc.l.close()
					return
				}
			case <-done:
				return
			}
		}
	}()
	err := sc.l.run(sc.onI, sc.onU)
	close(done)
	sc.l.close()
	s.mu.Lock()
	delete(s.conns, sc)
	s.mu.Unlock()
	s.logger().Info("iec104 session ended", "remote", c.RemoteAddr().String(), "err", err)
}

func (sc *srvConn) enqueue(a ASDU) bool {
	b, err := a.Encode()
	if err != nil {
		return false
	}
	return sc.enqueueRaw(b)
}

func (sc *srvConn) enqueueRaw(b []byte) bool {
	select {
	case sc.out <- b:
		return true
	case <-time.After(2 * time.Second): // back-pressure first; drop only a consumer that stays stuck
		sc.s.logger().Warn("slow consumer, dropping connection")
		sc.l.close()
		return false
	}
}

func (sc *srvConn) onU(code byte) {
	switch code {
	case UStartDTAct:
		sc.smu.Lock()
		sc.started = true
		sc.smu.Unlock()
		sc.l.sendU(UStartDTCon)
	case UStopDTAct:
		sc.smu.Lock()
		sc.started = false
		sc.smu.Unlock()
		sc.l.sendU(UStopDTCon)
	}
}

func (sc *srvConn) isStarted() bool {
	sc.smu.Lock()
	defer sc.smu.Unlock()
	return sc.started
}

func (sc *srvConn) onI(a ASDU) {
	if !sc.isStarted() {
		return // I-frames before STARTDT are ignored
	}
	if cfg := sc.s.Auth; cfg.ok() {
		switch {
		case isCritical(a.Type):
			ch, err := sc.auth.startChallenge(cfg, a)
			if err != nil {
				return
			}
			b, _ := ch.Encode()
			sc.enqueueRaw(b)
			return
		case a.Type == S_RP_NA_1:
			orig, code, csq, drop := sc.auth.verify(cfg, a)
			if code != 0 {
				sc.s.logger().Warn("control command refused: authentication failed", "code", code)
				if e, err := errorASDU(a.CA, csq, cfg.user(), code).Encode(); err == nil {
					sc.enqueueRaw(e)
				}
				if orig.Type != 0 { // answer the command negatively so the master does not wait
					orig.Cause, orig.Neg = CotActCon, true
					sc.enqueue(orig)
				}
				if drop {
					sc.s.logger().Warn("too many failed authentications: dropping the association")
					time.AfterFunc(300*time.Millisecond, sc.l.close) // let the error frames flush first
				}
				return
			}
			sc.dispatch(orig)
			return
		case a.Type == S_CH_NA_1 || a.Type == S_ER_NA_1:
			return
		}
	}
	sc.dispatch(a)
}

func (sc *srvConn) dispatch(a ASDU) {
	switch a.Type {
	case C_IC_NA_1:
		sc.interrogation(a)
	case C_DC_NA_1:
		sc.doubleCommand(a)
	default:
		a.Cause, a.Neg = CotUnknownType, true
		sc.enqueue(a)
	}
}

func chunk(n, per int) [][2]int {
	var r [][2]int
	for i := 0; i < n; i += per {
		j := i + per
		if j > n {
			j = n
		}
		r = append(r, [2]int{i, j})
	}
	return r
}

func maxObjs(t uint8) int {
	el, _ := objLen(t)
	return (maxAPDULen - 4 - headerLen) / (3 + el)
}

func (sc *srvConn) interrogation(a ASDU) {
	if len(a.Objects) != 1 || a.Cause != CotActivation || a.Objects[0].QOI != 20 {
		a.Neg, a.Cause = true, CotActCon
		sc.enqueue(a)
		return
	}
	pts := sc.s.H.Snapshot(a.CA)
	if len(pts) == 0 && a.CA != 0xFFFF {
		a.Cause, a.Neg = CotUnknownCA, true
		sc.enqueue(a)
		return
	}
	con := a
	con.Cause = CotActCon
	sc.enqueue(con)
	sc.sendPoints(pts, CotInrogen, false)
	term := a
	term.Cause = CotActTerm
	sc.enqueue(term)
}

// sendPoints groups points by (CA, kind) and emits ASDUs. timed=true uses CP56Time2a types (spontaneous).
func (sc *srvConn) sendPoints(pts []Point, cot uint8, timed bool) {
	type key struct {
		ca     uint16
		status bool
	}
	groups := map[key][]Point{}
	var order []key
	for _, p := range pts {
		k := key{p.CA, p.Status}
		if _, ok := groups[k]; !ok {
			order = append(order, k)
		}
		groups[k] = append(groups[k], p)
	}
	for _, k := range order {
		g := groups[k]
		var t uint8
		switch {
		case k.status && timed:
			t = M_DP_TB_1
		case k.status:
			t = M_DP_NA_1
		case timed:
			t = M_ME_TF_1
		default:
			t = M_ME_NC_1
		}
		for _, r := range chunk(len(g), maxObjs(t)) {
			a := ASDU{Type: t, Cause: cot, CA: k.ca}
			for _, p := range g[r[0]:r[1]] {
				ts := p.TS
				o := Object{IOA: p.IOA, Float: p.Float, DPI: p.DPI, Quality: p.Quality}
				if timed {
					o.Time = &ts
				}
				a.Objects = append(a.Objects, o)
			}
			if !sc.enqueue(a) {
				return
			}
		}
	}
}

func (sc *srvConn) doubleCommand(a ASDU) {
	reply := func(cot uint8, neg bool) {
		r := a
		r.Cause, r.Neg = cot, neg
		sc.enqueue(r)
	}
	if len(a.Objects) != 1 || a.Cause != CotActivation {
		reply(CotUnknownCause, true)
		return
	}
	o := a.Objects[0]
	status, ok := sc.s.H.IsStatus(a.CA, o.IOA)
	if !ok {
		reply(CotUnknownIOA, true)
		return
	}
	if !status || (o.DPI != 1 && o.DPI != 2) {
		reply(CotActCon, true) // not a controllable status point / invalid command state
		return
	}
	k := [2]uint32{uint32(a.CA), o.IOA}
	if o.Select {
		sc.smu.Lock()
		sc.selected[k] = selection{dcs: o.DPI, exp: time.Now().Add(selectTimeout)}
		sc.smu.Unlock()
		reply(CotActCon, false)
		return
	}
	// execute requires a live, matching selection (select-before-operate)
	sc.smu.Lock()
	sel, have := sc.selected[k]
	delete(sc.selected, k)
	sc.smu.Unlock()
	if !have || time.Now().After(sel.exp) || sel.dcs != o.DPI {
		reply(CotActCon, true)
		return
	}
	if err := sc.s.H.Command(a.CA, o.IOA, o.DPI); err != nil {
		sc.s.logger().Warn("command rejected by backend", "ca", a.CA, "ioa", o.IOA, "err", err)
		reply(CotActCon, true)
		return
	}
	reply(CotActCon, false)
	reply(CotActTerm, false)
}

// Publish pushes spontaneous (time-tagged) updates to every started master connection.
func (s *Server) Publish(pts []Point) {
	s.mu.Lock()
	cs := make([]*srvConn, 0, len(s.conns))
	for c := range s.conns {
		cs = append(cs, c)
	}
	s.mu.Unlock()
	for _, c := range cs {
		if c.isStarted() {
			c.sendPoints(pts, CotSpont, true)
		}
	}
}
