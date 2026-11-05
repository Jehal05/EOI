// Package c37118 implements a subset of IEEE C37.118.2 synchrophasor transfer: frame codec with CRC-CCITT,
// configuration frame 2 for a single PMU (float32 rectangular/polar phasors, float32 analogs, digital words),
// data frames, and command frames; plus a PMU simulator server and a PDC-style client.
// Not implemented: multi-PMU aggregation in one frame, integer formats, CFG-3, UDP, PDC time alignment buffers.
package c37118

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math"
	"strings"
	"time"
)

// Frame types (bits 6-4 of the SYNC low byte).
const (
	TypeData = 0
	TypeHdr  = 1
	TypeCfg1 = 2
	TypeCfg2 = 3
	TypeCmd  = 4
)

// Commands.
const (
	CmdOff  = 1
	CmdOn   = 2
	CmdHdr  = 3
	CmdCfg1 = 4
	CmdCfg2 = 5
)

const maxFrame = 65535

var (
	ErrCRC   = errors.New("c37118: CRC mismatch")
	ErrShort = errors.New("c37118: truncated frame")
	ErrFmt   = errors.New("c37118: malformed frame")
)

// CRC computes CRC-CCITT (poly 0x1021, init 0xFFFF, no reflection, no final xor).
func CRC(b []byte) uint16 {
	c := uint16(0xFFFF)
	for _, x := range b {
		c ^= uint16(x) << 8
		for i := 0; i < 8; i++ {
			if c&0x8000 != 0 {
				c = c<<1 ^ 0x1021
			} else {
				c <<= 1
			}
		}
	}
	return c
}

// Common is the header shared by all frames.
type Common struct {
	Type    int
	Version int // 1 (2005) or 2 (2011)
	ID      uint16
	SOC     uint32
	FracSec uint32 // 24-bit count; high byte of the wire word is the time quality
	TQ      byte
}

func encode(c Common, body []byte) []byte {
	n := 14 + len(body) + 2
	b := make([]byte, 0, n)
	b = append(b, 0xAA, byte(c.Type)<<4|byte(c.Version&0x0F))
	b = binary.BigEndian.AppendUint16(b, uint16(n))
	b = binary.BigEndian.AppendUint16(b, c.ID)
	b = binary.BigEndian.AppendUint32(b, c.SOC)
	b = binary.BigEndian.AppendUint32(b, c.FracSec&0xFFFFFF|uint32(c.TQ)<<24)
	b = append(b, body...)
	return binary.BigEndian.AppendUint16(b, CRC(b))
}

// Read reads one frame, resynchronising on 0xAA. Frames failing the CRC are returned with ErrCRC.
func Read(r io.Reader) (Common, []byte, error) {
	var h [1]byte
	for {
		if _, err := io.ReadFull(r, h[:]); err != nil {
			return Common{}, nil, err
		}
		if h[0] != 0xAA {
			continue
		}
		hd := make([]byte, 13)
		hd[0] = 0xAA
		if _, err := io.ReadFull(r, hd[1:4]); err != nil {
			return Common{}, nil, err
		}
		n := int(binary.BigEndian.Uint16(hd[2:4]))
		if n < 16 {
			continue
		}
		rest := make([]byte, n-4)
		if _, err := io.ReadFull(r, rest); err != nil {
			return Common{}, nil, err
		}
		all := append(append([]byte(nil), hd[:4]...), rest...)
		if CRC(all[:n-2]) != binary.BigEndian.Uint16(all[n-2:]) {
			return Common{}, nil, ErrCRC
		}
		fs := binary.BigEndian.Uint32(all[10:14])
		c := Common{Type: int(all[1]>>4) & 7, Version: int(all[1] & 0x0F), ID: binary.BigEndian.Uint16(all[4:6]),
			SOC: binary.BigEndian.Uint32(all[6:10]), FracSec: fs & 0xFFFFFF, TQ: byte(fs >> 24)}
		return c, all[14 : n-2], nil
	}
}

// Config is a single-PMU configuration (CFG-2 content).
type Config struct {
	Station  string // up to 16 chars
	PMUID    uint16
	PhasorsF bool // phasors as float (always true here); kept for clarity
	Polar    bool // polar (magnitude, angle in radians) instead of rectangular
	Phasors  []string
	Analogs  []string
	Digitals int // number of 16-bit digital words
	FNom     int // 50 or 60
	CfgCnt   uint16
	Rate     int16 // frames per second (positive) or seconds per frame (negative)
	TimeBase uint32
}

