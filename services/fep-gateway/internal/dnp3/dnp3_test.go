package dnp3

import (
	"bytes"
	"context"
	"errors"
	"io"
	"math/rand"
	"net"
	"sync"
	"testing"
	"time"

	"gridsovereign/fep/internal/iec104"
)

type memH struct {
	mu   sync.Mutex
	cmds [][3]uint32
	fail bool
	nAna int
}

func (h *memH) Snapshot(ca uint16) []iec104.Point {
	var p []iec104.Point
	n := h.nAna
	if n == 0 {
		n = 25
	}
	for i := 1; i <= n; i++ {
		p = append(p, iec104.Point{CA: 1, IOA: uint32(i), Float: float32(i) * 1.5, TS: time.Now()})
	}
	for i := uint32(100); i < 104; i++ {
		p = append(p, iec104.Point{CA: 1, IOA: i, Status: true, DPI: 2, TS: time.Now()})
	}
	p = append(p, iec104.Point{CA: 1, IOA: 104, Status: true, DPI: 1, Quality: iec104.QNotTopical, TS: time.Now()})
	return p
}
func (h *memH) IsStatus(ca uint16, ioa uint32) (bool, bool) {
	if ioa >= 100 && ioa <= 104 {
		return true, true
	}
	return false, ioa >= 1 && ioa <= 5000
}
func (h *memH) Command(ca uint16, ioa uint32, d uint8) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.fail {
		return errors.New("interlock")
	}
	h.cmds = append(h.cmds, [3]uint32{uint32(ca), ioa, uint32(d)})
	return nil
}
func (h *memH) n() int { h.mu.Lock(); defer h.mu.Unlock(); return len(h.cmds) }

func TestCRCCheckValue(t *testing.T) {
	// CRC-16/DNP catalogue check value (stored complemented, as DNP3 transmits it)
	if got := CRC([]byte("123456789")); got != 0xEA82 {
		t.Fatalf("CRC = %04x, want ea82", got)
	}
}

func TestFrameRoundTripAllLengths(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	for n := 0; n <= 250; n++ {
		d := make([]byte, n)
		r.Read(d)
		f := Frame{Ctrl: 0xC4, Dst: 0x1234, Src: 0xABCD, Data: d}
		b, err := f.Encode()
		if err != nil {
			t.Fatal(err)
		}
		g, err := ReadFrame(bytes.NewReader(append([]byte{0, 1, 5, 0x64, 9}, b...))) // noise, including a false start
		if err != nil || g.Dst != f.Dst || g.Src != f.Src || g.Ctrl != f.Ctrl || !bytes.Equal(g.Data, d) {
			t.Fatalf("n=%d: %+v %v", n, g, err)
		}
	}
	if _, err := (Frame{Data: make([]byte, 251)}).Encode(); err == nil {
		t.Fatal("oversize frame encoded")
	}
}

func TestEveryFlippedBitIsDetected(t *testing.T) {
	f := Frame{Ctrl: 0xC4, Dst: 1, Src: 2, Data: bytes.Repeat([]byte{0x5A}, 40)}
	b, _ := f.Encode()
	good, _ := Frame{Ctrl: 0xC4, Dst: 1, Src: 2, Data: []byte{7}}.Encode()
	for i := range b {
		for bit := 0; bit < 8; bit++ {
			c := append([]byte(nil), b...)
			c[i] ^= 1 << bit
			g, err := ReadFrame(bytes.NewReader(append(c, good...)))
			if err == nil && len(g.Data) == 40 {
				t.Fatalf("flip byte %d bit %d delivered the corrupted frame", i, bit)
			}
		}
	}
}

func TestRandomBytesNeverPanic(t *testing.T) {
	r := rand.New(rand.NewSource(3))
	for i := 0; i < 3000; i++ {
		b := make([]byte, r.Intn(400))
		r.Read(b)
		rd := bytes.NewReader(b)
		for {
			if _, err := ReadFrame(rd); err != nil {
				break
			}
		}
		m := &Master{CA: 1, OnASDU: func(iec104.ASDU) {}}
		m.decode(b, 20)
		if len(b) > 3 {
			parseQual(b[0], b[1:])
		}
	}
}

