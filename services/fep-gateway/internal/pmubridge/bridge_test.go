package pmubridge

import (
	"context"
	"io"
	"math"
	"net"
	"sync"
	"testing"
	"time"

	"gridsovereign/fep/internal/c37118"
	"gridsovereign/fep/internal/iec104"
)

func lookup(prefix string, phasors []string, analogs int) Addr {
	m := map[string]uint32{prefix + ".F": 1, prefix + ".DF": 2}
	n := uint32(10)
	for _, p := range phasors {
		m[prefix+"."+p+".MAG"], m[prefix+"."+p+".ANG"] = n, n+1
		n += 2
	}
	for j := 0; j < analogs; j++ {
		m[prefix+"."+"A"+string(rune('1'+j))] = 100 + uint32(j)
	}
	return func(tag string) (uint16, uint32, bool) { i, ok := m[tag]; return 7, i, ok }
}

func TestConvertRectangularAndPolar(t *testing.T) {
	lk := lookup("P", []string{"V1", "I1"}, 1)
	now := time.Unix(1700000000, 0)
	cfg := c37118.Config{Station: "S", Phasors: []string{"V1", "I1"}, Analogs: []string{"x"}, Polar: false}
	d := c37118.Data{Phasors: [][2]float32{{3, 4}, {-1, 0}}, Freq: 49.98, DFreq: -0.02, Analogs: []float32{12.5}}
	objs, miss := Convert("P", lk, cfg, now, d)
	if miss != 0 {
		t.Fatalf("missing %d", miss)
	}
	got := map[uint32]float32{}
	for _, o := range objs[7] {
		got[o.IOA] = o.Float
		if o.Quality != 0 || o.Time == nil || !o.Time.Equal(now) {
			t.Fatalf("object %+v", o)
		}
	}
	chk := func(ioa uint32, want, tol float64) {
		if math.Abs(float64(got[ioa])-want) > tol {
			t.Errorf("IOA %d = %v want %v", ioa, got[ioa], want)
		}
	}
	chk(10, 5, 1e-5)                            // |3+4j|
	chk(11, math.Atan2(4, 3)*180/math.Pi, 1e-4) // 53.1301 deg
	chk(12, 1, 1e-6)                            // |-1|
	chk(13, 180, 1e-4)                          // angle of -1 is 180 deg
	chk(1, 49.98, 1e-4)
	chk(2, -0.02, 1e-6)
	chk(100, 12.5, 1e-6)
	cfg.Polar = true
	d.Phasors = [][2]float32{{231.0, float32(math.Pi / 6)}, {1, -float32(math.Pi / 2)}}
	objs, _ = Convert("P", lk, cfg, now, d)
	got = map[uint32]float32{}
	for _, o := range objs[7] {
		got[o.IOA] = o.Float
	}
	chk(10, 231, 1e-3)
	chk(11, 30, 1e-4) // radians -> degrees
	chk(13, -90, 1e-4)
}

func TestQualityAndMissing(t *testing.T) {
	lk := lookup("P", []string{"V1"}, 0)
	cfg := c37118.Config{Phasors: []string{"V1", "V2"}}
	now := time.Now()
	d := c37118.Data{Stat: 0x8000, Phasors: [][2]float32{{1, 0}, {1, 0}}, Freq: 50}
	objs, miss := Convert("P", lk, cfg, now, d)
	if miss != 2 {
		t.Fatalf("expected V2.MAG/V2.ANG missing, got %d", miss)
	}
	for _, o := range objs[7] {
		if o.Quality&iec104.QInvalid == 0 {
			t.Fatalf("invalid frame must carry the invalid bit: %+v", o)
		}
	}
	objs, _ = Convert("P", lk, cfg, now, c37118.Data{Stat: 0x2000, Phasors: [][2]float32{{1, 0}}, Freq: 50})
	if objs[7][0].Quality&iec104.QNotTopical == 0 || objs[7][0].Quality&iec104.QInvalid != 0 {
		t.Fatalf("sync lost -> not topical only: %+v", objs[7][0])
	}
	objs, _ = Convert("P", lk, cfg, now, c37118.Data{Phasors: [][2]float32{{float32(math.NaN()), 0}}, Freq: float32(math.Inf(1))})
	for _, o := range objs[7] {
		if (o.IOA == 10 || o.IOA == 1) && (o.Quality&iec104.QInvalid == 0 || o.Float != 0) {
			t.Fatalf("NaN/Inf must be flagged invalid and zeroed: %+v", o)
		}
	}
}

