// fep: front-end processor. Maintains IEC 60870-5-104 master sessions to outstations, maps
// (CA,IOA) -> CIM-derived tags, normalises quality, and forwards telemetry to scada-core.
// It also executes operator commands (select-before-operate) on behalf of scada-core.
package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"gridsovereign/fep/internal/dnp3"
	"gridsovereign/fep/internal/iec101"
	"gridsovereign/fep/internal/iec104"
	"gridsovereign/fep/internal/opcua"
	"gridsovereign/fep/internal/pmubridge"
	"gridsovereign/fep/internal/points"
	"gridsovereign/fep/internal/secure"
	"gridsovereign/fep/internal/token"
)

type tagInfo struct {
	p points.Point
}

type sample struct {
	Tag     string   `json:"tag"`
	Kind    string   `json:"kind"`
	Qty     string   `json:"q"`
	Unit    string   `json:"unit,omitempty"`
	Value   *float64 `json:"v,omitempty"`  // analog
	State   *int     `json:"st,omitempty"` // 0 indeterminate, 1 open, 2 closed, 3 fault
	Quality string   `json:"quality"`
	SrcTS   string   `json:"ts,omitempty"`
	RxTS    string   `json:"rx"`
	Cause   uint8    `json:"cot"`
	Stale   bool     `json:"stale,omitempty"` // link to the outstation lost: keep the last value, mark it stale
	HB      bool     `json:"hb,omitempty"`    // heartbeat: this FEP is alive and talking to this core node
}

// liveCache keeps the latest value of every point so the OPC UA server can answer reads (the FEP otherwise only forwards).
type liveCache struct {
	mu  sync.RWMutex
	pts []opcua.Point
	m   map[string]*cacheEnt
}
type cacheEnt struct {
	v    float64
	q    uint8
	ts   time.Time
	have bool
}

