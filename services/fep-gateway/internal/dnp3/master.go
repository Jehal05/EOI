package dnp3

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"time"

	"gridsovereign/fep/internal/iec104"
)

// Master is a DNP3 master for one outstation. Point index = IOA, all points belong to CA.
type Master struct {
	Open    func() (io.ReadWriteCloser, error)
	Local   uint16
	Remote  uint16
	CA      uint16
	OnASDU  func(iec104.ASDU)
	OnState func(up bool)
	Poll    time.Duration // event poll interval (default 500 ms)
	Timeout time.Duration // response timeout (default 3 s)
	Stats   struct{ Responses, Unsupported, Confirms atomic.Int64 }

	mu   sync.Mutex
	jobs chan func() error
	up   atomic.Bool
	seq  uint8
	t    *transport
	rx   chan rxMsg
}

type rxMsg struct {
	msg []byte
	err error
}

var ErrNotUp = errors.New("dnp3: link not up")

func (m *Master) state(up bool) {
	if m.up.Swap(up) != up && m.OnState != nil {
		m.OnState(up)
	}
}

func (m *Master) nextSeq() uint8 { s := m.seq; m.seq = (m.seq + 1) & 0x0F; return s }

func (m *Master) send(fn uint8, body []byte) (uint8, error) {
	s := m.nextSeq()
	return s, m.t.send(buildMsg(header{fir: true, fin: true, seq: s, fn: fn}, body))
}

// exchange sends a request and gathers the (possibly multi-fragment) response, confirming as required.
func (m *Master) exchange(fn uint8, body []byte, cause uint8) ([]byte, uint16, error) {
	for len(m.rx) > 0 {
		<-m.rx
	}
	if _, err := m.send(fn, body); err != nil {
		return nil, 0, err
	}
	var all []byte
	var iin uint16
	for {
		select {
		case r := <-m.rx:
			if r.err != nil {
				return nil, 0, r.err
			}
			h, b, err := parseHeader(r.msg)
			if err != nil || h.fn != FnResponse && h.fn != FnUnsolResp {
				continue
			}
			m.Stats.Responses.Add(1)
			iin = h.iin
			if h.con {
				m.Stats.Confirms.Add(1)
				m.t.send(buildMsg(header{fir: true, fin: true, seq: h.seq, uns: h.uns, fn: FnConfirm}, nil))
			}
			if fn == FnRead {
				m.decode(b, cause)
			} else {
				all = append(all, b...)
			}
			if h.fin {
				return all, iin, nil
			}
		case <-time.After(m.Timeout):
			return nil, 0, fmt.Errorf("dnp3: no response from %d", m.Remote)
		}
	}
}

// decode converts response objects to ASDUs. Unknown group/variations stop parsing (counted).
func (m *Master) decode(b []byte, cause uint8) {
	for len(b) >= 3 {
		g, v, q := b[0], b[1], b[2]
		qq, err := parseQual(q, b[3:])
		if err != nil || qq.prefix == 0 {
			m.Stats.Unsupported.Add(1)
			return
		}
		b = b[3+qq.size:]
		var vsz int
		switch {
		case g == gBinIn && v == vBinFlags:
			vsz = 1
		case g == gBinEv && v == vBinEvTime:
			vsz = 7
		case g == gAnaIn && v == vAnaFloat:
			vsz = 5
		case g == gAnaEv && v == vAnaEvFTime:
			vsz = 11
		default:
			m.Stats.Unsupported.Add(1)
			return
		}
		osz := qq.prefix + vsz
		if len(b) < int(qq.count)*osz {
			m.Stats.Unsupported.Add(1)
			return
		}
		a := iec104.ASDU{CA: m.CA, Cause: cause}
		switch {
		case g == gBinIn:
			a.Type = iec104.M_DP_NA_1
		case g == gBinEv:
			a.Type = iec104.M_DP_TB_1
		case g == gAnaIn:
			a.Type = iec104.M_ME_NC_1
		default:
			a.Type = iec104.M_ME_TF_1
		}
		flush := func() {
			if len(a.Objects) > 0 && m.OnASDU != nil {
				m.OnASDU(a)
			}
			a.Objects = nil
		}
		for i := 0; i < int(qq.count); i++ {
			ob := b[i*osz : (i+1)*osz]
			o := iec104.Object{IOA: getIdx(ob, qq.prefix)}
			d := ob[qq.prefix:]
			switch g {
			case gBinIn, gBinEv:
				o.DPI, o.Quality = dpiFromBin(d[0])
			default:
				o.Quality = qualityFromFlags(d[0])
				o.Float = float32frombytes(d[1:5])
			}
			if g == gBinEv {
				t := msTime(d[1:7])
				o.Time = &t
			}
			if g == gAnaEv {
				t := msTime(d[5:11])
				o.Time = &t
			}
			a.Objects = append(a.Objects, o)
			if len(a.Objects) == 20 {
				flush()
			}
		}
		flush()
		b = b[int(qq.count)*osz:]
	}
}

