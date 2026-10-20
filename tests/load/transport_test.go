package main

import (
	"io"
	"net"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A transport failure stays one "transport" failure; its class is a counter
// beside it, never another failure.
func TestTransportErrorsAreClassifiedAndCountedOnce(t *testing.T) {
	cases := map[string]error{
		"dial:refused":      &net.OpError{Op: "dial", Net: "tcp", Err: os.NewSyscallError("connect", syscall.ECONNREFUSED)},
		"dial:addrNotAvail": &net.OpError{Op: "dial", Net: "tcp", Err: os.NewSyscallError("connect", syscall.EADDRNOTAVAIL)},
		"conn:reset":        &net.OpError{Op: "read", Net: "tcp", Err: os.NewSyscallError("read", syscall.ECONNRESET)},
		"conn:eof":          io.ErrUnexpectedEOF,
		"conn:timeout":      &net.DNSError{Err: "timed out", IsTimeout: true},
		"conn:other":        io.ErrClosedPipe,
	}
	rec := newRecorder()
	for want, err := range cases {
		if got := transportClass(err); got != want {
			t.Errorf("transportClass(%v) = %q, want %q", err, got, want)
		}
		rec.request(0, err)
	}
	if rec.codes["transport"] != len(cases) || rec.errors != len(cases) {
		t.Fatalf("transport %d, errors %d, want %d each", rec.codes["transport"], rec.errors, len(cases))
	}
	for want := range cases {
		if rec.counters["transport:"+want] != 1 {
			t.Errorf("counter transport:%s = %d, want 1", want, rec.counters["transport:"+want])
		}
	}
}

// The stream's advertised reconnect interval is kept; frames still arrive.
func TestTheAdvertisedRetryIntervalIsKept(t *testing.T) {
	retry := 500 * time.Millisecond
	var events []string
	err := readFramesRetry(strings.NewReader("retry: 3250\n\nid: 1\nevent: operation.changed\ndata: {}\n\n"), &retry, func(f frame) bool {
		events = append(events, f.Event)
		return true
	})
	if err != io.EOF {
		t.Fatalf("readFramesRetry: %v", err)
	}
	if retry != 3250*time.Millisecond || len(events) != 1 {
		t.Fatalf("retry %v, events %v", retry, events)
	}
}
