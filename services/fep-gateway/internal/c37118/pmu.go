package c37118

import (
	"context"
	"errors"
	"io"
	"net"
	"sync"
	"time"
)

// Source supplies the current measurement for the simulated PMU.
type Source func(t time.Time) Data

// Server is a PMU simulator: it answers commands and streams data frames at cfg.Rate while ON.
type Server struct {
	Cfg    Config
	ID     uint16
	Src    Source
	mu     sync.Mutex
	nconns int
}

func (s *Server) Serve(ln net.Listener) error {
	for {
		c, err := ln.Accept()
		if err != nil {
			return err
		}
		go s.ServeConn(c)
	}
}

func (s *Server) ServeConn(rw io.ReadWriteCloser) {
	defer rw.Close()
	var wmu sync.Mutex
	write := func(b []byte) error { wmu.Lock(); defer wmu.Unlock(); _, err := rw.Write(b); return err }
	stop := make(chan struct{})
	var once sync.Once
	var streaming sync.Mutex
	on := false
	defer once.Do(func() { close(stop) })
	startStream := func() {
		streaming.Lock()
		if on {
			streaming.Unlock()
			return
		}
		on = true
		streaming.Unlock()
		period := time.Second / time.Duration(maxInt(int(s.Cfg.Rate), 1))
		go func() {
			tk := time.NewTicker(period)
			defer tk.Stop()
			for {
				select {
				case <-stop:
					return
				case now := <-tk.C:
					streaming.Lock()
					o := on
					streaming.Unlock()
					if !o {
						return
					}
					soc, fr := StampFor(now, s.Cfg.TimeBase)
					f, err := EncodeData(Common{Version: 2, ID: s.ID, SOC: soc, FracSec: fr}, s.Cfg, s.Src(now))
					if err != nil || write(f) != nil {
						return
					}
				}
			}
		}()
	}
	for {
		c, body, err := Read(rw)
		if err != nil {
			if errors.Is(err, ErrCRC) {
				continue
			}
			return
		}
		if c.Type != TypeCmd || len(body) < 2 || c.ID != s.ID {
			continue
		}
		now := time.Now()
		soc, fr := StampFor(now, s.Cfg.TimeBase)
		switch uint16(body[0])<<8 | uint16(body[1]) {
		case CmdCfg2, CmdCfg1:
			f, _ := EncodeConfig(Common{Version: 2, ID: s.ID, SOC: soc, FracSec: fr}, s.Cfg)
			if write(f) != nil {
				return
			}
		case CmdOn:
			startStream()
		case CmdOff:
			streaming.Lock()
			on = false
			streaming.Unlock()
		}
	}
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// Client is a PDC-style client for one PMU stream.
type Client struct {
	Open    func() (io.ReadWriteCloser, error)
	ID      uint16
	OnData  func(cfg Config, t time.Time, tq byte, d Data)
	OnState func(up bool)
	Timeout time.Duration
	Stats   struct {
		sync.Mutex
		Frames, BadCRC, BadFrame, OutOfOrder, Gaps int64
	}
}

// Run connects, fetches the configuration, starts the stream, and delivers decoded frames until the link fails.
// A stream silent for Timeout is treated as lost.
func (c *Client) Run(ctx context.Context) error {
	if c.Timeout == 0 {
		c.Timeout = 3 * time.Second
	}
	rw, err := c.Open()
	if err != nil {
		return err
	}
	defer rw.Close()
	go func() { <-ctx.Done(); rw.Close() }()
	send := func(cmd uint16) error {
		soc, fr := StampFor(time.Now(), 0)
		_, err := rw.Write(EncodeCommand(Common{Version: 2, ID: c.ID, SOC: soc, FracSec: fr}, cmd))
		return err
	}
	if err := send(CmdOff); err != nil {
		return err
	}
	if err := send(CmdCfg2); err != nil {
		return err
	}
	var cfg Config
	haveCfg := false
	var last time.Time
	up := false
	defer func() {
		if up && c.OnState != nil {
			c.OnState(false)
		}
	}()
	for {
		if dl, ok := rw.(interface{ SetReadDeadline(time.Time) error }); ok {
			dl.SetReadDeadline(time.Now().Add(c.Timeout))
		}
		h, body, err := Read(rw)
		if err != nil {
			if errors.Is(err, ErrCRC) {
				c.Stats.Lock()
				c.Stats.BadCRC++
				c.Stats.Unlock()
				continue
			}
			return err
		}
		switch h.Type {
		case TypeCfg2, TypeCfg1:
			cf, err := DecodeConfig(body)
			if err != nil {
				return err
			}
			cfg, haveCfg = cf, true
			if err := send(CmdOn); err != nil {
				return err
			}
		case TypeData:
			if !haveCfg {
				continue
			}
			d, err := DecodeData(cfg, body)
			if err != nil {
				c.Stats.Lock()
				c.Stats.BadFrame++
				c.Stats.Unlock()
				continue
			}
			t := h.Time(cfg.TimeBase)
			c.Stats.Lock()
			c.Stats.Frames++
			if !last.IsZero() {
				if !t.After(last) {
					c.Stats.OutOfOrder++
				} else if cfg.Rate > 0 && t.Sub(last) > 3*time.Second/time.Duration(cfg.Rate)/2 {
					c.Stats.Gaps++
				}
			}
			c.Stats.Unlock()
			last = t
			if !up {
				up = true
				if c.OnState != nil {
					c.OnState(true)
				}
			}
			if c.OnData != nil {
				c.OnData(cfg, t, h.TQ, d)
			}
		}
	}
}