func pad16(s string) []byte {
	b := make([]byte, 16)
	copy(b, s)
	return b
}

func trim(b []byte) string { return strings.TrimRight(string(b), "\x00 ") }

// EncodeConfig builds a CFG-2 frame. idcode is the stream id; soc/frac the time stamp.
func EncodeConfig(c Common, cfg Config) ([]byte, error) {
	if len(cfg.Phasors) > 255 || len(cfg.Analogs) > 255 || cfg.Digitals > 255 || len(cfg.Station) > 16 {
		return nil, ErrFmt
	}
	c.Type = TypeCfg2
	tb := cfg.TimeBase
	if tb == 0 {
		tb = 1000000
	}
	var b []byte
	b = binary.BigEndian.AppendUint32(b, tb&0xFFFFFF)
	b = binary.BigEndian.AppendUint16(b, 1) // NUM_PMU
	b = append(b, pad16(cfg.Station)...)
	b = binary.BigEndian.AppendUint16(b, cfg.PMUID)
	format := uint16(0x0002 | 0x0004) // phasor float, analog float
	if cfg.Polar {
		format |= 0x0001
	}
	b = binary.BigEndian.AppendUint16(b, format)
	b = binary.BigEndian.AppendUint16(b, uint16(len(cfg.Phasors)))
	b = binary.BigEndian.AppendUint16(b, uint16(len(cfg.Analogs)))
	b = binary.BigEndian.AppendUint16(b, uint16(cfg.Digitals))
	for _, n := range cfg.Phasors {
		b = append(b, pad16(n)...)
	}
	for _, n := range cfg.Analogs {
		b = append(b, pad16(n)...)
	}
	for i := 0; i < cfg.Digitals*16; i++ {
		b = append(b, pad16(fmt.Sprintf("D%d", i))...)
	}
	for range cfg.Phasors { // PHUNIT: type 0 voltage, scale 1 (floats are in engineering units)
		b = binary.BigEndian.AppendUint32(b, 1)
	}
	for range cfg.Analogs {
		b = binary.BigEndian.AppendUint32(b, 1)
	}
	for i := 0; i < cfg.Digitals; i++ {
		b = binary.BigEndian.AppendUint32(b, 0xFFFF0000)
	}
	fn := uint16(0)
	if cfg.FNom == 50 {
		fn = 1
	}
	b = binary.BigEndian.AppendUint16(b, fn)
	b = binary.BigEndian.AppendUint16(b, cfg.CfgCnt)
	b = binary.BigEndian.AppendUint16(b, uint16(cfg.Rate))
	if 14+len(b)+2 > maxFrame {
		return nil, ErrFmt
	}
	return encode(c, b), nil
}

// DecodeConfig parses a CFG-1/CFG-2 body for a single PMU.
func DecodeConfig(b []byte) (Config, error) {
	var cfg Config
	if len(b) < 4+2+16+2+2+6 {
		return cfg, ErrShort
	}
	cfg.TimeBase = binary.BigEndian.Uint32(b) & 0xFFFFFF
	if binary.BigEndian.Uint16(b[4:]) != 1 {
		return cfg, fmt.Errorf("c37118: %d PMUs in one frame are not supported", binary.BigEndian.Uint16(b[4:]))
	}
	p := b[6:]
	cfg.Station = trim(p[:16])
	cfg.PMUID = binary.BigEndian.Uint16(p[16:])
	format := binary.BigEndian.Uint16(p[18:])
	if format&0x6 != 0x6 {
		return cfg, errors.New("c37118: only floating-point phasor/analog formats are supported")
	}
	cfg.Polar = format&1 != 0
	np, na, nd := int(binary.BigEndian.Uint16(p[20:])), int(binary.BigEndian.Uint16(p[22:])), int(binary.BigEndian.Uint16(p[24:]))
	p = p[26:]
	need := 16*(np+na+16*nd) + 4*(np+na+nd) + 6
	if len(p) < need {
		return cfg, ErrShort
	}
	for i := 0; i < np; i++ {
		cfg.Phasors = append(cfg.Phasors, trim(p[:16]))
		p = p[16:]
	}
	for i := 0; i < na; i++ {
		cfg.Analogs = append(cfg.Analogs, trim(p[:16]))
		p = p[16:]
	}
	p = p[16*16*nd:]
	p = p[4*(np+na+nd):]
	cfg.Digitals = nd
	cfg.FNom = 60
	if binary.BigEndian.Uint16(p)&1 != 0 {
		cfg.FNom = 50
	}
	cfg.CfgCnt = binary.BigEndian.Uint16(p[2:])
	cfg.Rate = int16(binary.BigEndian.Uint16(p[4:]))
	return cfg, nil
}