func TestCheckRefusesMismatch(t *testing.T) {
	cfg := c37118.Config{Station: "S", Phasors: []string{"V1", "V2"}, Analogs: []string{"a"}}
	if err := Check("P", lookup("P", []string{"V1", "V2"}, 1), cfg); err != nil {
		t.Fatal(err)
	}
	if err := Check("P", lookup("P", []string{"V1"}, 1), cfg); err == nil {
		t.Fatal("PMU announcing a channel with no point must be refused")
	}
}

func TestStreamThroughBridge(t *testing.T) {
	cfg := c37118.Config{Station: "BUS26", PMUID: 7, PhasorsF: true, Polar: false, Phasors: []string{"V1"}, Analogs: []string{"a"}, Digitals: 1,
		FNom: 50, Rate: 50, TimeBase: 1000000}
	cp, cs := net.Pipe()
	srv := &c37118.Server{Cfg: cfg, ID: 7, Src: func(time.Time) c37118.Data {
		return c37118.Data{Phasors: [][2]float32{{3, 4}}, Freq: 50.01, DFreq: 0.001, Analogs: []float32{9}, Digitals: []uint16{0}}
	}}
	go srv.ServeConn(cs)
	var mu sync.Mutex
	var asdus []iec104.ASDU
	var upv bool
	b := &Bridge{Open: func() (io.ReadWriteCloser, error) { return cp, nil }, IDCode: 7, Prefix: "P", Lookup: lookup("P", []string{"V1"}, 1), Every: 250 * time.Millisecond,
		OnASDU: func(a iec104.ASDU) { mu.Lock(); asdus = append(asdus, a); mu.Unlock() }, OnState: func(u bool) { mu.Lock(); upv = u; mu.Unlock() }}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- b.Run(ctx) }()
	time.Sleep(1700 * time.Millisecond)
	cancel()
	cp.Close()
	<-done
	mu.Lock()
	defer mu.Unlock()
	_ = upv
	b.Stats.Lock()
	fw, sk := b.Stats.Forwarded, b.Stats.Skipped
	b.Stats.Unlock()
	if fw < 4 || fw > 8 {
		t.Fatalf("forwarded %d windows in 1.7 s at 4/s", fw)
	}
	if sk < 5*fw { // 50 fps / 4 per second = ~12 frames skipped per forwarded one; allow scheduler slack
		t.Fatalf("expected the 50 fps stream to be thinned: forwarded %d skipped %d", fw, sk)
	}
	if len(asdus) == 0 || asdus[0].Type != iec104.M_ME_NC_1 || asdus[0].CA != 7 || len(asdus[0].Objects) != 5 {
		t.Fatalf("unexpected output %+v", asdus)
	}
	if err := b.SelectExecute(context.Background(), 7, 1, 1); err == nil {
		t.Fatal("PMU commands must be refused")
	}
	b.OnASDU = func(a iec104.ASDU) { asdus = append(asdus, a) }
	n := len(asdus)
	_ = b.Interrogate(7)
	if len(asdus) != n+1 || asdus[n].Cause != iec104.CotInrogen || len(asdus[n].Objects) != 5 {
		t.Fatalf("interrogation must replay the last values: %+v", asdus[n:])
	}
}

func TestStreamWithMismatchedPointListStops(t *testing.T) {
	cfg := c37118.Config{Station: "BUS26", PMUID: 7, PhasorsF: true, Phasors: []string{"V1"}, Digitals: 1, FNom: 50, Rate: 50, TimeBase: 1000000}
	cp, cs := net.Pipe()
	srv := &c37118.Server{Cfg: cfg, ID: 7, Src: func(time.Time) c37118.Data {
		return c37118.Data{Phasors: [][2]float32{{1, 0}}, Freq: 50, Digitals: []uint16{0}}
	}}
	go srv.ServeConn(cs)
	got := 0
	b := &Bridge{Open: func() (io.ReadWriteCloser, error) { return cp, nil }, IDCode: 7, Prefix: "P", Lookup: lookup("P", nil, 0), Every: time.Millisecond, Timeout: 500 * time.Millisecond,
		OnASDU: func(iec104.ASDU) { got++ }}
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	err := b.Run(ctx)
	if err == nil || got != 0 {
		t.Fatalf("a PMU whose channels are not in the point list must forward nothing and report why (err=%v forwarded=%d)", err, got)
	}
}
