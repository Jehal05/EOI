package iec104

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
)

// Client is a control-centre master session to one outstation.
type Client struct {
	Addr    string
	TLS     *tls.Config // non-nil: connect with TLS (IEC 62351-3 style)
	OnASDU  func(ASDU)  // data ASDUs (measurements, statuses)
	OnState func(up bool)
	Auth    *AuthConfig // non-nil: answer authentication challenges for control commands

	mu      sync.Mutex
	l       *link
	started chan struct{}
	waiters map[waitKey]chan ASDU
	lastRaw []byte // the control command awaiting a challenge reply
}

type waitKey struct {
	ca    uint16
	ioa   uint32
	sel   bool
	cause uint8
}

var ErrNotConnected = errors.New("iec104: not connected")

// Run makes one connection attempt and runs the session until it fails or ctx ends.
func (c *Client) Run(ctx context.Context) error {
	d := net.Dialer{Timeout: 5 * time.Second}
	var conn net.Conn
	var err error
	if c.TLS != nil {
		conn, err = tls.DialWithDialer(&d, "tcp", c.Addr, c.TLS)
	} else {
		conn, err = d.DialContext(ctx, "tcp", c.Addr)
	}
	if err != nil {
		return err
	}
	l := newLink(conn)
	started := make(chan struct{})
	c.mu.Lock()
	c.l, c.started, c.waiters = l, started, map[waitKey]chan ASDU{}
	c.mu.Unlock()
	go func() { <-ctx.Done(); l.close() }()
	if err := l.sendU(UStartDTAct); err != nil {
		l.close()
		return err
	}
	var once sync.Once
	err = l.run(c.onI, func(code byte) {
		if code == UStartDTCon {
			once.Do(func() {
				close(started)
				if c.OnState != nil {
					c.OnState(true)
				}
			})
		}
	})
	l.close()
	c.mu.Lock()
	c.l = nil
	c.mu.Unlock()
	if c.OnState != nil {
		c.OnState(false)
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return err
}

func (c *Client) onI(a ASDU) {
	if a.Type == S_CH_NA_1 {
		c.mu.Lock()
		raw, l := c.lastRaw, c.l
		c.mu.Unlock()
		if r, ok := answer(c.Auth, a, raw); ok && l != nil {
			if b, err := r.Encode(); err == nil {
				_ = l.sendI(b, 5*time.Second)
			}
		}
		return
	}
	if a.Type == S_ER_NA_1 || a.Type == S_RP_NA_1 {
		return
	}
	if a.Type == C_DC_NA_1 && len(a.Objects) == 1 && (a.Cause == CotActCon || a.Cause == CotActTerm || a.Cause >= CotUnknownType) {
		k := waitKey{a.CA, a.Objects[0].IOA, a.Objects[0].Select, a.Cause}
		if a.Cause >= CotUnknownType {
			k.cause = CotActCon
		}
		c.mu.Lock()
		ch := c.waiters[k]
		c.mu.Unlock()
		if ch != nil {
			select {
			case ch <- a:
			default:
			}
		}
		return
	}
	if a.Type == C_IC_NA_1 {
		return
	}
	if c.OnASDU != nil {
		c.OnASDU(a)
	}
}

func (c *Client) session() (*link, error) {
	c.mu.Lock()
	l, st := c.l, c.started
	c.mu.Unlock()
	if l == nil {
		return nil, ErrNotConnected
	}
	select {
	case <-st:
		return l, nil
	default:
		return nil, ErrNotConnected
	}
}

// Interrogate requests a station general interrogation for a common address.
func (c *Client) Interrogate(ca uint16) error {
	l, err := c.session()
	if err != nil {
		return err
	}
	b, err := ASDU{Type: C_IC_NA_1, Cause: CotActivation, CA: ca, Objects: []Object{{IOA: 0, QOI: 20}}}.Encode()
	if err != nil {
		return err
	}
	return l.sendI(b, 5*time.Second)
}

func (c *Client) sendCmd(l *link, ca uint16, ioa uint32, dcs uint8, sel bool) error {
	b, err := ASDU{Type: C_DC_NA_1, Cause: CotActivation, CA: ca, Objects: []Object{{IOA: ioa, DPI: dcs, Select: sel}}}.Encode()
	if err != nil {
		return err
	}
	c.mu.Lock()
	c.lastRaw = b
	c.mu.Unlock()
	return l.sendI(b, 5*time.Second)
}

func (c *Client) wait(k waitKey) chan ASDU {
	ch := make(chan ASDU, 1)
	c.mu.Lock()
	c.waiters[k] = ch
	c.mu.Unlock()
	return ch
}

func (c *Client) unwait(ks ...waitKey) {
	c.mu.Lock()
	for _, k := range ks {
		delete(c.waiters, k)
	}
	c.mu.Unlock()
}

func recv(ctx context.Context, ch chan ASDU, d time.Duration, what string) (ASDU, error) {
	select {
	case a := <-ch:
		if a.Neg || a.Cause >= CotUnknownType {
			return a, fmt.Errorf("iec104: %s rejected by outstation (cause %d)", what, a.Cause)
		}
		return a, nil
	case <-time.After(d):
		return ASDU{}, fmt.Errorf("iec104: %s timed out", what)
	case <-ctx.Done():
		return ASDU{}, ctx.Err()
	}
}

// SelectExecute performs a select-before-operate double command (dcs: 1=open, 2=close).
func (c *Client) SelectExecute(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	l, err := c.session()
	if err != nil {
		return err
	}
	selCon := waitKey{ca, ioa, true, CotActCon}
	exeCon := waitKey{ca, ioa, false, CotActCon}
	exeTerm := waitKey{ca, ioa, false, CotActTerm}
	chSel, chCon, chTerm := c.wait(selCon), c.wait(exeCon), c.wait(exeTerm)
	defer c.unwait(selCon, exeCon, exeTerm)
	if err := c.sendCmd(l, ca, ioa, dcs, true); err != nil {
		return err
	}
	if _, err := recv(ctx, chSel, 5*time.Second, "select"); err != nil {
		return err
	}
	if err := c.sendCmd(l, ca, ioa, dcs, false); err != nil {
		return err
	}
	if _, err := recv(ctx, chCon, 5*time.Second, "execute"); err != nil {
		return err
	}
	_, err = recv(ctx, chTerm, 10*time.Second, "execute termination")
	return err
}

// ExecuteWithoutSelect sends a bare execute (used by tests to prove the outstation rejects it).
func (c *Client) ExecuteWithoutSelect(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error {
	l, err := c.session()
	if err != nil {
		return err
	}
	k := waitKey{ca, ioa, false, CotActCon}
	ch := c.wait(k)
	defer c.unwait(k)
	if err := c.sendCmd(l, ca, ioa, dcs, false); err != nil {
		return err
	}
	_, err = recv(ctx, ch, 5*time.Second, "execute")
	return err
}