func (c *liveCache) Points() []opcua.Point { return c.pts }
func (c *liveCache) Value(tag string) (float64, uint8, time.Time, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	e := c.m[tag]
	if e == nil || !e.have {
		return 0, 2, time.Time{}, false
	}
	return e.v, e.q, e.ts, true
}
func (c *liveCache) put(sm sample, ts time.Time) {
	e := c.m[sm.Tag]
	if e == nil {
		return
	}
	var q uint8
	switch sm.Quality {
	case "good":
	case "invalid":
		q = 2
	default:
		q = 1
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	switch {
	case sm.Value != nil:
		e.v = *sm.Value
	case sm.State != nil:
		e.v = float64(*sm.State)
	default:
		return
	}
	e.q, e.ts, e.have = q, ts, true
}
func (c *liveCache) markStale(match func(tag string) bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for t, e := range c.m {
		if e.have && match(t) && e.q < 1 {
			e.q = 1
		}
	}
}

type metrics struct {
	asdu, samples, unknown, batches, posts, postFail, dropped, interrog atomic.Int64
	cmdOK, cmdFail                                                      atomic.Int64
	maxSkewMs                                                           atomic.Int64
}

type rtu struct {
	addr string
	cas  map[uint16]bool
	cl   rtuClient
	up   atomic.Bool
}

// rtuClient is what the FEP needs from a field protocol master (IEC 104 client or IEC 101 master).
type rtuClient interface {
	Run(ctx context.Context) error
	Interrogate(ca uint16) error
	SelectExecute(ctx context.Context, ca uint16, ioa uint32, dcs uint8) error
}

func quality(q uint8) string {
	switch {
	case q&iec104.QInvalid != 0:
		return "invalid"
	case q&iec104.QNotTopical != 0:
		return "stale"
	case q&iec104.QSubstitute != 0:
		return "substituted"
	case q&iec104.QBlocked != 0:
		return "blocked"
	case q&iec104.QOverflow != 0:
		return "overflow"
	}
	return "good"
}

func main() {
	cim := flag.String("cim", env("CIM_URL", "http://127.0.0.1:8081"), "cim-pds URL")
	model := flag.String("model", "ieee118", "model id")
	var ingests multi
	flag.Var(&ingests, "ingest", "scada-core ingest URL; repeatable (telemetry is fanned out to every node, each with its own buffer)")
	opcAddr := flag.String("opcua", "", "listen address of the read-only OPC UA server (SecurityPolicy None, anonymous); empty = off. Bind loopback or a protected segment only")
	httpAddr := flag.String("http", "127.0.0.1:8084", "command/metrics HTTP address")
	var rtus multi
	flag.Var(&rtus, "rtu", "outstation address[@caFrom-caTo]; repeatable (default 127.0.0.1:2404 for all CAs)")
	tlsCA := flag.String("tls-ca", "", "CA bundle to verify outstations (enables TLS)")
	tlsCert := flag.String("tls-cert", "", "client certificate (mutual TLS)")
	tlsKey := flag.String("tls-key", "", "client key")
	tlsName := flag.String("tls-name", "", "expected outstation server name")
	tlsCRL := flag.String("tls-crl", "", "CRL file (reloaded when it changes; an expired or unreadable CRL fails closed)")
	tlsAllow := flag.String("tls-allow", "", "comma list of certificate names the outstation may present (default: -tls-name only)")
	authKeyFile := flag.String("auth-key-file", env("GS_AUTH_KEY_FILE", ""), "file holding the shared key (>=16 bytes) for application-layer command authentication")
	authUser := flag.Int("auth-user", 1, "user number sent with authenticated commands")
	flag.Parse()
	if len(ingests) == 0 {
		ingests = multi{env("SCADA_INGEST_URL", "http://127.0.0.1:8083/api/ingest")}
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))

	if err := token.LoadFromEnv(); err != nil {
		log.Error("token keys", "err", err)
		os.Exit(2)
	}
	secret := []byte(os.Getenv("GS_SECRET"))
	if token.Asymmetric() {
		log.Info("asymmetric (Ed25519) tokens: only v2 tokens are accepted")
	} else if len(secret) < 16 {
		if os.Getenv("GS_DEV") == "1" {
			secret = []byte("dev-secret-do-not-use-in-prod")
			log.Warn("using insecure development secret (GS_DEV=1)")
		} else {
			log.Error("GS_SECRET (>=16 chars) required; GS_DEV=1 for local development only")
			os.Exit(2)
		}
	}
	mint := func(sub, role string, ttl time.Duration) string {
		return token.Mint(secret, token.Claims{Sub: sub, Role: role, Jur: []string{"*"}, Exp: time.Now().Add(ttl).Unix()})
	}
	pl, err := points.Fetch(*cim, *model, mint("fep-gateway", "viewer", 5*time.Minute))
	if err != nil {
		log.Error("point list", "err", err)
		os.Exit(1)
	}
	byKey := map[[2]uint32]*tagInfo{}
	byTag := map[string]*tagInfo{}
	allCA := map[uint16]bool{}
	for _, p := range pl.Points {
		ti := &tagInfo{p}
		byKey[[2]uint32{uint32(p.CA), p.IOA}] = ti
		byTag[p.Tag] = ti
		allCA[p.CA] = true
	}
	cache := &liveCache{m: map[string]*cacheEnt{}}
	for _, p := range pl.Points {
		cache.m[p.Tag] = &cacheEnt{}
		cache.pts = append(cache.pts, opcua.Point{Tag: p.Tag, Group: p.Substation, Unit: p.Unit, Quantity: p.Quantity, Status: p.Kind == "status"})
	}
	log.Info("point list loaded", "model", pl.ModelID, "version", pl.Version, "points", len(pl.Points), "stations", len(allCA))
	if len(rtus) == 0 {
		rtus = multi{"127.0.0.1:2404"}
	}

	var tlsCfg *tls.Config
	if *tlsCA != "" {
		prof := secure.Profile{CertFile: *tlsCert, KeyFile: *tlsKey, CAFile: *tlsCA, CRLFile: *tlsCRL,
			OnWarn: func(msg string) { log.Warn("tls", "msg", msg) }}
		if *tlsAllow != "" {
			prof.AllowedNames = strings.Split(*tlsAllow, ",")
		} else if *tlsName != "" {
			prof.AllowedNames = []string{*tlsName}
		}
		var err error
		if tlsCfg, err = secure.Client(prof, *tlsName); err != nil {
			log.Error("tls profile", "err", err)
			os.Exit(1)
		}
		log.Info("IEC 62351-3 style TLS enabled for outstation links", "mutual", *tlsCert != "", "crl", *tlsCRL != "")
	}
	var authCfg *iec104.AuthConfig
	if *authKeyFile != "" {
		k, err := os.ReadFile(*authKeyFile)
		k = bytes.TrimSpace(k)
		if err != nil || len(k) < 16 {
			log.Error("auth key file unreadable or shorter than 16 bytes", "err", err)
			os.Exit(1)
		}
		authCfg = &iec104.AuthConfig{Key: k, User: uint16(*authUser)}
		log.Info("application-layer command authentication enabled (HMAC-SHA256 challenge/reply)")
	}

	var m metrics
	out := make(chan sample, 200000)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	handle := func(a iec104.ASDU) {
		m.asdu.Add(1)
		rx := time.Now().UTC()
		for _, o := range a.Objects {
			ti, ok := byKey[[2]uint32{uint32(a.CA), o.IOA}]
			if !ok {
				m.unknown.Add(1)
				continue
			}
			s := sample{Tag: ti.p.Tag, Kind: ti.p.Kind, Qty: ti.p.Quantity, Unit: ti.p.Unit, Quality: quality(o.Quality), RxTS: rx.Format(time.RFC3339Nano), Cause: a.Cause}
			if o.Time != nil {
				s.SrcTS = o.Time.Format(time.RFC3339Nano)
				if skew := rx.Sub(*o.Time).Milliseconds(); skew > m.maxSkewMs.Load() {
					m.maxSkewMs.Store(skew)
				}
			}
			switch a.Type {
			case iec104.M_ME_NC_1, iec104.M_ME_TF_1:
				v := float64(o.Float)
				s.Value = &v
			case iec104.M_DP_NA_1, iec104.M_DP_TB_1:
				st := int(o.DPI)
				s.State = &st
			default:
				continue
			}
			cts := rx
			if o.Time != nil {
				cts = *o.Time
			}
			cache.put(s, cts)
			select {
			case out <- s:
				m.samples.Add(1)
			default:
				m.dropped.Add(1) // never block the field side
			}
		}
	}

	var rs []*rtu
	for _, spec := range rtus {
		addr, cas := parseRTU(spec, allCA)
		r := &rtu{addr: addr, cas: cas}
		onState := func(up bool) {
			r.up.Store(up)
			log.Info("outstation link", "addr", addr, "up", up)
			if !up { // comms loss: tell every core node which points are no longer being refreshed
				n := 0
				cache.markStale(func(tag string) bool { ti := byTag[tag]; return ti != nil && r.cas[ti.p.CA] })
				for tag, ti := range byTag {
					if !r.cas[ti.p.CA] {
						continue
					}
					select {
					case out <- sample{Tag: tag, Kind: ti.p.Kind, Quality: "stale", Stale: true, RxTS: time.Now().UTC().Format(time.RFC3339Nano)}:
						n++
					default:
						m.dropped.Add(1)
					}
				}
				log.Warn("comms loss: points marked stale", "addr", addr, "points", n)
			}
		}
		if cl, err := newClient(addr, cas, tlsCfg, authCfg, handle, onState, func(tag string) (uint16, uint32, bool) {
			if ti, ok := byTag[tag]; ok {
				return ti.p.CA, ti.p.IOA, true
			}
			return 0, 0, false
		}); err != nil {
			log.Error("outstation spec", "spec", spec, "err", err)
			os.Exit(2)
		} else {
			r.cl = cl
		}
		rs = append(rs, r)
		go func() { // supervisor: reconnect with backoff, GI on connect, periodic GI
			backoff := time.Second
			for ctx.Err() == nil {
				runCtx, cancel := context.WithCancel(ctx)
				go func() {
					for i := 0; i < 50 && runCtx.Err() == nil; i++ { // wait for STARTDT
						if r.up.Load() {
							interrogateAll(r, &m, log)
							t := time.NewTicker(5 * time.Minute)
							defer t.Stop()
							for {
								select {
								case <-t.C:
									interrogateAll(r, &m, log)
								case <-runCtx.Done():
									return
								}
							}
						}
						time.Sleep(100 * time.Millisecond)
					}
				}()
				t0 := time.Now()
				err := r.cl.Run(runCtx)
				cancel()
				if time.Since(t0) > 30*time.Second {
					backoff = time.Second
				}
				if ctx.Err() != nil {
					return
				}
				log.Warn("session ended", "addr", addr, "err", err, "retry_in", backoff)
				time.Sleep(backoff)
				if backoff < 30*time.Second {
					backoff *= 2
				}
			}
		}()
	}

	// fan-out: every scada-core node gets the full stream through its own bounded buffer, so a dead or slow
	// node can never delay or starve a healthy one.
	targets := make([]chan sample, len(ingests))
	for i, url := range ingests {
		targets[i] = make(chan sample, 200000)
		go forward(ctx, url, targets[i], mint, &m, func() { // a core node was unreachable and is back: resynchronise it
			for _, r := range rs {
				if r.up.Load() {
					interrogateAll(r, &m, log)
				}
			}
		})
	}
	go func() {
		for {
			select {
			case s := <-out:
				for _, t := range targets {
					select {
					case t <- s:
					default:
						m.dropped.Add(1)
					}
				}
			case <-ctx.Done():
				return
			}
		}
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		up := 0
		for _, x := range rs {
			if x.up.Load() {
				up++
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"status": "ok", "service": "fep-gateway", "outstations_up": up, "outstations": len(rs)})
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]int64{"asdu_in": m.asdu.Load(), "samples_out": m.samples.Load(), "unknown_points": m.unknown.Load(),
			"batches_ok": m.batches.Load(), "post_attempts": m.posts.Load(), "post_failures": m.postFail.Load(), "dropped": m.dropped.Load(),
			"interrogations": m.interrog.Load(), "commands_ok": m.cmdOK.Load(), "commands_failed": m.cmdFail.Load(), "max_source_skew_ms": m.maxSkewMs.Load()})
	})
	mux.HandleFunc("/gi", func(w http.ResponseWriter, r *http.Request) {
		c, err := token.Verify(secret, strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), time.Now())
		if err != nil || !c.AtLeast("operator") || r.Method != http.MethodPost {
			http.Error(w, `{"error":"operator token required (POST)"}`, 401)
			return
		}
		for _, x := range rs {
			if x.up.Load() {
				go interrogateAll(x, &m, log)
			}
		}
		w.WriteHeader(202)
		w.Write([]byte(`{"ok":true,"general_interrogation":"started"}`))
	})
	mux.HandleFunc("/command", func(w http.ResponseWriter, r *http.Request) {
		cmdHandler(w, r, secret, byTag, rs, &m, log)
	})
	srv := &http.Server{Addr: *httpAddr, Handler: mux}
	go func() { <-ctx.Done(); srv.Close() }()
	log.Info("fep ready", "http", *httpAddr, "rtus", []string(rtus), "ingest", []string(ingests))
	if *opcAddr != "" {
		ou := &opcua.Server{Src: cache, Log: log, EndpointURL: "opc.tcp://" + *opcAddr}
		go func() {
			if err := ou.ListenAndServe(ctx, *opcAddr); err != nil && ctx.Err() == nil {
				log.Error("opcua server", "err", err)
			}
		}()
		log.Info("OPC UA server (read-only, SecurityPolicy None, anonymous)", "addr", *opcAddr, "points", len(cache.pts))
	}
	if err := srv.ListenAndServe(); err != nil && ctx.Err() == nil {
		log.Error("http", "err", err)
	}
}