// Data is one measurement set.
type Data struct {
	Stat     uint16
	Phasors  [][2]float32 // (real, imag) or (magnitude, angle rad) per the configuration
	Freq     float32      // absolute frequency in Hz
	DFreq    float32      // Hz/s
	Analogs  []float32
	Digitals []uint16
}

// Stat word helpers: bit 15 data invalid, bit 14 PMU error, bit 13 sync lost, bits 5-4 unlock time, 3-0 trigger.
func (d Data) Invalid() bool  { return d.Stat&0x8000 != 0 }
func (d Data) PMUError() bool { return d.Stat&0x4000 != 0 }
func (d Data) SyncLost() bool { return d.Stat&0x2000 != 0 }

// EncodeData builds a data frame consistent with cfg. Freq is sent as deviation from nominal in mHz? No:
// with floating-point format C37.118 sends FREQ and DFREQ as IEEE floats of absolute Hz and Hz/s.
func EncodeData(c Common, cfg Config, d Data) ([]byte, error) {
	if len(d.Phasors) != len(cfg.Phasors) || len(d.Analogs) != len(cfg.Analogs) || len(d.Digitals) != cfg.Digitals {
		return nil, ErrFmt
	}
	c.Type = TypeData
	var b []byte
	b = binary.BigEndian.AppendUint16(b, d.Stat)
	for _, p := range d.Phasors {
		b = binary.BigEndian.AppendUint32(b, math.Float32bits(p[0]))
		b = binary.BigEndian.AppendUint32(b, math.Float32bits(p[1]))
	}
	b = binary.BigEndian.AppendUint32(b, math.Float32bits(d.Freq))
	b = binary.BigEndian.AppendUint32(b, math.Float32bits(d.DFreq))
	for _, a := range d.Analogs {
		b = binary.BigEndian.AppendUint32(b, math.Float32bits(a))
	}
	for _, w := range d.Digitals {
		b = binary.BigEndian.AppendUint16(b, w)
	}
	return encode(c, b), nil
}

// DecodeData parses a data frame body using cfg.
func DecodeData(cfg Config, b []byte) (Data, error) {
	var d Data
	need := 2 + 8*len(cfg.Phasors) + 8 + 4*len(cfg.Analogs) + 2*cfg.Digitals
	if len(b) != need {
		return d, fmt.Errorf("c37118: data frame body is %d bytes, configuration says %d", len(b), need)
	}
	d.Stat = binary.BigEndian.Uint16(b)
	p := b[2:]
	f := func() float32 { v := math.Float32frombits(binary.BigEndian.Uint32(p)); p = p[4:]; return v }
	for range cfg.Phasors {
		d.Phasors = append(d.Phasors, [2]float32{f(), f()})
	}
	d.Freq, d.DFreq = f(), f()
	for range cfg.Analogs {
		d.Analogs = append(d.Analogs, f())
	}
	for range cfg.Digitals {
		d.Digitals = append(d.Digitals, binary.BigEndian.Uint16(p))
		p = p[2:]
	}
	return d, nil
}

// EncodeCommand builds a command frame.
func EncodeCommand(c Common, cmd uint16) []byte {
	c.Type = TypeCmd
	return encode(c, binary.BigEndian.AppendUint16(nil, cmd))
}

// Time converts SOC and FRACSEC to a time using the configuration's TIME_BASE.
func (c Common) Time(timeBase uint32) time.Time {
	if timeBase == 0 {
		timeBase = 1000000
	}
	ns := int64(float64(c.FracSec) / float64(timeBase) * 1e9)
	return time.Unix(int64(c.SOC), ns).UTC()
}

// StampFor produces SOC/FRACSEC for t.
func StampFor(t time.Time, timeBase uint32) (soc, frac uint32) {
	if timeBase == 0 {
		timeBase = 1000000
	}
	t = t.UTC()
	return uint32(t.Unix()), uint32(float64(t.Nanosecond()) / 1e9 * float64(timeBase))
}
