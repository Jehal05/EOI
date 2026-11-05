package dnp3

import (
	"encoding/binary"
	"io"
	"log/slog"
	"net"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"gridsovereign/fep/internal/iec104"
)

const (
	evCap       = 10000
	selectTTL   = 10 * time.Second
	confirmWait = 3 * time.Second
)

type event struct {
	status bool
	ioa    uint32
	dpi    uint8
	q      uint8
	f      float32
	t      time.Time
}

type selEntry struct {
	dcs uint8
	seq uint8
	exp time.Time
}

// Outstation serves one common address (CA) of an iec104.Handler as a DNP3 outstation. The DNP3 point
// index equals the IOA. Class 1 = binary (status) events, class 2 = analog events, class 3 unused.
type Outstation struct {
	H    iec104.Handler
	CA   uint16
	Addr uint16
	Log  *slog.Logger

	Stats struct{ Requests, Dropped, Rejected atomic.Int64 }

	mu       sync.Mutex
	ev1, ev2 []event
	overflow bool
	restart  bool
	started  bool
	sel      map[uint32]selEntry
}

func (o *Outstation) log() *slog.Logger {
	if o.Log != nil {
		return o.Log
	}
	return slog.Default()
}

func (o *Outstation) init() {
	o.mu.Lock()
	if !o.started {
		o.started, o.restart, o.sel = true, true, map[uint32]selEntry{}
	}
	o.mu.Unlock()
}

// Publish queues change events.
func (o *Outstation) Publish(pts []iec104.Point) {
	o.init()
	o.mu.Lock()
	defer o.mu.Unlock()
	for _, p := range pts {
		if p.CA != o.CA {
			continue
		}
		e := event{status: p.Status, ioa: p.IOA, dpi: p.DPI, q: p.Quality, f: p.Float, t: p.TS}
		q := &o.ev2
		if p.Status {
			q = &o.ev1
		}
		if len(*q) >= evCap {
			*q = (*q)[1:]
			o.overflow = true
			o.Stats.Dropped.Add(1)
		}
		*q = append(*q, e)
	}
}

// Serve accepts connections until the listener closes.
func (o *Outstation) Serve(ln net.Listener) error {
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go o.ServeConn(c)
	}
}

func setDeadline(rw io.ReadWriter, d time.Duration) {
	if c, ok := rw.(interface{ SetReadDeadline(time.Time) error }); ok {
		if d == 0 {
			c.SetReadDeadline(time.Time{})
		} else {
			c.SetReadDeadline(time.Now().Add(d))
		}
	}
}

// ServeConn runs the outstation on one stream.
func (o *Outstation) ServeConn(rwc io.ReadWriteCloser) error {
	o.init()
	defer rwc.Close()
	t := &transport{rw: rwc, local: o.Addr, peer: 0xFFFF, rseq: -1}
	for {
		setDeadline(rwc, 0)
		msg, src, err := t.recv()
		if err != nil {
			return err
		}
		o.Stats.Requests.Add(1)
		h, body, err := parseHeader(msg)
		if err != nil || h.fn >= 129 {
			continue
		}
		t.peer = src
		o.request(t, rwc, h, body)
	}
}

func (o *Outstation) iin() uint16 {
	var v uint16
	if len(o.ev1) > 0 {
		v |= IINClass1
	}
	if len(o.ev2) > 0 {
		v |= IINClass2
	}
	if o.restart {
		v |= IINRestart
	}
	if o.overflow {
		v |= IINOverflow
	}
	return v
}

func (o *Outstation) request(t *transport, rw io.ReadWriter, h header, body []byte) {
	if !h.fir || !h.fin {
		o.reply(t, h, IINNoFunc, nil)
		return
	}
	switch h.fn {
	case FnConfirm:
	case FnRead:
		o.read(t, rw, h, body)
	case FnWrite:
		o.write(t, h, body)
	case FnSelect, FnOperate:
		o.control(t, h, body)
	default: // includes DIRECT_OPERATE: this outstation only allows select-before-operate
		o.Stats.Rejected.Add(1)
		o.reply(t, h, IINNoFunc, nil)
	}
}

