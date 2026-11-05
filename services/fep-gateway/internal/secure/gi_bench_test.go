package secure

import (
	"context"
	"crypto/tls"
	"net"
	"sync/atomic"
	"testing"
	"time"

	"gridsovereign/fep/internal/iec104"
)

// A tiled ~1k-bus model has about 14.9k telemetry points (tools/bench_logbook.py: 14,868 points, 1,062 substations); the
// benchmark interrogates a single outstation holding that many analogs and measures the time until every value arrived.
const giPoints = 14868

type giH struct{ pts []iec104.Point }

func newGiH(n int) *giH {
	h := &giH{}
	for i := 0; i < n; i++ {
		h.pts = append(h.pts, iec104.Point{CA: 1, IOA: uint32(1001 + i), Float: float32(i) * 0.5, TS: time.Now()})
	}
	return h
}
func (h *giH) Snapshot(ca uint16) []iec104.Point    { return h.pts }
func (h *giH) IsStatus(uint16, uint32) (bool, bool) { return false, true }
func (h *giH) Command(uint16, uint32, uint8) error  { return nil }

func giRig(b *testing.B, useTLS bool) (*iec104.Client, *atomic.Int64, func()) {
	var sc, cc *tls.Config
	if useTLS {
		t := &testing.T{}
		sc, cc = profiles(t, newEnv(t), "fep", Profile{})
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		b.Fatal(err)
	}
	if sc != nil {
		ln = tls.NewListener(ln, sc)
	}
	srv := &iec104.Server{H: newGiH(giPoints)}
	go srv.Serve(ln)
	var n atomic.Int64
	up := make(chan bool, 4)
	cl := &iec104.Client{Addr: ln.Addr().String(), TLS: cc, OnASDU: func(a iec104.ASDU) {
		if a.Cause == iec104.CotInrogen {
			n.Add(int64(len(a.Objects)))
		}
	}, OnState: func(u bool) {
		select {
		case up <- u:
		default:
		}
	}}
	ctx, cancel := context.WithCancel(context.Background())
	go cl.Run(ctx)
	select {
	case <-up:
	case <-time.After(10 * time.Second):
		b.Fatal("link did not come up")
	}
	return cl, &n, func() { cancel(); srv.Close() }
}

func benchGI(b *testing.B, useTLS bool) {
	cl, n, done := giRig(b, useTLS)
	defer done()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		n.Store(0)
		if err := cl.Interrogate(1); err != nil {
			b.Fatal(err)
		}
		for t0 := time.Now(); n.Load() < giPoints; {
			if time.Since(t0) > 30*time.Second {
				b.Fatalf("only %d of %d points arrived", n.Load(), giPoints)
			}
			time.Sleep(100 * time.Microsecond)
		}
	}
	b.ReportMetric(float64(giPoints), "points/op")
}

func BenchmarkGI15kPlain(b *testing.B)       { benchGI(b, false) }
func BenchmarkGI15kMutualTLS13(b *testing.B) { benchGI(b, true) }

func BenchmarkConnectPlain(b *testing.B) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	defer ln.Close()
	for i := 0; i < b.N; i++ {
		c, err := net.Dial("tcp", ln.Addr().String())
		if err != nil {
			b.Fatal(err)
		}
		c.Close()
	}
}

func BenchmarkHandshakeMutualTLS13(b *testing.B) {
	t := &testing.T{}
	sc, cc := profiles(t, newEnv(t), "fep", Profile{})
	ln, err := tls.Listen("tcp", "127.0.0.1:0", sc)
	if err != nil {
		b.Fatal(err)
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { c.(*tls.Conn).Handshake(); c.Close() }()
		}
	}()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		c, err := tls.Dial("tcp", ln.Addr().String(), cc)
		if err != nil {
			b.Fatal(err)
		}
		c.Close()
	}
}
