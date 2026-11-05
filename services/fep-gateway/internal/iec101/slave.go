package iec101

import (
	"io"
	"log/slog"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"gridsovereign/fep/internal/iec104"
)

const (
	class1Cap = 20000
	perASDU   = 10
	selectTTL = 10 * time.Second
)

// Slave is an unbalanced IEC 101 secondary station backed by an iec104.Handler.
type Slave struct {
	H     iec104.Handler
	Link  uint16
	P     Params
	Log   *slog.Logger
	Stats SlaveStats

	mu      sync.Mutex
	class1  [][]byte
	sel     map[[2]uint32]selState
	lastFCB bool
	hasLast bool
	lastRsp []byte
}

type SlaveStats struct {
	Frames, Duplicates, Dropped, BadFrames atomic.Int64
}

type selState struct {
	dcs uint8
	exp time.Time
}

func (s *Slave) log() *slog.Logger {
	if s.Log != nil {
		return s.Log
	}
	return slog.Default()
}

// Serve accepts connections (one master at a time is the norm on a multi-drop line; extra ones share state).
func (s *Slave) Serve(ln net.Listener) error {
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go s.ServeConn(c)
	}
}

// ServeConn runs the secondary state machine on one byte stream (TCP or serial) until it ends.
func (s *Slave) ServeConn(rw io.ReadWriteCloser) error {
	if s.P == (Params{}) {
		s.P = Default
	}
	defer rw.Close()
	s.mu.Lock()
	if s.sel == nil {
		s.sel = map[[2]uint32]selState{}
	}
	s.mu.Unlock()
	for {
		f, err := Read(rw, s.P.LinkAddr)
		if err != nil {
			return err
		}
		if f.Kind == 'S' || !isPrimary(f.Ctrl) {
			continue
		}
		bcast := (s.P.LinkAddr == 1 && f.Addr == 0xFF) || (s.P.LinkAddr == 2 && f.Addr == 0xFFFF)
		if f.Addr != s.Link && !bcast {
			continue
		}
		rsp := s.handle(f, bcast)
		if rsp != nil {
			if _, err := rw.Write(rsp); err != nil {
				return err
			}
		}
	}
}

func (s *Slave) fixed(fc byte) []byte {
	b, _ := Frame{Kind: 'F', Ctrl: secondary(fc, len(s.class1) > 0), Addr: s.Link, AddrSz: s.P.LinkAddr}.Encode()
	return b
}

func (s *Slave) varFrame(data []byte) []byte {
	b, err := Frame{Kind: 'V', Ctrl: secondary(FcUserData, len(s.class1) > 0), Addr: s.Link, AddrSz: s.P.LinkAddr, Data: data}.Encode()
	if err != nil {
		return s.fixed(FcNoData)
	}
	return b
}

func (s *Slave) handle(f Frame, bcast bool) []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Stats.Frames.Add(1)
	fc := fcOf(f.Ctrl)
	switch fc {
	case FcResetLink:
		s.hasLast, s.lastRsp = false, nil
		s.sel = map[[2]uint32]selState{}
		return s.fixed(FcAck)
	case FcReqStatus:
		return s.fixed(FcLinkState)
	case FcUserNoReply:
		if a, ok := s.decode(f.Data); ok {
			s.process(a)
		}
		return nil
	case FcUserConfirm, FcReqClass1, FcReqClass2:
		if !fcvOf(f.Ctrl) {
			return s.fixed(FcNack)
		}
		if s.hasLast && fcbOf(f.Ctrl) == s.lastFCB { // repeated frame: same answer again, no second execution
			s.Stats.Duplicates.Add(1)
			return s.lastRsp
		}
		var rsp []byte
		switch fc {
		case FcUserConfirm:
			a, ok := s.decode(f.Data)
			if !ok {
				rsp = s.fixed(FcNack)
			} else {
				s.process(a)
				rsp = s.fixed(FcAck)
			}
		case FcReqClass1:
			if len(s.class1) > 0 {
				d := s.class1[0]
				s.class1 = s.class1[1:]
				rsp = s.varFrame(d)
			} else {
				rsp = s.fixed(FcNoData)
			}
		default: // class 2: this slave carries no class-2 data; ACD tells the master to fetch class 1
			rsp = s.fixed(FcNoData)
		}
		s.hasLast, s.lastFCB, s.lastRsp = true, fcbOf(f.Ctrl), rsp
		return rsp
	}
	return s.fixed(FcNack)
}

func (s *Slave) decode(b []byte) (iec104.ASDU, bool) {
	a, err := s.P.Decode(b)
	if err != nil {
		s.Stats.BadFrames.Add(1)
		return a, false
	}
	return a, true
}