// forward batches samples as NDJSON to one scada-core node; bounded retry; never blocks the field side.
func forward(ctx context.Context, url string, in chan sample, mint func(string, string, time.Duration) string, m *metrics, resync func()) {
	hc := &http.Client{Timeout: 10 * time.Second}
	tok, tokExp := "", time.Time{}
	var batch []sample
	failed := false
	flush := func() {
		if len(batch) == 0 {
			return
		}
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		for _, s := range batch {
			enc.Encode(s)
		}
		for attempt := 0; attempt < 4; attempt++ {
			if time.Now().After(tokExp) {
				tok, tokExp = mint("fep-gateway", "operator", 10*time.Minute), time.Now().Add(9*time.Minute)
			}
			req, _ := http.NewRequest("POST", url, bytes.NewReader(buf.Bytes()))
			req.Header.Set("Authorization", "Bearer "+tok)
			req.Header.Set("Content-Type", "application/x-ndjson")
			resp, err := hc.Do(req)
			m.posts.Add(1)
			if err == nil {
				resp.Body.Close()
				if resp.StatusCode == 200 {
					m.batches.Add(1)
					batch = batch[:0]
					if failed {
						failed = false
						go resync()
					}
					return
				}
			}
			m.postFail.Add(1)
			time.Sleep(time.Duration(1<<attempt) * 100 * time.Millisecond)
		}
		failed = true
		m.dropped.Add(int64(len(batch))) // this node missed a batch; resynchronised by a general interrogation once it is back
		batch = batch[:0]
	}
	tk := time.NewTicker(200 * time.Millisecond)
	defer tk.Stop()
	hb := time.NewTicker(5 * time.Second)
	defer hb.Stop()
	for {
		select {
		case <-hb.C:
			batch = append(batch, sample{HB: true, Quality: "good", RxTS: time.Now().UTC().Format(time.RFC3339Nano)})
		case s := <-in:
			batch = append(batch, s)
			if len(batch) >= 2000 {
				flush()
			}
		case <-tk.C:
			flush()
		case <-ctx.Done():
			flush()
			return
		}
	}
}