func (o *Outstation) reply(t *transport, h header, extra uint16, body []byte) {
	o.mu.Lock()
	iin := o.iin() | extra
	o.mu.Unlock()
	t.send(buildMsg(header{fir: true, fin: true, seq: h.seq, fn: FnResponse, iin: iin}, body))
}

type item struct {
	g, v byte
	data []byte
}

func (o *Outstation) write(t *transport, h header, b []byte) {
	// supported: WRITE g80 v1 index 7 = 0 (clear the device-restart IIN)
	if len(b) >= 3+2+1 && b[0] == gIIN && b[1] == 1 && b[2] == 0x00 && b[3] == 7 && b[4] == 7 && b[5]&0x01 == 0 && len(b) == 6 {
		o.mu.Lock()
		o.restart = false
		o.mu.Unlock()
		o.reply(t, h, 0, nil)
		return
	}
	o.reply(t, h, IINParamErr, nil)
}

func (o *Outstation) read(t *transport, rw io.ReadWriter, h header, b []byte) {
	var wantBin, wantAna, wantE1, wantE2 bool
	var extra uint16
	for len(b) > 0 {
		if len(b) < 3 {
			extra |= IINParamErr
			break
		}
		g, v, q := b[0], b[1], b[2]
		b = b[3:]
		qq, err := parseQual(q, b)
		if err != nil {
			extra |= IINParamErr
			break
		}
		b = b[qq.size:]
		switch {
		case g == gClass && v == 1:
			wantBin, wantAna = true, true
		case g == gClass && v == 2:
			wantE1 = true
		case g == gClass && v == 3:
			wantE2 = true
		case g == gClass && v == 4: // class 3: no data
		case g == gBinIn && qq.all:
			wantBin = true
		case g == gAnaIn && qq.all:
			wantAna = true
		case g == gBinEv && qq.all:
			wantE1 = true
		case g == gAnaEv && qq.all:
			wantE2 = true
		default:
			extra |= IINObjUnknown
		}
	}
	var items []item
	if wantBin || wantAna {
		pts := o.H.Snapshot(o.CA)
		sort.Slice(pts, func(i, j int) bool { return pts[i].IOA < pts[j].IOA })
		for _, p := range pts {
			if p.CA != o.CA {
				continue
			}
			if p.Status && wantBin {
				items = append(items, item{gBinIn, vBinFlags, append(putIdx(p.IOA), binFlags(p.DPI, p.Quality))})
			}
		}
		for _, p := range pts {
			if p.CA == o.CA && !p.Status && wantAna {
				d := append(putIdx(p.IOA), flagsFromQuality(p.Quality))
				items = append(items, item{gAnaIn, vAnaFloat, append(d, putF32(p.Float)...)})
			}
		}
	}
	o.mu.Lock()
	var in1, in2 []event
	if wantE1 {
		in1, o.ev1 = o.ev1, nil
	}
	if wantE2 {
		in2, o.ev2 = o.ev2, nil
	}
	o.mu.Unlock()
	for _, e := range in1 {
		d := append(putIdx(e.ioa), binFlags(e.dpi, e.q))
		items = append(items, item{gBinEv, vBinEvTime, append(d, timeMs(e.t)...)})
	}
	for _, e := range in2 {
		d := append(putIdx(e.ioa), flagsFromQuality(e.q))
		d = append(d, putF32(e.f)...)
		items = append(items, item{gAnaEv, vAnaEvFTime, append(d, timeMs(e.t)...)})
	}
	frags := pack(items)
	hasEvents := len(in1)+len(in2) > 0
	if len(frags) == 0 {
		frags = [][]byte{nil}
	}
	seq := h.seq
	for i, body := range frags {
		last := i == len(frags)-1
		o.mu.Lock()
		iin := o.iin() | extra
		o.mu.Unlock()
		rh := header{fir: i == 0, fin: last, con: !last || hasEvents, seq: seq, fn: FnResponse, iin: iin}
		if err := t.send(buildMsg(rh, body)); err != nil {
			o.restore(in1, in2)
			return
		}
		if rh.con { // events leave the buffer only when the master confirms them
			setDeadline(rw, confirmWait)
			ok := false
			for {
				msg, _, err := t.recv()
				if err != nil {
					break
				}
				ch, _, err := parseHeader(msg)
				if err == nil && ch.fn == FnConfirm && ch.seq == seq {
					ok = true
					break
				}
			}
			setDeadline(rw, 0)
			if !ok {
				o.restore(in1, in2) // not confirmed: keep them for the next poll
				return
			}
		}
		seq = (seq + 1) & 0x0F
	}
}

