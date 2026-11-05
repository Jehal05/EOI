package iec101

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"

	"gridsovereign/fep/internal/iec104"
)

// Master is an unbalanced IEC 101 primary station polling one slave. One Run call is one link session.
type Master struct {
	Open    func() (io.ReadWriteCloser, error) // TCP dial or serial device open
	Link    uint16                             // slave link address
	P       Params
	OnASDU  func(iec104.ASDU)
	OnState func(up bool)
	Poll    time.Duration // class-2 poll interval (default 250 ms)
	Timeout time.Duration // reply timeout per transaction (default 1 s)
	Retries int           // repeats of an unanswered frame (default 3)
	Stats   Stats
	mu      sync.Mutex
	jobs    chan *job
	waiters map[wk]chan iec104.ASDU
	up      atomic.Bool
	fcb     bool
	conn    io.ReadWriteCloser
	rx      chan rxRes
}

// Stats are cumulative counters (atomic).
type Stats struct {
	Frames, Retries, Timeouts, BadFrames, ASDUs atomic.Int64
}

type rxRes struct {
	f   Frame
	err error
}

type wk struct {
	ca  uint16
	ioa uint32
	typ uint8
	sel bool
	cot uint8
}

type job struct {
	a    iec104.ASDU
	done chan error
}

func (m *Master) defaults() {
	if m.Poll == 0 {
		m.Poll = 250 * time.Millisecond
	}
	if m.Timeout == 0 {
		m.Timeout = time.Second
	}
	if m.Retries == 0 {
		m.Retries = 3
	}
	if m.P == (Params{}) {
		m.P = Default
	}
}

var ErrNotUp = errors.New("iec101: link not up")

func (m *Master) state(up bool) {
	if m.up.Swap(up) != up && m.OnState != nil {
		m.OnState(up)
	}
}

// txn sends f and waits for the secondary's answer, repeating the identical frame (same FCB) on timeout.
// A single reader goroutine owns the stream; stale frames from earlier transactions are drained first.
func (m *Master) txn(f Frame, wantReply bool) (Frame, error) {
	b, err := f.Encode()
	if err != nil {
		return Frame{}, err
	}
	for len(m.rx) > 0 {
		<-m.rx
		m.Stats.BadFrames.Add(1)
	}
	for try := 0; try <= m.Retries; try++ {
		if try > 0 {
			m.Stats.Retries.Add(1)
		}
		if _, err := m.conn.Write(b); err != nil {
			return Frame{}, err
		}
		m.Stats.Frames.Add(1)
		if !wantReply {
			return Frame{}, nil
		}
		select {
		case r, ok := <-m.rx:
			if !ok || r.err != nil {
				if r.err == nil {
					r.err = io.EOF
				}
				return Frame{}, r.err
			}
			return r.f, nil
		case <-time.After(m.Timeout):
			m.Stats.Timeouts.Add(1)
		}
	}
	return Frame{}, fmt.Errorf("iec101: no answer from link %d after %d tries", m.Link, m.Retries+1)
}

func (m *Master) prim(fc byte, useFCB bool) Frame {
	f := Frame{Kind: 'F', Addr: m.Link, AddrSz: m.P.LinkAddr}
	if useFCB {
		m.fcb = !m.fcb
		f.Ctrl = primary(fc, m.fcb, true)
	} else {
		f.Ctrl = primary(fc, false, false)
	}
	return f
}