type rig struct {
	m   *Master
	o   *Outstation
	h   *memH
	got chan iec104.ASDU
	up  chan bool
}

func start(t *testing.T, nAna int) *rig {
	t.Helper()
	cm, co := net.Pipe()
	h := &memH{nAna: nAna}
	o := &Outstation{H: h, CA: 1, Addr: 10}
	go o.ServeConn(co)
	r := &rig{o: o, h: h, got: make(chan iec104.ASDU, 100000), up: make(chan bool, 4)}
	r.m = &Master{Open: func() (io.ReadWriteCloser, error) { return cm, nil }, Local: 1, Remote: 10, CA: 1,
		OnASDU: func(a iec104.ASDU) { r.got <- a }, OnState: func(u bool) { r.up <- u }, Poll: 20 * time.Millisecond, Timeout: 2 * time.Second}
	ctx, cancel := context.WithCancel(context.Background())
	go r.m.Run(ctx)
	t.Cleanup(func() { cancel(); cm.Close() })
	select {
	case <-r.up:
	case <-time.After(5 * time.Second):
		t.Fatal("not up")
	}
	return r
}

func (r *rig) count(t *testing.T, want int, f func(iec104.ASDU) bool) {
	t.Helper()
	n := 0
	to := time.After(8 * time.Second)
	for n < want {
		select {
		case a := <-r.got:
			if f(a) {
				n += len(a.Objects)
			}
		case <-to:
			t.Fatalf("got %d of %d", n, want)
		}
	}
}

func TestIntegrityPollAndQualityMapping(t *testing.T) {
	r := start(t, 0)
	var sawStale bool
	n := map[uint8]int{}
	to := time.After(5 * time.Second)
	for n[iec104.M_ME_NC_1] < 25 || n[iec104.M_DP_NA_1] < 5 {
		select {
		case a := <-r.got:
			n[a.Type] += len(a.Objects)
			for _, o := range a.Objects {
				if a.Type == iec104.M_DP_NA_1 && o.IOA == 104 {
					sawStale = o.Quality&iec104.QNotTopical != 0 && o.DPI == iec104.DPIOff
				}
				if a.Type == iec104.M_ME_NC_1 && o.Float != float32(o.IOA)*1.5 {
					t.Fatalf("value of %d: %v", o.IOA, o.Float)
				}
			}
		case <-to:
			t.Fatalf("incomplete integrity data %v", n)
		}
	}
	if !sawStale {
		t.Fatal("stale quality and state did not survive the mapping")
	}
}

func TestMultiFragmentIntegrity(t *testing.T) {
	r := start(t, 1200) // ~11 kB of analog objects: several confirmed fragments
	r.count(t, 1200, func(a iec104.ASDU) bool { return a.Type == iec104.M_ME_NC_1 })
	if r.m.Stats.Confirms.Load() < 3 {
		t.Fatalf("expected several confirmed fragments, got %d", r.m.Stats.Confirms.Load())
	}
}