func interrogateAll(r *rtu, m *metrics, log *slog.Logger) {
	for ca := range r.cas {
		if err := r.cl.Interrogate(ca); err != nil {
			log.Warn("interrogation failed", "ca", ca, "err", err)
			return
		}
		m.interrog.Add(1)
		time.Sleep(2 * time.Millisecond)
	}
}

// fenceMax is the highest leader term seen on the command path (in memory: a restarted gateway starts again at 0).
var fenceMax atomic.Int64

func cmdHandler(w http.ResponseWriter, r *http.Request, secret []byte, byTag map[string]*tagInfo, rs []*rtu, m *metrics, log *slog.Logger) {
	reply := func(code int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		json.NewEncoder(w).Encode(v)
	}
	if r.Method != http.MethodPost {
		reply(405, map[string]string{"error": "POST only"})
		return
	}
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	c, err := token.Verify(secret, tok, time.Now())
	if err != nil {
		reply(401, map[string]string{"error": err.Error()})
		return
	}
	if !c.AtLeast("operator") {
		reply(403, map[string]string{"error": "operator role required"})
		return
	}
	// fencing: a control-centre leader sends its election term; a command from an older term (a deposed leader that has not
	// noticed yet) is refused, and once any term has been seen a command without one is refused too (no downgrade).
	if ft := r.Header.Get("X-Fence-Term"); ft != "" || fenceMax.Load() > 0 {
		term, perr := strconv.ParseInt(ft, 10, 64)
		if perr != nil || term <= 0 {
			reply(409, map[string]string{"error": "fenced: a valid leader term is required"})
			return
		}
		for {
			cur := fenceMax.Load()
			if term < cur {
				reply(409, map[string]string{"error": fmt.Sprintf("fenced: leader term %d is older than %d", term, cur)})
				return
			}
			if term == cur || fenceMax.CompareAndSwap(cur, term) {
				break
			}
		}
	}
	var req struct {
		Tag    string `json:"tag"`
		Action string `json:"action"`
	}
	if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&req) != nil {
		reply(400, map[string]string{"error": "bad json"})
		return
	}
	ti, ok := byTag[req.Tag]
	if !ok || ti.p.Kind != "status" {
		reply(404, map[string]string{"error": "unknown or non-controllable tag"})
		return
	}
	dcs := map[string]uint8{"open": 1, "close": 2}[req.Action]
	if dcs == 0 {
		reply(400, map[string]string{"error": "action must be open or close"})
		return
	}
	var target *rtu
	for _, x := range rs {
		if x.cas[ti.p.CA] {
			target = x
		}
	}
	if target == nil || !target.up.Load() {
		m.cmdFail.Add(1)
		reply(503, map[string]string{"error": "outstation not connected"})
		return
	}
	t0 := time.Now()
	ctx, cancel := context.WithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	err = target.cl.SelectExecute(ctx, ti.p.CA, ti.p.IOA, dcs)
	log.Info("AUDIT command", "user", c.Sub, "tag", req.Tag, "action", req.Action, "ok", err == nil, "err", fmt.Sprint(err), "ms", time.Since(t0).Milliseconds())
	if err != nil {
		m.cmdFail.Add(1)
		reply(502, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	m.cmdOK.Add(1)
	reply(200, map[string]any{"ok": true, "tag": req.Tag, "action": req.Action, "ms": time.Since(t0).Milliseconds()})
}

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ",") }
func (m *multi) Set(s string) error { *m = append(*m, s); return nil }

