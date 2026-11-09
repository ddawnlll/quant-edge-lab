package main

import "testing"

func TestWaitComplete(t *testing.T) {
	for _, state := range []string{"OUTPUT_SEALED", "DECIDED", "FAILED", "RESOURCE_LIMIT", "ABORTED", "WAITING_DATA", "WAITING_FRESH_OOS", "WAITING_COMPUTE_BUDGET", "WAITING_AUTHORIZATION"} {
		if !isWaitComplete(state) {
			t.Fatalf("%s not wait-complete", state)
		}
	}
	for _, state := range []string{"QUEUED", "LEASED", "RUNNING", "OUTPUT_VALIDATED", "AUDITING"} {
		if isWaitComplete(state) {
			t.Fatalf("%s unexpectedly wait-complete", state)
		}
	}
}

func TestFailureStates(t *testing.T) {
	for _, state := range []string{"FAILED", "RESOURCE_LIMIT", "ABORTED"} {
		if !isFailureState(state) {
			t.Fatalf("%s not marked failure", state)
		}
	}
	for _, state := range []string{"OUTPUT_SEALED", "DECIDED", "WAITING_DATA", "QUEUED"} {
		if isFailureState(state) {
			t.Fatalf("%s unexpectedly marked failure", state)
		}
	}
}
