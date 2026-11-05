package secure

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"
)

// Profile selects the transport-security settings for one end of a link.
type Profile struct {
	CertFile, KeyFile string
	CAFile            string        // CAs trusted to sign the peer
	AllowedNames      []string      // peer CN or DNS SAN must be in this list (empty: any name signed by the CA)
	CRLFile           string        // optional PEM CRL signed by the CA, re-read when the file changes
	TLS13Only         bool          // refuse TLS 1.2
	ExpiryWarn        time.Duration // warn through OnWarn when the own or peer certificate expires within this time (default 30 days)
	OnWarn            func(string)
}

// CipherSuites are the TLS 1.2 suites allowed: ECDHE with an AEAD only. (TLS 1.3 suites are fixed by Go and all AEAD.)
var CipherSuites = []uint16{
	tls.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384, tls.TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384,
	tls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256, tls.TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,
	tls.TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256, tls.TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256,
}

type crl struct {
	mu    sync.Mutex
	path  string
	ca    *x509.Certificate
	mtime time.Time
	size  int64
	list  *x509.RevocationList
}

func (c *crl) revoked(cert *x509.Certificate) (bool, error) {
	if c == nil || c.path == "" {
		return false, nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	st, err := os.Stat(c.path)
	if err != nil {
		return false, fmt.Errorf("revocation list unreadable: %w", err) // fail closed
	}
	if c.list == nil || !st.ModTime().Equal(c.mtime) || st.Size() != c.size {
		raw, err := os.ReadFile(c.path)
		if err != nil {
			return false, err
		}
		b, _ := pem.Decode(raw)
		if b == nil {
			return false, errors.New("revocation list is not PEM")
		}
		l, err := x509.ParseRevocationList(b.Bytes)
		if err != nil {
			return false, err
		}
		if err := l.CheckSignatureFrom(c.ca); err != nil {
			return false, fmt.Errorf("revocation list signature: %w", err)
		}
		c.list, c.mtime, c.size = l, st.ModTime(), st.Size()
	}
	if time.Now().After(c.list.NextUpdate) {
		return false, errors.New("revocation list is stale") // fail closed
	}
	for _, e := range c.list.RevokedCertificateEntries {
		if e.SerialNumber.Cmp(cert.SerialNumber) == 0 {
			return true, nil
		}
	}
	return false, nil
}

func pool(file string) (*x509.CertPool, *x509.Certificate, error) {
	pem1, err := os.ReadFile(file)
	if err != nil {
		return nil, nil, err
	}
	p := x509.NewCertPool()
	if !p.AppendCertsFromPEM(pem1) {
		return nil, nil, errors.New("secure: no certificate in CA file")
	}
	b, _ := pem.Decode(pem1)
	c, err := x509.ParseCertificate(b.Bytes)
	return p, c, err
}

func (p Profile) base() (*tls.Config, *x509.CertPool, error) {
	if p.CertFile == "" || p.KeyFile == "" || p.CAFile == "" {
		return nil, nil, errors.New("secure: certificate, key and CA file are all required (mutual authentication)")
	}
	cert, err := tls.LoadX509KeyPair(p.CertFile, p.KeyFile)
	if err != nil {
		return nil, nil, err
	}
	ca, caCert, err := pool(p.CAFile)
	if err != nil {
		return nil, nil, err
	}
	warn := p.ExpiryWarn
	if warn == 0 {
		warn = 30 * 24 * time.Hour
	}
	if leaf, err := x509.ParseCertificate(cert.Certificate[0]); err == nil && time.Until(leaf.NotAfter) < warn && p.OnWarn != nil {
		p.OnWarn(fmt.Sprintf("own certificate %q expires %s", leaf.Subject.CommonName, leaf.NotAfter.Format(time.RFC3339)))
	}
	min := uint16(tls.VersionTLS12)
	if p.TLS13Only {
		min = tls.VersionTLS13
	}
	allowed := map[string]bool{}
	for _, n := range p.AllowedNames {
		allowed[n] = true
	}
	rl := &crl{path: p.CRLFile, ca: caCert}
	cfg := &tls.Config{
		Certificates: []tls.Certificate{cert}, MinVersion: min, CipherSuites: CipherSuites,
		CurvePreferences: []tls.CurveID{tls.X25519, tls.CurveP256, tls.CurveP384}, SessionTicketsDisabled: true, // every connection authenticates afresh
		VerifyConnection: func(cs tls.ConnectionState) error {
			if len(cs.PeerCertificates) == 0 {
				return errors.New("secure: no peer certificate")
			}
			leaf := cs.PeerCertificates[0]
			if time.Until(leaf.NotAfter) < warn && p.OnWarn != nil {
				p.OnWarn(fmt.Sprintf("peer certificate %q expires %s", leaf.Subject.CommonName, leaf.NotAfter.Format(time.RFC3339)))
			}
			if len(allowed) > 0 {
				ok := allowed[leaf.Subject.CommonName]
				for _, d := range leaf.DNSNames {
					ok = ok || allowed[d]
				}
				if !ok {
					return fmt.Errorf("secure: peer %q is not on the allow-list", leaf.Subject.CommonName)
				}
			}
			if r, err := rl.revoked(leaf); err != nil {
				return err
			} else if r {
				return fmt.Errorf("secure: peer certificate %s is revoked", leaf.SerialNumber)
			}
			return nil
		},
	}
	return cfg, ca, nil
}

// Server returns the listener-side configuration: clients must present a certificate signed by the CA (mutual TLS).
func Server(p Profile) (*tls.Config, error) {
	cfg, ca, err := p.base()
	if err != nil {
		return nil, err
	}
	cfg.ClientCAs, cfg.ClientAuth = ca, tls.RequireAndVerifyClientCert
	return cfg, nil
}

// Client returns the dialling-side configuration; serverName must match the outstation certificate.
func Client(p Profile, serverName string) (*tls.Config, error) {
	cfg, ca, err := p.base()
	if err != nil {
		return nil, err
	}
	cfg.RootCAs, cfg.ServerName = ca, serverName
	return cfg, nil
}

func parse(der []byte) (*x509.Certificate, error) { return x509.ParseCertificate(der) }
