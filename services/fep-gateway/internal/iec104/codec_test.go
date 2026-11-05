package iec104

import (
	"bytes"
	"encoding/hex"
	"math"
	"reflect"
	"testing"
	"time"
)

func mustHex(s string) []byte {
	b, err := hex.DecodeString(s)
	if err != nil {
		panic(err)
	}
	return b
}

// Known-answer vectors: canonical IEC 60870-5-104 byte sequences.
func TestKnownFrames(t *testing.T) {
	cases := []struct {
		name string
		f    Frame
		hex  string
	}{
		{"STARTDT act", Frame{Kind: KindU, U: UStartDTAct}, "680407000000"},
		{"STARTDT con", Frame{Kind: KindU, U: UStartDTCon}, "68040b000000"},
		{"STOPDT act", Frame{Kind: KindU, U: UStopDTAct}, "680413000000"},
		{"TESTFR act", Frame{Kind: KindU, U: UTestFRAct}, "680443000000"},
		{"TESTFR con", Frame{Kind: KindU, U: UTestFRCon}, "680483000000"},
		{"S-frame N(R)=5", Frame{Kind: KindS, RecvSeq: 5}, "680401000a00"},
		{"S-frame N(R)=300", Frame{Kind: KindS, RecvSeq: 300}, "680401005802"},
	}
	for _, c := range cases {
		got, err := c.f.Encode()
		if err != nil {
			t.Fatal(c.name, err)
		}
		want := mustHex(c.hex)
		if !bytes.Equal(got, want) {
			t.Errorf("%s: got %x want %x", c.name, got, want)
		}
		back, err := DecodeFrame(got)
		if err != nil || !reflect.DeepEqual(back, c.f) {
			t.Errorf("%s: round trip %v %v", c.name, back, err)
		}
	}
}

func TestInterrogationVector(t *testing.T) {
	a := ASDU{Type: C_IC_NA_1, Cause: CotActivation, CA: 1, Objects: []Object{{QOI: 20}}}
	b, _ := a.Encode()
	if hex.EncodeToString(b) != "640106000100000000"+"14" {
		t.Fatalf("GI ASDU %x", b)
	}
	f, _ := Frame{Kind: KindI, ASDU: b}.Encode()
	if hex.EncodeToString(f) != "680e0000000064010600010000000014" {
		t.Fatalf("GI frame %x", f)
	}
}

func TestDoubleCommandVector(t *testing.T) {
	a := ASDU{Type: C_DC_NA_1, Cause: CotActivation, CA: 1, Objects: []Object{{IOA: 0x000100, DPI: 1, Select: true}}}
	b, _ := a.Encode()
	if hex.EncodeToString(b) != "2e0106000100"+"000100"+"81" {
		t.Fatalf("select open %x", b)
	}
	a.Objects[0].Select, a.Objects[0].DPI = false, 2
	b, _ = a.Encode()
	if hex.EncodeToString(b) != "2e0106000100"+"000100"+"02" {
		t.Fatalf("execute close %x", b)
	}
}

func TestCP56Time2aVector(t *testing.T) {
	tm := time.Date(2026, 11, 3, 10, 25, 30, 123e6, time.UTC) // a Tuesday
	b := EncodeTime(tm)
	if hex.EncodeToString(b[:]) != "ab75190a430b1a" {
		t.Fatalf("time %x", b)
	}
	got, ok := DecodeTime(b[:])
	if !ok || !got.Equal(tm) {
		t.Fatalf("decode %v %v", got, ok)
	}
	bad := b
	bad[2] |= 0x80
	if _, ok := DecodeTime(bad[:]); ok {
		t.Fatal("IV bit must mark time invalid")
	}
}

func TestFloatVector(t *testing.T) {
	a := ASDU{Type: M_ME_NC_1, Cause: CotSpont, CA: 7, Objects: []Object{{IOA: 1001, Float: 1.0}}}
	b, _ := a.Encode()
	if hex.EncodeToString(b) != "0d0103000700"+"e90300"+"0000803f"+"00" {
		t.Fatalf("float %x", b)
	}
}

func TestASDURoundTripAllTypes(t *testing.T) {
	now := time.Date(2026, 1, 2, 3, 4, 5, 678e6, time.UTC)
	for _, a := range []ASDU{
		{Type: M_ME_NC_1, Cause: CotInrogen, CA: 3, Objects: []Object{{IOA: 1001, Float: -12.5, Quality: QNotTopical}, {IOA: 1002, Float: 1e9}}},
		{Type: M_ME_TF_1, Cause: CotSpont, CA: 9, Objects: []Object{{IOA: 1500, Float: 3.25, Time: &now}}},
		{Type: M_DP_NA_1, Cause: CotInrogen, CA: 4, Objects: []Object{{IOA: 5, DPI: DPIOn}, {IOA: 6, DPI: DPIOff, Quality: QInvalid}}},
		{Type: M_DP_TB_1, Cause: CotSpont, CA: 4, Objects: []Object{{IOA: 5, DPI: DPIOn, Time: &now}}},
		{Type: M_SP_NA_1, Cause: CotSpont, CA: 4, Objects: []Object{{IOA: 5, DPI: 1}}},
		{Type: C_DC_NA_1, Cause: CotActivation, CA: 1, Orig: 3, Objects: []Object{{IOA: 77, DPI: 2, Select: true, QU: 1}}},
		{Type: C_SC_NA_1, Cause: CotActCon, Neg: true, CA: 1, Objects: []Object{{IOA: 77, DPI: 1}}},
		{Type: C_IC_NA_1, Cause: CotActTerm, CA: 0xFFFF, Objects: []Object{{QOI: 20}}},
	} {
		b, err := a.Encode()
		if err != nil {
			t.Fatalf("type %d encode: %v", a.Type, err)
		}
		got, err := DecodeASDU(b)
		if err != nil {
			t.Fatalf("type %d decode: %v", a.Type, err)
		}
		if !reflect.DeepEqual(got, a) {
			t.Errorf("type %d round trip:\n got  %+v\n want %+v", a.Type, got, a)
		}
	}
}

