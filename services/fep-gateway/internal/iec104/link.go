package iec104

import (
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"time"
)

// Protocol parameters (IEC 60870-5-104 defaults).
const (
	paramK  = 12               // max unacknowledged I-frames
	paramW  = 8                // acknowledge after this many received I-frames
	paramT1 = 15 * time.Second // timeout for acknowledgement of a sent test/start frame
	paramT2 = 2 * time.Second  // (reduced from 10 s) max delay before acknowledging
	paramT3 = 20 * time.Second // idle time before TESTFR
)

var (
	ErrWindowFull = errors.New("iec104: send window full")
	ErrClosed     = errors.New("iec104: link closed")
)

type link struct {
	c        net.Conn
	wmu      sync.Mutex // serialises writes + sequence assignment
	mu       sync.Mutex // guards counters
	cond     *sync.Cond
	ns, nr   uint16
	peerAck  uint16
	pending  int // I-frames received since last ack sent
	closed   bool
	lastRx   atomic.Int64
	testSent atomic.Int64 // unix nanos of unanswered TESTFR act, 0 if none
}

func newLink(c net.Conn) *link {
	l := &link{c: c}
	l.cond = sync.NewCond(&l.mu)
	l.lastRx.Store(time.Now().UnixNano())
	return l
}

func (l *link) close() {
	l.mu.Lock()
	l.closed = true
	l.mu.Unlock()
	l.cond.Broadcast()
	l.c.Close()
}

func (l *link) writeFrame(f Frame) error {
	b, err := f.Encode()
	if err != nil {
		return err
	}
	l.c.SetWriteDeadline(time.Now().Add(paramT1))
	_, err = l.c.Write(b)
	return err
}

// sendI sends one ASDU as an I-frame, waiting (up to timeout) for room in the k-window.
func (l *link) sendI(asdu []byte, timeout time.Duration) error {
	l.wmu.Lock()
	defer l.wmu.Unlock()
	deadline := time.Now().Add(timeout)
	timer := time.AfterFunc(timeout, l.cond.Broadcast)
	defer timer.Stop()
	l.mu.Lock()
	for !l.closed && int((l.ns-l.peerAck)&0x7FFF) >= paramK {
		if time.Now().After(deadline) {
			l.mu.Unlock()
			return ErrWindowFull
		}
		l.cond.Wait()
	}
	if l.closed {
		l.mu.Unlock()
		return ErrClosed
	}
	f := Frame{Kind: KindI, SendSeq: l.ns, RecvSeq: l.nr, ASDU: asdu}
	l.ns = (l.ns + 1) & 0x7FFF
	l.pending = 0 // N(R) piggy-backed
	l.mu.Unlock()
	return l.writeFrame(f)
}

func (l *link) sendS() error {
	l.wmu.Lock()
	defer l.wmu.Unlock()
	l.mu.Lock()
	f := Frame{Kind: KindS, RecvSeq: l.nr}
	l.pending = 0
	l.mu.Unlock()
	return l.writeFrame(f)
}

func (l *link) sendU(code byte) error {
	l.wmu.Lock()
	defer l.wmu.Unlock()
	return l.writeFrame(Frame{Kind: KindU, U: code})
}

// ackFrom validates and records the peer's N(R).
func (l *link) ackFrom(nr uint16) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	// peer may only acknowledge frames we have sent: peerAck <= nr <= ns (mod 2^15)
	if ((nr - l.peerAck) & 0x7FFF) > ((l.ns - l.peerAck) & 0x7FFF) {
		return fmt.Errorf("iec104: invalid N(R)=%d (ns=%d, acked=%d)", nr, l.ns, l.peerAck)
	}
	l.peerAck = nr
	l.cond.Broadcast()
	return nil
}

// run reads frames until error. onI is called for each in-sequence I-frame ASDU; onU for U-frames.
func (l *link) run(onI func(ASDU), onU func(byte)) error {
	stop := make(chan struct{})
	defer close(stop)
	go l.timers(stop)
	for {
		l.c.SetReadDeadline(time.Now().Add(paramT3 + paramT1 + 5*time.Second))
		f, err := ReadFrame(l.c)
		if err != nil {
			return err
		}
		l.lastRx.Store(time.Now().UnixNano())
		switch f.Kind {
		case KindU:
			switch f.U {
			case UTestFRAct:
				if err := l.sendU(UTestFRCon); err != nil {
					return err
				}
			case UTestFRCon:
				l.testSent.Store(0)
			default:
				onU(f.U)
			}
		case KindS:
			if err := l.ackFrom(f.RecvSeq); err != nil {
				return err
			}
		case KindI:
			l.mu.Lock()
			if f.SendSeq != l.nr {
				got, want := f.SendSeq, l.nr
				l.mu.Unlock()
				return fmt.Errorf("iec104: sequence error: got N(S)=%d want %d", got, want)
			}
			l.nr = (l.nr + 1) & 0x7FFF
			l.pending++
			doAck := l.pending >= paramW
			l.mu.Unlock()
			if err := l.ackFrom(f.RecvSeq); err != nil {
				return err
			}
			if a, err := DecodeASDU(f.ASDU); err == nil {
				onI(a)
			} // undecodable ASDUs are dropped (still acknowledged at link level)
			if doAck {
				if err := l.sendS(); err != nil {
					return err
				}
			}
		}
	}
}

// timers implements t2 (delayed ack) and t3 (idle TESTFR) / t1 (TESTFR unanswered).
func (l *link) timers(stop chan struct{}) {
	tk := time.NewTicker(500 * time.Millisecond)
	defer tk.Stop()
	for {
		select {
		case <-stop:
			return
		case now := <-tk.C:
			l.mu.Lock()
			p := l.pending
			closed := l.closed
			l.mu.Unlock()
			if closed {
				return
			}
			if p > 0 && time.Since(time.Unix(0, l.lastRx.Load())) >= paramT2 {
				l.sendS()
			}
			if ts := l.testSent.Load(); ts != 0 && now.Sub(time.Unix(0, ts)) > paramT1 {
				l.close() // peer did not answer TESTFR within t1
				return
			}
			if time.Since(time.Unix(0, l.lastRx.Load())) >= paramT3 && l.testSent.Load() == 0 {
				l.testSent.Store(now.UnixNano())
				l.sendU(UTestFRAct)
			}
		}
	}
}
