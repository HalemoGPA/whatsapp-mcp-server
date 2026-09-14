package main

// Regression tests for two failures that only show up in specific process
// states, so the pure-helper tests in main_test.go cannot reach them. Unlike
// that file these use whatsmeow's sqlstore and a temporary SQLite file, but
// they stay offline: no WhatsApp connection, no network.

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

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

func TestGetChatName_InsideTransactionDoesNotDeadlock(t *testing.T) {
	// Mirrors NewMessageStore's pool: one connection. handleHistorySync holds a
	// transaction while resolving chat names, so the lookup must go through that
	// transaction. Querying the pool instead waits forever for a free connection.
	db, err := sql.Open("sqlite3", "file:"+filepath.Join(t.TempDir(), "messages.db")+
		"?_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		t.Fatal(err)
	}
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	t.Cleanup(func() { _ = db.Close() })

	const chatJID = "10000000000@s.whatsapp.net"
	if _, err := db.Exec(`CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO chats (jid, name) VALUES (?, ?)`, chatJID, "Test Chat"); err != nil {
		t.Fatal(err)
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = tx.Rollback() }()

	done := make(chan string, 1)
	go func() {
		done <- GetChatName(nil, &MessageStore{db: db}, tx,
			types.NewJID("10000000000", types.DefaultUserServer), chatJID, nil, "", waLog.Noop)
	}()
	select {
	case name := <-done:
		if name != "Test Chat" {
			t.Fatalf("GetChatName = %q, want %q", name, "Test Chat")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("GetChatName blocked while the caller held the pool's only connection")
	}
}
