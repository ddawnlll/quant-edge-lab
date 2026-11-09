// Command qel-bench runs the TASK-25 B1 and B5 benchmark workloads.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: qel-bench <b1|b5|b5-child> [flags]")
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "b1":
		err = runB1(os.Args[2:])
	case "b5":
		err = runB5(os.Args[2:])
	case "b5-child":
		err = runB5Child(os.Args[2:])
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n", os.Args[1])
		os.Exit(2)
	}
	if err != nil {
		if errorsIsFlag(err) {
			os.Exit(2)
		}
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func errorsIsFlag(err error) bool {
	return err == flag.ErrHelp
}

func writeJSON(path string, value any) error {
	if path == "" {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(value)
	}
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(raw, '\n'), 0o640)
}