func float32frombytes(b []byte) float32 {
	return iec104Float(binary.LittleEndian.Uint32(b))
}

var (
	integrity = []byte{gClass, 4, 0x06, gClass, 1, 0x06, gClass, 2, 0x06, gClass, 3, 0x06}
	events    = []byte{gClass, 2, 0x06, gClass, 3, 0x06}
)

// Run performs one session.
func (m *Master) Run(ctx context.Context) error {
	if m.Poll == 0 {
		m.Poll = 500 * time.Millisecond
	}
	if m.Timeout == 0 {
		m.Timeout = 3 * time.Second
	}
	c, err := m.Open()
	if err != nil {
		return err
	}
	defer c.Close()
	defer m.state(false)
	go func() { <-ctx.Done(); c.Close() }()
	m.t = &transport{rw: c, local: m.Local, peer: m.Remote, dir: ctrlDIR, rseq: -1}
	m.rx = make(chan rxMsg, 16)
	m.jobs = make(chan func() error, 8)
	go func(t *transport, rx chan rxMsg) {
		for {
			msg, _, err := t.recv()
			rx <- rxMsg{msg, err}
			if err != nil {
				return
			}
		}
	}(m.t, m.rx)

	// clear the device-restart indication, then read everything
	m.exchange(FnWrite, []byte{gIIN, 1, 0x00, 7, 7, 0x00}, 0)
	if _, _, err := m.exchange(FnRead, integrity, iec104.CotInrogen); err != nil {
		return err
	}
	m.state(true)
	tick := time.NewTicker(m.Poll)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case j := <-m.jobs:
			if err := j(); err != nil {
				return err
			}
		case <-tick.C:
			for i := 0; i < 20; i++ { // drain while the outstation reports events
				_, iin, err := m.exchange(FnRead, events, iec104.CotSpont)
				if err != nil {
					return err
				}
				if iin&(IINClass1|IINClass2|IINClass3) == 0 {
					break
				}
			}
		}
	}
}

func (m *Master) do(f func() error) error {
	if !m.up.Load() {
		return ErrNotUp
	}
	done := make(chan error, 1)
	select {
	case m.jobs <- func() error {
		e := f()
		done <- e
		var r refusal
		if errors.As(e, &r) {
			return nil // the outstation said no; the link is fine
		}
		return e
	}:
	case <-time.After(2 * time.Second):
		return errors.New("dnp3: job queue full")
	}
	select {
	case e := <-done:
		return e
	case <-time.After(3 * m.Timeout):
		return errors.New("dnp3: request timed out")
	}
}

// Interrogate runs an integrity poll (class 0 + all event classes). Other CAs than the configured one are a no-op.
func (m *Master) Interrogate(ca uint16) error {
	if ca != m.CA {
		return nil
	}
	return m.do(func() error {
		_, _, err := m.exchange(FnRead, integrity, iec104.CotInrogen)
		return err
	})
}

func crob(ioa uint32, dcs uint8) []byte {
	code := byte(0x41) // PULSE_ON + CLOSE
	if dcs == 1 {
		code = 0x81 // PULSE_ON + TRIP
	}
	b := []byte{gCROB, vCROB, 0x38, 1, 0}
	b = append(b, putIdx(ioa)...)
	b = append(b, code, 1)
	b = append(b, 0, 0, 0, 0, 0, 0, 0, 0, 0)
	return b
}

func (m *Master) ctl(fn uint8, ioa uint32, dcs uint8, seqCheck *uint8) error {
	if seqCheck != nil {
		// the operate must carry the sequence number following the select
		m.seq = (*seqCheck + 1) & 0x0F
	}
	resp, _, err := m.exchange(fn, crob(ioa, dcs), 0)
	if err != nil {
		return err
	}
	if len(resp) < 3+2+4+11 || resp[0] != gCROB {
		return errors.New("dnp3: malformed control response")
	}
	if st := resp[len(resp)-1]; st != StSuccess {
		return fmt.Errorf("dnp3: control refused, status %d", st)
	}
	return nil
}

// SelectExecute performs select-before-operate on a breaker (dcs 1 = open, 2 = close).
func (m *Master) SelectExecute(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	if ca != m.CA {
		return fmt.Errorf("dnp3: CA %d is not served by this outstation", ca)
	}
	return m.do(func() error {
		sel := m.seq
		if err := m.ctl(FnSelect, ioa, dcs, nil); err != nil {
			return softErr(err)
		}
		return softErr(m.ctl(FnOperate, ioa, dcs, &sel))
	})
}

// ExecuteWithoutSelect sends OPERATE with no select (a conforming outstation refuses it).
func (m *Master) ExecuteWithoutSelect(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	return m.do(func() error { return softErr(m.ctl(FnOperate, ioa, dcs, nil)) })
}

// A refused control is reported to the caller but does not end the session.
type refusal struct{ error }

func softErr(e error) error {
	if e != nil && len(e.Error()) > 12 && e.Error()[:12] == "dnp3: contro" {
		return refusal{e}
	}
	return e
}
