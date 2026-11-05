// Command pki is the development PKI for the control-centre links: it writes a CA, issues server/client certificates and builds CRLs.
//
//	pki ca     -dir DIR [-name NAME]
//	pki issue  -dir DIR -cn NAME [-dns a,b] [-server] [-days N]
//	pki revoke -dir DIR -serial HEX[,HEX...]
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"gridsovereign/fep/internal/token"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"time"

	"gridsovereign/fep/internal/secure"
)

func die(err error) {
	fmt.Fprintln(os.Stderr, "pki:", err)
	os.Exit(1)
}

func main() {
	if len(os.Args) < 2 {
		die(fmt.Errorf("usage: pki ca|issue|revoke ..."))
	}
	fs := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	dir := fs.String("dir", "pki", "directory")
	name := fs.String("name", "GridSovereign development CA", "CA name")
	cn := fs.String("cn", "", "certificate common name")
	dns := fs.String("dns", "", "comma separated DNS names")
	server := fs.Bool("server", false, "server certificate (default client)")
	dual := fs.Bool("dual", false, "certificate usable as both server and client (service mesh node)")
	days := fs.Int("days", 365, "validity in days")
	serials := fs.String("serial", "", "hex serial numbers to revoke")
	maxRole := fs.String("max-role", "viewer", "tokenkey: highest role this key may issue")
	subPrefix := fs.String("sub-prefix", "", "tokenkey: required subject prefix for tokens signed by this key")
	_ = fs.Parse(os.Args[2:])
	_ = os.MkdirAll(*dir, 0o700)
	w := func(n string, b []byte) { must(os.WriteFile(filepath.Join(*dir, n), b, 0o600)) }
	switch os.Args[1] {
	case "ca":
		ca, err := secure.NewCA(*name, 3650)
		must(err)
		w("ca.pem", ca.CertPEM())
		kb, _ := ca.KeyPEM()
		w("ca.key", kb)
	case "issue":
		cb, _ := os.ReadFile(filepath.Join(*dir, "ca.pem"))
		kb, _ := os.ReadFile(filepath.Join(*dir, "ca.key"))
		ca, err := secure.LoadCA(cb, kb)
		must(err)
		var names []string
		if *dns != "" {
			names = strings.Split(*dns, ",")
		}
		var is *secure.Issued
		if *dual {
			is, err = ca.IssueDual(*cn, names, *days)
		} else {
			is, err = ca.Issue(*cn, names, *server, *days)
		}
		must(err)
		w(*cn+".pem", is.CertPEM)
		w(*cn+".key", is.KeyPEM)
		fmt.Printf("%s serial %x\n", *cn, is.Cert.SerialNumber)
	case "tokenkey":
		// a token-signing key pair for one service: <dir>/<cn>.tokkey (private, 0600) and an entry in <dir>/trust.json
		if *cn == "" {
			die(fmt.Errorf("tokenkey needs -cn (the key id)"))
		}
		kf, tk := token.Generate()
		kf.Kid, tk.Kid, tk.MaxRole, tk.SubPrefix = *cn, *cn, *maxRole, *subPrefix
		kb, _ := json.Marshal(kf)
		w(*cn+".tokkey", kb)
		must(token.AddToTrustFile(filepath.Join(*dir, "trust.json"), tk))
		fmt.Printf("token key %s (max role %s)\n", *cn, *maxRole)
	case "revoke":
		cb, _ := os.ReadFile(filepath.Join(*dir, "ca.pem"))
		kb, _ := os.ReadFile(filepath.Join(*dir, "ca.key"))
		ca, err := secure.LoadCA(cb, kb)
		must(err)
		var rs []*big.Int
		for _, s := range strings.Split(*serials, ",") {
			n, ok := new(big.Int).SetString(strings.TrimSpace(s), 16)
			if !ok {
				die(fmt.Errorf("bad serial %q", s))
			}
			rs = append(rs, n)
		}
		out, err := ca.CRL(rs, time.Now().Unix(), 7*24*time.Hour)
		must(err)
		w("crl.pem", out)
	default:
		die(fmt.Errorf("unknown command %s", os.Args[1]))
	}
}

func must(err error) {
	if err != nil {
		die(err)
	}
}