func TestSequenceEncodingDecoded(t *testing.T) {
	// SQ=1: three consecutive short floats starting at IOA 100
	raw := mustHex("0d83" + "0300" + "0100" + "640000" + "0000803f00" + "00000040" + "00" + "00004040" + "00")
	a, err := DecodeASDU(raw)
	if err != nil || len(a.Objects) != 3 {
		t.Fatalf("%v %+v", err, a)
	}
	if a.Objects[0].IOA != 100 || a.Objects[2].IOA != 102 || a.Objects[1].Float != 2.0 || a.Objects[2].Float != 3.0 {
		t.Fatalf("%+v", a.Objects)
	}
}

func TestRejectsMalformed(t *testing.T) {
	for _, h := range []string{"", "68", "6804", "690407000000", "680307000000", "680407000001", "6804ff000000",
		"680401000100", "680400000000" /* I-frame w/o ASDU */, "68ff" + "00"} {
		if _, err := DecodeFrame(mustHex(h)); err == nil {
			t.Errorf("frame %q should be rejected", h)
		}
	}
	for _, h := range []string{"", "0d0103000700", "0d01030007000000", "ff01030007000100", "6401060001000000" /* short */, "0d00030007"} {
		if _, err := DecodeASDU(mustHex(h)); err == nil {
			t.Errorf("ASDU %q should be rejected", h)
		}
	}
}

func TestEncodeRejectsOversize(t *testing.T) {
	a := ASDU{Type: M_ME_TF_1, Cause: CotSpont, CA: 1}
	for i := 0; i < 20; i++ { // 20*15+6 = 306 > 249
		a.Objects = append(a.Objects, Object{IOA: uint32(i), Float: float32(i)})
	}
	if _, err := a.Encode(); err == nil {
		t.Fatal("oversize ASDU must be rejected")
	}
	if maxObjs(M_ME_TF_1) != 16 || maxObjs(M_DP_TB_1) != 22 || maxObjs(M_ME_NC_1) != 30 {
		t.Fatalf("per-ASDU capacity wrong: %d %d %d", maxObjs(M_ME_TF_1), maxObjs(M_DP_TB_1), maxObjs(M_ME_NC_1))
	}
}

func TestNaNInfSurviveTransport(t *testing.T) {
	for _, v := range []float32{float32(math.NaN()), float32(math.Inf(1)), float32(math.Inf(-1)), math.SmallestNonzeroFloat32} {
		b, _ := ASDU{Type: M_ME_NC_1, Cause: 1, CA: 1, Objects: []Object{{IOA: 1, Float: v}}}.Encode()
		a, err := DecodeASDU(b)
		if err != nil || math.Float32bits(a.Objects[0].Float) != math.Float32bits(v) {
			t.Fatalf("value %v not preserved", v)
		}
	}
}

func FuzzDecodeFrame(f *testing.F) {
	for _, s := range []string{"680407000000", "680401000a00", "680e0000000064010600010000000014"} {
		f.Add(mustHex(s))
	}
	f.Fuzz(func(t *testing.T, b []byte) {
		fr, err := DecodeFrame(b)
		if err != nil {
			return
		}
		enc, err := fr.Encode() // anything that decodes must re-encode identically
		if err != nil || !bytes.Equal(enc, b) {
			t.Fatalf("decode/encode mismatch: %x -> %+v -> %x (%v)", b, fr, enc, err)
		}
	})
}

func FuzzDecodeASDU(f *testing.F) {
	for _, s := range []string{"640106000100000000" + "14", "0d0103000700" + "e90300" + "0000803f" + "00",
		"0d83030001006400000000803f0000000040000000404000"} {
		f.Add(mustHex(s))
	}
	f.Fuzz(func(t *testing.T, b []byte) {
		a, err := DecodeASDU(b)
		if err != nil || a.SQ {
			return
		}
		// NaN payloads compare unequal, so verify via a second encode of the decoded value
		enc, err := a.Encode()
		if err != nil {
			t.Fatalf("decoded ASDU failed to encode: %v (%x)", err, b)
		}
		a2, err := DecodeASDU(enc)
		if err != nil {
			t.Fatalf("re-decode failed: %v", err)
		}
		enc2, _ := a2.Encode()
		if !bytes.Equal(enc, enc2) {
			t.Fatalf("unstable encoding: %x vs %x", enc, enc2)
		}
	})
}
