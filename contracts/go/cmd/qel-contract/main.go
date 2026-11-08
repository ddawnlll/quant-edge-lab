package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/ddawnlll/quant-edge-lab/contracts/go/qelcontract"
)

func decode(r io.Reader) (any, error) {
	dec := json.NewDecoder(r)
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "ERROR:", err)
	os.Exit(1)
}

func main() {
	if len(os.Args) < 2 {
		fail(fmt.Errorf("usage: qel-contract canonical|hash|runkey|validate|selftest"))
	}
	cmd := os.Args[1]
	switch cmd {
	case "canonical", "hash":
		v, err := decode(os.Stdin)
		if err != nil {
			fail(err)
		}
		b, err := qelcontract.CanonicalBytes(v)
		if err != nil {
			fail(err)
		}
		if cmd == "canonical" {
			fmt.Println(string(b))
		} else {
			h, err := qelcontract.HashJCS(v)
			if err != nil {
				fail(err)
			}
			fmt.Println(h)
		}
	case "runkey":
		v, err := decode(os.Stdin)
		if err != nil {
			fail(err)
		}
		m, ok := v.(map[string]any)
		if !ok {
			fail(fmt.Errorf("runkey material must be an object"))
		}
		h, err := qelcontract.RunKey(m)
		if err != nil {
			fail(err)
		}
		fmt.Println(h)
	case "validate":
		def := ""
		file := ""
		for i := 2; i < len(os.Args); i++ {
			switch os.Args[i] {
			case "--def":
				i++
				def = os.Args[i]
			case "--file":
				i++
				file = os.Args[i]
			}
		}
		if def == "" {
			fail(fmt.Errorf("--def is required"))
		}
		var r io.Reader = os.Stdin
		if file != "" {
			f, err := os.Open(file)
			if err != nil {
				fail(err)
			}
			defer f.Close()
			r = f
		}
		v, err := decode(r)
		if err != nil {
			fail(err)
		}
		if err := qelcontract.ValidateDef(def, v); err != nil {
			fail(err)
		}
		fmt.Println("VALID", def)
	case "selftest":
		vectors := map[string]any{"a": "9007199254740993", "b": "1.25", "c": nil}
		b, _ := qelcontract.CanonicalBytes(vectors)
		h, _ := qelcontract.HashJCS(vectors)
		want := `{"a":"9007199254740993","b":"1.25","c":null}`
		ok := string(b) == want && h == "129712fa856798af9106eb65d951f98ef56459ae40d587552cd7a4b162b7ae6e"
		rejected := 0
		for _, bad := range []string{"-0", "01", "1e3"} {
			if _, _, err := qelcontract.ParseDecimal(bad); err != nil {
				rejected++
			}
		}
		if _, err := qelcontract.ParseInt128Domain("170141183460469231731687303715884105728"); err != nil {
			rejected++
		}
		if _, _, err := qelcontract.ParseDecimal("1.230"); err != nil {
			rejected++
		}
		out := map[string]any{
			"canonical_utf8": string(b),
			"sha256":         h,
			"vector_pass":    ok,
			"rejected":       rejected,
			"nfc":            strings.Contains("é", "e"),
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(out)
		if !ok || rejected != 5 {
			os.Exit(2)
		}
	default:
		fail(fmt.Errorf("unknown command %s", cmd))
	}
}
