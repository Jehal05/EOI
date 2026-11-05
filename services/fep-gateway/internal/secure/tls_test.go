package secure

import (
	"crypto/tls"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type env struct {
	dir string
	ca  *CA
}

func newEnv(t *testing.T) *env {
	d := t.TempDir()
	ca, err := NewCA("test ca", 30)
	if err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(d, "ca.pem"), ca.CertPEM(), 0o600)
	return &env{d, ca}
}

func (e *env) issue(t *testing.T, ca *CA, cn string, server bool) (*Issued, string, string) {
	is, err := ca.Issue(cn, []string{cn}, server, 30)
	if err != nil {
		t.Fatal(err)
	}
	c, k := filepath.Join(e.dir, cn+".pem"), filepath.Join(e.dir, cn+".key")
	os.WriteFile(c, is.CertPEM, 0o600)
	os.WriteFile(k, is.KeyPEM, 0o600)
	return is, c, k
}

// handshake runs a server and a client over TCP; it returns both errors.
func handshake(t *testing.T, sc, cc *tls.Config) (srvErr, cliErr error, ver uint16) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	done := make(chan error, 1)
	go func() {
		c, err := ln.Accept()
		if err != nil {
			done <- err
			return
		}
		defer c.Close()
		tc := tls.Server(c, sc)
		done <- tc.Handshake()
	}()
	c, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	c.SetDeadline(time.Now().Add(5 * time.Second))
	tc := tls.Client(c, cc)
	cliErr = tc.Handshake()
	if cliErr == nil {
		// TLS 1.3 reports client-certificate rejection on the first read
		buf := make([]byte, 1)
		tc.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
		if _, err := tc.Read(buf); err != nil && !strings.Contains(err.Error(), "timeout") && !strings.Contains(err.Error(), "EOF") {
			cliErr = err
		}
		ver = tc.ConnectionState().Version
	}
	c.Close() // the server side may still be waiting for the client
	return <-done, cliErr, ver
}

func profiles(t *testing.T, e *env, client string, p Profile) (*tls.Config, *tls.Config) {
	_, sc, sk := e.issue(t, e.ca, "outstation-1", true)
	_, cc, ck := e.issue(t, e.ca, client, false)
	sp := Profile{CertFile: sc, KeyFile: sk, CAFile: filepath.Join(e.dir, "ca.pem"), AllowedNames: p.AllowedNames, CRLFile: p.CRLFile}
	cp := Profile{CertFile: cc, KeyFile: ck, CAFile: filepath.Join(e.dir, "ca.pem")}
	s, err := Server(sp)
	if err != nil {
		t.Fatal(err)
	}
	c, err := Client(cp, "outstation-1")
	if err != nil {
		t.Fatal(err)
	}
	return s, c
}

func TestMutualHandshakeWorksAndUsesModernTLS(t *testing.T) {
	e := newEnv(t)
	s, c := profiles(t, e, "fep-1", Profile{})
	se, ce, ver := handshake(t, s, c)
	if se != nil || ce != nil {
		t.Fatalf("handshake failed: %v / %v", se, ce)
	}
	if ver < tls.VersionTLS12 {
		t.Fatalf("version %x", ver)
	}
}

func TestTLS12SuitesAreAEADOnly(t *testing.T) {
	for _, id := range CipherSuites {
		name := tls.CipherSuiteName(id)
		if !strings.Contains(name, "GCM") && !strings.Contains(name, "CHACHA20") || !strings.Contains(name, "ECDHE") {
			t.Fatalf("weak suite %s", name)
		}
	}
}

func TestOldTLSVersionsAreRefused(t *testing.T) {
	e := newEnv(t)
	s, c := profiles(t, e, "fep-1", Profile{})
	c.MaxVersion = tls.VersionTLS11
	se, ce, _ := handshake(t, s, c)
	if se == nil && ce == nil {
		t.Fatal("TLS 1.1 accepted")
	}
}

func TestTLS13OnlyProfile(t *testing.T) {
	e := newEnv(t)
	_, sc, sk := e.issue(t, e.ca, "outstation-1", true)
	_, cc, ck := e.issue(t, e.ca, "fep-1", false)
	ca := filepath.Join(e.dir, "ca.pem")
	s, _ := Server(Profile{CertFile: sc, KeyFile: sk, CAFile: ca, TLS13Only: true})
	c, _ := Client(Profile{CertFile: cc, KeyFile: ck, CAFile: ca}, "outstation-1")
	c.MaxVersion = tls.VersionTLS12
	se, ce, _ := handshake(t, s, c)
	if se == nil && ce == nil {
		t.Fatal("TLS 1.2 accepted by a TLS 1.3-only server")
	}
}

