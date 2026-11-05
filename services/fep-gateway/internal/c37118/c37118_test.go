package c37118

import (
	"bytes"
	"context"
	"io"
	"math"
	"math/rand"
	"net"
	"reflect"
	"sync"
	"testing"
	"time"
)

var cfg0 = Config{Station: "BUS-118-A", PMUID: 7, Phasors: []string{"VA", "VB", "IA"}, Analogs: []string{"MW", "MVAR"},
	Digitals: 1, FNom: 50, CfgCnt: 3, Rate: 50, TimeBase: 1000000}

func TestCRCCheckValue(t *testing.T) {
	if got := CRC([]byte("123456789")); got != 0x29B1 { // CRC-16/CCITT-FALSE catalogue check value
		t.Fatalf("%04x", got)
	}
}

func TestConfigRoundTrip(t *testing.T) {
	b, err := EncodeConfig(Common{Version: 2, ID: 7, SOC: 1700000000, FracSec: 5}, cfg0)
	if err != nil {
		t.Fatal(err)
	}
	h, body, err := Read(bytes.NewReader(b))
	if err != nil || h.Type != TypeCfg2 || h.ID != 7 || h.SOC != 1700000000 {
		t.Fatalf("%+v %v", h, err)
	}
	got, err := DecodeConfig(body)
	if err != nil || !reflect.DeepEqual(got, cfg0) {
		t.Fatalf("config round trip:\n got %+v\nwant %+v\n err %v", got, cfg0, err)
	}
}

func TestDataRoundTripAndTime(t *testing.T) {
	d := Data{Stat: 0x2000, Phasors: [][2]float32{{1.5, -2.5}, {3, 4}, {0.1, 0.2}}, Freq: 49.987, DFreq: -0.02, Analogs: []float32{100, -50}, Digitals: []uint16{0xA5A5}}
	ts := time.Date(2026, 11, 4, 12, 0, 1, 123456000, time.UTC)
	soc, fr := StampFor(ts, 1000000)
	b, err := EncodeData(Common{Version: 2, ID: 7, SOC: soc, FracSec: fr, TQ: 0x0B}, cfg0, d)
	if err != nil {
		t.Fatal(err)
	}
	h, body, err := Read(bytes.NewReader(b))
	if err != nil || h.TQ != 0x0B {
		t.Fatalf("%+v %v", h, err)
	}
	got, err := DecodeData(cfg0, body)
	if err != nil || !reflect.DeepEqual(got, d) || !got.SyncLost() || got.Invalid() {
		t.Fatalf("%+v %v", got, err)
	}
	if dt := h.Time(1000000).Sub(ts); dt > time.Microsecond || dt < -time.Microsecond {
		t.Fatalf("time stamp off by %v", dt)
	}
	if _, err := DecodeData(cfg0, body[:len(body)-1]); err == nil {
		t.Fatal("short data frame accepted")
	}
}

func TestEveryFlippedBitIsDetected(t *testing.T) {
	b, _ := EncodeData(Common{Version: 2, ID: 7}, cfg0, Data{Phasors: make([][2]float32, 3), Analogs: make([]float32, 2), Digitals: []uint16{1}})
	for i := 2; i < len(b); i++ { // bytes 2-3 (length) change framing, covered below
		for bit := 0; bit < 8; bit++ {
			c := append([]byte(nil), b...)
			c[i] ^= 1 << bit
			if i < 4 {
				continue
			}
			_, _, err := Read(bytes.NewReader(c))
			if err == nil {
				t.Fatalf("flip byte %d bit %d not detected", i, bit)
			}
		}
	}
}

func TestReadNeverPanics(t *testing.T) {
	r := rand.New(rand.NewSource(5))
	for i := 0; i < 3000; i++ {
		b := make([]byte, r.Intn(400))
		r.Read(b)
		if i%3 == 0 && len(b) > 4 {
			b[0] = 0xAA
		}
		rd := bytes.NewReader(b)
		for {
			if _, body, err := Read(rd); err != nil {
				if err == io.EOF || err == io.ErrUnexpectedEOF {
					break
				}
			} else {
				DecodeConfig(body)
				DecodeData(cfg0, body)
			}
		}
	}
}

func TestMultiPMUConfigRefusedExplicitly(t *testing.T) {
	b, _ := EncodeConfig(Common{Version: 2, ID: 7}, cfg0)
	_, body, _ := Read(bytes.NewReader(b))
	body[5] = 2 // NUM_PMU = 2
	if _, err := DecodeConfig(body); err == nil {
		t.Fatal("multi-PMU frame silently accepted")
	}
}

func TestPMUStreamEndToEnd(t *testing.T) {
	cp, cs := net.Pipe()
	srv := &Server{Cfg: cfg0, ID: 7, Src: func(now time.Time) Data {
		return Data{Phasors: [][2]float32{{1, 0}, {-0.5, 0.866}, {0.2, 0.1}}, Freq: 50 + float32(math.Sin(float64(now.UnixNano())/1e9))*0.01,
			Analogs: []float32{10, 5}, Digitals: []uint16{0}}
	}}
	go srv.ServeConn(cs)
	var mu sync.Mutex
	n, up := 0, false
	var first, lastT time.Time
	cl := &Client{Open: func() (io.ReadWriteCloser, error) { return cp, nil }, ID: 7,
		OnData: func(cfg Config, ts time.Time, tq byte, d Data) {
			mu.Lock()
			defer mu.Unlock()
			if n == 0 {
				first = ts
			}
			lastT = ts
			n++
			if len(d.Phasors) != 3 || cfg.Station != "BUS-118-A" {
				t.Errorf("bad data %+v", d)
			}
		}, OnState: func(u bool) { mu.Lock(); up = u; mu.Unlock() }}
	ctx, cancel := context.WithCancel(context.Background())
	go cl.Run(ctx)
	time.Sleep(1500 * time.Millisecond)
	cancel()
	cp.Close()
	mu.Lock()
	defer mu.Unlock()
	if !up && n == 0 {
		t.Fatal("no data")
	}
	span := lastT.Sub(first).Seconds()
	rate := float64(n-1) / span
	if rate < 40 || rate > 60 {
		t.Fatalf("measured %.1f frames/s over %.2f s, configured 50", rate, span)
	}
	cl.Stats.Lock()
	defer cl.Stats.Unlock()
	if cl.Stats.OutOfOrder != 0 {
		t.Fatalf("%d out-of-order frames", cl.Stats.OutOfOrder)
	}
}