func TestEventsDeliveredOnceInOrderWithSourceTime(t *testing.T) {
	r := start(t, 0)
	r.count(t, 30, func(a iec104.ASDU) bool { return a.Cause == iec104.CotInrogen })
	t0 := time.Now().Add(-time.Hour).Truncate(time.Millisecond)
	for i := 0; i < 40; i++ {
		r.o.Publish([]iec104.Point{{CA: 1, IOA: 101, Status: true, DPI: uint8(1 + i%2), TS: t0.Add(time.Duration(i) * time.Second)}})
	}
	r.o.Publish([]iec104.Point{{CA: 1, IOA: 7, Float: 42.5, TS: t0}, {CA: 2, IOA: 7, Float: 1, TS: t0}}) // CA 2 is not ours
	seen, ana := 0, 0
	to := time.After(8 * time.Second)
	for seen < 40 || ana < 1 {
		select {
		case a := <-r.got:
			if a.Cause != iec104.CotSpont {
				continue
			}
			for _, o := range a.Objects {
				if a.Type == iec104.M_DP_TB_1 {
					if o.DPI != uint8(1+seen%2) || o.Time == nil || !o.Time.Equal(t0.Add(time.Duration(seen)*time.Second)) {
						t.Fatalf("event %d wrong: %+v", seen, o)
					}
					seen++
				} else if a.Type == iec104.M_ME_TF_1 && o.IOA == 7 && o.Float == 42.5 {
					ana++
				}
			}
		case <-to:
			t.Fatalf("events %d/40 analog %d/1", seen, ana)
		}
	}
	time.Sleep(200 * time.Millisecond)
	select {
	case a := <-r.got:
		if a.Cause == iec104.CotSpont {
			t.Fatalf("event delivered twice: %+v", a)
		}
	default:
	}
}

func TestSelectBeforeOperate(t *testing.T) {
	r := start(t, 0)
	ctx := context.Background()
	if err := r.m.SelectExecute(ctx, 1, 101, 1); err != nil || r.h.n() != 1 || r.h.cmds[0] != [3]uint32{1, 101, 1} {
		t.Fatalf("SBO failed: %v %v", err, r.h.cmds)
	}
	if err := r.m.ExecuteWithoutSelect(ctx, 1, 101, 1); err == nil || r.h.n() != 1 {
		t.Fatal("operate without select was executed")
	}
	if err := r.m.SelectExecute(ctx, 1, 5, 1); err == nil { // analog
		t.Fatal("control of an analog accepted")
	}
	r.h.fail = true
	if err := r.m.SelectExecute(ctx, 1, 102, 2); err == nil {
		t.Fatal("backend refusal not reported")
	}
	r.h.fail = false
	if err := r.m.SelectExecute(ctx, 1, 103, 2); err != nil { // the link survived the refusals
		t.Fatalf("session broken after refusals: %v", err)
	}
	if r.h.n() != 2 {
		t.Fatalf("executed %d", r.h.n())
	}
}

func TestOperateMustFollowSelectSequence(t *testing.T) {
	r := start(t, 0)
	// select, then a different request in between, then operate with a stale sequence
	err := r.m.do(func() error {
		if e := r.m.ctl(FnSelect, 100, 1, nil); e != nil {
			return e
		}
		r.m.exchange(FnRead, events, 0) // consumes a sequence number
		return softErr(r.m.ctl(FnOperate, 100, 1, nil))
	})
	if err == nil || r.h.n() != 0 {
		t.Fatalf("operate with the wrong sequence executed (err=%v)", err)
	}
}

func TestDirectOperateIsRefused(t *testing.T) {
	r := start(t, 0)
	err := r.m.do(func() error {
		_, iin, e := r.m.exchange(FnDirectOp, crob(101, 1), 0)
		if e != nil {
			return e
		}
		if iin&IINNoFunc == 0 {
			t.Error("DIRECT OPERATE not flagged unsupported")
		}
		return nil
	})
	if err != nil || r.h.n() != 0 {
		t.Fatalf("direct operate executed or failed: %v", err)
	}
}

func TestWrongAddressIsIgnored(t *testing.T) {
	cm, co := net.Pipe()
	o := &Outstation{H: &memH{}, CA: 1, Addr: 10}
	go o.ServeConn(co)
	defer cm.Close()
	tr := &transport{rw: cm, local: 1, peer: 99, dir: ctrlDIR, rseq: -1}
	tr.send(buildMsg(header{fir: true, fin: true, fn: FnRead}, integrity))
	cm.SetReadDeadline(time.Now().Add(300 * time.Millisecond))
	if _, err := ReadFrame(cm); err == nil {
		t.Fatal("outstation answered a frame addressed to another outstation")
	}
}
