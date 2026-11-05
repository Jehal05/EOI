// outstation-sim: a multi-RTU IEC 60870-5-104 outstation simulator. Values arrive from the
// field physics simulator over HTTP (/ingest); breaker commands are forwarded back to it.
package main

import (
	"bytes"
	"crypto/tls"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"math"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"gridsovereign/fep/internal/dnp3"
	"gridsovereign/fep/internal/iec101"
	"gridsovereign/fep/internal/iec104"
	"gridsovereign/fep/internal/points"
	"gridsovereign/fep/internal/secure"
)

type store struct {
	mu    sync.Mutex
	pts   map[[2]uint32]*iec104.Point
	last  map[[2]uint32]float32 // last published analog value
	limit map[[2]uint32]float64
	fwd   string // field-sim URL for commands
}

func (s *store) Snapshot(ca uint16) []iec104.Point {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []iec104.Point
	for _, p := range s.pts {
		if ca == 0xFFFF || p.CA == ca {
			out = append(out, *p)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		return out[i].CA < out[j].CA || (out[i].CA == out[j].CA && out[i].IOA < out[j].IOA)
	})
	return out
}

func (s *store) IsStatus(ca uint16, ioa uint32) (bool, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.pts[[2]uint32{uint32(ca), ioa}]
	if !ok {
		return false, false
	}
	return p.Status, true
}

func (s *store) Command(ca uint16, ioa uint32, dcs uint8) error {
	if s.fwd == "" {
		return errors.New("no field simulator attached: command not executable")
	}
	b, _ := json.Marshal(map[string]any{"ca": ca, "ioa": ioa, "dcs": dcs})
	resp, err := (&http.Client{Timeout: 5 * time.Second}).Post(s.fwd+"/command", "application/json", bytes.NewReader(b))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("field rejected command: %s", resp.Status)
	}
	return nil
}

type update struct {
	CA  uint16   `json:"ca"`
	IOA uint32   `json:"ioa"`
	V   *float64 `json:"v,omitempty"`   // analog value
	DPI *uint8   `json:"dpi,omitempty"` // status
	Q   uint8    `json:"q,omitempty"`
}