// Run performs the link session until ctx ends or the link fails.
func (m *Master) Run(ctx context.Context) error {
	m.defaults()
	if err := m.P.Valid(); err != nil {
		return err
	}
	c, err := m.Open()
	if err != nil {
		return err
	}
	m.conn = c
	defer c.Close()
	defer m.state(false)
	m.mu.Lock()
	m.jobs = make(chan *job, 16)
	m.waiters = map[wk]chan iec104.ASDU{}
	m.mu.Unlock()
	m.rx = make(chan rxRes, 8)
	go func() { <-ctx.Done(); c.Close() }()
	go func(rx chan rxRes) {
		for {
			f, err := Read(c, m.P.LinkAddr)
			select {
			case rx <- rxRes{f, err}:
			default: // consumer is behind: drop, the retry logic recovers
			}
			if err != nil {
				return
			}
		}
	}(m.rx)

	if r, err := m.txn(m.prim(FcResetLink, false), true); err != nil || (r.Kind != 'S' && fcOf(r.Ctrl) != FcAck) {
		return fmt.Errorf("iec101: reset link failed: %v", err)
	}
	m.fcb = false
	if r, err := m.txn(m.prim(FcReqStatus, false), true); err != nil || r.Kind != 'F' || fcOf(r.Ctrl) != FcLinkState {
		return fmt.Errorf("iec101: link status failed: %v", err)
	}
	m.state(true)
	tick := time.NewTicker(m.Poll)
	defer tick.Stop()
	acd := true // drain class 1 at start
	for {
		if acd {
			more, err := m.pollClass(FcReqClass1)
			if err != nil {
				return err
			}
			acd = more
			if acd {
				continue
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case j := <-m.jobs:
			err := m.sendASDU(j.a, &acd)
			j.done <- err
			if err != nil && !errors.Is(err, errRefused) {
				return err
			}
		case <-tick.C:
			more, err := m.pollClass(FcReqClass2)
			if err != nil {
				return err
			}
			acd = more
		}
	}
}

var errRefused = errors.New("iec101: slave refused the frame")

func (m *Master) sendASDU(a iec104.ASDU, acd *bool) error {
	b, err := m.P.Encode(a)
	if err != nil {
		return err
	}
	f := m.prim(FcUserConfirm, true)
	f.Kind, f.Data = 'V', b
	r, err := m.txn(f, true)
	if err != nil {
		return err
	}
	if r.Kind == 'F' || r.Kind == 'V' {
		*acd = *acd || acdOf(r.Ctrl)
		if fcOf(r.Ctrl) == FcNack {
			return errRefused
		}
		return nil
	}
	return nil // single-char E5 = ACK
}

// pollClass polls class 1 or 2 once; returns whether the slave reports more class-1 data (ACD).
func (m *Master) pollClass(fc byte) (bool, error) {
	r, err := m.txn(m.prim(fc, true), true)
	if err != nil {
		return false, err
	}
	switch r.Kind {
	case 'S':
		return false, nil
	case 'F':
		return acdOf(r.Ctrl), nil
	case 'V':
		a, err := m.P.Decode(r.Data)
		if err != nil {
			m.Stats.BadFrames.Add(1)
			return acdOf(r.Ctrl), nil
		}
		m.Stats.ASDUs.Add(1)
		m.deliver(a)
		return acdOf(r.Ctrl), nil
	}
	return false, nil
}

func (m *Master) deliver(a iec104.ASDU) {
	m.mu.Lock()
	for _, o := range a.Objects {
		k := wk{a.CA, o.IOA, a.Type, o.Select, a.Cause}
		if ch, ok := m.waiters[k]; ok {
			select {
			case ch <- a:
			default:
			}
		}
	}
	if a.Type == iec104.C_IC_NA_1 {
		if ch, ok := m.waiters[wk{a.CA, 0, a.Type, false, a.Cause}]; ok {
			select {
			case ch <- a:
			default:
			}
		}
	}
	m.mu.Unlock()
	if m.OnASDU != nil {
		m.OnASDU(a)
	}
}

func (m *Master) submit(a iec104.ASDU) error {
	if !m.up.Load() {
		return ErrNotUp
	}
	j := &job{a: a, done: make(chan error, 1)}
	select {
	case m.jobs <- j:
	case <-time.After(2 * time.Second):
		return errors.New("iec101: command queue full")
	}
	select {
	case err := <-j.done:
		return err
	case <-time.After(time.Duration(m.Retries+2) * m.Timeout):
		return errors.New("iec101: send timed out")
	}
}

func (m *Master) wait(k wk) chan iec104.ASDU {
	ch := make(chan iec104.ASDU, 4)
	m.mu.Lock()
	m.waiters[k] = ch
	m.mu.Unlock()
	return ch
}

func (m *Master) unwait(ks ...wk) {
	m.mu.Lock()
	for _, k := range ks {
		delete(m.waiters, k)
	}
	m.mu.Unlock()
}

// Interrogate sends a station interrogation; the data arrives through OnASDU via class-1 polling.
func (m *Master) Interrogate(ca uint16) error {
	return m.submit(iec104.ASDU{Type: iec104.C_IC_NA_1, Cause: iec104.CotActivation, CA: ca, Objects: []iec104.Object{{QOI: 20}}})
}

func (m *Master) cmd(ca uint16, ioa uint32, dcs uint8, sel bool) error {
	return m.submit(iec104.ASDU{Type: iec104.C_DC_NA_1, Cause: iec104.CotActivation, CA: ca, Objects: []iec104.Object{{IOA: ioa, DPI: dcs, Select: sel}}})
}

func (m *Master) await(ch chan iec104.ASDU, what string) (iec104.ASDU, error) {
	select {
	case a := <-ch:
		return a, nil
	case <-time.After(5 * time.Second):
		return iec104.ASDU{}, fmt.Errorf("iec101: %s: no confirmation", what)
	}
}

// SelectExecute performs select-before-operate on a double command.
func (m *Master) SelectExecute(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	ks, ke := wk{ca, ioa, iec104.C_DC_NA_1, true, iec104.CotActCon}, wk{ca, ioa, iec104.C_DC_NA_1, false, iec104.CotActCon}
	cs := m.wait(ks)
	ce := m.wait(ke)
	defer m.unwait(ks, ke)
	if err := m.cmd(ca, ioa, dcs, true); err != nil {
		return err
	}
	a, err := m.await(cs, "select")
	if err != nil {
		return err
	}
	if a.Neg {
		return errors.New("iec101: select refused")
	}
	if err := m.cmd(ca, ioa, dcs, false); err != nil {
		return err
	}
	a, err = m.await(ce, "execute")
	if err != nil {
		return err
	}
	if a.Neg {
		return errors.New("iec101: execute refused")
	}
	return nil
}

// ExecuteWithoutSelect sends an execute with no preceding select (a conforming slave must refuse it).
func (m *Master) ExecuteWithoutSelect(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	ke := wk{ca, ioa, iec104.C_DC_NA_1, false, iec104.CotActCon}
	ce := m.wait(ke)
	defer m.unwait(ke)
	if err := m.cmd(ca, ioa, dcs, false); err != nil {
		return err
	}
	a, err := m.await(ce, "execute")
	if err != nil {
		return err
	}
	if a.Neg {
		return errors.New("iec101: execute refused")
	}
	return nil
}
