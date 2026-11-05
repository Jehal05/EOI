package iec104

import (
	"context"
	"testing"
)

func benchCmd(b *testing.B, srv, cli *AuthConfig) {
	t := &testing.T{}
	r := startAuth(t, srv, cli)
	ctx := context.Background()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if err := r.cl.SelectExecute(ctx, 1, 1, 1); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkSBOPlain(b *testing.B) { benchCmd(b, nil, nil) }
func BenchmarkSBOAuth(b *testing.B) {
	benchCmd(b, &AuthConfig{Key: key16}, &AuthConfig{Key: key16})
}
