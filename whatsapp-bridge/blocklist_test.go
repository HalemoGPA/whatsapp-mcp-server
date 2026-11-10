package main

// Offline test for the /api/blocklist mapping. whatsmeow changed the blocklist
// from a flat list of JIDs to items that carry the listed JID and, when the
// server sends it, the phone JID beside it. The endpoint must keep returning
// phone JIDs where it can and must never drop an entry that has no phone JID.

import (
	"reflect"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestBlockedJIDStrings(t *testing.T) {
	lid := types.NewJID("100000000000001", types.HiddenUserServer)
	pn := types.NewJID("15550000001", types.DefaultUserServer)
	lidOnly := types.NewJID("100000000000002", types.HiddenUserServer)
	pnListed := types.NewJID("15550000003", types.DefaultUserServer)

	items := []types.BlocklistItem{
		{LID: lid, PN: pn, Active: true}, // LID-addressed with a phone JID: report the phone JID
		{LID: lidOnly},                   // no phone JID sent: keep the LID rather than drop it
		{LID: pnListed},                  // server listed a phone JID directly: pass it through
	}
	want := []string{
		"15550000001@s.whatsapp.net",
		"100000000000002@lid",
		"15550000003@s.whatsapp.net",
	}

	if got := blockedJIDStrings(items); !reflect.DeepEqual(got, want) {
		t.Fatalf("blockedJIDStrings() = %v, want %v", got, want)
	}
}

func TestBlockedJIDStrings_EmptyIsNotNil(t *testing.T) {
	// An empty blocklist must encode as [] in JSON, not null.
	got := blockedJIDStrings(nil)
	if got == nil || len(got) != 0 {
		t.Fatalf("blockedJIDStrings(nil) = %#v, want an empty non-nil slice", got)
	}
}