func (s *Slave) push(a iec104.ASDU) {
	b, err := s.P.Encode(a)
	if err != nil {
		return
	}
	if len(s.class1) >= class1Cap {
		s.class1 = s.class1[1:] // oldest event lost; counted and visible in stats
		s.Stats.Dropped.Add(1)
	}
	s.class1 = append(s.class1, b)
}

func (s *Slave) process(a iec104.ASDU) {
	switch a.Type {
	case iec104.C_IC_NA_1:
		s.interrogation(a)
	case iec104.C_DC_NA_1:
		s.command(a)
	default:
		a.Cause, a.Neg = iec104.CotUnknownType, true
		s.push(a)
	}
}

func (s *Slave) interrogation(a iec104.ASDU) {
	if len(a.Objects) != 1 || a.Cause != iec104.CotActivation || a.Objects[0].QOI != 20 {
		a.Cause, a.Neg = iec104.CotActCon, true
		s.push(a)
		return
	}
	pts := s.H.Snapshot(a.CA)
	if len(pts) == 0 && a.CA != 0xFFFF {
		a.Cause, a.Neg = iec104.CotUnknownCA, true
		s.push(a)
		return
	}
	con := a
	con.Cause = iec104.CotActCon
	s.push(con)
	s.points(pts, iec104.CotInrogen, false)
	term := a
	term.Cause = iec104.CotActTerm
	s.push(term)
}

func (s *Slave) points(pts []iec104.Point, cot uint8, timed bool) {
	type key struct {
		ca     uint16
		status bool
	}
	groups := map[key][]iec104.Point{}
	var order []key
	for _, p := range pts {
		k := key{p.CA, p.Status}
		if _, ok := groups[k]; !ok {
			order = append(order, k)
		}
		groups[k] = append(groups[k], p)
	}
	for _, k := range order {
		var t uint8
		switch {
		case k.status && timed:
			t = iec104.M_DP_TB_1
		case k.status:
			t = iec104.M_DP_NA_1
		case timed:
			t = iec104.M_ME_TF_1
		default:
			t = iec104.M_ME_NC_1
		}
		g := groups[k]
		for i := 0; i < len(g); i += perASDU {
			j := i + perASDU
			if j > len(g) {
				j = len(g)
			}
			a := iec104.ASDU{Type: t, Cause: cot, CA: k.ca}
			for _, p := range g[i:j] {
				ts := p.TS
				o := iec104.Object{IOA: p.IOA, Float: p.Float, DPI: p.DPI, Quality: p.Quality}
				if timed {
					o.Time = &ts
				}
				a.Objects = append(a.Objects, o)
			}
			s.push(a)
		}
	}
}

func (s *Slave) command(a iec104.ASDU) {
	reply := func(cot uint8, neg bool) {
		r := a
		r.Cause, r.Neg = cot, neg
		s.push(r)
	}
	if len(a.Objects) != 1 || a.Cause != iec104.CotActivation {
		reply(iec104.CotUnknownCause, true)
		return
	}
	o := a.Objects[0]
	status, ok := s.H.IsStatus(a.CA, o.IOA)
	if !ok {
		reply(iec104.CotUnknownIOA, true)
		return
	}
	if !status || (o.DPI != 1 && o.DPI != 2) {
		reply(iec104.CotActCon, true)
		return
	}
	k := [2]uint32{uint32(a.CA), o.IOA}
	if o.Select {
		s.sel[k] = selState{o.DPI, time.Now().Add(selectTTL)}
		reply(iec104.CotActCon, false)
		return
	}
	st, have := s.sel[k]
	delete(s.sel, k)
	if !have || time.Now().After(st.exp) || st.dcs != o.DPI {
		reply(iec104.CotActCon, true)
		return
	}
	s.mu.Unlock() // the backend call may be slow; never hold the link state across it
	err := s.H.Command(a.CA, o.IOA, o.DPI)
	s.mu.Lock()
	if err != nil {
		s.log().Warn("command rejected by backend", "ca", a.CA, "ioa", o.IOA, "err", err)
		reply(iec104.CotActCon, true)
		return
	}
	reply(iec104.CotActCon, false)
	reply(iec104.CotActTerm, false)
}

// Publish queues spontaneous time-tagged updates as class-1 data.
func (s *Slave) Publish(pts []iec104.Point) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.points(pts, iec104.CotSpont, true)
}

// Pending returns the class-1 queue length.
func (s *Slave) Pending() int { s.mu.Lock(); defer s.mu.Unlock(); return len(s.class1) }
