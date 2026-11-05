// Command mtlsproxy runs mTLS ingress/egress listeners (see internal/mtlsproxy).
//
//	mtlsproxy -cert C -key K -ca CA [-crl CRL] \
//	   -ingress 127.0.0.1:9083=http://127.0.0.1:8083 \
//	   -internal /api/replicate,/api/cluster/:core-A,core-B,core-C \
//	   -egress 127.0.0.1:19183=https://127.0.0.1:9183@core-B
package main

import (
	"crypto/tls"
	"flag"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"gridsovereign/fep/internal/mtlsproxy"
	"gridsovereign/fep/internal/secure"
)

type multi []string

func (m *multi) String() string     { return strings.Join(*m, ";") }
func (m *multi) Set(v string) error { *m = append(*m, v); return nil }

func main() {
	cert := flag.String("cert", "", "own certificate")
	key := flag.String("key", "", "own key")
	ca := flag.String("ca", "", "CA certificate")
	crl := flag.String("crl", "", "CRL file (optional)")
	var in, internal, eg multi
	flag.Var(&in, "ingress", "LISTEN=UPSTREAM_URL (repeatable)")
	flag.Var(&internal, "internal", "PREFIX[,PREFIX]:NAME[,NAME] restricts path prefixes to certificate names (repeatable)")
	flag.Var(&eg, "egress", "LISTEN=REMOTE_URL@SERVER_NAME (repeatable)")
	flag.Parse()
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	prof := secure.Profile{CertFile: *cert, KeyFile: *key, CAFile: *ca, CRLFile: *crl, TLS13Only: true,
		OnWarn: func(s string) { log.Warn(s) }}
	var rules []mtlsproxy.Rule
	for _, r := range internal {
		p, n, ok := strings.Cut(r, ":")
		if !ok {
			log.Error("bad -internal", "v", r)
			os.Exit(2)
		}
		for _, pre := range strings.Split(p, ",") {
			rules = append(rules, mtlsproxy.Rule{Prefix: pre, Names: strings.Split(n, ",")})
		}
	}
	done := make(chan error, 8)
	for _, s := range in {
		l, u, _ := strings.Cut(s, "=")
		up, err := url.Parse(u)
		if err != nil || up.Scheme != "http" || !strings.HasPrefix(up.Host, "127.0.0.1") {
			log.Error("ingress upstream must be a loopback http URL", "v", u)
			os.Exit(2)
		}
		cfg, err := secure.Server(prof)
		if err != nil {
			log.Error("tls", "err", err)
			os.Exit(1)
		}
		srv := &http.Server{Addr: l, Handler: mtlsproxy.Ingress(up, rules), TLSConfig: cfg, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second}
		go func() { done <- srv.ListenAndServeTLS("", "") }()
		log.Info("ingress", "listen", l, "upstream", u)
	}
	for _, s := range eg {
		l, rest, _ := strings.Cut(s, "=")
		u, name, _ := strings.Cut(rest, "@")
		ru, err := url.Parse(u)
		if err != nil || ru.Scheme != "https" || !strings.HasPrefix(l, "127.0.0.1") {
			log.Error("egress needs a loopback listener and an https remote", "v", s)
			os.Exit(2)
		}
		var cfg *tls.Config
		if cfg, err = secure.Client(prof, name); err != nil {
			log.Error("tls", "err", err)
			os.Exit(1)
		}
		srv := &http.Server{Addr: l, Handler: mtlsproxy.Egress(ru, cfg), ReadHeaderTimeout: 5 * time.Second}
		go func() { done <- srv.ListenAndServe() }()
		log.Info("egress", "listen", l, "remote", u, "server_name", name)
	}
	log.Error("proxy stopped", "err", <-done)
	os.Exit(1)
}
