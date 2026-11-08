package cgroup

import (
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestDelegatedCgroupLimits(t *testing.T) {
	parent, err := FindDelegated()
	if err != nil {
		t.Skipf("no delegated cgroup: %v", err)
	}
	name := fmt.Sprintf("test-%d", os.Getpid())
	g, err := Create(name, Limits{MemoryMaxMiB: 128, MemoryHighMiB: 96, CPUMillicores: 500, PidsMax: 32})
	if err != nil {
		t.Skipf("create: %v", err)
	}
	defer func() {
		g.Close()
		time.Sleep(100 * time.Millisecond)
		g.Remove()
	}()
	cmd := exec.Command("/bin/sh", "-c", "echo ok")
	g.Attach(cmd)
	out, err := cmd.Output()
	if err != nil || string(out) != "ok\n" {
		t.Fatalf("run: %v %q", err, out)
	}
	u := g.Usage()
	if u.PidsPeak < 1 {
		t.Fatalf("pids peak not observed: %+v", u)
	}
	if _, err := os.ReadFile(g.Path + "/memory.max"); err != nil {
		t.Fatal(err)
	}
	t.Logf("delegated=%s usage=%+v", parent, u)
}

func TestMemoryLimitKillsOversize(t *testing.T) {
	name := fmt.Sprintf("oom-%d", os.Getpid())
	g, err := Create(name, Limits{MemoryMaxMiB: 32, PidsMax: 16})
	if err != nil {
		t.Skipf("create: %v", err)
	}
	defer func() {
		g.Close()
		time.Sleep(100 * time.Millisecond)
		g.Remove()
	}()
	cmd := exec.Command("/home/quant/quant/quant-edge-lab/.venv/bin/python", "-c", "x=bytearray(200*1024*1024); print(len(x))")
	g.Attach(cmd)
	err = cmd.Run()
	if err == nil {
		t.Fatal("oversize allocation was not killed")
	}
	t.Logf("expected kill: %v", err)
}

func TestAdmission(t *testing.T) {
	b := NewBroker("RP-8G")
	first := b.Admit(2048, 1500, true, "/home/quant/quant")
	if !first.OK {
		t.Fatalf("first heavy denied: %+v", first)
	}
	second := b.Admit(2048, 1500, true, "/home/quant/quant")
	if !second.OK {
		t.Fatalf("second heavy denied under 2-token profile: %+v", second)
	}
	third := b.Admit(2048, 1500, true, "/home/quant/quant")
	if third.OK {
		t.Fatal("third heavy admitted")
	}
	b.Release(2048, true)
	if !b.Admit(2048, 1500, true, "/home/quant/quant").OK {
		t.Fatal("token not released")
	}
	b.DiskUsedFn = func(string) (float64, error) { return 0.95, nil }
	if b.Admit(128, 100, false, "/tmp").OK {
		t.Fatal("disk-full admission accepted")
	}
}
