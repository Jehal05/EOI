package iec101

import (
	"context"
	"io"
	"net"
	"testing"
	"time"

	"gridsovereign/fep/internal/iec104"
)

// BenchmarkClass1Throughput measures events/s a master can drain from a slave over an in-memory link
// (no serial baud limit: it measures protocol processing, not a physical line).
func BenchmarkClass1Throughput(b *testing.B) {
	cm, cs := net.Pipe()
	s := &Slave{H: &memH{}, Link: 3}
	go s.ServeConn(cs)
	n := 0
	m := &Master{Open: func() (io.ReadWriteCloser, error) { return cm, nil }, Link: 3,
		OnASDU: func(a iec104.ASDU) { n += len(a.Objects) }, Poll: 2 * time.Millisecond, Timeout: time.Second}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go m.Run(ctx)
	time.Sleep(200 * time.Millisecond)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s.Publish([]iec104.Point{{CA: 1, IOA: 100, Status: true, DPI: 2, TS: time.Now()}})
	}
	for s.Pending() > 0 {
		time.Sleep(time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond)
}
