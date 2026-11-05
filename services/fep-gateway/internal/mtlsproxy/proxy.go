// Package mtlsproxy is a small mutual-TLS reverse proxy used to put certificate-authenticated transport in front of
// services written in other languages. Ingress terminates mTLS and forwards to a loopback upstream; egress accepts plain
// loopback connections and forwards them over mTLS to a remote ingress.
//
// Ingress rules: (1) any client certificate must chain to the CA, be unexpired and not revoked (secure.Server);
// (2) X-Client-CN is always overwritten with the verified certificate's CommonName, never taken from the request;
// (3) internal path prefixes (replication, cluster, ingest ...) are reachable only by the listed certificate names.
package mtlsproxy

import (
	"crypto/tls"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

// Rule restricts a path prefix to certificate names.
type Rule struct {
	Prefix string
	Names  []string
}

const CNHeader = "X-Client-CN"

// Ingress returns a handler that forwards verified requests to upstream.
func Ingress(upstream *url.URL, rules []Rule) http.Handler {
	rp := httputil.NewSingleHostReverseProxy(upstream)
	rp.FlushInterval = -1 // streaming endpoints (NDJSON) must not be buffered
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.TLS == nil || len(r.TLS.PeerCertificates) == 0 {
			http.Error(w, `{"error":"client certificate required"}`, http.StatusUnauthorized)
			return
		}
		cn := r.TLS.PeerCertificates[0].Subject.CommonName
		for _, ru := range rules {
			if strings.HasPrefix(r.URL.Path, ru.Prefix) && !contains(ru.Names, cn) {
				http.Error(w, `{"error":"certificate not permitted for this endpoint"}`, http.StatusForbidden)
				return
			}
		}
		r.Header.Set(CNHeader, cn) // overwrite whatever the client sent
		rp.ServeHTTP(w, r)
	})
}

func contains(l []string, s string) bool {
	for _, x := range l {
		if x == s {
			return true
		}
	}
	return false
}

// Egress returns a handler that forwards plain loopback requests to a remote mTLS ingress.
func Egress(remote *url.URL, client *tls.Config) http.Handler {
	rp := httputil.NewSingleHostReverseProxy(remote)
	rp.FlushInterval = -1
	rp.Transport = &http.Transport{TLSClientConfig: client, ForceAttemptHTTP2: false, MaxIdleConnsPerHost: 8,
		IdleConnTimeout: 30 * time.Second, DialContext: (&net.Dialer{Timeout: 3 * time.Second}).DialContext,
		TLSHandshakeTimeout: 3 * time.Second, ResponseHeaderTimeout: 30 * time.Second}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Header.Del(CNHeader)
		rp.ServeHTTP(w, r)
	})
}
