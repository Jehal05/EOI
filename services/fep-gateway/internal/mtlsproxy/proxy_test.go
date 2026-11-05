package mtlsproxy

import (
	"crypto/tls"
	"crypto/x509"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"
	"time"

	"gridsovereign/fep/internal/secure"
)

type env struct {
	dir string
	ca  *secure.CA
}

func setup(t *testing.T) *env {
	d := t.TempDir()
	ca, err := secure.NewCA("test CA", 30)
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(d, "ca.pem"), ca.CertPEM(), 0o600)
	empty, _ := ca.CRL(nil, 1, time.Hour)
	os.WriteFile(filepath.Join(d, "crl.pem"), empty, 0o600)
	return &env{d, ca}
}

func (e *env) issue(t *testing.T, cn string, server bool) secure.Profile {
	is, err := e.ca.Issue(cn, []string{"localhost", cn}, server, 30)
	if err != nil {
		t.Fatal(err)
	}
	c, k := filepath.Join(e.dir, cn+".pem"), filepath.Join(e.dir, cn+".key")
	os.WriteFile(c, is.CertPEM, 0o600)
	os.WriteFile(k, is.KeyPEM, 0o600)
	return secure.Profile{CertFile: c, KeyFile: k, CAFile: filepath.Join(e.dir, "ca.pem"), TLS13Only: true, CRLFile: filepath.Join(e.dir, "crl.pem")}
}

func start(t *testing.T, e *env, rules []Rule, seen *string) *httptest.Server {
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*seen = r.Header.Get(CNHeader)
		io.WriteString(w, "ok")
	}))
	t.Cleanup(up.Close)
	u, _ := url.Parse(up.URL)
	sp := e.issue(t, "svc", true)
	cfg, err := secure.Server(sp)
	if err != nil {
		t.Fatal(err)
	}
	p := httptest.NewUnstartedServer(Ingress(u, rules))
	p.TLS = cfg
	p.StartTLS()
	t.Cleanup(p.Close)
	return p
}

func get(t *testing.T, base string, prof *secure.Profile, path string, hdr map[string]string) (int, error) {
	var cfg *tls.Config
	if prof != nil {
		c, err := secure.Client(*prof, "svc")
		if err != nil {
			t.Fatal(err)
		}
		cfg = c
	} else {
		pool := x509.NewCertPool()
		cfg = &tls.Config{RootCAs: pool, InsecureSkipVerify: true} // no client cert, server not checked: we test the server side
	}
	req, _ := http.NewRequest("GET", base+path, nil)
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	r, err := (&http.Client{Transport: &http.Transport{TLSClientConfig: cfg}, Timeout: 5 * time.Second}).Do(req)
	if err != nil {
		return 0, err
	}
	defer r.Body.Close()
	return r.StatusCode, nil
}

func TestIngressRules(t *testing.T) {
	e := setup(t)
	var seen string
	p := start(t, e, []Rule{{"/api/replicate", []string{"core-A"}}}, &seen)
	a, b := e.issue(t, "core-A", false), e.issue(t, "ui-gw", false)
	if c, err := get(t, p.URL, &a, "/x", nil); err != nil || c != 200 {
		t.Fatal(c, err)
	}
	if c, _ := get(t, p.URL, &a, "/api/replicate", nil); c != 200 {
		t.Fatalf("allowed name refused: %d", c)
	}
	if c, _ := get(t, p.URL, &b, "/api/replicate", nil); c != 403 {
		t.Fatalf("unlisted name must get 403, got %d", c)
	}
	if c, _ := get(t, p.URL, &b, "/api/replicate/x", nil); c != 403 {
		t.Fatalf("prefix match, got %d", c)
	}
	seen = ""
	get(t, p.URL, &b, "/y", map[string]string{CNHeader: "core-A"})
	if seen != "ui-gw" {
		t.Fatalf("spoofed identity header must be overwritten, upstream saw %q", seen)
	}
}

func TestNoCertAndRevokedAreRefused(t *testing.T) {
	e := setup(t)
	var seen string
	p := start(t, e, nil, &seen)
	if c, err := get(t, p.URL, nil, "/x", nil); err == nil && c == 200 {
		t.Fatal("connection without a client certificate must fail")
	}
	bad := e.issue(t, "evil", false)
	cb, _ := os.ReadFile(bad.CertFile)
	_ = cb
	is, _ := e.ca.Issue("gone", nil, false, 30)
	crl, _ := e.ca.CRL([]*big.Int{is.Cert.SerialNumber}, 1, time.Hour)
	os.WriteFile(filepath.Join(e.dir, "crl.pem"), crl, 0o600)
	os.WriteFile(filepath.Join(e.dir, "gone.pem"), is.CertPEM, 0o600)
	os.WriteFile(filepath.Join(e.dir, "gone.key"), is.KeyPEM, 0o600)
	gone := secure.Profile{CertFile: filepath.Join(e.dir, "gone.pem"), KeyFile: filepath.Join(e.dir, "gone.key"), CAFile: filepath.Join(e.dir, "ca.pem"), TLS13Only: true}
	if c, err := get(t, p.URL, &gone, "/x", nil); err == nil && c == 200 {
		t.Fatal("revoked certificate must be refused")
	}
	ok := e.issue(t, "fine", false)
	if c, err := get(t, p.URL, &ok, "/x", nil); err != nil || c != 200 {
		t.Fatal("valid certificate refused", c, err)
	}
}

func TestEgressToIngress(t *testing.T) {
	e := setup(t)
	var seen string
	p := start(t, e, nil, &seen)
	cp := e.issue(t, "core-B", false)
	cfg, _ := secure.Client(cp, "svc")
	ru, _ := url.Parse(p.URL)
	eg := httptest.NewServer(Egress(ru, cfg))
	defer eg.Close()
	r, err := http.Get(eg.URL + "/z")
	if err != nil || r.StatusCode != 200 || seen != "core-B" {
		t.Fatal(err, r, seen)
	}
}