func main() {
	listen := flag.String("listen", "127.0.0.1:2404", "IEC 104 listen address")
	httpAddr := flag.String("http", "127.0.0.1:8090", "ingest/control HTTP address")
	cim := flag.String("cim", env("CIM_URL", "http://127.0.0.1:8081"), "cim-pds URL")
	model := flag.String("model", "ieee118", "model id")
	token := flag.String("token", os.Getenv("CIM_TOKEN"), "bearer token for cim-pds")
	fieldSim := flag.String("field-sim", env("FIELD_SIM_URL", "http://127.0.0.1:8091"), "physics simulator URL (for commands)")
	deadband := flag.Float64("deadband", 0.002, "analog report-by-exception deadband as fraction of range")
	certF := flag.String("cert", "", "TLS server cert (enables TLS)")
	keyF := flag.String("key", "", "TLS server key")
	caF := flag.String("ca", "", "CA bundle for verifying master client certs (enables mutual TLS)")
	crlF := flag.String("crl", "", "CRL file for master certificates (fails closed when expired or unreadable)")
	allow := flag.String("allow", "", "comma list of master certificate names allowed to connect")
	authKeyFile := flag.String("auth-key-file", env("GS_AUTH_KEY_FILE", ""), "shared key file (>=16 bytes): control commands need an HMAC challenge/reply")
	listen101 := flag.String("listen101", "", "also serve IEC 101 FT1.2 over TCP on this address (empty: off)")
	link101 := flag.Int("link101", 1, "IEC 101 link address")
	listenDNP3 := flag.String("listen-dnp3", "", "also serve one common address over DNP3 on this address (TLS follows -cert/-key/-ca)")
	dnp3Addr := flag.Int("dnp3-addr", 10, "DNP3 outstation address")
	dnp3CA := flag.Int("dnp3-ca", 1, "common address served over DNP3")
	flag.Parse()
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if *token == "" {
		log.Error("CIM_TOKEN / -token required")
		os.Exit(2)
	}
	pl, err := points.Fetch(*cim, *model, *token)
	if err != nil {
		log.Error("point list", "err", err)
		os.Exit(1)
	}
	st := &store{pts: map[[2]uint32]*iec104.Point{}, last: map[[2]uint32]float32{}, limit: map[[2]uint32]float64{}, fwd: *fieldSim}
	now := time.Now()
	for _, p := range pl.Points {
		k := [2]uint32{uint32(p.CA), p.IOA}
		ip := &iec104.Point{CA: p.CA, IOA: p.IOA, Status: p.Kind == "status", TS: now}
		if ip.Status {
			ip.DPI = iec104.DPIOn
		} else {
			ip.Quality = iec104.QNotTopical // no value yet
			rng := 100.0
			if p.High != nil && p.Low != nil {
				rng = math.Max(math.Abs(*p.High-*p.Low), 1)
			} else if p.High != nil {
				rng = math.Max(math.Abs(*p.High), 1)
			}
			st.limit[k] = rng * *deadband
		}
		st.pts[k] = ip
	}
	log.Info("point list loaded", "model", pl.ModelID, "version", pl.Version, "points", len(pl.Points))

	srv := &iec104.Server{H: st, Log: log}
	if *authKeyFile != "" {
		k, err := os.ReadFile(*authKeyFile)
		k = bytes.TrimSpace(k)
		if err != nil || len(k) < 16 {
			log.Error("auth key file unreadable or shorter than 16 bytes", "err", err)
			os.Exit(1)
		}
		srv.Auth = &iec104.AuthConfig{Key: k}
		log.Info("application-layer command authentication required")
	}
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Error("listen", "err", err)
		os.Exit(1)
	}
	var tlsServer *tls.Config
	if *certF != "" {
		prof := secure.Profile{CertFile: *certF, KeyFile: *keyF, CAFile: *caF, CRLFile: *crlF,
			OnWarn: func(m string) { log.Warn("tls", "msg", m) }}
		if *allow != "" {
			prof.AllowedNames = strings.Split(*allow, ",")
		}
		cfg, err := secure.Server(prof)
		if err != nil {
			log.Error("tls profile", "err", err)
			os.Exit(1)
		}
		tlsServer = cfg
		ln = tls.NewListener(ln, cfg)
		log.Info("IEC 62351-3 style TLS enabled", "mutual", *caF != "", "crl", *crlF != "")
	}
	var slave *iec101.Slave
	if *listen101 != "" {
		slave = &iec101.Slave{H: st, Link: uint16(*link101), Log: log}
		if *link101 > 255 {
			slave.P = iec101.Params{LinkAddr: 2, COT: 2, CA: 2, IOA: 3}
		}
		l101, err := net.Listen("tcp", *listen101)
		if err != nil {
			log.Error("listen101", "err", err)
			os.Exit(1)
		}
		go func() { log.Error("iec101 slave stopped", "err", slave.Serve(l101)) }()
		log.Info("IEC 101 slave enabled", "addr", *listen101, "link", *link101)
	}
	var dn *dnp3.Outstation
	if *listenDNP3 != "" {
		dn = &dnp3.Outstation{H: st, CA: uint16(*dnp3CA), Addr: uint16(*dnp3Addr), Log: log}
		l3, err := net.Listen("tcp", *listenDNP3)
		if err != nil {
			log.Error("listen-dnp3", "err", err)
			os.Exit(1)
		}
		if tlsServer != nil {
			l3 = tls.NewListener(l3, tlsServer)
		}
		go func() { log.Error("dnp3 outstation stopped", "err", dn.Serve(l3)) }()
		log.Info("DNP3 outstation enabled", "addr", *listenDNP3, "dnp3_addr", *dnp3Addr, "ca", *dnp3CA, "tls", tlsServer != nil)
	}
	go func() { log.Error("iec104 server stopped", "err", srv.Serve(ln)) }()

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"ok","service":"outstation-sim"}`))
	})
	mux.HandleFunc("/ingest", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", 405)
			return
		}
		var body struct {
			Values []update `json:"values"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32<<20)).Decode(&body); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		var changed []iec104.Point
		st.mu.Lock()
		ts := time.Now()
		for _, u := range body.Values {
			k := [2]uint32{uint32(u.CA), u.IOA}
			p, ok := st.pts[k]
			if !ok {
				continue
			}
			switch {
			case p.Status && u.DPI != nil:
				if p.DPI != *u.DPI || p.Quality != u.Q {
					p.DPI, p.Quality, p.TS = *u.DPI, u.Q, ts
					changed = append(changed, *p)
				}
			case !p.Status && u.V != nil:
				v := float32(*u.V)
				first := p.Quality&iec104.QNotTopical != 0
				if first || u.Q != p.Quality || math.Abs(float64(v-st.last[k])) > st.limit[k] {
					p.Float, p.Quality, p.TS = v, u.Q, ts
					st.last[k] = v
					changed = append(changed, *p)
				} else {
					p.Float = v // keep the holding register fresh without reporting
				}
			}
		}
		st.mu.Unlock()
		if len(changed) > 0 {
			srv.Publish(changed)
			if slave != nil {
				slave.Publish(changed)
			}
			if dn != nil {
				dn.Publish(changed)
			}
		}
		json.NewEncoder(w).Encode(map[string]int{"received": len(body.Values), "reported": len(changed)})
	})
	log.Info("outstation-sim ready", "iec104", *listen, "http", *httpAddr)
	log.Error("http", "err", http.ListenAndServe(*httpAddr, mux))
}

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
