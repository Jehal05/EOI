// Package points loads the model-derived SCADA point list from cim-pds.
package points

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

type Point struct {
	CA         uint16   `json:"ca"`
	IOA        uint32   `json:"ioa"`
	Tag        string   `json:"tag"`
	Element    string   `json:"element"`
	Substation string   `json:"substation"`
	Kind       string   `json:"kind"` // status | analog
	Quantity   string   `json:"quantity"`
	Unit       string   `json:"unit"`
	Low        *float64 `json:"low"`
	High       *float64 `json:"high"`
}

type List struct {
	ModelID string  `json:"modelId"`
	Version uint64  `json:"version"`
	Count   int     `json:"count"`
	Points  []Point `json:"points"`
}

func Fetch(cimURL, model, bearer string) (*List, error) {
	req, _ := http.NewRequest("GET", fmt.Sprintf("%s/models/%s/pointlist", cimURL, model), nil)
	req.Header.Set("Authorization", "Bearer "+bearer)
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("cim-pds %s: %s", resp.Status, string(b[:min(len(b), 200)]))
	}
	var l List
	if err := json.Unmarshal(b, &l); err != nil {
		return nil, err
	}
	seen := map[[2]uint32]bool{}
	for _, p := range l.Points {
		k := [2]uint32{uint32(p.CA), p.IOA}
		if seen[k] {
			return nil, fmt.Errorf("duplicate (CA,IOA)=(%d,%d) in point list", p.CA, p.IOA)
		}
		seen[k] = true
	}
	return &l, nil
}
