package main

// Offline unit tests for the bridge's pure helpers - no WhatsApp connection, no
// SQLite, no network. These guard the small pieces of logic that protect the
// process: the send rate limiter, the JID path-traversal check, the LID->PN
// pass-through, and the fixed-shape voice waveform. Standard library only, so
// `go test ./...` needs no extra dependencies.

import (
	"testing"
	"time"
)

func TestTokenBucket_BurstThenEmpty(t *testing.T) {
	// A fresh bucket is pre-filled to `burst`, so exactly `burst` calls to
	// allow() succeed immediately and the next one fails (the refill ticker
	// runs on a timer we deliberately do not wait for here).
	const burst = 10
	tb := newTokenBucket(5, burst)

	for i := 0; i < burst; i++ {
		if !tb.allow() {
			t.Fatalf("allow() #%d should succeed on a freshly pre-filled bucket", i+1)
		}
	}
	if tb.allow() {
		t.Fatal("allow() should fail once the pre-filled burst is exhausted")
	}
}

func TestTokenBucket_Refills(t *testing.T) {
	// At 50/s the ticker refills every 20ms. Drain the burst, then confirm a
	// token comes back. This is the one time-dependent test; it waits for a
	// real refill rather than asserting on the clock.
	tb := newTokenBucket(50, 1)
	if !tb.allow() {
		t.Fatal("first allow() should succeed")
	}
	if tb.allow() {
		t.Fatal("second immediate allow() should fail (burst was 1)")
	}
	// Poll for a refill up to a generous ceiling so the test is not flaky on a
	// loaded CI runner.
	deadline := 200 // * 5ms = up to 1s
	for i := 0; i < deadline; i++ {
		if tb.allow() {
			return // refilled, as expected
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("bucket never refilled within 1s at 50 tokens/sec")
}

func TestValidateJIDForFilesystem(t *testing.T) {
	cases := []struct {
		name string
		jid  string
		ok   bool
	}{
		{"plain phone jid", "201234567890@s.whatsapp.net", true},
		{"group jid", "120363000000000000@g.us", true},
		{"lid", "10000000000000@lid", true},
		{"device suffix with colon", "201234567890:12@s.whatsapp.net", false}, // ':' is not allowed
		{"empty", "", false},
		{"path traversal", "../../etc/passwd", false},
		{"slash", "abc/def@g.us", false},
		{"null byte", "abc\x00@g.us", false},
		{"space", "abc def@g.us", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := validateJIDForFilesystem(c.jid)
			if c.ok && err != nil {
				t.Fatalf("expected %q to be allowed, got error: %v", c.jid, err)
			}
			if !c.ok && err == nil {
				t.Fatalf("expected %q to be rejected, got no error", c.jid)
			}
		})
	}
}

func TestValidateJIDForFilesystem_LengthCap(t *testing.T) {
	long := make([]byte, 201)
	for i := range long {
		long[i] = 'a'
	}
	if err := validateJIDForFilesystem(string(long)); err == nil {
		t.Fatal("a 201-char jid should be rejected by the length cap")
	}
}

func TestResolveToPNStr_NonLIDPassThrough(t *testing.T) {
	// Without a WhatsApp client, resolveToPNStr must return anything that is not
	// a @lid unchanged, and must not panic on a nil client for the @lid path
	// (it falls back to the input when no mapping is available).
	cases := []string{
		"201234567890@s.whatsapp.net",
		"120363000000000000@g.us",
		"not-a-jid",
		"",
	}
	for _, in := range cases {
		if got := resolveToPNStr(nil, in); got != in {
			t.Fatalf("resolveToPNStr(nil, %q) = %q, want it unchanged", in, got)
		}
	}
}

func TestResolveToPNStr_LIDWithNilClientDoesNotPanic(t *testing.T) {
	// A @lid with no client cannot be resolved, so it comes back as the parsed
	// JID string. The point of the test is that it does not panic on nil.
	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("resolveToPNStr panicked on a nil client: %v", r)
		}
	}()
	got := resolveToPNStr(nil, "10000000000000@lid")
	if got == "" {
		t.Fatal("expected the lid to round-trip to a non-empty string")
	}
}

func TestPlaceholderWaveform_ShapeAndRange(t *testing.T) {
	// WhatsApp expects a 64-byte waveform with every sample in 0..100.
	w := placeholderWaveform(30)
	if len(w) != 64 {
		t.Fatalf("waveform length = %d, want 64", len(w))
	}
	for i, b := range w {
		if b > 100 {
			t.Fatalf("sample %d = %d, out of the 0..100 range WhatsApp expects", i, b)
		}
	}
}

func TestPlaceholderWaveform_DeterministicPerDuration(t *testing.T) {
	// It is seeded by the duration, so the same duration yields the same bytes
	// (important: a re-send must not produce a different waveform).
	a := placeholderWaveform(42)
	b := placeholderWaveform(42)
	if string(a) != string(b) {
		t.Fatal("same duration should produce an identical waveform")
	}
	c := placeholderWaveform(7)
	if string(a) == string(c) {
		t.Fatal("different durations should produce different waveforms")
	}
}
