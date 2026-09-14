package main

// Regression tests for failures that only show up in specific process
// states, so the pure-helper tests in main_test.go cannot reach them. Unlike
// that file these use whatsmeow's sqlstore and a temporary SQLite file, but
// they stay offline: no WhatsApp connection, no network.

import (
	"context"
	"path/filepath"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
)

func TestResolveContactNames_UnpairedDeviceDoesNotPanic(t *testing.T) {
	// A device from NewDevice() has not paired, so its sqlstore sub-stores are
	// still nil. The startup backfill used to call resolveContactNames on
	// exactly this client and take the whole process down.
	container, err := sqlstore.New(context.Background(), "sqlite3",
		"file:"+filepath.Join(t.TempDir(), "whatsapp.db")+"?_foreign_keys=on", waLog.Noop)
	if err != nil {
		t.Fatal(err)
	}
	client := whatsmeow.NewClient(container.NewDevice(), waLog.Noop)
	if client.Store.Contacts != nil {
		t.Skip("whatsmeow now initialises Contacts before pairing; this guard is no longer exercised")
	}

	defer func() {
		if r := recover(); r != nil {
			t.Fatalf("resolveContactNames panicked on an unpaired client: %v", r)
		}
	}()
	jid := types.NewJID("10000000000", types.DefaultUserServer)
	if saved, push := resolveContactNames(client, jid); saved != "" || push != "" {
		t.Fatalf("got (%q, %q), want empty names for an unpaired client", saved, push)
	}
	if saved, push := resolveContactNames(nil, jid); saved != "" || push != "" {
		t.Fatalf("got (%q, %q), want empty names for a nil client", saved, push)
	}
}
