// Package secure holds the transport-security profile for the control-centre/outstation links (IEC 62351-3 style TLS)
// and a small first-party development PKI. A real deployment brings certificates from the utility's own CA; the profile
// only needs PEM files.
package secure

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"time"
)

// CA is a certificate authority held in memory.
type CA struct {
	Cert *x509.Certificate
	Key  *ecdsa.PrivateKey
	DER  []byte
}

func serial() *big.Int {
	n, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 100))
	return n.Add(n, big.NewInt(1))
}

// NewCA creates a self-signed P-256 CA valid for `days`.
func NewCA(name string, days int) (*CA, error) {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	t := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: name, Organization: []string{"GridSovereign development PKI"}},
		NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().AddDate(0, 0, days), IsCA: true, BasicConstraintsValid: true, MaxPathLenZero: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature}
	der, err := x509.CreateCertificate(rand.Reader, t, t, &k.PublicKey, k)
	if err != nil {
		return nil, err
	}
	c, _ := x509.ParseCertificate(der)
	return &CA{Cert: c, Key: k, DER: der}, nil
}

// Issued is a certificate with its key.
type Issued struct {
	CertPEM, KeyPEM []byte
	Cert            *x509.Certificate
}

// Issue signs a leaf certificate. server=true: serverAuth (names go into the SAN), false: clientAuth.
func (ca *CA) Issue(cn string, dns []string, server bool, days int) (*Issued, error) {
	usage := x509.ExtKeyUsageClientAuth
	if server {
		usage = x509.ExtKeyUsageServerAuth
	}
	return ca.issue(cn, dns, days, usage)
}

// IssueDual issues a certificate valid for both ends of a link (a node that listens and also dials its peers).
func (ca *CA) IssueDual(cn string, dns []string, days int) (*Issued, error) {
	return ca.issue(cn, dns, days, x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth)
}

func (ca *CA) issue(cn string, dns []string, days int, usage ...x509.ExtKeyUsage) (*Issued, error) {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	t := &x509.Certificate{SerialNumber: serial(), Subject: pkix.Name{CommonName: cn}, DNSNames: dns, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().AddDate(0, 0, days),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: usage}
	der, err := x509.CreateCertificate(rand.Reader, t, ca.Cert, &k.PublicKey, ca.Key)
	if err != nil {
		return nil, err
	}
	kd, _ := x509.MarshalECPrivateKey(k)
	c, _ := x509.ParseCertificate(der)
	return &Issued{CertPEM: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), KeyPEM: pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kd}), Cert: c}, nil
}

// CRL builds a signed revocation list for the given serial numbers.
func (ca *CA) CRL(revoked []*big.Int, number int64, valid time.Duration) ([]byte, error) {
	var rc []x509.RevocationListEntry
	for _, s := range revoked {
		rc = append(rc, x509.RevocationListEntry{SerialNumber: s, RevocationTime: time.Now().Add(-time.Second)})
	}
	der, err := x509.CreateRevocationList(rand.Reader, &x509.RevocationList{Number: big.NewInt(number), ThisUpdate: time.Now().Add(-time.Minute), NextUpdate: time.Now().Add(valid), RevokedCertificateEntries: rc}, ca.Cert, ca.Key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "X509 CRL", Bytes: der}), nil
}

// CertPEM returns the CA certificate in PEM form.
func (ca *CA) CertPEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.DER})
}

// LoadCA parses a PEM CA certificate and key (to issue more certificates).
func LoadCA(certPEM, keyPEM []byte) (*CA, error) {
	b, _ := pem.Decode(certPEM)
	kb, _ := pem.Decode(keyPEM)
	if b == nil || kb == nil {
		return nil, errors.New("secure: bad PEM")
	}
	c, err := x509.ParseCertificate(b.Bytes)
	if err != nil {
		return nil, err
	}
	k, err := x509.ParseECPrivateKey(kb.Bytes)
	if err != nil {
		return nil, err
	}
	return &CA{Cert: c, Key: k, DER: b.Bytes}, nil
}

// KeyPEM returns the CA private key in PEM form (for the development tool).
func (ca *CA) KeyPEM() ([]byte, error) {
	kd, err := x509.MarshalECPrivateKey(ca.Key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kd}), nil
}