// newClient builds the field-protocol master for an address spec:
//
//	host:port                     IEC 104 (TLS and auth per flags)
//	c37118://host:port#ID/PREFIX  C37.118 PMU stream, points <PREFIX>.* from the model's point list (read-only)
//	dnp3://host:port#ADDR@CA      DNP3 (one CA per outstation; TLS per flags; local address 1)
//	iec101+tcp://host:port#LINK   IEC 101 FT1.2 over a TCP serial server
//	iec101+dev:/dev/ttyS0#LINK    IEC 101 on a serial device (the line must already be configured, e.g. with stty)
//
// LINK is the slave link address (default 1). TLS/auth do not apply to IEC 101 links.
func newClient(spec string, cas map[uint16]bool, tlsCfg *tls.Config, auth *iec104.AuthConfig, on func(iec104.ASDU), st func(bool), lk pmubridge.Addr) (rtuClient, error) {
	if strings.HasPrefix(spec, "c37118://") {
		a, rest, _ := strings.Cut(strings.TrimPrefix(spec, "c37118://"), "#")
		idS, prefix, _ := strings.Cut(rest, "/")
		id, err := strconv.Atoi(idS)
		if err != nil || id < 1 || id > 65534 || prefix == "" {
			return nil, fmt.Errorf("bad PMU spec %q: want c37118://host:port#IDCODE/PREFIX", spec)
		}
		open := func() (io.ReadWriteCloser, error) {
			if tlsCfg != nil {
				return tls.DialWithDialer(&net.Dialer{Timeout: 5 * time.Second}, "tcp", a, tlsCfg)
			}
			return net.DialTimeout("tcp", a, 5*time.Second)
		}
		return &pmubridge.Bridge{Open: open, IDCode: uint16(id), Prefix: prefix, Lookup: lk, OnASDU: on, OnState: st}, nil
	}
	if strings.HasPrefix(spec, "dnp3://") {
		a, dest, _ := strings.Cut(strings.TrimPrefix(spec, "dnp3://"), "#")
		d := 10
		if dest != "" {
			n, err := strconv.Atoi(dest)
			if err != nil || n < 0 || n > 65519 {
				return nil, fmt.Errorf("bad DNP3 address %q", dest)
			}
			d = n
		}
		if len(cas) != 1 {
			return nil, fmt.Errorf("a DNP3 outstation serves exactly one common address: give dnp3://host:port#addr@CA (got %d CAs)", len(cas))
		}
		var ca uint16
		for k := range cas {
			ca = k
		}
		open := func() (io.ReadWriteCloser, error) {
			if tlsCfg != nil {
				return tls.DialWithDialer(&net.Dialer{Timeout: 5 * time.Second}, "tcp", a, tlsCfg)
			}
			return net.DialTimeout("tcp", a, 5*time.Second)
		}
		return &dnp3.Master{Open: open, Local: 1, Remote: uint16(d), CA: ca, OnASDU: on, OnState: st}, nil
	}
	if !strings.HasPrefix(spec, "iec101+") {
		c := &iec104.Client{Addr: spec, TLS: tlsCfg, Auth: auth, OnASDU: on, OnState: st}
		return c, nil
	}
	rest, link, _ := strings.Cut(spec, "#")
	la := 1
	if link != "" {
		n, err := strconv.Atoi(link)
		if err != nil || n < 0 || n > 65534 {
			return nil, fmt.Errorf("bad link address %q", link)
		}
		la = n
	}
	p := iec101.Default
	if la > 255 {
		p.LinkAddr = 2
	}
	var open func() (io.ReadWriteCloser, error)
	switch {
	case strings.HasPrefix(rest, "iec101+tcp://"):
		a := strings.TrimPrefix(rest, "iec101+tcp://")
		open = func() (io.ReadWriteCloser, error) { return net.DialTimeout("tcp", a, 5*time.Second) }
	case strings.HasPrefix(rest, "iec101+dev:"):
		path := strings.TrimPrefix(rest, "iec101+dev:")
		open = func() (io.ReadWriteCloser, error) { return os.OpenFile(path, os.O_RDWR, 0) }
	default:
		return nil, fmt.Errorf("unknown scheme in %q", spec)
	}
	return &iec101.Master{Open: open, Link: uint16(la), P: p, OnASDU: on, OnState: st}, nil
}

func parseRTU(spec string, all map[uint16]bool) (string, map[uint16]bool) {
	addr, rng, has := strings.Cut(spec, "@")
	cas := map[uint16]bool{}
	if !has {
		for ca := range all {
			cas[ca] = true
		}
		return addr, cas
	}
	a, b, _ := strings.Cut(rng, "-")
	lo, _ := strconv.Atoi(a)
	hi := lo
	if b != "" {
		hi, _ = strconv.Atoi(b)
	}
	for ca := range all {
		if int(ca) >= lo && int(ca) <= hi {
			cas[ca] = true
		}
	}
	return addr, cas
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

var _ sync.Mutex
