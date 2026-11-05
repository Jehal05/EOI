package iec101

import (
	"bytes"
	"context"
	"errors"
	"io"
	"math/rand"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gridsovereign/fep/internal/iec104"
)

type memH struct {
	mu   sync.Mutex
	cmds [][3]uint32
	fail bool
}

func (h *memH) Snapshot(ca uint16) []iec104.Point {
	var p []iec104.Point
	for i := uint32(1); i <= 25; i++ {
		p = append(p, iec104.Point{CA: 1, IOA: i, Float: float32(i) * 1.5, TS: time.Now()})
	}
	for i := uint32(100); i < 104; i++ {
		p = append(p, iec104.Point{CA: 1, IOA: i, Status: true, DPI: 2, TS: time.Now()})
	}
	return p
}
func (h *memH) IsStatus(ca uint16, ioa uint32) (bool, bool) {
	if ioa >= 100 && ioa < 104 {
		return true, true
	}
	return false, ioa >= 1 && ioa <= 25
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

func TestFrameRoundTripAndResync(t *testing.T) {
	for _, sz := range []int{1, 2} {
		fs := []Frame{
			{Kind: 'F', Ctrl: 0x5A, Addr: 7, AddrSz: sz},
			{Kind: 'V', Ctrl: 0x73, Addr: 9, AddrSz: sz, Data: []byte{1, 2, 3, 4, 5}},
			{Kind: 'S'},
		}
		var buf bytes.Buffer
		buf.Write([]byte{0x00, 0xFF, 0x13}) // line noise first
		for _, f := range fs {
			b, err := f.Encode()
			if err != nil {
				t.Fatal(err)
			}
			buf.Write(b)
		}
		for _, want := range fs {
			got, err := Read(&buf, sz)
			if err != nil || got.Kind != want.Kind || got.Ctrl != want.Ctrl || got.Addr != want.Addr || !bytes.Equal(got.Data, want.Data) {
				t.Fatalf("sz=%d got %+v err %v want %+v", sz, got, err, want)
			}
		}
	}
}

func TestCorruptFramesAreRejectedNotDelivered(t *testing.T) {
	f := Frame{Kind: 'V', Ctrl: 0x73, Addr: 1, AddrSz: 1, Data: []byte{9, 9, 9}}
	b, _ := f.Encode()
	for i := range b {
		c := append([]byte(nil), b...)
		c[i] ^= 0x01
		good, _ := Frame{Kind: 'F', Ctrl: 0x0B, Addr: 1, AddrSz: 1}.Encode()
		got, err := Read(bytes.NewReader(append(c, good...)), 1)
		if err != nil {
			continue
		}
		if got.Kind == 'V' {
			t.Fatalf("flipped byte %d still delivered a variable frame", i)
		}
	}
}

func TestReadNeverPanicsOnRandomBytes(t *testing.T) {
	r := rand.New(rand.NewSource(1))
	for i := 0; i < 3000; i++ {
		b := make([]byte, r.Intn(300))
		r.Read(b)
		rd := bytes.NewReader(b)
		for {
			if _, err := Read(rd, 1+i%2); err != nil {
				break
			}
		}
	}
}

func TestParamsRoundTripAllSizes(t *testing.T) {
	for _, lc := range []int{1, 2} {
		for _, cot := range []int{1, 2} {
			for _, ca := range []int{1, 2} {
				for _, ioa := range []int{1, 2, 3} {
					p := Params{lc, cot, ca, ioa}
					now := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
					in := []iec104.ASDU{
						{Type: iec104.M_ME_TF_1, Cause: 3, CA: 5, Objects: []iec104.Object{{IOA: 12, Float: 49.97, Time: &now}, {IOA: 13, Float: -3.5, Time: &now}}},
						{Type: iec104.M_DP_NA_1, Cause: 20, CA: 5, Objects: []iec104.Object{{IOA: 200, DPI: 2}}},
						{Type: iec104.C_DC_NA_1, Cause: 6, CA: 5, Objects: []iec104.Object{{IOA: 77, DPI: 1, Select: true}}},
					}
					for _, a := range in {
						b, err := p.Encode(a)
						if err != nil {
							t.Fatalf("%+v: %v", p, err)
						}
						d, err := p.Decode(b)
						if err != nil || d.Type != a.Type || d.CA != a.CA || len(d.Objects) != len(a.Objects) || d.Objects[0].IOA != a.Objects[0].IOA {
							t.Fatalf("%+v round trip: %+v %v", p, d, err)
						}
						if p.COT == 1 && len(b) != 2+1+ca+len(a.Objects)*(ioa+mustLen(a.Type)) {
							t.Fatalf("wrong length for %+v", p)
						}
					}
				}
			}
		}
	}
	if _, err := (Params{1, 2, 1, 1}).Encode(iec104.ASDU{Type: iec104.C_DC_NA_1, Cause: 6, CA: 1, Objects: []iec104.Object{{IOA: 70000}}}); err == nil {
		t.Fatal("IOA that does not fit was encoded")
	}
}

func mustLen(t uint8) int { n, _ := iec104.ObjectLen(t); return n }

func TestDecodeExpandsSequences(t *testing.T) {
	// SQ=1, 3 single-float objects starting at IOA 10: header + one IOA + 3*(5)
	b := []byte{iec104.M_ME_NC_1, 0x83, 20, 0, 1, 0, 10, 0, 0}
	for i := 0; i < 3; i++ {
		b = append(b, 0, 0, 0x80, 0x3F, 0) // 1.0
	}
	a, err := Default.Decode(b)
	if err != nil || len(a.Objects) != 3 || a.Objects[2].IOA != 12 || a.Objects[1].Float != 1 {
		t.Fatalf("%+v %v", a, err)
	}
	if _, err := Default.Decode(b[:len(b)-1]); err == nil {
		t.Fatal("truncated ASDU accepted")
	}
}

// lossy wraps a conn and drops the Nth slave->master write (simulating a corrupted/lost reply).
type lossy struct {
	net.Conn
	n    atomic.Int64
	drop map[int64]bool
}

func (l *lossy) Write(b []byte) (int, error) {
	if l.drop[l.n.Add(1)] {
		return len(b), nil
	}
	return l.Conn.Write(b)
}

type rig struct {
	m   *Master
	s   *Slave
	h   *memH
	got chan iec104.ASDU
	up  chan bool
}

func start(t *testing.T, p Params, drop map[int64]bool) *rig {
	t.Helper()
	cm, cs := net.Pipe()
	h := &memH{}
	s := &Slave{H: h, Link: 3, P: p}
	var sconn io.ReadWriteCloser = cs
	if drop != nil {
		sconn = &lossy{Conn: cs, drop: drop}
	}
	go s.ServeConn(sconn)
	r := &rig{s: s, h: h, got: make(chan iec104.ASDU, 10000), up: make(chan bool, 4)}
	r.m = &Master{Open: func() (io.ReadWriteCloser, error) { return cm, nil }, Link: 3, P: p,
		OnASDU: func(a iec104.ASDU) { r.got <- a }, OnState: func(u bool) { r.up <- u },
		Poll: 20 * time.Millisecond, Timeout: 150 * time.Millisecond, Retries: 4}
	ctx, cancel := context.WithCancel(context.Background())
	go r.m.Run(ctx)
	t.Cleanup(func() { cancel(); cm.Close() })
	select {
	case <-r.up:
	case <-time.After(5 * time.Second):
		t.Fatal("link did not come up")
	}
	return r
}

func (r *rig) collect(t *testing.T, until func(a iec104.ASDU) bool) (n int) {
	t.Helper()
	to := time.After(8 * time.Second)
	for {
		select {
		case a := <-r.got:
			for range a.Objects {
				n++
			}
			if until(a) {
				return
			}
		case <-to:
			t.Fatal("timeout waiting for ASDUs")
		}
	}
}

func TestInterrogationOverTheLink(t *testing.T) {
	for _, p := range []Params{Default, {1, 1, 1, 2}, {2, 2, 2, 3}} {
		r := start(t, p, nil)
		if err := r.m.Interrogate(1); err != nil {
			t.Fatal(err)
		}
		n := r.collect(t, func(a iec104.ASDU) bool { return a.Type == iec104.C_IC_NA_1 && a.Cause == iec104.CotActTerm })
		if n != 25+4+2 { // 25 analogs, 4 status, ActCon + ActTerm
			t.Fatalf("params %+v: %d objects", p, n)
		}
	}
}

func TestSelectBeforeOperateAndRefusals(t *testing.T) {
	r := start(t, Default, nil)
	ctx := context.Background()
	if err := r.m.SelectExecute(ctx, 1, 101, 1); err != nil {
		t.Fatal(err)
	}
	if r.h.n() != 1 {
		t.Fatal("not executed")
	}
	if err := r.m.ExecuteWithoutSelect(ctx, 1, 101, 1); err == nil || r.h.n() != 1 {
		t.Fatal("execute without select was accepted")
	}
	if err := r.m.SelectExecute(ctx, 1, 5, 1); err == nil { // analog point is not controllable
		t.Fatal("command on an analog accepted")
	}
	r.h.fail = true
	if err := r.m.SelectExecute(ctx, 1, 102, 1); err == nil {
		t.Fatal("backend refusal not reported")
	}
}

func TestLostRepliesAreRetriedWithoutDoubleExecution(t *testing.T) {
	// slave writes: 1 reset ack, 2 status, then poll/cmd replies. Drop several in a row.
	r := start(t, Default, map[int64]bool{5: true, 6: true, 9: true, 10: true, 11: true})
	for i := 0; i < 3; i++ {
		if err := r.m.SelectExecute(context.Background(), 1, 100+uint32(i), 1); err != nil {
			t.Fatalf("command %d: %v", i, err)
		}
	}
	if r.h.n() != 3 {
		t.Fatalf("executed %d times for 3 commands (duplicate frames must not re-execute)", r.h.n())
	}
	if r.m.Stats.Retries.Load() == 0 || r.s.Stats.Duplicates.Load() == 0 {
		t.Fatal("test did not exercise retransmission")
	}
}

func TestSpontaneousEventsAreDeliveredInOrder(t *testing.T) {
	r := start(t, Default, nil)
	for i := 0; i < 50; i++ {
		r.s.Publish([]iec104.Point{{CA: 1, IOA: 100, Status: true, DPI: uint8(1 + i%2), TS: time.Now()}})
	}
	seen := 0
	to := time.After(8 * time.Second)
	for seen < 50 {
		select {
		case a := <-r.got:
			if a.Cause == iec104.CotSpont {
				if a.Objects[0].DPI != uint8(1+seen%2) {
					t.Fatalf("event %d out of order", seen)
				}
				seen++
			}
		case <-to:
			t.Fatalf("only %d of 50 events delivered", seen)
		}
	}
}

func TestSlaveIgnoresOtherAddressesAndSurvivesGarbage(t *testing.T) {
	cm, cs := net.Pipe()
	s := &Slave{H: &memH{}, Link: 3}
	go s.ServeConn(cs)
	defer cm.Close()
	other, _ := Frame{Kind: 'F', Ctrl: primary(FcReqStatus, false, false), Addr: 9, AddrSz: 1}.Encode()
	mine, _ := Frame{Kind: 'F', Ctrl: primary(FcReqStatus, false, false), Addr: 3, AddrSz: 1}.Encode()
	rnd := make([]byte, 500)
	rand.New(rand.NewSource(2)).Read(rnd)
	cm.Write(append(append(rnd, other...), mine...))
	cm.SetReadDeadline(time.Now().Add(2 * time.Second))
	f, err := Read(cm, 1)
	if err != nil || f.Addr != 3 || fcOf(f.Ctrl) != FcLinkState {
		t.Fatalf("%+v %v", f, err)
	}
}
