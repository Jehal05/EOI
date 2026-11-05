// Package pmubridge presents one C37.118 PMU stream to the FEP as an ordinary telemetry source: every frame is turned into
// IEC 104-style measured values (M_ME_NC_1) addressed through the model's point list (tags <prefix>.F, .DF, .<phasor>.MAG/.ANG, .A<n>),
// so the rest of the chain (FEP -> SCADA core -> alarms, trends) treats synchrophasors like any other analog.
// The stream is read-only: commands are refused. Values are forwarded at most once per Every (default 1 s), the last frame
// in the window wins; frames flagged invalid / PMU error / sync lost carry quality bits instead of being dropped.
package pmubridge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"sync"
	"time"

	"gridsovereign/fep/internal/c37118"
	"gridsovereign/fep/internal/iec104"
)

// Addr resolves a tag of the model's point list to its (CA, IOA).
type Addr func(tag string) (ca uint16, ioa uint32, ok bool)

type Bridge struct {
	Open    func() (io.ReadWriteCloser, error)
	IDCode  uint16
	Prefix  string
	Lookup  Addr
	OnASDU  func(iec104.ASDU)
	OnState func(up bool)
	Every   time.Duration // forwarding interval (default 1 s)
	Timeout time.Duration
	Stats   struct {
		sync.Mutex
		Forwarded, Skipped, Missing int64
		CfgMismatch                 string
	}
	mu    sync.Mutex
	last  map[[2]uint32]float32 // latest forwarded value per (CA,IOA), served on interrogation
	lastQ map[[2]uint32]uint8
	lastT time.Time
}

// Convert maps one frame to objects per CA. Pure function: exported for tests.
func Convert(prefix string, lk Addr, cfg c37118.Config, t time.Time, d c37118.Data) (map[uint16][]iec104.Object, int) {
	q := uint8(0)
	if d.Invalid() || d.PMUError() {
		q |= iec104.QInvalid
	}
	if d.SyncLost() {
		q |= iec104.QNotTopical // time-quality: measured against a lost sync reference
	}
	out := map[uint16][]iec104.Object{}
	missing := 0
	tt := t
	add := func(tag string, v float64) {
		ca, ioa, ok := lk(tag)
		if !ok {
			missing++
			return
		}
		if math.IsNaN(v) || math.IsInf(v, 0) {
			out[ca] = append(out[ca], iec104.Object{IOA: ioa, Float: 0, Quality: q | iec104.QInvalid, Time: &tt})
			return
		}
		out[ca] = append(out[ca], iec104.Object{IOA: ioa, Float: float32(v), Quality: q, Time: &tt})
	}
	add(prefix+".F", float64(d.Freq))
	add(prefix+".DF", float64(d.DFreq))
	for i, name := range cfg.Phasors {
		if i >= len(d.Phasors) {
			break
		}
		a, b := float64(d.Phasors[i][0]), float64(d.Phasors[i][1])
		var mag, ang float64
		if cfg.Polar {
			mag, ang = a, b*180/math.Pi
		} else {
			mag, ang = math.Hypot(a, b), math.Atan2(b, a)*180/math.Pi
		}
		add(fmt.Sprintf("%s.%s.MAG", prefix, name), mag)
		add(fmt.Sprintf("%s.%s.ANG", prefix, name), ang)
	}
	for j, v := range d.Analogs {
		add(fmt.Sprintf("%s.A%d", prefix, j+1), float64(v))
	}
	return out, missing
}

// Check compares the PMU's own configuration with the point list: every phasor and analog it announces must have points.
func Check(prefix string, lk Addr, cfg c37118.Config) error {
	var bad []string
	need := []string{prefix + ".F", prefix + ".DF"}
	for _, p := range cfg.Phasors {
		need = append(need, fmt.Sprintf("%s.%s.MAG", prefix, p), fmt.Sprintf("%s.%s.ANG", prefix, p))
	}
	for j := range cfg.Analogs {
		need = append(need, fmt.Sprintf("%s.A%d", prefix, j+1))
	}
	for _, t := range need {
		if _, _, ok := lk(t); !ok {
			bad = append(bad, t)
		}
	}
	if len(bad) > 0 {
		return fmt.Errorf("PMU %q announces channels with no point in the model's point list: %v", cfg.Station, bad)
	}
	return nil
}

func (b *Bridge) Run(ctx context.Context) error {
	if b.Every == 0 {
		b.Every = time.Second
	}
	var lastFwd time.Time
	var mismatch bool
	cl := &c37118.Client{Open: b.Open, ID: b.IDCode, Timeout: b.Timeout, OnState: b.OnState}
	cl.OnData = func(cfg c37118.Config, t time.Time, tq byte, d c37118.Data) {
		if !mismatch {
			if err := Check(b.Prefix, b.Lookup, cfg); err != nil {
				mismatch = true
				b.Stats.Lock()
				b.Stats.CfgMismatch = err.Error()
				b.Stats.Unlock()
			}
		}
		if mismatch { // refuse to guess: the configuration and the point list disagree
			return
		}
		now := time.Now()
		if !lastFwd.IsZero() && now.Sub(lastFwd) < b.Every {
			b.Stats.Lock()
			b.Stats.Skipped++
			b.Stats.Unlock()
			return
		}
		lastFwd = now
		objs, miss := Convert(b.Prefix, b.Lookup, cfg, t, d)
		b.mu.Lock()
		if b.last == nil {
			b.last, b.lastQ = map[[2]uint32]float32{}, map[[2]uint32]uint8{}
		}
		for ca, os := range objs {
			for _, o := range os {
				b.last[[2]uint32{uint32(ca), o.IOA}], b.lastQ[[2]uint32{uint32(ca), o.IOA}] = o.Float, o.Quality
			}
		}
		b.lastT = t
		b.mu.Unlock()
		b.Stats.Lock()
		b.Stats.Forwarded++
		b.Stats.Missing += int64(miss)
		b.Stats.Unlock()
		if b.OnASDU != nil {
			for ca, os := range objs {
				b.OnASDU(iec104.ASDU{Type: iec104.M_ME_NC_1, Cause: iec104.CotSpont, CA: ca, Objects: os})
			}
		}
	}
	err := cl.Run(ctx)
	b.Stats.Lock()
	mm := b.Stats.CfgMismatch
	b.Stats.Unlock()
	if mm != "" {
		return errors.New(mm)
	}
	return err
}

// Interrogate re-sends the last forwarded values with the interrogation cause so a restarted consumer gets a full picture.
func (b *Bridge) Interrogate(ca uint16) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	var os []iec104.Object
	for k, v := range b.last {
		if uint16(k[0]) == ca {
			t := b.lastT
			os = append(os, iec104.Object{IOA: k[1], Float: v, Quality: b.lastQ[k], Time: &t})
		}
	}
	if len(os) > 0 && b.OnASDU != nil {
		b.OnASDU(iec104.ASDU{Type: iec104.M_ME_NC_1, Cause: iec104.CotInrogen, CA: ca, Objects: os})
	}
	return nil
}

func (b *Bridge) SelectExecute(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	return errors.New("C37.118 PMU streams are read-only: commands are not supported")
}