func TestClientWithoutCertificateIsRefused(t *testing.T) {
	e := newEnv(t)
	s, c := profiles(t, e, "fep-1", Profile{})
	c.Certificates = nil
	se, ce, _ := handshake(t, s, c)
	if se == nil && ce == nil {
		t.Fatal("anonymous client accepted")
	}
}

func TestCertificateFromAnotherCAIsRefused(t *testing.T) {
	e := newEnv(t)
	s, _ := profiles(t, e, "fep-1", Profile{})
	other := newEnv(t)
	_, oc, ok := other.issue(t, other.ca, "fep-1", false)
	c, err := Client(Profile{CertFile: oc, KeyFile: ok, CAFile: filepath.Join(e.dir, "ca.pem")}, "outstation-1")
	if err != nil {
		t.Fatal(err)
	}
	se, ce, _ := handshake(t, s, c)
	if se == nil && ce == nil {
		t.Fatal("foreign CA accepted")
	}
}

func TestAllowListAndNameMismatch(t *testing.T) {
	e := newEnv(t)
	s, c := profiles(t, e, "fep-1", Profile{AllowedNames: []string{"fep-1"}})
	if se, ce, _ := handshake(t, s, c); se != nil || ce != nil {
		t.Fatalf("allowed name refused: %v %v", se, ce)
	}
	e2 := newEnv(t)
	s2, c2 := profiles(t, e2, "intruder", Profile{AllowedNames: []string{"fep-1"}})
	if se, ce, _ := handshake(t, s2, c2); se == nil && ce == nil {
		t.Fatal("name outside the allow-list accepted")
	}
	e3 := newEnv(t)
	s3, c3 := profiles(t, e3, "fep-1", Profile{})
	c3.ServerName = "someone-else"
	if se, ce, _ := handshake(t, s3, c3); se == nil && ce == nil {
		t.Fatal("server name mismatch accepted")
	}
}

func TestRevocationListIsEnforcedAndReloaded(t *testing.T) {
	e := newEnv(t)
	crlPath := filepath.Join(e.dir, "crl.pem")
	empty, _ := e.ca.CRL(nil, 1, time.Hour)
	os.WriteFile(crlPath, empty, 0o600)
	s, c := profiles(t, e, "fep-1", Profile{CRLFile: crlPath})
	if se, ce, _ := handshake(t, s, c); se != nil || ce != nil {
		t.Fatalf("clean CRL refused: %v %v", se, ce)
	}
	leaf := c.Certificates[0].Leaf
	if leaf == nil {
		x, _ := tls.X509KeyPair(nil, nil)
		_ = x
	}
	var serialNo *big.Int
	for _, der := range c.Certificates[0].Certificate[:1] {
		cert, _ := parse(der)
		serialNo = cert.SerialNumber
	}
	time.Sleep(1100 * time.Millisecond) // file modification time granularity
	rev, _ := e.ca.CRL([]*big.Int{serialNo}, 2, time.Hour)
	os.WriteFile(crlPath, rev, 0o600)
	if se, ce, _ := handshake(t, s, c); se == nil && ce == nil {
		t.Fatal("revoked certificate accepted after the CRL changed")
	}
}

func TestMissingOrStaleCRLFailsClosed(t *testing.T) {
	e := newEnv(t)
	crlPath := filepath.Join(e.dir, "crl.pem")
	s, c := profiles(t, e, "fep-1", Profile{CRLFile: crlPath}) // file does not exist
	if se, ce, _ := handshake(t, s, c); se == nil && ce == nil {
		t.Fatal("accepted with no revocation list")
	}
	stale, _ := e.ca.CRL(nil, 1, -time.Hour)
	os.WriteFile(crlPath, stale, 0o600)
	if se, ce, _ := handshake(t, s, c); se == nil && ce == nil {
		t.Fatal("accepted with a stale revocation list")
	}
}

func TestProfileRequiresAllFiles(t *testing.T) {
	if _, err := Server(Profile{}); err == nil {
		t.Fatal("empty profile accepted")
	}
}

func TestExpiryWarning(t *testing.T) {
	e := newEnv(t)
	is, _ := e.ca.Issue("short", []string{"short"}, false, 1)
	c, k := filepath.Join(e.dir, "s.pem"), filepath.Join(e.dir, "s.key")
	os.WriteFile(c, is.CertPEM, 0o600)
	os.WriteFile(k, is.KeyPEM, 0o600)
	var got string
	if _, err := Client(Profile{CertFile: c, KeyFile: k, CAFile: filepath.Join(e.dir, "ca.pem"), OnWarn: func(s string) { got = s }}, "x"); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(got, "expires") {
		t.Fatalf("no expiry warning: %q", got)
	}
}
