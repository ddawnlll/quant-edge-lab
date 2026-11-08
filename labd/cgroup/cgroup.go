// Package cgroup provides delegated cgroup v2 admission and worker limits.
package cgroup

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
)

type Limits struct {
	MemoryMaxMiB  int
	MemoryHighMiB int
	CPUMillicores int
	PidsMax       int
	SwapMaxMiB    int
}

type Group struct {
	Path string
	file *os.File
}

// FindDelegated locates a writable ancestor cgroup for this process.
func FindDelegated() (string, error) {
	raw, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		return "", err
	}
	for _, line := range strings.Split(string(raw), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) != 3 || parts[0] != "0" {
			continue
		}
		path := parts[2]
		for path != "/" && path != "." {
			full := filepath.Join("/sys/fs/cgroup", path)
			if st, err := os.Stat(full); err == nil && st.IsDir() && unixWritable(full) {
				ctrl, _ := os.ReadFile(filepath.Join(full, "cgroup.controllers"))
				sub, _ := os.ReadFile(filepath.Join(full, "cgroup.subtree_control"))
				if strings.Contains(string(ctrl), "memory") && strings.Contains(string(sub), "memory") {
					return full, nil
				}
			}
			path = filepath.Dir(path)
		}
	}
	return "", fmt.Errorf("no delegated writable cgroup")
}

func unixWritable(path string) bool {
	return syscall.Access(path, 0x2) == nil
}

func enableControllers(parent string) error {
	for _, c := range []string{"+memory", "+pids", "+cpu"} {
		_ = os.WriteFile(filepath.Join(parent, "cgroup.subtree_control"), []byte(c), 0o644)
	}
	return nil
}