func (o *Outstation) restore(in1, in2 []event) {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.ev1 = append(in1, o.ev1...)
	o.ev2 = append(in2, o.ev2...)
}

// pack splits items into application fragment bodies (prefix 4-byte index, 2-byte count: qualifier 0x38).
func pack(items []item) [][]byte {
	var out [][]byte
	var cur []byte
	var gi int   // offset of current group header in cur
	var gcnt int // objects in current group
	var cg, cv byte
	flush := func() {
		if gcnt > 0 {
			binary.LittleEndian.PutUint16(cur[gi+3:], uint16(gcnt))
		}
		gcnt = 0
	}
	for _, it := range items {
		need := len(it.data)
		if gcnt == 0 || it.g != cg || it.v != cv {
			need += 5
		}
		if len(cur)+need > maxFragment-8 {
			flush()
			out = append(out, cur)
			cur = nil
			gcnt = 0
		}
		if gcnt == 0 || it.g != cg || it.v != cv {
			flush()
			gi = len(cur)
			cur = append(cur, it.g, it.v, 0x38, 0, 0)
			cg, cv = it.g, it.v
		}
		cur = append(cur, it.data...)
		gcnt++
	}
	flush()
	if len(cur) > 0 {
		out = append(out, cur)
	}
	return out
}

// control handles SELECT and OPERATE with g12v1 CROBs.
func (o *Outstation) control(t *transport, h header, b []byte) {
	if len(b) < 3 || b[0] != gCROB || b[1] != vCROB {
		o.reply(t, h, IINObjUnknown, nil)
		return
	}
	qq, err := parseQual(b[2], b[3:])
	if err != nil || qq.prefix == 0 || qq.isRng {
		o.reply(t, h, IINParamErr, nil)
		return
	}
	data := b[3+qq.size:]
	osz := qq.prefix + 11
	if int(qq.count)*osz != len(data) || qq.count == 0 || qq.count > 16 {
		o.reply(t, h, IINParamErr, nil)
		return
	}
	resp := append([]byte(nil), b[:3+qq.size]...)
	for i := 0; i < int(qq.count); i++ {
		ob := data[i*osz : (i+1)*osz]
		idx := getIdx(ob, qq.prefix)
		code := ob[qq.prefix]
		st := o.one(h, idx, code, ob[qq.prefix+1])
		cp := append([]byte(nil), ob...)
		cp[len(cp)-1] = st
		resp = append(resp, cp...)
	}
	o.reply(t, h, 0, resp)
}

func dcsOf(code byte) uint8 {
	switch code & 0xC0 {
	case 0x80:
		return 1 // TRIP -> open
	case 0x40:
		return 2 // CLOSE -> close
	}
	switch code & 0x0F {
	case 3:
		return 2 // LATCH_ON
	case 4:
		return 1 // LATCH_OFF
	}
	return 0
}

func (o *Outstation) one(h header, idx uint32, code, count byte) byte {
	dcs := dcsOf(code)
	if dcs == 0 || (code&0x0F != 1 && code&0x0F != 3 && code&0x0F != 4 && code&0x0F != 2) {
		return StNotSupp
	}
	if count != 1 {
		return StFormat
	}
	status, ok := o.H.IsStatus(o.CA, idx)
	if !ok || !status {
		return StNotSupp
	}
	o.mu.Lock()
	if h.fn == FnSelect {
		o.sel[idx] = selEntry{dcs, h.seq, time.Now().Add(selectTTL)}
		o.mu.Unlock()
		return StSuccess
	}
	s, have := o.sel[idx]
	delete(o.sel, idx)
	o.mu.Unlock()
	if !have || time.Now().After(s.exp) || s.dcs != dcs || (s.seq+1)&0x0F != h.seq {
		o.Stats.Rejected.Add(1)
		return StNoSelect
	}
	if err := o.H.Command(o.CA, idx, dcs); err != nil {
		o.log().Warn("dnp3 command rejected by backend", "ioa", idx, "err", err)
		return StNotSupp
	}
	return StSuccess
}
