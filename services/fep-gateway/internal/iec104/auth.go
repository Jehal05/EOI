package iec104

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"sync"
	"time"
)

// AuthConfig enables application-layer authentication of critical (control) ASDUs: a subset of IEC 60870-5-7 / IEC 62351-5
// "challenge-reply" with HMAC-SHA-256 and a pre-shared update key. Implemented here: challenge on every control command, single-use
// challenges that expire, replay protection through the challenge sequence number, a failure limit that drops the association.
// NOT implemented: aggressive mode, session-key change/status ASDUs, key wrapping, per-user roles, MAC algorithms other than
// HMAC-SHA-256 (truncated to 16 bytes). It has not been interop-tested against any third-party implementation.
type AuthConfig struct {
	Key     []byte        // pre-shared secret, at least 16 bytes
	User    uint16        // user number carried in the exchange (default 1)
	Window  time.Duration // how long a challenge stays valid (default 5 s)
	MaxFail int           // failed replies before the association is dropped (default 3)
}

func (c *AuthConfig) ok() bool { return c != nil && len(c.Key) >= 16 }
func (c *AuthConfig) user() uint16 {
	if c.User == 0 {
		return 1
	}
	return c.User
}
func (c *AuthConfig) window() time.Duration {
	if c.Window <= 0 {
		return 5 * time.Second
	}
	return c.Window
}
func (c *AuthConfig) maxFail() int {
	if c.MaxFail <= 0 {
		return 3
	}
	return c.MaxFail
}

// Error codes carried by S_ER_NA_1.
const (
	AuthErrFailed  = 1 // the reply did not verify
	AuthErrExpired = 2 // no pending challenge, or it expired or was already used
)

func isCritical(t uint8) bool { return t == C_DC_NA_1 || t == C_SC_NA_1 }

func mac(key []byte, csq uint32, user uint16, chal []byte, raw []byte) []byte {
	m := hmac.New(sha256.New, key)
	var h [6]byte
	binary.BigEndian.PutUint32(h[0:4], csq)
	binary.BigEndian.PutUint16(h[4:6], user)
	m.Write([]byte("GSA1"))
	m.Write(h[:])
	m.Write(chal)
	m.Write(raw)
	return m.Sum(nil)[:16]
}

// challenge element: CSQ(4) user(2) alg(1)=1 reason(1)=1 (critical operation) challenge(8)
func challengeASDU(ca uint16, csq uint32, user uint16, chal [8]byte) ASDU {
	d := make([]byte, 16)
	binary.BigEndian.PutUint32(d[0:4], csq)
	binary.BigEndian.PutUint16(d[4:6], user)
	d[6], d[7] = 1, 1
	copy(d[8:], chal[:])
	return ASDU{Type: S_CH_NA_1, Cause: CotSpont, CA: ca, Objects: []Object{{IOA: 0, Data: d}}}
}

func replyASDU(ca uint16, csq uint32, user uint16, m []byte) ASDU {
	d := make([]byte, 22)
	binary.BigEndian.PutUint32(d[0:4], csq)
	binary.BigEndian.PutUint16(d[4:6], user)
	copy(d[6:], m)
	return ASDU{Type: S_RP_NA_1, Cause: CotSpont, CA: ca, Objects: []Object{{IOA: 0, Data: d}}}
}

func errorASDU(ca uint16, csq uint32, user uint16, code byte) ASDU {
	d := make([]byte, 8)
	binary.BigEndian.PutUint32(d[0:4], csq)
	binary.BigEndian.PutUint16(d[4:6], user)
	d[6] = code
	return ASDU{Type: S_ER_NA_1, Cause: CotSpont, CA: ca, Objects: []Object{{IOA: 0, Data: d}}}
}

// ---- outstation side

type pendingAuth struct {
	a    ASDU
	raw  []byte
	csq  uint32
	chal [8]byte
	exp  time.Time
}

type authState struct {
	mu      sync.Mutex
	csq     uint32
	pending *pendingAuth
	fails   int
}

// startChallenge stores the command and returns the challenge to send.
func (st *authState) startChallenge(cfg *AuthConfig, a ASDU) (ASDU, error) {
	raw, err := a.Encode()
	if err != nil {
		return ASDU{}, err
	}
	var ch [8]byte
	if _, err := rand.Read(ch[:]); err != nil {
		return ASDU{}, err
	}
	st.mu.Lock()
	defer st.mu.Unlock()
	st.csq++
	st.pending = &pendingAuth{a: a, raw: raw, csq: st.csq, chal: ch, exp: time.Now().Add(cfg.window())}
	return challengeASDU(a.CA, st.csq, cfg.user(), ch), nil
}

// verify consumes the pending challenge (always: it is single use). It returns the original command when the reply is valid.
func (st *authState) verify(cfg *AuthConfig, r ASDU) (orig ASDU, errCode byte, csq uint32, drop bool) {
	st.mu.Lock()
	defer st.mu.Unlock()
	p := st.pending
	st.pending = nil
	if len(r.Objects) != 1 || len(r.Objects[0].Data) != 22 {
		return ASDU{}, AuthErrFailed, 0, st.fail(cfg)
	}
	d := r.Objects[0].Data
	rcsq := binary.BigEndian.Uint32(d[0:4])
	if p == nil || time.Now().After(p.exp) || rcsq != p.csq {
		return ASDU{}, AuthErrExpired, rcsq, st.fail(cfg)
	}
	want := mac(cfg.Key, p.csq, cfg.user(), p.chal[:], p.raw)
	if binary.BigEndian.Uint16(d[4:6]) != cfg.user() || !hmac.Equal(want, d[6:22]) {
		return p.a, AuthErrFailed, p.csq, st.fail(cfg)
	}
	st.fails = 0
	return p.a, 0, p.csq, false
}

func (st *authState) fail(cfg *AuthConfig) bool {
	st.fails++
	return st.fails >= cfg.maxFail()
}

// ---- master side

// answer builds the reply to a challenge for the critical ASDU the master sent last.
func answer(cfg *AuthConfig, ch ASDU, lastRaw []byte) (ASDU, bool) {
	if !cfg.ok() || len(ch.Objects) != 1 || len(ch.Objects[0].Data) != 16 || lastRaw == nil {
		return ASDU{}, false
	}
	d := ch.Objects[0].Data
	csq := binary.BigEndian.Uint32(d[0:4])
	return replyASDU(ch.CA, csq, cfg.user(), mac(cfg.Key, csq, cfg.user(), d[8:16], lastRaw)), true
}