// Create makes a child group under <delegated>/qel/<name>.
func Create(name string, limits Limits) (*Group, error) {
	parent, err := FindDelegated()
	if err != nil {
		return nil, err
	}
	_ = enableControllers(parent)
	base := filepath.Join(parent, "qel")
	if err := os.MkdirAll(base, 0o755); err != nil {
		return nil, err
	}
	_ = enableControllers(base)
	path := filepath.Join(base, name)
	if err := os.MkdirAll(path, 0o755); err != nil {
		return nil, err
	}
	write := func(file, value string) error {
		return os.WriteFile(filepath.Join(path, file), []byte(value+"\n"), 0o644)
	}
	if limits.MemoryMaxMiB > 0 {
		if err := write("memory.max", strconv.Itoa(limits.MemoryMaxMiB*1024*1024)); err != nil {
			return nil, err
		}
	}
	if limits.MemoryHighMiB > 0 {
		_ = write("memory.high", strconv.Itoa(limits.MemoryHighMiB*1024*1024))
	}
	if limits.PidsMax > 0 {
		if err := write("pids.max", strconv.Itoa(limits.PidsMax)); err != nil {
			return nil, err
		}
	}
	if limits.CPUMillicores > 0 {
		quota := limits.CPUMillicores * 100
		if err := write("cpu.max", fmt.Sprintf("%d 100000", quota)); err != nil {
			return nil, err
		}
	}
	if err := write("memory.swap.max", "0"); err != nil {
		return nil, err
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	return &Group{Path: path, file: f}, nil
}

// Attach sets the child process cgroup descriptor.
func (g *Group) Attach(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.UseCgroupFD = true
	cmd.SysProcAttr.CgroupFD = int(g.file.Fd())
}

func (g *Group) Close() {
	if g.file != nil {
		g.file.Close()
	}
}

func (g *Group) Remove() {
	_ = os.Remove(g.Path)
}

func readInt(path string) int64 {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	v, _ := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	return v
}

type Usage struct {
	MemoryCurrentBytes int64 `json:"memory_current_bytes"`
	MemoryPeakBytes    int64 `json:"memory_peak_bytes"`
	PidsCurrent        int64 `json:"pids_current"`
	PidsPeak           int64 `json:"pids_peak"`
	CPUUsageUsec       int64 `json:"cpu_usage_usec"`
	CPUUserUsec        int64 `json:"cpu_user_usec"`
	CPUSystemUsec      int64 `json:"cpu_system_usec"`
}

func (g *Group) Usage() Usage {
	u := Usage{
		MemoryCurrentBytes: readInt(filepath.Join(g.Path, "memory.current")),
		MemoryPeakBytes:    readInt(filepath.Join(g.Path, "memory.peak")),
		PidsCurrent:        readInt(filepath.Join(g.Path, "pids.current")),
		PidsPeak:           readInt(filepath.Join(g.Path, "pids.peak")),
	}
	if f, err := os.Open(filepath.Join(g.Path, "cpu.stat")); err == nil {
		scanner := bufio.NewScanner(f)
		for scanner.Scan() {
			fields := strings.Fields(scanner.Text())
			if len(fields) != 2 {
				continue
			}
			v, _ := strconv.ParseInt(fields[1], 10, 64)
			switch fields[0] {
			case "usage_usec":
				u.CPUUsageUsec = v
			case "user_usec":
				u.CPUUserUsec = v
			case "system_usec":
				u.CPUSystemUsec = v
			}
		}
		f.Close()
	}
	return u
}

type Profile struct {
	ID             string
	ParentMaxMiB   int
	HeavyMaxMiB    int
	MaxHeavy       int
	HostReserveMiB int
}

var Profiles = map[string]Profile{
	"RP-4G": {ID: "RP-4G", ParentMaxMiB: 3072, HeavyMaxMiB: 1792, MaxHeavy: 1, HostReserveMiB: 1024},
	"RP-8G": {ID: "RP-8G", ParentMaxMiB: 6144, HeavyMaxMiB: 4096, MaxHeavy: 2, HostReserveMiB: 2048},
}

type Broker struct {
	mu         sync.Mutex
	Profile    Profile
	heavy      int
	reservedMi int
	DiskFree   func(path string) (freeBytes, totalBytes int64, err error)
	DiskUsedFn func(path string) (float64, error)
}

func NewBroker(profileID string) *Broker {
	p, ok := Profiles[profileID]
	if !ok {
		p = Profiles["RP-8G"]
	}
	return &Broker{Profile: p, DiskFree: statfs}
}

type Admission struct {
	OK     bool
	Reason string
}

func (b *Broker) Admit(memoryMiB, cpuMillicores int, heavy bool, diskPath string) Admission {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.Profile.ParentMaxMiB <= b.Profile.HostReserveMiB {
		return Admission{false, "RESOURCE_LIMIT: parent budget"}
	}
	if memoryMiB <= 0 {
		return Admission{false, "RESOURCE_LIMIT: memory request"}
	}
	if heavy && memoryMiB > b.Profile.HeavyMaxMiB {
		return Admission{false, "RESOURCE_LIMIT: heavy max"}
	}
	if heavy && b.heavy >= b.Profile.MaxHeavy {
		return Admission{false, "RESOURCE_LIMIT: heavy token"}
	}
	if b.reservedMi+memoryMiB > b.Profile.ParentMaxMiB-b.Profile.HostReserveMiB {
		return Admission{false, "RESOURCE_LIMIT: parent reservations"}
	}
	if b.DiskUsedFn != nil {
		used, err := b.DiskUsedFn(diskPath)
		if err == nil && used >= 0.90 {
			return Admission{false, "RESOURCE_LIMIT: disk full"}
		}
	}
	if heavy {
		b.heavy++
	}
	b.reservedMi += memoryMiB
	return Admission{true, ""}
}

func (b *Broker) Release(memoryMiB int, heavy bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if heavy && b.heavy > 0 {
		b.heavy--
	}
	b.reservedMi -= memoryMiB
	if b.reservedMi < 0 {
		b.reservedMi = 0
	}
}

func statfs(path string) (int64, int64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, 0, err
	}
	return int64(st.Bavail) * int64(st.Bsize), int64(st.Blocks) * int64(st.Bsize), nil
}

func DiskUsed(path string) (float64, error) {
	free, total, err := statfs(path)
	if err != nil || total == 0 {
		return 0, err
	}
	return 1.0 - float64(free)/float64(total), nil
}
