// Command pmu-sim is a SYNTHETIC C37.118.2 PMU for tests and demos. It is not coupled to any power-flow solution: frequency swings
// +-20 mHz over 20 s around nominal, the voltage phasor is 1.0 pu with a slow 0.05 rad angle wobble. Use -drop-after to stop sending
// (comms-loss tests) and -sync-lost-after to raise the sync-lost status bit.
package main

import (
	"flag"
	"fmt"
	"log"
	"math"
	"net"
	"time"

	"gridsovereign/fep/internal/c37118"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:4712", "listen address")
	id := flag.Int("id", 7, "PMU ID code")
	station := flag.String("station", "PMU26", "station name (<=16 chars)")
	rate := flag.Int("rate", 30, "frames per second")
	fnom := flag.Int("fnom", 50, "nominal frequency, 50 or 60")
	syncLostAfter := flag.Duration("sync-lost-after", 0, "set the sync-lost status bit after this time (0 = never)")
	flag.Parse()
	cfg := c37118.Config{Station: *station, PMUID: uint16(*id), PhasorsF: true, Polar: true, Phasors: []string{"V1"}, Digitals: 1,
		FNom: *fnom, Rate: int16(*rate), TimeBase: 1000000}
	t0 := time.Now()
	srv := &c37118.Server{Cfg: cfg, ID: uint16(*id), Src: func(now time.Time) c37118.Data {
		s := now.Sub(t0).Seconds()
		d := c37118.Data{Phasors: [][2]float32{{1.0, float32(0.05 * math.Sin(2*math.Pi*s/30))}}, Digitals: []uint16{0},
			Freq:  float32(float64(*fnom) + 0.02*math.Sin(2*math.Pi*s/20)),
			DFreq: float32(0.02 * 2 * math.Pi / 20 * math.Cos(2*math.Pi*s/20))}
		if *syncLostAfter > 0 && now.Sub(t0) > *syncLostAfter {
			d.Stat |= 0x2000
		}
		return d
	}}
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("pmu-sim (synthetic) listening on", *listen)
	log.Fatal(srv.Serve(ln))
}
