package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/rand"
	"mime"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"rsc.io/qr"

	"image"
	"image/color"
	"image/png"

	"bytes"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waMmsRetry"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages.
	// WAL+NORMAL gives concurrent reads (the MCP container reads RO via a shared
	// volume) without sacrificing durability for a personal-scale workload.
	// busy_timeout=5000 makes both sides retry instead of erroring on lock contention.
	// journal_size_limit goes in the DSN so it applies to every connection in
	// Go's pool (a per-Exec PRAGMA only binds the one connection that ran it).
	db, err := sql.Open("sqlite3",
		"file:store/messages.db?"+
			"_foreign_keys=on"+
			"&_journal_mode=WAL"+
			"&_synchronous=NORMAL"+
			"&_busy_timeout=5000"+
			"&_journal_size_limit=67108864")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}

	// SQLite is single-writer; pin Go's pool to one connection so writes serialize
	// cleanly and our PRAGMAs always bind to the same conn. This also avoids any
	// risk of two pool connections racing on a long-held write transaction.
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)

	// Create tables if they don't exist.
	//
	// `name`       = display name (preference: user's saved contact name -> push name -> JID local-part)
	// `push_name`  = the contact's self-set display name (what they call themselves in WhatsApp).
	//                Stored separately so MCP tools can return BOTH values; clients render
	//                "Saved as Smith, calls themselves Jo" without parsing a combined string.
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}

	// Idempotent migration: add chats.push_name on databases that pre-date it.
	// SQLite returns "duplicate column" on re-run, which we swallow.
	if _, err := db.Exec(`ALTER TABLE chats ADD COLUMN push_name TEXT`); err != nil &&
		!strings.Contains(err.Error(), "duplicate column name") {
		db.Close()
		return nil, fmt.Errorf("failed to add chats.push_name column: %v", err)
	}

	// Batch α G2: Receipt-tracking columns. All NULLable so existing rows
	// coexist. delivered_at fires on events.Receipt Type "" (delivered),
	// read_at on Type "read"/"read-self", played_at on Type "played".
	// Batch α N2: raw_proto BLOB. Stores the marshalled *waProto.Message so
	// downstream features (vote_in_poll, forward_message with media,
	// comment encrypt, re-download) can reconstruct the exact original
	// without protocol gymnastics.
	// view_once: WhatsApp enforces "view once" client-side only, so the media
	// bytes stay downloadable via url+media_key long after the recipient has
	// burned their single view. What was missing was knowing WHICH rows those
	// were - the envelope is unwrapped before storage, so nothing distinguished
	// them. This flag records it at write time. Forward-only: rows written
	// before this column existed cannot be back-filled, because raw_proto is
	// marshalled post-unwrap and no longer carries the envelope.
	for _, col := range []string{
		`ALTER TABLE messages ADD COLUMN delivered_at TIMESTAMP`,
		`ALTER TABLE messages ADD COLUMN read_at      TIMESTAMP`,
		`ALTER TABLE messages ADD COLUMN played_at    TIMESTAMP`,
		`ALTER TABLE messages ADD COLUMN view_once    BOOLEAN DEFAULT 0`,
		`ALTER TABLE messages ADD COLUMN raw_proto    BLOB`,
	} {
		if _, err := db.Exec(col); err != nil && !strings.Contains(err.Error(), "duplicate column name") {
			db.Close()
			return nil, fmt.Errorf("failed to run migration %q: %v", col, err)
		}
	}

	// Batch α N12: cached profile pictures keyed on picture_id. events.Picture
	// invalidates a row so the next get_profile_picture refetches. Cuts
	// repeated CDN hits for tools that show avatars.
	if _, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS profile_pictures (
			jid         TEXT PRIMARY KEY,
			picture_id  TEXT,
			url         TEXT,
			direct_path TEXT,
			fetched_at  TIMESTAMP NOT NULL
		);
	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create profile_pictures: %v", err)
	}

	// Batch xi: reply-context columns on messages. Populated whenever the
	// inbound message carries ContextInfo.StanzaID (a quote-reply). Enables
	// get_message_thread / get_replies_to without walking every message row.
	for _, col := range []string{
		`ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT`,
		`ALTER TABLE messages ADD COLUMN reply_to_sender_jid TEXT`,
	} {
		if _, err := db.Exec(col); err != nil && !strings.Contains(err.Error(), "duplicate column name") {
			db.Close()
			return nil, fmt.Errorf("failed to run reply-context migration %q: %v", col, err)
		}
	}
	if _, err := db.Exec(
		`CREATE INDEX IF NOT EXISTS idx_messages_reply_to
		   ON messages(reply_to_message_id, chat_jid)
		   WHERE reply_to_message_id IS NOT NULL`,
	); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create idx_messages_reply_to: %v", err)
	}

	// Batch xi: group participants table. Populated on incoming group
	// messages (participant JID + group JID) and on GetGroupInfo lookups.
	// Enables offline "which groups is X in" and "who's in group Y" queries.
	if _, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS group_participants (
			group_jid       TEXT NOT NULL,
			jid             TEXT NOT NULL,
			is_admin        BOOLEAN DEFAULT 0,
			is_super_admin  BOOLEAN DEFAULT 0,
			first_seen_at   TIMESTAMP,
			last_seen_at    TIMESTAMP,
			PRIMARY KEY (group_jid, jid)
		);
		CREATE INDEX IF NOT EXISTS idx_group_participants_jid
			ON group_participants(jid);
	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create group_participants: %v", err)
	}

	// Batch nu: persisted reactions. WhatsApp reactions arrive as normal
	// messages whose payload is a ReactionMessage referring to a prior
	// message id. We store them separately so read tools can answer
	// "who reacted to X with what" without walking every row.
	if _, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS reactions (
			id                TEXT NOT NULL,
			chat_jid          TEXT NOT NULL,
			target_message_id TEXT NOT NULL,
			target_from_me    BOOLEAN,
			sender            TEXT,
			emoji             TEXT,
			timestamp         TIMESTAMP,
			PRIMARY KEY (id, chat_jid)
		);
		CREATE INDEX IF NOT EXISTS idx_reactions_target
			ON reactions(target_message_id, chat_jid);
		CREATE INDEX IF NOT EXISTS idx_reactions_sender
			ON reactions(sender, timestamp DESC);
	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create reactions table: %v", err)
	}

	// Polls. Two tables because a poll is a question plus N options, and a vote
	// points at an option by SHA-256 of its name (that is all the wire carries
	// - whatsmeow's HashPollOptions is sha256.Sum256([]byte(optionName))).
	// Storing the hash alongside the name at creation time is what lets a vote
	// be resolved back to something readable later.
	//
	// Before this, polls were invisible: extractTextContent had no poll case,
	// so a PollCreationMessage produced no content and no media and got dropped
	// by the empty-message guard - and PollUpdateMessage (the votes) the same
	// way. create_poll would send a poll that never appeared in our own history.
	if _, err := db.Exec(`
		CREATE TABLE IF NOT EXISTS polls (
			message_id       TEXT NOT NULL,
			chat_jid         TEXT NOT NULL,
			name             TEXT,
			option_index     INTEGER NOT NULL,
			option_name      TEXT NOT NULL,
			option_hash      BLOB NOT NULL,
			selectable_count INTEGER,
			created_at       TIMESTAMP,
			PRIMARY KEY (message_id, chat_jid, option_hash)
		);
		CREATE INDEX IF NOT EXISTS idx_polls_chat ON polls(chat_jid, created_at DESC);

		CREATE TABLE IF NOT EXISTS poll_votes (
			poll_message_id TEXT NOT NULL,
			poll_chat_jid   TEXT NOT NULL,
			voter_jid       TEXT NOT NULL,
			option_hash     BLOB NOT NULL,
			voted_at        TIMESTAMP,
			PRIMARY KEY (poll_message_id, poll_chat_jid, voter_jid, option_hash)
		);
		CREATE INDEX IF NOT EXISTS idx_poll_votes_poll
			ON poll_votes(poll_message_id, poll_chat_jid);
	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create poll tables: %v", err)
	}

	// Batch iota: chat state columns. All NULLable + idempotent so re-runs
	// are safe. Event handlers (events.Pin/Mute/Archive/MarkChatAsUnread)
	// keep them fresh once installed.
	for _, col := range []string{
		`ALTER TABLE chats ADD COLUMN is_pinned    BOOLEAN DEFAULT 0`,
		`ALTER TABLE chats ADD COLUMN is_muted     BOOLEAN DEFAULT 0`,
		`ALTER TABLE chats ADD COLUMN mute_end_ts  TIMESTAMP`,
		`ALTER TABLE chats ADD COLUMN is_archived  BOOLEAN DEFAULT 0`,
		`ALTER TABLE chats ADD COLUMN mark_unread  BOOLEAN DEFAULT 0`,
	} {
		if _, err := db.Exec(col); err != nil && !strings.Contains(err.Error(), "duplicate column name") {
			db.Close()
			return nil, fmt.Errorf("failed to run migration %q: %v", col, err)
		}
	}

	// Indexes for the hot read paths in the MCP server tools.
	// idx_messages_chat_time backs list_messages by chat (was full SCAN of 77k+ rows).
	// idx_messages_sender_time (composite) backs list_messages by sender AND lets
	// the planner satisfy ORDER BY timestamp without a TEMP B-TREE, so it's a strict
	// superset of the older idx_messages_sender. Drop the redundant one to save
	// b-tree writes on every insert and ~0.6 MB on disk.
	_, err = db.Exec(`
		CREATE INDEX IF NOT EXISTS idx_messages_chat_time
			ON messages(chat_jid, timestamp DESC);
		CREATE INDEX IF NOT EXISTS idx_messages_sender_time
			ON messages(sender, timestamp DESC);
		CREATE INDEX IF NOT EXISTS idx_messages_timestamp
			ON messages(timestamp DESC);
		-- F2: covering index for list_chats. Last_message_time DESC orders the
		-- response; jid+name+push_name covers every column the SELECT needs so
		-- list_chats is index-only (no row fetch into the heap).
		DROP INDEX IF EXISTS idx_chats_last_msg_time;
		CREATE INDEX IF NOT EXISTS idx_chats_last_msg_time
			ON chats(last_message_time DESC, jid, name, push_name);
		-- F3: partial index for list_media_in_chat. Only rows with media; zero
		-- insert cost on text rows, 5-50x fewer page fetches in text-heavy chats.
		CREATE INDEX IF NOT EXISTS idx_messages_chat_media_time
			ON messages(chat_jid, media_type, timestamp DESC)
			WHERE media_type IS NOT NULL AND media_type != '';
		DROP INDEX IF EXISTS idx_messages_sender;
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create indexes: %v", err)
	}

	// FTS5 contentless mirror for messages.content. The Python `list_messages`
	// `query` filter previously ran `LOWER(content) LIKE LOWER('%foo%')`, which
	// SCAN'd all 77k+ rows on every call. With FTS5 + the unicode61 tokenizer
	// (remove_diacritics 2 - matches case-insensitively AND folds Arabic/accented
	// forms, which is relevant given the user's chat languages) MATCH searches
	// run from a small posting-list lookup. Content stays in `messages` (no
	// duplication); triggers keep `messages_fts` in sync on insert/delete/update.
	// External-content semantics: external rowid IS messages.rowid.
	_, err = db.Exec(`
		CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
			content,
			content='messages',
			content_rowid='rowid',
			tokenize='unicode61 remove_diacritics 2'
		);
		CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
			INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
		END;
		CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
			INSERT INTO messages_fts(messages_fts, rowid, content)
				VALUES('delete', old.rowid, old.content);
		END;
		CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
			INSERT INTO messages_fts(messages_fts, rowid, content)
				VALUES('delete', old.rowid, old.content);
			INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
		END;
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create FTS5 schema: %v", err)
	}

	// Idempotent FTS5 backfill via the canonical 'rebuild' command for
	// external-content tables. The 'rebuild' command re-reads every row in the
	// external content table (messages) and rebuilds the FTS index from scratch.
	// We trigger it only when the on-disk index is essentially empty (less than
	// 50 docs in messages_fts_docsize - small enough that any real data is missing,
	// large enough to ignore the handful of post-startup triggers). On a fresh
	// 77k-message DB the rebuild takes single-digit seconds and runs in a
	// goroutine so the bridge stays responsive.
	//
	// Note: SELECT COUNT(*) FROM messages_fts is MISLEADING for external-content
	// tables - it returns the count of the underlying messages table, not the
	// indexed-row count. The truth is in the internal messages_fts_docsize table.
	var ftsDocCount, msgCount int
	_ = db.QueryRow(`SELECT COUNT(*) FROM messages_fts_docsize`).Scan(&ftsDocCount)
	_ = db.QueryRow(`SELECT COUNT(*) FROM messages WHERE content IS NOT NULL AND content != ''`).Scan(&msgCount)
	if ftsDocCount < 50 && msgCount > 0 {
		go func() {
			fmt.Printf("FTS5 rebuild starting (target ~%d docs, currently indexed %d)...\n", msgCount, ftsDocCount)
			if _, e := db.Exec(`INSERT INTO messages_fts(messages_fts) VALUES('rebuild')`); e != nil {
				fmt.Printf("FTS5 rebuild failed (non-fatal): %v\n", e)
				return
			}
			var newCount int
			_ = db.QueryRow(`SELECT COUNT(*) FROM messages_fts_docsize`).Scan(&newCount)
			fmt.Printf("FTS5 rebuild complete; %d docs indexed\n", newCount)
		}()
	}

	// Run ANALYZE once if the planner has no stats yet (sqlite_stat1 absent).
	// Cost is sub-second on this dataset; enables better plan choices once
	// idx_messages_sender_time is in play. We use a goroutine to avoid blocking
	// container startup if the table is large.
	var hasStats int
	_ = db.QueryRow(`SELECT COUNT(*) FROM sqlite_master WHERE name = 'sqlite_stat1'`).Scan(&hasStats)
	if hasStats == 0 {
		go func() {
			if _, e := db.Exec(`ANALYZE`); e != nil {
				fmt.Printf("ANALYZE failed (non-fatal): %v\n", e)
			} else {
				fmt.Println("ANALYZE complete; planner stats populated")
			}
		}()
	}

	return &MessageStore{db: db}, nil
}

// startMediaReaper deletes downloaded media files under storeRoot older than
// ttl, hourly. Walks storeRoot/<jid-looking-dir>/* and removes regular files
// whose mtime is before now-ttl. Never touches files at the storeRoot itself
// (where messages.db / whatsapp.db / qr.png live) - only descends into
// subdirectories whose name contains "@" (the chat-JID dirs the bridge writes
// downloaded media into). Disable with BRIDGE_MEDIA_TTL_DAYS=0; default 14d.
func startMediaReaper(parent context.Context, storeRoot string, ttl time.Duration) {
	if ttl <= 0 {
		return
	}
	go func() {
		ticker := time.NewTicker(1 * time.Hour)
		defer ticker.Stop()
		reapOnce := func() {
			cutoff := time.Now().Add(-ttl)
			entries, err := os.ReadDir(storeRoot)
			if err != nil {
				return
			}
			var totalCount int
			var totalBytes int64
			for _, e := range entries {
				if !e.IsDir() || !strings.Contains(e.Name(), "@") {
					continue
				}
				chatDir := filepath.Join(storeRoot, e.Name())
				files, err := os.ReadDir(chatDir)
				if err != nil {
					continue
				}
				for _, f := range files {
					if f.IsDir() {
						continue
					}
					info, err := f.Info()
					if err != nil {
						continue
					}
					if info.ModTime().Before(cutoff) {
						p := filepath.Join(chatDir, f.Name())
						sz := info.Size()
						if err := os.Remove(p); err == nil {
							totalCount++
							totalBytes += sz
						}
					}
				}
			}
			if totalCount > 0 {
				fmt.Printf("Media reaper: deleted %d file(s), freed %d KiB\n",
					totalCount, totalBytes/1024)
			}
		}
		// First pass on startup so a long-stopped container catches up.
		reapOnce()
		for {
			select {
			case <-parent.Done():
				return
			case <-ticker.C:
				reapOnce()
			}
		}
	}()
}

// startWALCheckpoints runs PRAGMA wal_checkpoint(TRUNCATE) every interval to
// keep messages.db-wal bounded. Long-lived RO readers (the MCP server) can
// prevent automatic PASSIVE checkpoints from advancing; this catches up.
// TRUNCATE tolerates concurrent readers in WAL mode and blocks new writers only
// briefly.
//
// F10: every 12th tick (one hour at the 5-min default interval) also runs
// PRAGMA optimize. Cheap maintenance that keeps query plans stable past
// ~200k rows. analysis_limit caps work so it can't run away on huge tables.
// Stopped when ctx is cancelled.
func (store *MessageStore) startWALCheckpoints(ctx context.Context, interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		tick := 0
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if _, err := store.db.ExecContext(ctx, `PRAGMA wal_checkpoint(TRUNCATE)`); err != nil {
					fmt.Printf("WAL checkpoint failed (non-fatal): %v\n", err)
				}
				tick++
				if tick%12 == 0 {
					if _, err := store.db.ExecContext(ctx, `PRAGMA analysis_limit=400; PRAGMA optimize`); err != nil {
						fmt.Printf("PRAGMA optimize failed (non-fatal): %v\n", err)
					}
				}
			}
		}
	}()
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// isViewOnceProto reports whether a RAW (still-wrapped) message is a view-once
// envelope.
//
// events.Message exposes IsViewOnce for live incoming messages because
// whatsmeow unwraps them for us. The outgoing-send and history-sync paths never
// go through that struct - they hold the raw *waProto.Message - so they detect
// it here instead. The getters are nil-safe, so a nil message is simply false.
func isViewOnceProto(m *waProto.Message) bool {
	if m == nil {
		return false
	}
	if m.GetViewOnceMessage().GetMessage() != nil ||
		m.GetViewOnceMessageV2().GetMessage() != nil ||
		m.GetViewOnceMessageV2Extension().GetMessage() != nil {
		return true
	}
	// Also honour the payload's own viewOnce bool. That is the flag the mobile
	// client actually enforces, and it survives on its own once an envelope has
	// been unwrapped - so a message can be genuinely view-once without any
	// wrapper still attached.
	return m.GetImageMessage().GetViewOnce() ||
		m.GetVideoMessage().GetViewOnce()
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64, rawProto []byte,
	viewOnce bool) error {
	return store.StoreMessageWithReply(id, chatJID, sender, content, timestamp, isFromMe,
		mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, rawProto, "", "", viewOnce)
}

// StoreMessageWithReply is the full-parameter form; StoreMessage stays as a
// thin wrapper so no existing caller has to know about reply-context.
//
// view_once is written as part of the INSERT rather than a follow-up UPDATE on
// purpose: this is INSERT OR REPLACE, so a later re-store of the same id (a
// history resync, say) replaces the whole row and would silently reset a
// separately-written flag back to its default.
func (store *MessageStore) StoreMessageWithReply(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64, rawProto []byte,
	replyToMessageID, replyToSenderJID string, viewOnce bool) error {
	if content == "" && mediaType == "" {
		return nil
	}
	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length, raw_proto, reply_to_message_id, reply_to_sender_jid, view_once)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, timestamp, isFromMe, mediaType, filename, url,
		mediaKey, fileSHA256, fileEncSHA256, fileLength, rawProto,
		nullIfEmpty(replyToMessageID), nullIfEmpty(replyToSenderJID), viewOnce,
	)
	return err
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// StorePoll records a poll's question and options. Hashes are computed here,
// at creation time, because a vote only ever carries sha256(optionName) - if we
// don't keep the mapping now, an incoming vote is an unresolvable digest later.
func (store *MessageStore) StorePoll(msgID, chatJID, name string, options []string,
	selectableCount int, ts time.Time) error {
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	for i, opt := range options {
		h := sha256.Sum256([]byte(opt))
		if _, err := tx.Exec(
			`INSERT OR REPLACE INTO polls
			 (message_id, chat_jid, name, option_index, option_name, option_hash, selectable_count, created_at)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
			msgID, chatJID, name, i, opt, h[:], selectableCount, ts,
		); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// StorePollVote replaces a voter's selection on a poll.
//
// Replace, not append: a PollUpdateMessage carries the voter's COMPLETE current
// selection, so someone changing their mind sends a fresh update listing only
// what they now have selected. Appending would leave the old choice counted
// forever, and an empty selection (vote retracted) would be a no-op instead of
// a clear. Done in one transaction so a tally can never observe a voter
// mid-update with zero rows.
func (store *MessageStore) StorePollVote(pollMsgID, pollChatJID, voter string,
	optionHashes [][]byte, ts time.Time) error {
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	if _, err := tx.Exec(
		`DELETE FROM poll_votes WHERE poll_message_id = ? AND poll_chat_jid = ? AND voter_jid = ?`,
		pollMsgID, pollChatJID, voter,
	); err != nil {
		return err
	}
	for _, h := range optionHashes {
		if _, err := tx.Exec(
			`INSERT OR REPLACE INTO poll_votes
			 (poll_message_id, poll_chat_jid, voter_jid, option_hash, voted_at)
			 VALUES (?, ?, ?, ?, ?)`,
			pollMsgID, pollChatJID, voter, h, ts,
		); err != nil {
			return err
		}
	}
	return tx.Commit()
}

// StoreMessageTx is StoreMessage running inside an existing transaction.
// History sync writes thousands of rows in one burst; doing them via the
// autocommit path means one fsync per row at synchronous=NORMAL, which is
// where most of history-sync time was being spent and where the WAL grew
// faster than the checkpoint goroutine could keep up with. Batching into
// one BEGIN/COMMIT per ~500 rows collapses fsyncs by 500x.
func (store *MessageStore) StoreMessageTx(tx *sql.Tx, id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64, rawProto []byte,
	viewOnce bool) error {
	if content == "" && mediaType == "" {
		return nil
	}
	_, err := tx.Exec(
		`INSERT OR REPLACE INTO messages
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length, raw_proto, view_once)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, rawProto, viewOnce,
	)
	return err
}

// StoreChatTx is StoreChat running inside an existing transaction (companion to StoreMessageTx).
func (store *MessageStore) StoreChatTx(tx *sql.Tx, jid, name string, lastMessageTime time.Time) error {
	_, err := tx.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// BeginTx opens a transaction on the underlying database/sql pool. The pool is
// capped at MaxOpenConns=1 so there's only ever one writer, but BeginTx still
// gives us BEGIN/COMMIT batching semantics for the WAL.
func (store *MessageStore) BeginTx(ctx context.Context) (*sql.Tx, error) {
	return store.db.BeginTx(ctx, nil)
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// GetMessageByID fetches one message's sender, content and media type by its id
// within a chat. Used by the send path to (a) auto-fill the reply participant
// when the caller didn't supply one and (b) build a REAL quoted-reply preview
// from the original text. found=false when the id isn't in the store, so the
// caller can fall back to the previous behaviour (no regression).
func (store *MessageStore) GetMessageByID(id, chatJID string) (sender, content, mediaType string, found bool) {
	row := store.db.QueryRow(
		"SELECT sender, COALESCE(content,''), COALESCE(media_type,'') FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	)
	if err := row.Scan(&sender, &content, &mediaType); err != nil {
		return "", "", "", false
	}
	return sender, content, mediaType, true
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// Extract text content from a message
// pollCreation returns the poll payload from whichever versioned field carries
// it, or nil.
//
// WhatsApp has SIX poll creation fields on Message and a client picks one by
// version: pollCreationMessage=49, V2=60, V3=64, V4=93, V5=111, V6=119. Five
// carry *PollCreationMessage directly; V4 alone is a FutureProofMessage
// envelope that must be unwrapped.
//
// Checking only the original made polls from a current phone invisible: the
// getter returned nil, extractTextContent produced "", and the empty-message
// guard dropped the row - while the VOTES on that poll still arrived and were
// stored, leaving orphan votes pointing at a poll we had no record of. Found
// 2026-07-16 by creating a poll on the phone and watching only its votes land.
func pollCreation(msg *waProto.Message) *waProto.PollCreationMessage {
	return pollCreationDepth(msg, 0)
}

func pollCreationDepth(msg *waProto.Message, depth int) *waProto.PollCreationMessage {
	// Envelopes nest in principle; bound the recursion so a malformed message
	// can't run us out of stack.
	if msg == nil || depth > 3 {
		return nil
	}
	for _, pc := range []*waProto.PollCreationMessage{
		msg.GetPollCreationMessage(),
		msg.GetPollCreationMessageV2(),
		msg.GetPollCreationMessageV3(),
		msg.GetPollCreationMessageV5(),
		msg.GetPollCreationMessageV6(),
	} {
		if pc != nil {
			return pc
		}
	}
	if v4 := msg.GetPollCreationMessageV4(); v4 != nil {
		return pollCreationDepth(v4.GetMessage(), depth+1)
	}
	return nil
}

func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try plain text first.
	if text := msg.GetConversation(); text != "" {
		return text
	}
	if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		return extendedText.GetText()
	}
	// Adopted from upstream PR #268: media captions were previously dropped.
	if im := msg.GetImageMessage(); im != nil && im.GetCaption() != "" {
		return im.GetCaption()
	}
	if vm := msg.GetVideoMessage(); vm != nil && vm.GetCaption() != "" {
		return vm.GetCaption()
	}
	if dm := msg.GetDocumentMessage(); dm != nil && dm.GetCaption() != "" {
		return dm.GetCaption()
	}
	// Adopted from upstream PR #264: WhatsApp Business senders (DHL, banks,
	// OTP services) pack their text into rich-message envelopes. Fall
	// through to each so the row lands with real content instead of ''.
	if tm := msg.GetTemplateMessage(); tm != nil {
		if fm := tm.GetHydratedTemplate(); fm != nil {
			if t := fm.GetHydratedContentText(); t != "" {
				return t
			}
		}
	}
	if im := msg.GetInteractiveMessage(); im != nil {
		if body := im.GetBody(); body != nil && body.GetText() != "" {
			return body.GetText()
		}
	}
	if bm := msg.GetButtonsMessage(); bm != nil {
		if bm.GetContentText() != "" {
			return bm.GetContentText()
		}
	}
	if lm := msg.GetListMessage(); lm != nil {
		if lm.GetDescription() != "" {
			return lm.GetDescription()
		}
	}
	// Polls: surface the question as the row's content. Without this a poll has
	// no text and no media, so the empty-message guard in handleMessage drops it
	// and the poll never exists in our history at all - which also made its
	// votes unresolvable, since votes reference the creation message by ID.
	if pc := pollCreation(msg); pc != nil && pc.GetName() != "" {
		return pc.GetName()
	}
	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// SendMessageRequest represents the request body for the send message API.
// New optional fields ReplyToMessageID/ReplyToSenderJID/MentionedJIDs are
// populated on the outgoing message's ContextInfo when present, so a single
// /api/send call can do "plain text", "quoted reply", "with @mentions" or any
// combination - no separate endpoint needed.
type SendMessageRequest struct {
	Recipient        string   `json:"recipient"`
	Message          string   `json:"message"`
	MediaPath        string   `json:"media_path,omitempty"`
	ReplyToMessageID string   `json:"reply_to_message_id,omitempty"`
	ReplyToSenderJID string   `json:"reply_to_sender_jid,omitempty"`
	MentionedJIDs    []string `json:"mentioned_jids,omitempty"`
	// ViewOnce wraps an outgoing image or video in ViewOnceMessageV2 so the
	// recipient's official WhatsApp app shows a one-tap-only media. Note that
	// other custom clients (including this bridge) ignore the enforcement -
	// `download_media` on a received view-once still returns the raw bytes.
	ViewOnce bool `json:"view_once,omitempty"`
}

// HealthResponse is the JSON shape of GET /api/health.
type HealthResponse struct {
	Connected bool   `json:"connected"`
	LoggedIn  bool   `json:"logged_in"`
	PushName  string `json:"push_name,omitempty"`
	PingMS    int64  `json:"ping_ms,omitempty"`
}

// ReactRequest: POST /api/react - add or remove a reaction.
type ReactRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	SenderJID string `json:"sender_jid,omitempty"` // omit for messages you sent (== self JID)
	Emoji     string `json:"emoji"`                // empty string removes the reaction
}

// EditRequest: POST /api/edit - edit your own text message (24h window).
type EditRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	NewText   string `json:"new_text"`
}

// DeleteRequest: POST /api/delete - revoke (delete-for-everyone) a message you sent.
type DeleteRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	SenderJID string `json:"sender_jid,omitempty"` // omit for messages you sent
}

// MarkReadRequest: POST /api/mark_read - send read receipts.
type MarkReadRequest struct {
	ChatJID    string   `json:"chat_jid"`
	MessageIDs []string `json:"message_ids"`
	SenderJID  string   `json:"sender_jid,omitempty"` // omit for direct chats (defaults to ChatJID)
}

// PresenceRequest: POST /api/presence - typing/recording indicator.
type PresenceRequest struct {
	ChatJID string `json:"chat_jid"`
	State   string `json:"state"`           // "composing" (typing), "paused", "recording"
	Media   string `json:"media,omitempty"` // "audio" for voice recording; omit for typing
}

// CreateGroupRequest: POST /api/group/create - create a new WhatsApp group.
type CreateGroupRequest struct {
	Subject      string   `json:"subject"`
	Participants []string `json:"participants"` // phone numbers or JIDs
}

// GroupParticipantsRequest: POST /api/group/participants - manage a group's members.
type GroupParticipantsRequest struct {
	GroupJID     string   `json:"group_jid"`
	Action       string   `json:"action"`       // add | remove | promote | demote
	Participants []string `json:"participants"` // JIDs to act on
}

// GroupInfoRequest: POST /api/group/info - metadata for a group I'm in.
type GroupInfoRequest struct {
	GroupJID string `json:"group_jid"`
}

// BlockRequest: POST /api/block - block a contact.
type BlockRequest struct {
	JID string `json:"jid"`
}

// NewsletterInfoRequest: POST /api/newsletter/info - metadata for a channel/newsletter.
type NewsletterInfoRequest struct {
	NewsletterJID string `json:"newsletter_jid"`
}

// CheckPhonesRequest: POST /api/contacts/check - resolve raw phone numbers
// (E.164 without +) to JIDs and confirm WhatsApp registration.
type CheckPhonesRequest struct {
	Phones []string `json:"phones"`
}

// UsersInfoRequest: POST /api/users/info - bulk fetch UserInfo for JIDs.
type UsersInfoRequest struct {
	JIDs []string `json:"jids"`
}

// GroupInviteLinkRequest: POST /api/group/invite_link - get/reset invite link.
type GroupInviteLinkRequest struct {
	GroupJID string `json:"group_jid"`
	Reset    bool   `json:"reset"`
}

// GroupJoinRequest: POST /api/group/join - preview or join a group by link.
type GroupJoinRequest struct {
	Link        string `json:"link"`         // either the full chat.whatsapp.com URL or just the code
	PreviewOnly bool   `json:"preview_only"` // true = GetGroupInfoFromLink, false = JoinGroupWithLink
}

// GroupLeaveRequest: POST /api/group/leave - leave a group.
type GroupLeaveRequest struct {
	GroupJID string `json:"group_jid"`
}

// --- G6 chat state ops ------------------------------------------------------

// ChatStateRequest: POST /api/chat/{mute,pin,archive,mark_unread}
type ChatStateRequest struct {
	ChatJID   string `json:"chat_jid"`
	Value     bool   `json:"value"`                // true = mute/pin/archive/mark_unread
	DurationS int    `json:"duration_s,omitempty"` // mute only; 0 = default
}

// StarMessageRequest: POST /api/message/star
type StarMessageRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	SenderJID string `json:"sender_jid,omitempty"` // required for group messages
	IsFromMe  bool   `json:"is_from_me"`
	Starred   bool   `json:"starred"`
}

// --- N9 status / privacy ----------------------------------------------------

// PostStatusRequest: POST /api/status/post - post to your status broadcast.
type PostStatusRequest struct {
	Message string `json:"message"`
}

// --- N11 forward message ----------------------------------------------------

// ForwardMessageRequest: POST /api/forward
type ForwardMessageRequest struct {
	SourceChatJID string `json:"source_chat_jid"`
	MessageID     string `json:"message_id"`
	TargetJID     string `json:"target_jid"`
}

// VotePollRequest: POST /api/poll/vote
type VotePollRequest struct {
	PollChatJID   string   `json:"poll_chat_jid"`
	PollMessageID string   `json:"poll_message_id"`
	PollSenderJID string   `json:"poll_sender_jid,omitempty"` // required for group polls
	OptionNames   []string `json:"option_names"`
}

// --- N10 labels (WA Business) ----------------------------------------------

type LabelEditRequest struct {
	LabelID    string `json:"label_id"`
	LabelName  string `json:"label_name,omitempty"`
	LabelColor int32  `json:"label_color,omitempty"`
	Delete     bool   `json:"delete,omitempty"`
}
type LabelChatRequest struct {
	LabelID string `json:"label_id"`
	ChatJID string `json:"chat_jid"`
	Labeled bool   `json:"labeled"`
}
type LabelMessageRequest struct {
	LabelID   string `json:"label_id"`
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	Labeled   bool   `json:"labeled"`
}

// SendLocationRequest: POST /api/send_location - share a static location.
type SendLocationRequest struct {
	Recipient string  `json:"recipient"`
	Latitude  float64 `json:"latitude"`
	Longitude float64 `json:"longitude"`
	Name      string  `json:"name,omitempty"`
	Address   string  `json:"address,omitempty"`
}

// SetDisappearingRequest: POST /api/set_disappearing - configure ephemeral msgs in a chat.
type SetDisappearingRequest struct {
	ChatJID string `json:"chat_jid"`
	Seconds int    `json:"seconds"` // 0 disables; 86400/604800/7776000 = 24h/7d/90d
}

// CreatePollRequest: POST /api/create_poll - send a poll to a chat.
type CreatePollRequest struct {
	Recipient              string   `json:"recipient"`
	Name                   string   `json:"name"`
	Options                []string `json:"options"`
	SelectableOptionsCount int      `json:"selectable_options_count,omitempty"` // 0/1 = single-choice
}

// ProfilePictureRequest: POST /api/profile_picture - fetch URL of a JID's avatar.
type ProfilePictureRequest struct {
	JID     string `json:"jid"`
	Preview bool   `json:"preview,omitempty"`
}

// ProfilePictureResponse: response for /api/profile_picture.
type ProfilePictureResponse struct {
	Success bool   `json:"success"`
	URL     string `json:"url,omitempty"`
	Type    string `json:"type,omitempty"`
	ID      string `json:"id,omitempty"`
	Message string `json:"message,omitempty"`
}

// GenericResponse is the common JSON shape for endpoints that only need
// success/message (react/edit/delete/mark_read/presence).
type GenericResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message,omitempty"`
}

// sendWhatsAppMessageEx is the full-feature send. ctx is propagated to whatsmeow
// client.Upload and client.SendMessage so cancellation (client disconnect, HTTP
// server WriteTimeout, deliberate abort) flows through to WhatsApp. When
// replyToMessageID is set the outgoing message carries a ContextInfo with
// StanzaID + Participant + a QuotedMessage. The participant and the quoted text
// are auto-filled from the original message in our store (looked up by id), so a
// reply works from the message id alone and the quote bar shows the real text.
// MentionedJIDs populates
// ContextInfo.MentionedJID; the message body should include `@<localpart>` for
// each mention so renderers highlight. viewOnce wraps an Image/Video payload
// in ViewOnceMessageV2 (the recipient's official client enforces "view once",
// but the bytes are still on the CDN for whatsmeow to fetch).
func sendWhatsAppMessageEx(
	ctx context.Context,
	client *whatsmeow.Client,
	messageStore *MessageStore,
	recipient string,
	message string,
	mediaPath string,
	replyToMessageID string,
	replyToSenderJID string,
	mentionedJIDs []string,
	viewOnce bool,
) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err)
		}
	} else {
		// Create JID from phone number
		recipientJID = types.JID{
			User:   recipient,
			Server: "s.whatsapp.net", // For personal chats
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err)
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.ToLower(mediaPath[strings.LastIndex(mediaPath, ".")+1:])
		var mediaType whatsmeow.MediaType
		var mimeType string

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type). Adopted from upstream
		// PRs #199 / #236 / #243: previously EVERY non-image/audio/video
		// arrived as application/octet-stream + no FileName -> WhatsApp
		// showed the message as "Untitled" with no preview / no icon.
		// Explicit MIME table + mime.TypeByExtension fallback for anything
		// we don't hard-code.
		default:
			mediaType = whatsmeow.MediaDocument
			switch fileExt {
			case "pdf":
				mimeType = "application/pdf"
			case "doc":
				mimeType = "application/msword"
			case "docx":
				mimeType = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
			case "xls":
				mimeType = "application/vnd.ms-excel"
			case "xlsx":
				mimeType = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
			case "ppt":
				mimeType = "application/vnd.ms-powerpoint"
			case "pptx":
				mimeType = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
			case "odt":
				mimeType = "application/vnd.oasis.opendocument.text"
			case "ods":
				mimeType = "application/vnd.oasis.opendocument.spreadsheet"
			case "txt":
				mimeType = "text/plain"
			case "csv":
				mimeType = "text/csv"
			case "json":
				mimeType = "application/json"
			case "xml":
				mimeType = "application/xml"
			case "zip":
				mimeType = "application/zip"
			default:
				if t := mime.TypeByExtension("." + fileExt); t != "" {
					mimeType = t
				} else {
					mimeType = "application/octet-stream"
				}
			}
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(ctx, mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err)
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err)
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			// Adopted from upstream PRs #199/#236: WhatsApp shows the FileName
			// field (not Title) to the recipient. Setting only Title left
			// documents labelled "Untitled". Set both.
			docName := filepath.Base(mediaPath)
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(docName),
				FileName:      proto.String(docName),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		// Plain text: pick Conversation (smallest wire) unless we need
		// ContextInfo (for reply or mentions), in which case switch to
		// ExtendedTextMessage which is the only text variant that carries
		// ContextInfo.
		if replyToMessageID != "" || len(mentionedJIDs) > 0 {
			msg.ExtendedTextMessage = &waProto.ExtendedTextMessage{
				Text: proto.String(message),
			}
		} else {
			msg.Conversation = proto.String(message)
		}
	}

	// Apply ContextInfo for replies/mentions, if requested. ContextInfo
	// hangs off ExtendedTextMessage / ImageMessage / VideoMessage etc; one
	// is attached depending on which payload we built above.
	if replyToMessageID != "" || len(mentionedJIDs) > 0 {
		ctx := &waProto.ContextInfo{}
		if replyToMessageID != "" {
			ctx.StanzaID = proto.String(replyToMessageID)
			// Look up the quoted message in our own store so a reply "just works"
			// from the message id alone: (a) auto-fill the participant when the
			// caller didn't pass one (required for group quotes), and (b) embed
			// the ORIGINAL text as the QuotedMessage so the grey quote bar renders
			// what you're replying to instead of an empty stub. Best-effort - if
			// the id isn't in the store we keep the previous behaviour.
			quotedText := ""
			if messageStore != nil {
				if snd, content, mtype, ok := messageStore.GetMessageByID(replyToMessageID, recipientJID.String()); ok {
					if replyToSenderJID == "" && snd != "" {
						if strings.Contains(snd, "@") {
							replyToSenderJID = snd
						} else {
							replyToSenderJID = snd + "@s.whatsapp.net"
						}
					}
					if content != "" {
						quotedText = content
					} else if mtype != "" {
						quotedText = "[" + mtype + "]"
					}
				}
			}
			if replyToSenderJID == "" {
				replyToSenderJID = recipientJID.String()
			}
			ctx.Participant = proto.String(replyToSenderJID)
			ctx.QuotedMessage = &waProto.Message{Conversation: proto.String(quotedText)}
		}
		if len(mentionedJIDs) > 0 {
			ctx.MentionedJID = mentionedJIDs
		}
		switch {
		case msg.ExtendedTextMessage != nil:
			msg.ExtendedTextMessage.ContextInfo = ctx
		case msg.ImageMessage != nil:
			msg.ImageMessage.ContextInfo = ctx
		case msg.VideoMessage != nil:
			msg.VideoMessage.ContextInfo = ctx
		case msg.AudioMessage != nil:
			msg.AudioMessage.ContextInfo = ctx
		case msg.DocumentMessage != nil:
			msg.DocumentMessage.ContextInfo = ctx
		}
	}

	// View-once wrap. Only image and video are accepted by WhatsApp's view-once
	// flow; for other media types we silently send as a normal message rather
	// than fail (caller likely doesn't care if a document is view-once).
	//
	// TWO things must be set, not one. The ViewOnceMessageV2 envelope is what
	// WhatsApp Desktop reads (it renders "only once - view from your phone"),
	// but the MOBILE client enforces view-once off the inner payload's own
	// viewOnce bool. Setting only the envelope produced a message that Desktop
	// labelled view-once while the phone happily re-opened it forever - which
	// is exactly the bug reported on 2026-07-16, and why it looked like a
	// self-chat quirk at first. Official clients set both; so do we now.
	if viewOnce && (msg.ImageMessage != nil || msg.VideoMessage != nil) {
		inner := &waProto.Message{}
		if msg.ImageMessage != nil {
			msg.ImageMessage.ViewOnce = proto.Bool(true)
			inner.ImageMessage = msg.ImageMessage
			msg.ImageMessage = nil
		}
		if msg.VideoMessage != nil {
			msg.VideoMessage.ViewOnce = proto.Bool(true)
			inner.VideoMessage = msg.VideoMessage
			msg.VideoMessage = nil
		}
		msg.ViewOnceMessageV2 = &waProto.FutureProofMessage{Message: inner}
	}

	// Send message
	resp, err := client.SendMessage(ctx, recipientJID, msg)

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err)
	}

	// #97: persist our outbound message so list_messages reflects sends
	// initiated from the MCP side (whatsmeow doesn't echo our own sends
	// back to us as events.Message). Extract content + media info from
	// the same message struct we just sent. messageStore may be nil for
	// the legacy sendWhatsAppMessage wrapper - skip persistence there.
	var outContent string
	if msg.Conversation != nil {
		outContent = msg.GetConversation()
	} else if msg.ExtendedTextMessage != nil {
		outContent = msg.ExtendedTextMessage.GetText()
	}
	outMediaType, outFilename, outURL, outMediaKey, outSHA, outEncSHA, outLen := extractMediaInfo(msg)

	sender := ""
	if client.Store != nil && client.Store.ID != nil {
		sender = client.Store.ID.User
	}
	var outRaw []byte
	if b, mErr := proto.Marshal(msg); mErr == nil {
		outRaw = b
	}
	// Also make sure the chat row exists so list_chats picks it up.
	if messageStore != nil {
		_ = messageStore.StoreChat(recipientJID.String(), "", resp.Timestamp)
		// Outgoing path: msg is the proto we just built, and the send path
		// wraps it in ViewOnceMessageV2 above, so detect from the proto.
		if err := messageStore.StoreMessage(
			resp.ID, recipientJID.String(), sender, outContent, resp.Timestamp, true,
			outMediaType, outFilename, outURL, outMediaKey, outSHA, outEncSHA, outLen, outRaw,
			isViewOnceProto(msg),
		); err != nil {
			fmt.Printf("outgoing persist failed for %s: %v\n", resp.ID, err)
		}
	}
	// Also emit a bridgeEvent so SSE/webhook consumers see outbound sends.
	select {
	case eventBus <- bridgeEvent{
		Type:      "message",
		ChatJID:   recipientJID.String(),
		MessageID: resp.ID,
		Sender:    sender,
		IsFromMe:  true,
		Content:   outContent,
		MediaType: outMediaType,
		Timestamp: resp.Timestamp,
	}:
	default:
	}

	return true, fmt.Sprintf("Message sent to %s", recipient)
}

// mimeForFilename picks a Content-Type based on filename extension first, then
// falls back to a per-media-type default. Covers what extractMediaInfo emits.
func mimeForFilename(filename, mediaType string) string {
	if i := strings.LastIndex(filename, "."); i >= 0 {
		switch strings.ToLower(filename[i+1:]) {
		case "jpg", "jpeg":
			return "image/jpeg"
		case "png":
			return "image/png"
		case "gif":
			return "image/gif"
		case "webp":
			return "image/webp"
		case "mp4":
			return "video/mp4"
		case "mov":
			return "video/quicktime"
		case "avi":
			return "video/avi"
		case "ogg":
			return "audio/ogg"
		case "opus":
			return "audio/ogg; codecs=opus"
		case "mp3":
			return "audio/mpeg"
		case "m4a":
			return "audio/mp4"
		case "pdf":
			return "application/pdf"
		}
	}
	switch mediaType {
	case "image":
		return "image/jpeg"
	case "video":
		return "video/mp4"
	case "audio":
		return "audio/ogg"
	}
	return "application/octet-stream"
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// View-once messages wrap a real media message in an envelope. Unwrap
	// them so the same code path handles regular and view-once images/videos
	// uniformly.
	//
	// This only ever fires for view-once media WE SEND. Incoming view-once
	// never reaches this code: since Meta's server-side fix of ~Nov 2024, the
	// server strips the E2EE payload before delivering to a companion device,
	// so we receive `<unavailable type="view_once"/>` with no <enc> child at
	// all. whatsmeow then asks the primary phone to resend (ungated,
	// automatic) and the phone declines. Verified on the wire 2026-07-16.
	//
	// The comment that used to live here claimed the bytes were "downloadable
	// by whatsmeow regardless of view count". That was true when it was
	// written - it is the bypass Tal Be'ery disclosed in Aug 2024 - and Meta
	// closed it. Nothing client-side can undo that: the ciphertext is never
	// sent to us.
	if vo := msg.GetViewOnceMessage(); vo != nil && vo.GetMessage() != nil {
		return extractMediaInfo(vo.GetMessage())
	}
	if vo := msg.GetViewOnceMessageV2(); vo != nil && vo.GetMessage() != nil {
		return extractMediaInfo(vo.GetMessage())
	}
	if vo := msg.GetViewOnceMessageV2Extension(); vo != nil && vo.GetMessage() != nil {
		return extractMediaInfo(vo.GetMessage())
	}

	// Content-hash-derived filenames (adopted from upstream PR #273): timestamp
	// collisions during bursty history sync were producing duplicate names
	// and clobbering rows on the disk-fallback download path.
	sha6 := func(prefix, ext string, sha []byte) string {
		if len(sha) >= 6 {
			return fmt.Sprintf("%s_%x%s", prefix, sha[:6], ext)
		}
		return prefix + "_" + time.Now().Format("20060102_150405") + ext
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", sha6("image", ".jpg", img.GetFileSHA256()),
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", sha6("video", ".mp4", vid.GetFileSHA256()),
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", sha6("audio", ".ogg", aud.GetFileSHA256()),
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := doc.GetFileName()
		if filename == "" {
			filename = "document_" + time.Now().Format("20060102_150405")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// Handle regular incoming messages with media support
// handlePollVote decrypts an incoming poll vote and records it.
//
// Votes are E2E-encrypted with a secret derived from the POLL CREATION message,
// so whatsmeow can only decrypt this if it saw that poll go by (it keeps the
// secret in its msgsecret store). A vote for a poll from before this bridge was
// linked is therefore undecryptable - that is expected, not a bug, and we log
// it rather than treating it as an error.
//
// The decrypted vote gives sha256(optionName) digests only; resolving those to
// readable options is what the polls table is for.
func handlePollVote(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	if messageStore == nil {
		return
	}
	pu := msg.Message.GetPollUpdateMessage()
	key := pu.GetPollCreationMessageKey()
	pollID := key.GetID()
	if pollID == "" {
		logger.Warnf("poll vote %s has no poll creation key; dropping", msg.Info.ID)
		return
	}
	pollChat := key.GetRemoteJID()
	if pollChat == "" {
		// 1:1 polls often omit remoteJID; the vote arrives in the poll's chat.
		pollChat = msg.Info.Chat.String()
	}
	// Normalise @lid -> phone-number JID, exactly as handleMessage does for
	// chat_jid. Votes cast from the user's own phone arrive LID-addressed while
	// the poll row was written under the PN form, so skipping this silently
	// breaks the join in /api/poll/results: the vote is stored but never
	// counted. Found 2026-07-16 when a phone vote vanished from the tally.
	pollChat = resolveToPNStr(client, pollChat)

	vote, err := client.DecryptPollVote(context.Background(), msg)
	if err != nil {
		logger.Warnf("Failed to decrypt poll vote %s for poll %s: %v", msg.Info.ID, pollID, err)
		return
	}
	voter := resolveToPN(client, msg.Info.Sender).ToNonAD().String()
	if err := messageStore.StorePollVote(pollID, pollChat, voter, vote.GetSelectedOptions(), msg.Info.Timestamp); err != nil {
		logger.Warnf("Failed to store poll vote %s: %v", msg.Info.ID, err)
		return
	}
	logger.Debugf("Stored poll vote from %s on poll %s (%d option(s))",
		voter, pollID, len(vote.GetSelectedOptions()))
}

func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Save message to database. #244: normalize LID -> PN at write time
	// so a contact that arrives under both addressing schemes doesn't
	// produce two separate chat rows.
	chatJID := resolveToPN(client, msg.Info.Chat).String()
	senderJID := resolveToPN(client, msg.Info.Sender)
	sender := senderJID.User

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Batch nu: intercept ReactionMessage before content extraction. A
	// reaction has no user-facing text so we don't want it landing in
	// messages.content; instead persist to the reactions table so read
	// tools can answer "who reacted to X".
	if msg.Message != nil {
		if rx := msg.Message.GetReactionMessage(); rx != nil {
			target := ""
			targetFromMe := false
			if key := rx.GetKey(); key != nil {
				target = key.GetID()
				targetFromMe = key.GetFromMe()
			}
			emoji := rx.GetText()
			if target != "" {
				if _, err := messageStore.db.Exec(
					`INSERT OR REPLACE INTO reactions
					 (id, chat_jid, target_message_id, target_from_me, sender, emoji, timestamp)
					 VALUES (?, ?, ?, ?, ?, ?, ?)`,
					msg.Info.ID, chatJID, target, targetFromMe, sender, emoji, msg.Info.Timestamp,
				); err != nil {
					logger.Warnf("reactions insert failed: %v", err)
				}
			}
			// Skip normal storage; a reaction row already lives in `reactions`.
			return
		}
	}

	// Poll votes arrive as PollUpdateMessage: no text, no media, so they would
	// hit the empty-message guard below and vanish. Handle them first and
	// return - a vote is not a message row, it belongs in poll_votes.
	if msg.Message.GetPollUpdateMessage() != nil {
		handlePollVote(client, messageStore, msg, logger)
		return
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		return
	}

	// Batch α N2: marshal the raw *waProto.Message for downstream reuse
	// (vote_in_poll, forward_message with media, comment encrypt). Failure to
	// marshal is non-fatal - we store the message row anyway.
	var rawProto []byte
	if msg.Message != nil {
		if b, mErr := proto.Marshal(msg.Message); mErr == nil {
			rawProto = b
		} else {
			logger.Warnf("proto.Marshal failed for %s: %v", msg.Info.ID, mErr)
		}
	}

	// Batch xi: extract reply-context if the message quotes another.
	var replyID, replySender string
	if msg.Message != nil {
		if ext := msg.Message.GetExtendedTextMessage(); ext != nil {
			if ci := ext.GetContextInfo(); ci != nil {
				replyID = ci.GetStanzaID()
				replySender = ci.GetParticipant()
			}
		}
		if replyID == "" {
			if im := msg.Message.GetImageMessage(); im != nil && im.GetContextInfo() != nil {
				replyID = im.GetContextInfo().GetStanzaID()
				replySender = im.GetContextInfo().GetParticipant()
			}
		}
		if replyID == "" {
			if vm := msg.Message.GetVideoMessage(); vm != nil && vm.GetContextInfo() != nil {
				replyID = vm.GetContextInfo().GetStanzaID()
				replySender = vm.GetContextInfo().GetParticipant()
			}
		}
		if replyID == "" {
			if dm := msg.Message.GetDocumentMessage(); dm != nil && dm.GetContextInfo() != nil {
				replyID = dm.GetContextInfo().GetStanzaID()
				replySender = dm.GetContextInfo().GetParticipant()
			}
		}
	}

	// Batch xi: group participants persistence. On any inbound group
	// message, upsert the (group, sender) row. Idempotent + cheap.
	if senderJID.Server != "" && strings.HasSuffix(chatJID, "@g.us") && sender != "" {
		participantJID := sender
		if !strings.Contains(participantJID, "@") {
			participantJID += "@s.whatsapp.net"
		}
		if _, err := messageStore.db.Exec(
			`INSERT INTO group_participants (group_jid, jid, first_seen_at, last_seen_at)
			 VALUES (?, ?, ?, ?)
			 ON CONFLICT(group_jid, jid) DO UPDATE SET last_seen_at = excluded.last_seen_at`,
			chatJID, participantJID, msg.Info.Timestamp, msg.Info.Timestamp,
		); err != nil {
			logger.Warnf("group_participants upsert failed: %v", err)
		}
	}

	// Store message in database
	err = messageStore.StoreMessageWithReply(
		msg.Info.ID,
		chatJID,
		sender,
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
		rawProto,
		replyID,
		replySender,
		// whatsmeow already unwrapped the envelope before handing us
		// msg.Message, and records what it unwrapped here. IsViewOnce covers
		// V1, V2 and V2Extension.
		msg.IsViewOnce,
	)

	// Record the poll's options alongside the message row. Must happen for
	// incoming polls too, not just ours: a vote can only be resolved to a
	// readable option if we hold the option->hash mapping for that poll.
	if pc := pollCreation(msg.Message); pc != nil {
		opts := make([]string, 0, len(pc.GetOptions()))
		for _, o := range pc.GetOptions() {
			opts = append(opts, o.GetOptionName())
		}
		if len(opts) > 0 {
			if perr := messageStore.StorePoll(msg.Info.ID, chatJID, pc.GetName(), opts,
				int(pc.GetSelectableOptionsCount()), msg.Info.Timestamp); perr != nil {
				logger.Warnf("Failed to store poll %s: %v", msg.Info.ID, perr)
			}
		}
	}

	// N3: fan-out event even if the DB write failed (webhooks want to know)
	select {
	case eventBus <- bridgeEvent{
		Type:      "message",
		ChatJID:   chatJID,
		MessageID: msg.Info.ID,
		Sender:    sender,
		IsFromMe:  msg.Info.IsFromMe,
		Content:   content,
		MediaType: mediaType,
		Timestamp: msg.Info.Timestamp,
	}:
	default:
		// Event bus full; drop and log at debug volume.
	}

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else if os.Getenv("BRIDGE_LOG_MESSAGES") == "1" {
		// Per-message print is opt-in via BRIDGE_LOG_MESSAGES=1. Default off
		// because (a) it doubles log volume during burst, (b) it writes message
		// bodies to docker's rotated log files (PII on disk), and (c) the same
		// data is in messages.db where it belongs.
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}
		if mediaType != "" {
			fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
		} else if content != "" {
			fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
		}
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// Media-retry protocol state (adopted from upstream PR #273). When
// the CDN URL is expired, downloadMedia sends a SendMediaRetryReceipt
// asking the sender's device to re-upload, then waits for the
// events.MediaRetry response routed here by MessageID.
var (
	mediaRetryChans = make(map[string]chan *events.MediaRetry)
	mediaRetryMutex sync.Mutex
)

// downloadViaMediaRetry asks the sender's phone to re-upload expired
// media and downloads from the fresh direct path it returns. Returns
// error if the sender's phone is offline (45s timeout), rejects the
// retry, or the message row can't be found.
func downloadViaMediaRetry(
	client *whatsmeow.Client,
	messageStore *MessageStore,
	messageID, chatJID string,
	mediaKey []byte,
	downloader *MediaDownloader,
) ([]byte, error) {
	var sender, ts string
	var isFromMe bool
	if err := messageStore.db.QueryRow(
		"SELECT sender, is_from_me, timestamp FROM messages WHERE id = ? AND chat_jid = ?",
		messageID, chatJID,
	).Scan(&sender, &isFromMe, &ts); err != nil {
		return nil, fmt.Errorf("failed to look up message sender: %v", err)
	}

	chatJIDParsed, err := types.ParseJID(chatJID)
	if err != nil {
		return nil, fmt.Errorf("invalid chat JID: %v", err)
	}
	if !strings.Contains(sender, "@") {
		sender += "@s.whatsapp.net"
	}
	senderJID, err := types.ParseJID(sender)
	if err != nil {
		return nil, fmt.Errorf("invalid sender JID: %v", err)
	}

	timestamp, terr := time.Parse(time.RFC3339, strings.Replace(ts, " ", "T", 1))
	if terr != nil {
		timestamp = time.Now()
	}

	msgInfo := &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     chatJIDParsed,
			Sender:   senderJID,
			IsFromMe: isFromMe,
		},
		ID:        messageID,
		Timestamp: timestamp,
	}

	ch := make(chan *events.MediaRetry, 1)
	mediaRetryMutex.Lock()
	mediaRetryChans[messageID] = ch
	mediaRetryMutex.Unlock()
	defer func() {
		mediaRetryMutex.Lock()
		delete(mediaRetryChans, messageID)
		mediaRetryMutex.Unlock()
	}()

	if err := client.SendMediaRetryReceipt(context.Background(), msgInfo, mediaKey); err != nil {
		return nil, fmt.Errorf("failed to send media retry receipt: %v", err)
	}

	select {
	case evt := <-ch:
		notif, err := whatsmeow.DecryptMediaRetryNotification(evt, mediaKey)
		if err != nil {
			return nil, fmt.Errorf("failed to decrypt media retry notification: %v", err)
		}
		if notif.GetResult() != waMmsRetry.MediaRetryNotification_SUCCESS {
			return nil, fmt.Errorf("media retry rejected by sender's phone: %s", notif.GetResult().String())
		}
		if notif.GetDirectPath() == "" {
			return nil, fmt.Errorf("media retry returned an empty direct path")
		}
		downloader.URL = ""
		downloader.DirectPath = notif.GetDirectPath()
		return client.Download(context.Background(), downloader)
	case <-time.After(45 * time.Second):
		return nil, fmt.Errorf("timed out waiting for media retry (sender's phone must be online)")
	}
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// --- N3: event fan-out (SSE + webhook) -------------------------------------
// Central event bus. Every fanned-out event is pushed onto eventBus. The SSE
// handler creates a per-connection subscriber; a single webhook goroutine
// (if WHATSAPP_WEBHOOK_URL is set) POSTs each event with an HMAC signature.
//
// This is deliberately kept as an in-process buffered channel so a slow SSE
// client can't stall event delivery to other subscribers.
type bridgeEvent struct {
	Type      string    `json:"type"`
	ChatJID   string    `json:"chat_jid,omitempty"`
	MessageID string    `json:"message_id,omitempty"`
	Sender    string    `json:"sender,omitempty"`
	IsFromMe  bool      `json:"is_from_me,omitempty"`
	Content   string    `json:"content,omitempty"`
	MediaType string    `json:"media_type,omitempty"`
	Timestamp time.Time `json:"timestamp"`
}

var (
	eventBusMu   sync.Mutex
	eventSubs    []chan bridgeEvent
	eventBus     = make(chan bridgeEvent, 256)
	eventBusOnce sync.Once
)

func startEventFanout() {
	eventBusOnce.Do(func() {
		go func() {
			for ev := range eventBus {
				eventBusMu.Lock()
				subs := append([]chan bridgeEvent(nil), eventSubs...)
				eventBusMu.Unlock()
				for _, sub := range subs {
					// Drop for slow subscribers instead of blocking.
					select {
					case sub <- ev:
					default:
					}
				}
				// Webhook fan-out (best effort).
				if url := os.Getenv("WHATSAPP_WEBHOOK_URL"); url != "" {
					go postWebhook(url, ev)
				}
			}
		}()
	})
}

func subscribeEvents() (chan bridgeEvent, func()) {
	ch := make(chan bridgeEvent, 64)
	eventBusMu.Lock()
	eventSubs = append(eventSubs, ch)
	eventBusMu.Unlock()
	unsub := func() {
		eventBusMu.Lock()
		for i, s := range eventSubs {
			if s == ch {
				eventSubs = append(eventSubs[:i], eventSubs[i+1:]...)
				break
			}
		}
		eventBusMu.Unlock()
		close(ch)
	}
	return ch, unsub
}

func postWebhook(url string, ev bridgeEvent) {
	body, err := json.Marshal(ev)
	if err != nil {
		return
	}
	req, err := http.NewRequest("POST", url, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if secret := os.Getenv("WHATSAPP_WEBHOOK_SECRET"); secret != "" {
		mac := hmac.New(sha256.New, []byte(secret))
		mac.Write(body)
		req.Header.Set("X-Wamcp-Signature", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	}
	req.Header.Set("X-Wamcp-Event-Type", ev.Type)
	client := &http.Client{Timeout: 10 * time.Second}
	if resp, err := client.Do(req); err == nil {
		resp.Body.Close()
	}
}

// processStart is stamped at package init so /api/bridge/stats can report
// uptime without depending on any external state.
var processStart = time.Now()

// --- G7: Prometheus metrics ------------------------------------------------
// Loopback-only /metrics endpoint. Counters for events received + sends, a
// histogram for send latency, and a connected gauge so a scrape can detect
// "StreamReplaced" windows without parsing logs.
var (
	metricEventsReceived = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "whatsapp_bridge_events_received_total",
		Help: "Count of whatsmeow events received, by event type.",
	}, []string{"type"})
	metricConnected = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "whatsapp_bridge_connected",
		Help: "1 when whatsmeow Client.IsConnected, else 0.",
	})
)

// --- LID -> PN normalization (adopted from upstream PR #244) ----------------
// WhatsApp has rolled out a new addressing scheme for users: LID
// ("<random>@lid") alongside the historical phone-number JID PN
// ("<number>@s.whatsapp.net"). The same contact can arrive under either
// form. Storing both splits one conversation into two separate chats.
//
// resolveToPN looks up the PN for any LID via the whatsmeow LID store.
// It's called at every write site (handleMessage, handleHistorySync) so
// no LID row is ever inserted when a PN mapping is already known.
//
// migrateLIDChats is a one-shot startup pass that merges any @lid chats
// (from earlier runs before this normalization existed) into their PN
// equivalents, transactionally.
func resolveToPN(client *whatsmeow.Client, jid types.JID) types.JID {
	if jid.Server != "lid" || client == nil || client.Store == nil || client.Store.LIDs == nil {
		return jid
	}
	pn, err := client.Store.LIDs.GetPNForLID(context.Background(), jid)
	if err == nil && !pn.IsEmpty() {
		return pn
	}
	return jid
}

// resolveToPNStr is a convenience for stringified JIDs (message rows use
// strings). Returns the input unchanged if not @lid or if no mapping exists.
func resolveToPNStr(client *whatsmeow.Client, jidStr string) string {
	if !strings.HasSuffix(jidStr, "@lid") {
		return jidStr
	}
	jid, err := types.ParseJID(jidStr)
	if err != nil {
		return jidStr
	}
	return resolveToPN(client, jid).String()
}

func migrateLIDChats(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	if client == nil || client.Store == nil || client.Store.LIDs == nil {
		return
	}
	rows, err := store.db.Query(
		`SELECT jid, name, last_message_time FROM chats WHERE jid LIKE '%@lid'`,
	)
	if err != nil {
		logger.Warnf("migrateLIDChats: SELECT failed: %v", err)
		return
	}
	type lidRow struct {
		jid, name string
		lastTS    time.Time
	}
	var pending []lidRow
	for rows.Next() {
		var row lidRow
		if err := rows.Scan(&row.jid, &row.name, &row.lastTS); err == nil {
			pending = append(pending, row)
		}
	}
	rows.Close()
	if len(pending) == 0 {
		return
	}
	tx, err := store.db.Begin()
	if err != nil {
		logger.Warnf("migrateLIDChats: Begin failed: %v", err)
		return
	}
	var merged, skipped int
	for _, row := range pending {
		pnStr := resolveToPNStr(client, row.jid)
		if pnStr == row.jid {
			skipped++
			continue
		}
		// Upsert PN row keeping the more recent timestamp.
		if _, err := tx.Exec(
			`INSERT INTO chats (jid, name, last_message_time)
			 VALUES (?, ?, ?)
			 ON CONFLICT(jid) DO UPDATE SET
			   name = COALESCE(NULLIF(chats.name, ''), excluded.name),
			   last_message_time = MAX(chats.last_message_time, excluded.last_message_time)`,
			pnStr, row.name, row.lastTS,
		); err != nil {
			logger.Warnf("migrateLIDChats: upsert PN %s failed: %v", pnStr, err)
			continue
		}
		// Move messages. UPDATE OR IGNORE drops rows that would collide on
		// (id, chat_jid) primary key with the PN chat.
		if _, err := tx.Exec(
			`UPDATE OR IGNORE messages SET chat_jid = ? WHERE chat_jid = ?`,
			pnStr, row.jid,
		); err != nil {
			logger.Warnf("migrateLIDChats: UPDATE messages failed: %v", err)
			continue
		}
		if _, err := tx.Exec(`DELETE FROM messages WHERE chat_jid = ?`, row.jid); err != nil {
			logger.Warnf("migrateLIDChats: DELETE messages failed: %v", err)
			continue
		}
		if _, err := tx.Exec(`DELETE FROM chats WHERE jid = ?`, row.jid); err != nil {
			logger.Warnf("migrateLIDChats: DELETE chat failed: %v", err)
			continue
		}
		merged++
	}
	if err := tx.Commit(); err != nil {
		logger.Warnf("migrateLIDChats: Commit failed: %v", err)
		return
	}
	logger.Infof("LID->PN migration: merged=%d skipped=%d (skipped rows have no PN mapping yet; will retry on next start)", merged, skipped)
}

// validateJIDForFilesystem rejects chatJID strings that contain anything
// outside the WhatsApp JID alphabet, so they can never escape store/.
// F8: defense-in-depth against path-traversal on the bridge's filesystem.
var jidFsAllowed = regexp.MustCompile(`^[A-Za-z0-9@._\-]+$`)

func validateJIDForFilesystem(jid string) error {
	if jid == "" || len(jid) > 200 {
		return fmt.Errorf("invalid jid: empty or too long")
	}
	if !jidFsAllowed.MatchString(jid) {
		return fmt.Errorf("invalid jid: contains disallowed characters")
	}
	return nil
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// F8: validate chatJID first - it goes into store/<chatJID>/... path.
	if err := validateJIDForFilesystem(chatJID); err != nil {
		return false, "", "", "", err
	}
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := fmt.Sprintf("store/%s", strings.ReplaceAll(chatJID, ":", "_"))
	localPath := ""

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Generate a local path for the file
	// F8: filepath.Base() strips any directory components an attacker might have
	// stuffed into filename (e.g. "../../etc/passwd").
	filename = filepath.Base(filename)
	if filename == "" || filename == "." || filename == "/" {
		filename = "media.bin"
	}
	localPath = fmt.Sprintf("%s/%s", chatDir, filename)

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		// File exists, return it
		return true, mediaType, filename, absPath, nil
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client. When the CDN URL has
	// expired (history-synced messages, forwards, or anything past the
	// signature lifetime), fall through to the media-retry protocol
	// (adopted from upstream PR #273 - closes issue #222).
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		fmt.Printf("Direct download failed (%v), requesting media retry from sender's phone...\n", err)
		mediaData, err = downloadViaMediaRetry(client, messageStore, messageID, chatJID, mediaKey, downloader)
		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
		}
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Remove query parameters
	pathPart = strings.SplitN(pathPart, "?", 2)[0]

	// Create proper direct path format
	return "/" + pathPart
}

// Safety limits for the REST surface.
//
//	maxMediaBytes  cap on media files sent via /api/send and written by
//	               /api/download. The bridge buffers media in RAM; a 200MB
//	               upload would spike RSS from ~25MB to ~250MB and risk
//	               OOMKill on a 512MB cgroup with 8 other containers
//	               sharing 3.8GB of RAM.
//	maxJSONBytes   small cap on inbound JSON bodies so a malformed/giant
//	               request can't allocate hundreds of MB.
const (
	maxMediaBytes = 64 << 20 // 64 MiB
	maxJSONBytes  = 1 << 20  // 1 MiB
)

// sendLimiter is a tiny token-bucket rate limiter on /api/send. The MCP server
// is single-user so 5/sec with a burst of 10 is generous. WhatsApp's anti-spam
// will TemporaryBan a device that fires hundreds of messages in a few seconds,
// so the limit also protects the paired session.
//
// Implementation: a buffered channel of "tokens" filled by a goroutine on a
// tick. Avoid the golang.org/x/time/rate dependency since we only need one
// shape of limiter and adding a new module bumps the build's network blast
// radius.
type tokenBucket struct {
	tokens chan struct{}
}

func newTokenBucket(rate int, burst int) *tokenBucket {
	tb := &tokenBucket{tokens: make(chan struct{}, burst)}
	// Pre-fill so the burst is available immediately.
	for i := 0; i < burst; i++ {
		tb.tokens <- struct{}{}
	}
	go func() {
		ticker := time.NewTicker(time.Second / time.Duration(rate))
		defer ticker.Stop()
		for range ticker.C {
			select {
			case tb.tokens <- struct{}{}:
			default: // bucket full, drop
			}
		}
	}()
	return tb
}

func (tb *tokenBucket) allow() bool {
	select {
	case <-tb.tokens:
		return true
	default:
		return false
	}
}

// Start a REST API server to expose the WhatsApp client functionality
func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, port int) {
	mux := http.NewServeMux()
	sendBucket := newTokenBucket(5, 10)

	// Handler for sending messages
	mux.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		// Fail fast when WA is disconnected instead of waiting 30s for
		// whatsmeow's internal SendMessage timeout. The caller can retry
		// after the bridge reconnects (auto, no manual step).
		if !client.IsConnected() {
			http.Error(w, "WhatsApp bridge not connected to WA; retry after auto-reconnect", http.StatusServiceUnavailable)
			return
		}
		if !sendBucket.allow() {
			http.Error(w, "rate limited - too many sends", http.StatusTooManyRequests)
			return
		}

		// Bound the JSON parser so a malformed body can't allocate huge memory.
		r.Body = http.MaxBytesReader(w, r.Body, maxJSONBytes)
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}
		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		// 64MB media cap: stat early so we reject before reading. Pure-text
		// sends (no MediaPath) bypass this check.
		if req.MediaPath != "" {
			fi, statErr := os.Stat(req.MediaPath)
			if statErr != nil {
				http.Error(w, fmt.Sprintf("media file not accessible: %v", statErr), http.StatusBadRequest)
				return
			}
			if fi.Size() > maxMediaBytes {
				http.Error(w, fmt.Sprintf("media exceeds %d-byte cap", maxMediaBytes), http.StatusRequestEntityTooLarge)
				return
			}
		}

		// Tight 15s deadline by default - whatsmeow blocks on the WA server
		// ACK; a real recipient online ACKs in <2s. Anything past 15s usually
		// means the recipient is unreachable, in which case failing fast is
		// better UX than hanging the caller. Optional override via the
		// delivery_timeout_seconds query param (uploads / large media may
		// need longer).
		timeoutS := 15
		if v := r.URL.Query().Get("delivery_timeout_seconds"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 300 {
				timeoutS = n
			}
		}
		sendCtx, sendCancel := context.WithTimeout(r.Context(), time.Duration(timeoutS)*time.Second)
		defer sendCancel()
		success, message := sendWhatsAppMessageEx(
			sendCtx, client, messageStore, req.Recipient, req.Message, req.MediaPath,
			req.ReplyToMessageID, req.ReplyToSenderJID, req.MentionedJIDs,
			req.ViewOnce,
		)

		w.Header().Set("Content-Type", "application/json")
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}
		_ = json.NewEncoder(w).Encode(SendMessageResponse{
			Success: success,
			Message: message,
		})
	})

	// Handler for downloading media
	mux.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		r.Body = http.MaxBytesReader(w, r.Body, maxJSONBytes)
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)

		w.Header().Set("Content-Type", "application/json")
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}
		_ = json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	})

	// --- Tier-1 feature endpoints (Batch E) ---------------------------------
	// All of these are thin wrappers around whatsmeow Client methods that
	// mautrix-whatsapp / beeper have been shipping for years; the bridge just
	// exposes them as flat REST so the MCP layer can register them as tools.
	// The same auth model as /api/send applies: bridge is private to the
	// docker network, never published to a host port.

	// parseRecipient accepts either a JID (contains "@") or a bare phone
	// number and returns a types.JID. Mirrors the logic in sendWhatsAppMessageEx
	// so the new feature endpoints don't have to duplicate it.
	parseRecipient := func(s string) (types.JID, error) {
		if strings.Contains(s, "@") {
			return types.ParseJID(s)
		}
		return types.JID{User: s, Server: "s.whatsapp.net"}, nil
	}
	_ = parseRecipient // used by /api/send_location etc.

	writeJSON := func(w http.ResponseWriter, status int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(v)
	}
	parsePOSTJSON := func(w http.ResponseWriter, r *http.Request, dst any) bool {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return false
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxJSONBytes)
		if err := json.NewDecoder(r.Body).Decode(dst); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return false
		}
		return true
	}

	// POST /api/send_bytes - multipart/form-data send. Lets a caller upload media
	// bytes directly instead of pointing at a path on the bridge filesystem
	// (the existing /api/send is useless when the caller's file lives on a
	// different machine than the bridge container).
	//
	// Form fields:
	//   recipient                (required) phone number or JID
	//   media                    (required) the file bytes
	//   filename                 (optional) overrides multipart filename; the
	//                             extension drives image/video/audio/document
	//                             detection in sendWhatsAppMessageEx.
	//   message                  (optional) caption text
	//   view_once                (optional) "true" to wrap in ViewOnceMessageV2
	//   reply_to_message_id      (optional)
	//   reply_to_sender_jid      (optional)
	//   mentioned_jids           (optional) comma-separated JIDs
	mux.HandleFunc("/api/send_bytes", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !client.IsConnected() {
			http.Error(w, "WhatsApp bridge not connected to WA; retry after auto-reconnect", http.StatusServiceUnavailable)
			return
		}
		if !sendBucket.allow() {
			http.Error(w, "rate limited - too many sends", http.StatusTooManyRequests)
			return
		}
		// 64MB media + 1MB form overhead. ParseMultipartForm keeps up to 8MB
		// in memory and spills the rest to disk under /tmp.
		r.Body = http.MaxBytesReader(w, r.Body, maxMediaBytes+(1<<20))
		if err := r.ParseMultipartForm(8 << 20); err != nil {
			http.Error(w, fmt.Sprintf("multipart parse failed: %v", err), http.StatusBadRequest)
			return
		}

		recipient := r.FormValue("recipient")
		if recipient == "" {
			http.Error(w, "recipient required", http.StatusBadRequest)
			return
		}
		msgText := r.FormValue("message")
		filename := r.FormValue("filename")
		viewOnce := r.FormValue("view_once") == "true" || r.FormValue("view_once") == "1"
		replyMsg := r.FormValue("reply_to_message_id")
		replySender := r.FormValue("reply_to_sender_jid")
		var mentions []string
		if m := r.FormValue("mentioned_jids"); m != "" {
			for _, s := range strings.Split(m, ",") {
				if s = strings.TrimSpace(s); s != "" {
					mentions = append(mentions, s)
				}
			}
		}

		file, header, err := r.FormFile("media")
		if err != nil {
			http.Error(w, "media file part required", http.StatusBadRequest)
			return
		}
		defer file.Close()
		if filename == "" {
			filename = header.Filename
		}
		if filename == "" {
			filename = "upload.bin"
		}

		// Materialize to a temp file with the right extension so the existing
		// extension-based mime detection in sendWhatsAppMessageEx picks the
		// right whatsmeow.MediaType (image/video/audio/document).
		tmpDir := os.TempDir()
		tmpPath := filepath.Join(tmpDir,
			fmt.Sprintf("wamcp-upload-%d-%s", time.Now().UnixNano(), filename))
		out, ferr := os.Create(tmpPath)
		if ferr != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{
				Success: false, Message: ferr.Error(),
			})
			return
		}
		// Guard cleanup of the tempfile regardless of how we exit.
		defer func() {
			out.Close()
			os.Remove(tmpPath)
		}()
		if _, err := io.Copy(out, file); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{
				Success: false, Message: err.Error(),
			})
			return
		}
		out.Close() // flush before whatsmeow reads it

		// /api/send_bytes also accepts delivery_timeout_seconds. Default
		// 60s for media uploads because Upload to the WA CDN + SendMessage
		// can legitimately take 5-30s for a 5MB+ file.
		timeoutS := 60
		if v := r.URL.Query().Get("delivery_timeout_seconds"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 300 {
				timeoutS = n
			}
		}
		sendCtx, cancel := context.WithTimeout(r.Context(), time.Duration(timeoutS)*time.Second)
		defer cancel()

		success, message := sendWhatsAppMessageEx(
			sendCtx, client, messageStore, recipient, msgText, tmpPath,
			replyMsg, replySender, mentions, viewOnce,
		)

		w.Header().Set("Content-Type", "application/json")
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}
		_ = json.NewEncoder(w).Encode(SendMessageResponse{Success: success, Message: message})
	})

	// POST /api/download_bytes - returns the media bytes inline (capped at 20MB)
	// so the MCP server can re-emit them as FastMCP Image/Audio/File content. The
	// older /api/download writes to disk and returns just a path, which is useless
	// to a client that can't see the bridge container's filesystem.
	//
	// Headers on success:
	//   Content-Type        : detected MIME (image/jpeg, audio/ogg, video/mp4, ...)
	//   Content-Length      : file size
	//   Content-Disposition : attachment; filename="..."
	//   X-Media-Type        : "image" | "audio" | "video" | "document"
	//   X-Filename          : same as in Disposition (easier parse)
	//
	// Files > maxInlineBytes (20 MB) are refused with 413; the caller can still
	// /api/download to disk and `docker cp` it out.
	const maxInlineBytes = 20 << 20
	mux.HandleFunc("/api/download_bytes", func(w http.ResponseWriter, r *http.Request) {
		var req DownloadMediaRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "message_id and chat_jid required", http.StatusBadRequest)
			return
		}
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)
		if !success || err != nil {
			msg := "unknown error"
			if err != nil {
				msg = err.Error()
			}
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: msg})
			return
		}
		f, ferr := os.Open(path)
		if ferr != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: ferr.Error()})
			return
		}
		defer f.Close()
		info, _ := f.Stat()
		if info.Size() > maxInlineBytes {
			http.Error(w, fmt.Sprintf("media is %d bytes (>%d cap); use /api/download + docker cp",
				info.Size(), maxInlineBytes), http.StatusRequestEntityTooLarge)
			return
		}
		mime := mimeForFilename(filename, mediaType)
		w.Header().Set("Content-Type", mime)
		w.Header().Set("Content-Length", fmt.Sprintf("%d", info.Size()))
		w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", filename))
		w.Header().Set("X-Media-Type", mediaType)
		w.Header().Set("X-Filename", filename)
		_, _ = io.Copy(w, f)
	})

	// N3: start the event fan-out goroutine once the mux is being wired.
	startEventFanout()

	// GET /api/events/stream - SSE stream of bridge events.
	mux.HandleFunc("/api/events/stream", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		flusher, ok := w.(http.Flusher)
		if !ok {
			http.Error(w, "streaming unsupported", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("X-Accel-Buffering", "no")

		sub, unsub := subscribeEvents()
		defer unsub()

		ping := time.NewTicker(15 * time.Second)
		defer ping.Stop()
		notify := r.Context().Done()
		for {
			select {
			case <-notify:
				return
			case <-ping.C:
				fmt.Fprintf(w, ": ping - %s\n\n", time.Now().Format(time.RFC3339))
				flusher.Flush()
			case ev, ok := <-sub:
				if !ok {
					return
				}
				payload, err := json.Marshal(ev)
				if err != nil {
					continue
				}
				fmt.Fprintf(w, "event: %s\ndata: %s\n\n", ev.Type, payload)
				flusher.Flush()
			}
		}
	})

	// G7: Prometheus /metrics. Loopback-only via docker network (the whole
	// bridge API is already private). Spawn a 60s ticker that updates the
	// connected gauge so a scrape can spot disconnect windows.
	mux.Handle("/metrics", promhttp.Handler())
	go func() {
		t := time.NewTicker(time.Minute)
		defer t.Stop()
		for range t.C {
			if client != nil && client.IsConnected() {
				metricConnected.Set(1)
			} else {
				metricConnected.Set(0)
			}
		}
	}()

	// GET /api/health - liveness + connection state. Cheap; used by the MCP
	// healthcheck wrapper. No auth (consistent with the rest of the bridge).
	mux.HandleFunc("/api/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		resp := HealthResponse{
			Connected: client.IsConnected(),
			LoggedIn:  client.IsLoggedIn(),
		}
		if client.Store != nil && client.Store.ID != nil {
			resp.PushName = client.Store.PushName
		}
		writeJSON(w, http.StatusOK, resp)
	})

	// requireLoggedIn returns true and writes 503 if the client isn't connected.
	// F9: stops the nil-deref panic that used to fire on /api/react /delete /edit
	// during the brief window between bridge restart and WhatsApp reconnect.
	requireLoggedIn := func(w http.ResponseWriter) bool {
		if client.Store == nil || client.Store.ID == nil {
			http.Error(w, "not logged in to WhatsApp", http.StatusServiceUnavailable)
			return false
		}
		return true
	}

	// POST /api/react - add or clear a reaction on a message.
	mux.HandleFunc("/api/react", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req ReactRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "chat_jid and message_id required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		// Sender defaults to self if not provided (i.e. reacting to your own message).
		sender := *client.Store.ID
		if req.SenderJID != "" {
			sender, err = types.ParseJID(req.SenderJID)
			if err != nil {
				http.Error(w, fmt.Sprintf("bad sender_jid: %v", err), http.StatusBadRequest)
				return
			}
		}
		reaction := client.BuildReaction(chat, sender, req.MessageID, req.Emoji)
		if _, err := client.SendMessage(r.Context(), chat, reaction); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Reaction sent"})
	})

	// POST /api/edit - edit your own text message (24h window per WhatsApp).
	mux.HandleFunc("/api/edit", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req EditRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" || req.MessageID == "" || req.NewText == "" {
			http.Error(w, "chat_jid, message_id, new_text required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		newMsg := &waProto.Message{Conversation: proto.String(req.NewText)}
		edit := client.BuildEdit(chat, req.MessageID, newMsg)
		if _, err := client.SendMessage(r.Context(), chat, edit); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Message edited"})
	})

	// POST /api/delete - revoke (delete-for-everyone) a message you sent.
	mux.HandleFunc("/api/delete", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req DeleteRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "chat_jid and message_id required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		sender := *client.Store.ID
		if req.SenderJID != "" {
			sender, err = types.ParseJID(req.SenderJID)
			if err != nil {
				http.Error(w, fmt.Sprintf("bad sender_jid: %v", err), http.StatusBadRequest)
				return
			}
		}
		revoke := client.BuildRevoke(chat, sender, req.MessageID)
		if _, err := client.SendMessage(r.Context(), chat, revoke); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Message deleted for everyone"})
	})

	// POST /api/mark_read - send read receipts for one or more message IDs.
	mux.HandleFunc("/api/mark_read", func(w http.ResponseWriter, r *http.Request) {
		var req MarkReadRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" || len(req.MessageIDs) == 0 {
			http.Error(w, "chat_jid and message_ids required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		// For direct chats the sender is the same as the chat JID; for groups
		// it's the actual participant who sent the message - caller passes that.
		sender := chat
		if req.SenderJID != "" {
			sender, err = types.ParseJID(req.SenderJID)
			if err != nil {
				http.Error(w, fmt.Sprintf("bad sender_jid: %v", err), http.StatusBadRequest)
				return
			}
		}
		// Convert []string to []types.MessageID (alias-or-defined-string in whatsmeow).
		mids := make([]types.MessageID, len(req.MessageIDs))
		for i, s := range req.MessageIDs {
			mids[i] = types.MessageID(s)
		}
		if err := client.MarkRead(r.Context(), mids, time.Now(), chat, sender); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("Marked %d message(s) read", len(req.MessageIDs))})
	})

	// POST /api/presence - typing/recording indicator in a chat.
	mux.HandleFunc("/api/presence", func(w http.ResponseWriter, r *http.Request) {
		var req PresenceRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" || req.State == "" {
			http.Error(w, "chat_jid and state required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		var state types.ChatPresence
		switch req.State {
		case "composing", "typing":
			state = types.ChatPresenceComposing
		case "paused":
			state = types.ChatPresencePaused
		default:
			http.Error(w, "state must be composing|paused", http.StatusBadRequest)
			return
		}
		var media types.ChatPresenceMedia
		if req.Media == "audio" || req.Media == "recording" {
			media = types.ChatPresenceMediaAudio
		}
		if err := client.SendChatPresence(r.Context(), chat, state, media); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Presence sent"})
	})

	// --- Batch I: group ops + blocklist + newsletters --------------------

	parseJIDList := func(in []string) ([]types.JID, error) {
		out := make([]types.JID, 0, len(in))
		for _, s := range in {
			if strings.Contains(s, "@") {
				j, err := types.ParseJID(s)
				if err != nil {
					return nil, fmt.Errorf("bad jid %q: %w", s, err)
				}
				out = append(out, j)
			} else {
				out = append(out, types.JID{User: s, Server: "s.whatsapp.net"})
			}
		}
		return out, nil
	}

	// POST /api/group/create - create a new group with the caller as admin.
	mux.HandleFunc("/api/group/create", func(w http.ResponseWriter, r *http.Request) {
		var req CreateGroupRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.Subject == "" || len(req.Participants) == 0 {
			http.Error(w, "subject and at least 1 participant required", http.StatusBadRequest)
			return
		}
		jids, err := parseJIDList(req.Participants)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		info, err := client.CreateGroup(r.Context(), whatsmeow.ReqCreateGroup{
			Name:         req.Subject,
			Participants: jids,
		})
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":   true,
			"group_jid": info.JID.String(),
			"name":      info.Name,
		})
	})

	// POST /api/group/participants - add/remove/promote/demote members.
	mux.HandleFunc("/api/group/participants", func(w http.ResponseWriter, r *http.Request) {
		var req GroupParticipantsRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.GroupJID == "" || req.Action == "" || len(req.Participants) == 0 {
			http.Error(w, "group_jid, action, and participants required", http.StatusBadRequest)
			return
		}
		group, err := types.ParseJID(req.GroupJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad group_jid: %v", err), http.StatusBadRequest)
			return
		}
		var action whatsmeow.ParticipantChange
		switch req.Action {
		case "add":
			action = whatsmeow.ParticipantChangeAdd
		case "remove":
			action = whatsmeow.ParticipantChangeRemove
		case "promote":
			action = whatsmeow.ParticipantChangePromote
		case "demote":
			action = whatsmeow.ParticipantChangeDemote
		default:
			http.Error(w, "action must be add|remove|promote|demote", http.StatusBadRequest)
			return
		}
		jids, err := parseJIDList(req.Participants)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		_, err = client.UpdateGroupParticipants(r.Context(), group, jids, action)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("%s applied to %d participant(s)", req.Action, len(jids))})
	})

	// POST /api/group/info - fetch metadata for a group.
	mux.HandleFunc("/api/group/info", func(w http.ResponseWriter, r *http.Request) {
		var req GroupInfoRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.GroupJID == "" {
			http.Error(w, "group_jid required", http.StatusBadRequest)
			return
		}
		group, err := types.ParseJID(req.GroupJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad group_jid: %v", err), http.StatusBadRequest)
			return
		}
		info, err := client.GetGroupInfo(r.Context(), group)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		participants := make([]map[string]any, 0, len(info.Participants))
		for _, p := range info.Participants {
			participants = append(participants, map[string]any{
				"jid":            p.JID.String(),
				"is_admin":       p.IsAdmin,
				"is_super_admin": p.IsSuperAdmin,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":      true,
			"jid":          info.JID.String(),
			"name":         info.Name,
			"topic":        info.Topic,
			"created":      info.GroupCreated,
			"is_announce":  info.IsAnnounce,
			"is_locked":    info.IsLocked,
			"participants": participants,
		})
	})

	// GET /api/group/joined - list groups the bridge is a member of.
	mux.HandleFunc("/api/group/joined", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		groups, err := client.GetJoinedGroups(r.Context())
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]map[string]any, 0, len(groups))
		for _, g := range groups {
			out = append(out, map[string]any{
				"jid":     g.JID.String(),
				"name":    g.Name,
				"topic":   g.Topic,
				"created": g.GroupCreated,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "groups": out})
	})

	// GET /api/blocklist - list blocked contact JIDs.
	mux.HandleFunc("/api/blocklist", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		bl, err := client.GetBlocklist(r.Context())
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		jids := make([]string, 0, len(bl.JIDs))
		for _, j := range bl.JIDs {
			jids = append(jids, j.String())
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "blocked_jids": jids})
	})

	// POST /api/block - block a contact.
	mux.HandleFunc("/api/block", func(w http.ResponseWriter, r *http.Request) {
		var req BlockRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.JID == "" {
			http.Error(w, "jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad jid: %v", err), http.StatusBadRequest)
			return
		}
		if _, err := client.UpdateBlocklist(r.Context(), jid, events.BlocklistChangeActionBlock); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Blocked"})
	})

	// POST /api/unblock - unblock a contact.
	mux.HandleFunc("/api/unblock", func(w http.ResponseWriter, r *http.Request) {
		var req BlockRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.JID == "" {
			http.Error(w, "jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad jid: %v", err), http.StatusBadRequest)
			return
		}
		if _, err := client.UpdateBlocklist(r.Context(), jid, events.BlocklistChangeActionUnblock); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Unblocked"})
	})

	// GET /api/newsletters/subscribed - list channels/newsletters I follow.
	mux.HandleFunc("/api/newsletters/subscribed", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		ns, err := client.GetSubscribedNewsletters(r.Context())
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]map[string]any, 0, len(ns))
		for _, n := range ns {
			out = append(out, map[string]any{
				"jid":  n.ID.String(),
				"name": n.ThreadMeta.Name.Text,
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "newsletters": out})
	})

	// POST /api/newsletter/info - metadata for a newsletter/channel.
	mux.HandleFunc("/api/newsletter/info", func(w http.ResponseWriter, r *http.Request) {
		var req NewsletterInfoRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.NewsletterJID == "" {
			http.Error(w, "newsletter_jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.NewsletterJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad newsletter_jid: %v", err), http.StatusBadRequest)
			return
		}
		info, err := client.GetNewsletterInfo(r.Context(), jid)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success": true,
			"jid":     info.ID.String(),
			"name":    info.ThreadMeta.Name.Text,
		})
	})

	// --- G3 + G4 endpoints ----------------------------------------------------

	// POST /api/contacts/check - validate phone numbers and return JIDs.
	mux.HandleFunc("/api/contacts/check", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req CheckPhonesRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if len(req.Phones) == 0 || len(req.Phones) > 100 {
			http.Error(w, "phones must be 1..100 entries", http.StatusBadRequest)
			return
		}
		resps, err := client.IsOnWhatsApp(r.Context(), req.Phones)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]map[string]any, 0, len(resps))
		for _, r := range resps {
			row := map[string]any{
				"phone":       r.Query,
				"jid":         r.JID.String(),
				"on_whatsapp": r.IsIn,
			}
			if r.VerifiedName != nil && r.VerifiedName.Details != nil {
				row["verified_business_name"] = r.VerifiedName.Details.GetVerifiedName()
			}
			out = append(out, row)
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "results": out})
	})

	// POST /api/users/info - bulk UserInfo (about, picture_id, devices, verified-business).
	mux.HandleFunc("/api/users/info", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req UsersInfoRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if len(req.JIDs) == 0 || len(req.JIDs) > 100 {
			http.Error(w, "jids must be 1..100 entries", http.StatusBadRequest)
			return
		}
		jids, err := parseJIDList(req.JIDs)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		info, err := client.GetUserInfo(r.Context(), jids)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		out := make(map[string]any, len(info))
		for jid, ui := range info {
			row := map[string]any{
				"about":         ui.Status,
				"picture_id":    ui.PictureID,
				"devices_count": len(ui.Devices),
			}
			if ui.VerifiedName != nil && ui.VerifiedName.Details != nil {
				row["verified_business_name"] = ui.VerifiedName.Details.GetVerifiedName()
			}
			out[jid.String()] = row
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "users": out})
	})

	// GET /api/business/profile?jid= - business profile metadata.
	mux.HandleFunc("/api/business/profile", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !requireLoggedIn(w) {
			return
		}
		jidStr := r.URL.Query().Get("jid")
		if jidStr == "" {
			http.Error(w, "jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(jidStr)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad jid: %v", err), http.StatusBadRequest)
			return
		}
		bp, err := client.GetBusinessProfile(r.Context(), jid)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if bp == nil {
			writeJSON(w, http.StatusNotFound, GenericResponse{Success: false, Message: "no business profile"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":                 true,
			"jid":                     bp.JID.String(),
			"address":                 bp.Address,
			"email":                   bp.Email,
			"business_hours_timezone": bp.BusinessHoursTimeZone,
			"category":                bp.Categories,
		})
	})

	// POST /api/group/invite_link - get (or reset) the chat.whatsapp.com link.
	mux.HandleFunc("/api/group/invite_link", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req GroupInviteLinkRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.GroupJID == "" {
			http.Error(w, "group_jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.GroupJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad group_jid: %v", err), http.StatusBadRequest)
			return
		}
		link, err := client.GetGroupInviteLink(r.Context(), jid, req.Reset)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success": true,
			"link":    "https://chat.whatsapp.com/" + link,
			"code":    link,
		})
	})

	// POST /api/group/join - preview or join a group by invite link.
	mux.HandleFunc("/api/group/join", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req GroupJoinRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.Link == "" {
			http.Error(w, "link required", http.StatusBadRequest)
			return
		}
		code := req.Link
		if i := strings.Index(code, "chat.whatsapp.com/"); i >= 0 {
			code = code[i+len("chat.whatsapp.com/"):]
		}
		code = strings.TrimSuffix(strings.Split(code, "?")[0], "/")
		if req.PreviewOnly {
			info, err := client.GetGroupInfoFromLink(r.Context(), code)
			if err != nil {
				writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{
				"success":      true,
				"jid":          info.JID.String(),
				"name":         info.Name,
				"topic":        info.Topic,
				"participants": len(info.Participants),
			})
			return
		}
		joinedJID, err := client.JoinGroupWithLink(r.Context(), code)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "group_jid": joinedJID.String()})
	})

	// --- G6 chat state ops ------------------------------------------------

	// POST /api/chat/archive {chat_jid, value} - archive / unarchive.
	// Needs the last-message key; loaded from messages.db.
	mux.HandleFunc("/api/chat/archive", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req ChatStateRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		var lastID string
		var lastFromMe bool
		var lastTS time.Time
		_ = messageStore.db.QueryRow(
			`SELECT id, is_from_me, timestamp FROM messages
			 WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT 1`,
			req.ChatJID,
		).Scan(&lastID, &lastFromMe, &lastTS)
		if lastTS.IsZero() {
			lastTS = time.Now()
		}
		key := &waCommon.MessageKey{
			ID: proto.String(lastID), FromMe: proto.Bool(lastFromMe),
			RemoteJID: proto.String(req.ChatJID),
		}
		patch := appstate.BuildArchive(jid, req.Value, lastTS, key)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Archived"
		if !req.Value {
			msg = "Unarchived"
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: msg})
	})

	// POST /api/chat/mark_unread {chat_jid, value}
	mux.HandleFunc("/api/chat/mark_unread", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req ChatStateRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		var lastID string
		var lastFromMe bool
		var lastTS time.Time
		_ = messageStore.db.QueryRow(
			`SELECT id, is_from_me, timestamp FROM messages
			 WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT 1`,
			req.ChatJID,
		).Scan(&lastID, &lastFromMe, &lastTS)
		if lastTS.IsZero() {
			lastTS = time.Now()
		}
		key := &waCommon.MessageKey{
			ID: proto.String(lastID), FromMe: proto.Bool(lastFromMe),
			RemoteJID: proto.String(req.ChatJID),
		}
		// BuildMarkChatAsRead takes `read` bool - inverse of mark_unread.
		patch := appstate.BuildMarkChatAsRead(jid, !req.Value, lastTS, key)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Marked unread"
		if !req.Value {
			msg = "Marked read"
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: msg})
	})

	// POST /api/status/set_message {message} - set your "About" text.
	mux.HandleFunc("/api/status/set_message", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req PostStatusRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if err := client.SetStatusMessage(r.Context(), req.Message); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "About text updated"})
	})

	// POST /api/chat/mute {chat_jid, value:bool, duration_s:optional}
	mux.HandleFunc("/api/chat/mute", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req ChatStateRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		patch := appstate.BuildMute(jid, req.Value, time.Duration(req.DurationS)*time.Second)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Muted"
		if !req.Value {
			msg = "Unmuted"
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: msg})
	})

	// POST /api/chat/pin {chat_jid, value:bool}
	mux.HandleFunc("/api/chat/pin", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req ChatStateRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		patch := appstate.BuildPin(jid, req.Value)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Pinned"
		if !req.Value {
			msg = "Unpinned"
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: msg})
	})

	// POST /api/message/star {chat_jid, message_id, sender_jid?, is_from_me, starred}
	mux.HandleFunc("/api/message/star", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req StarMessageRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "chat_jid and message_id required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		sender := *client.Store.ID
		if req.SenderJID != "" {
			s, err := types.ParseJID(req.SenderJID)
			if err != nil {
				http.Error(w, fmt.Sprintf("bad sender_jid: %v", err), http.StatusBadRequest)
				return
			}
			sender = s
		}
		patch := appstate.BuildStar(chat, sender, req.MessageID, req.IsFromMe, req.Starred)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Starred"
		if !req.Starred {
			msg = "Unstarred"
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: msg})
	})

	// --- N9 status broadcast ----------------------------------------------

	// POST /api/status/post {message} - posts text to status@broadcast.
	mux.HandleFunc("/api/status/post", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req PostStatusRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.Message == "" {
			http.Error(w, "message required", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()
		msg := &waProto.Message{Conversation: proto.String(req.Message)}
		if _, err := client.SendMessage(ctx, types.StatusBroadcastJID, msg); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Status posted"})
	})

	// GET /api/privacy/settings - fetch WhatsApp privacy settings.
	mux.HandleFunc("/api/privacy/settings", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !requireLoggedIn(w) {
			return
		}
		settings, err := client.TryFetchPrivacySettings(r.Context(), false)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":       true,
			"group_add":     string(settings.GroupAdd),
			"last_seen":     string(settings.LastSeen),
			"status":        string(settings.Status),
			"profile":       string(settings.Profile),
			"read_receipts": string(settings.ReadReceipts),
			"online":        string(settings.Online),
			"call_add":      string(settings.CallAdd),
		})
	})

	// --- N11 forward message (text-only) ----------------------------------

	// POST /api/forward - forward a stored message to another chat. Text
	// messages are reconstructed with ContextInfo{IsForwarded, ForwardingScore};
	// media forwarding uses the raw_proto BLOB (batch alpha N2) when present.
	mux.HandleFunc("/api/forward", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req ForwardMessageRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.SourceChatJID == "" || req.MessageID == "" || req.TargetJID == "" {
			http.Error(w, "source_chat_jid, message_id, target_jid required", http.StatusBadRequest)
			return
		}
		target, err := parseRecipient(req.TargetJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad target_jid: %v", err), http.StatusBadRequest)
			return
		}
		// Load the source message: content + raw_proto (may be nil for
		// history-sync rows that predate batch alpha).
		var content string
		var rawProto []byte
		row := messageStore.db.QueryRow(
			`SELECT content, raw_proto FROM messages WHERE id = ? AND chat_jid = ?`,
			req.MessageID, req.SourceChatJID,
		)
		if err := row.Scan(&content, &rawProto); err != nil {
			if err == sql.ErrNoRows {
				http.Error(w, "source message not found", http.StatusNotFound)
				return
			}
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// If we have the raw proto, use it (preserves media). Otherwise
		// rebuild as text-only with ContextInfo.
		var msg *waProto.Message
		if len(rawProto) > 0 {
			msg = &waProto.Message{}
			if err := proto.Unmarshal(rawProto, msg); err != nil {
				writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: "raw_proto unmarshal failed: " + err.Error()})
				return
			}
		} else if content != "" {
			msg = &waProto.Message{
				ExtendedTextMessage: &waProto.ExtendedTextMessage{
					Text: proto.String(content),
				},
			}
		} else {
			http.Error(w, "source message has no forwardable content", http.StatusBadRequest)
			return
		}
		// Attach forwarded context. Build minimal ContextInfo if the message
		// type doesn't carry one. For simplicity we set it on ExtendedText
		// (falling through if the message has typed media).
		ctxInfo := &waProto.ContextInfo{
			IsForwarded:     proto.Bool(true),
			ForwardingScore: proto.Uint32(1),
		}
		if msg.ExtendedTextMessage != nil {
			if msg.ExtendedTextMessage.ContextInfo != nil && msg.ExtendedTextMessage.ContextInfo.ForwardingScore != nil {
				ctxInfo.ForwardingScore = proto.Uint32(msg.ExtendedTextMessage.ContextInfo.GetForwardingScore() + 1)
			}
			msg.ExtendedTextMessage.ContextInfo = ctxInfo
		} else if msg.Conversation != nil {
			msg.ExtendedTextMessage = &waProto.ExtendedTextMessage{
				Text:        msg.Conversation,
				ContextInfo: ctxInfo,
			}
			msg.Conversation = nil
		} else if msg.ImageMessage != nil {
			msg.ImageMessage.ContextInfo = ctxInfo
		} else if msg.VideoMessage != nil {
			msg.VideoMessage.ContextInfo = ctxInfo
		} else if msg.AudioMessage != nil {
			msg.AudioMessage.ContextInfo = ctxInfo
		} else if msg.DocumentMessage != nil {
			msg.DocumentMessage.ContextInfo = ctxInfo
		}
		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()
		if _, err := client.SendMessage(ctx, target, msg); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Forwarded"})
	})

	// POST /api/send_sticker - multipart upload of a WebP sticker.
	// WhatsApp expects stickers as 512x512 WebP (static or animated). We
	// don't validate/transcode the payload here; caller is responsible.
	mux.HandleFunc("/api/send_sticker", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !requireLoggedIn(w) {
			return
		}
		if !sendBucket.allow() {
			http.Error(w, "rate limited - too many sends", http.StatusTooManyRequests)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxMediaBytes+(1<<20))
		if err := r.ParseMultipartForm(8 << 20); err != nil {
			http.Error(w, fmt.Sprintf("multipart parse failed: %v", err), http.StatusBadRequest)
			return
		}
		recipient := r.FormValue("recipient")
		if recipient == "" {
			http.Error(w, "recipient required", http.StatusBadRequest)
			return
		}
		recipientJID, err := parseRecipient(recipient)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad recipient: %v", err), http.StatusBadRequest)
			return
		}
		file, _, err := r.FormFile("media")
		if err != nil {
			http.Error(w, "media file part required", http.StatusBadRequest)
			return
		}
		defer file.Close()
		data, err := io.ReadAll(file)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		sendCtx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
		defer cancel()
		resp, err := client.Upload(sendCtx, data, whatsmeow.MediaImage)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: "upload: " + err.Error()})
			return
		}
		msg := &waProto.Message{
			StickerMessage: &waProto.StickerMessage{
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				Mimetype:      proto.String("image/webp"),
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			},
		}
		if _, err := client.SendMessage(sendCtx, recipientJID, msg); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Sticker sent"})
	})

	// POST /api/poll/vote - cast a vote on someone else's poll.
	// Requires the original poll's message_id + sender + chat, plus the
	// list of option names to vote for. Uses the raw_proto BLOB stored at
	// batch alpha ingest to reconstruct enough MessageInfo for
	// BuildPollVote's decryption context.
	mux.HandleFunc("/api/poll/vote", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req VotePollRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.PollChatJID == "" || req.PollMessageID == "" || len(req.OptionNames) == 0 {
			http.Error(w, "poll_chat_jid, poll_message_id, option_names required", http.StatusBadRequest)
			return
		}
		chatJID, err := types.ParseJID(req.PollChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad poll_chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		// Sender defaults to self; caller must supply for group polls
		// they didn't create.
		var senderJID types.JID
		if req.PollSenderJID != "" {
			s, err := types.ParseJID(req.PollSenderJID)
			if err != nil {
				http.Error(w, fmt.Sprintf("bad poll_sender_jid: %v", err), http.StatusBadRequest)
				return
			}
			senderJID = s
		} else if client.Store != nil && client.Store.ID != nil {
			senderJID = *client.Store.ID
		} else {
			http.Error(w, "poll_sender_jid required (not logged in?)", http.StatusBadRequest)
			return
		}
		// Look up the stored poll message so we have its actual timestamp
		// and is_from_me flag.
		var isFromMe bool
		var ts time.Time
		if err := messageStore.db.QueryRow(
			`SELECT is_from_me, timestamp FROM messages WHERE id = ? AND chat_jid = ?`,
			req.PollMessageID, req.PollChatJID,
		).Scan(&isFromMe, &ts); err != nil {
			if err == sql.ErrNoRows {
				http.Error(w, "poll message not found in messages.db", http.StatusNotFound)
				return
			}
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		pollInfo := &types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:     chatJID,
				Sender:   senderJID,
				IsFromMe: isFromMe,
			},
			ID:        req.PollMessageID,
			Timestamp: ts,
		}
		vote, err := client.BuildPollVote(r.Context(), pollInfo, req.OptionNames)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		sendCtx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()
		resp, err := client.SendMessage(sendCtx, chatJID, vote)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// NOTE: a companion-cast GROUP poll vote does NOT reflect on the user's
		// own PHONE as their selection. whatsmeow builds the DeviceSentMessage
		// self-sync copy only for 1:1 chats (marshalMessage guards it behind
		// `to.Server != GroupServer`), which is why a self-chat vote shows on
		// all the user's devices and a group vote does not. Sending an explicit
		// DeviceSentMessage as a separate peer message was tried and did NOT
		// make the phone reflect it (2026-07-16) - the phone appears to honour a
		// DSM only when it arrives inline in the original send fan-out, not as a
		// standalone peer message. Truly fixing this would require patching
		// whatsmeow's group send path, not the bridge. The vote itself is cast
		// and counts correctly in get_poll_results regardless.
		// Record our own vote. whatsmeow does not echo our sends back as
		// events.Message, so handlePollVote never sees this one - without this
		// our vote is cast on WhatsApp but invisible in our own tally. We hash
		// the option names exactly as BuildPollVote did (sha256 of the name).
		if messageStore != nil {
			voter := senderJID.ToNonAD().String()
			if client.Store != nil && client.Store.ID != nil {
				voter = client.Store.ID.ToNonAD().String()
			}
			hashes := make([][]byte, 0, len(req.OptionNames))
			for _, opt := range req.OptionNames {
				h := sha256.Sum256([]byte(opt))
				hashes = append(hashes, h[:])
			}
			if err := messageStore.StorePollVote(req.PollMessageID, req.PollChatJID,
				voter, hashes, resp.Timestamp); err != nil {
				fmt.Printf("own poll vote persist failed for %s: %v\n", req.PollMessageID, err)
			}
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Vote cast"})
	})

	// POST /api/backfill/group_participants - one-shot backfill trigger.
	// Runs the same INSERT-SELECT as before but off the startup path so
	// the caller controls when the writer takes a big hit.
	mux.HandleFunc("/api/backfill/group_participants", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		res, err := messageStore.db.Exec(
			`INSERT INTO group_participants (group_jid, jid, first_seen_at, last_seen_at)
			 SELECT chat_jid,
			        CASE WHEN sender LIKE '%@%' THEN sender ELSE sender || '@s.whatsapp.net' END,
			        MIN(timestamp), MAX(timestamp)
			 FROM messages
			 WHERE chat_jid LIKE '%@g.us' AND sender != ''
			 GROUP BY chat_jid, sender
			 ON CONFLICT(group_jid, jid) DO UPDATE SET
			   first_seen_at = MIN(group_participants.first_seen_at, excluded.first_seen_at),
			   last_seen_at  = MAX(group_participants.last_seen_at,  excluded.last_seen_at)`,
		)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		n, _ := res.RowsAffected()
		writeJSON(w, http.StatusOK, map[string]any{"success": true, "rows_touched": n})
	})

	// GET /api/bridge/diagnostics - richer than /api/bridge/stats. Adds
	// WAL size, reactions/participants counts, and outstanding backfill work.
	mux.HandleFunc("/api/bridge/diagnostics", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var walBytes int64
		if fi, err := os.Stat("store/messages.db-wal"); err == nil {
			walBytes = fi.Size()
		}
		var reactionCount, participantCount, groupCount, chatsUnknownName int64
		_ = messageStore.db.QueryRow(`SELECT COUNT(*) FROM reactions`).Scan(&reactionCount)
		_ = messageStore.db.QueryRow(`SELECT COUNT(*) FROM group_participants`).Scan(&participantCount)
		_ = messageStore.db.QueryRow(`SELECT COUNT(*) FROM chats WHERE jid LIKE '%@g.us'`).Scan(&groupCount)
		_ = messageStore.db.QueryRow(
			`SELECT COUNT(*) FROM chats WHERE (name IS NULL OR name = '') AND (push_name IS NULL OR push_name = '')`,
		).Scan(&chatsUnknownName)
		var pendingSchedules, sentSchedules int64
		// scheduling.db lives in the MCP server; not queryable from the
		// bridge. Left here so a follow-up can plumb through if desired.
		out := map[string]any{
			"success":                 true,
			"connected":               client != nil && client.IsConnected(),
			"logged_in":               client != nil && client.IsLoggedIn(),
			"db_wal_bytes":            walBytes,
			"reactions_count":         reactionCount,
			"group_participants":      participantCount,
			"group_chats":             groupCount,
			"chats_missing_name":      chatsUnknownName,
			"uptime_s":                int64(time.Since(processStart).Seconds()),
			"pending_scheduled_sends": pendingSchedules,
			"sent_scheduled_sends":    sentSchedules,
		}
		writeJSON(w, http.StatusOK, out)
	})

	// GET /api/bridge/stats - operational read-only counts + uptime.
	mux.HandleFunc("/api/bridge/stats", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var chatCount, messageCount, mediaCount int64
		var oldestTS, newestTS sql.NullString
		_ = messageStore.db.QueryRow(`SELECT COUNT(*) FROM chats`).Scan(&chatCount)
		_ = messageStore.db.QueryRow(`SELECT COUNT(*) FROM messages`).Scan(&messageCount)
		_ = messageStore.db.QueryRow(
			`SELECT COUNT(*) FROM messages WHERE media_type IS NOT NULL AND media_type != ''`,
		).Scan(&mediaCount)
		_ = messageStore.db.QueryRow(
			`SELECT MIN(timestamp), MAX(timestamp) FROM messages`,
		).Scan(&oldestTS, &newestTS)
		var dbBytes int64
		if fi, err := os.Stat("store/messages.db"); err == nil {
			dbBytes = fi.Size()
		}
		out := map[string]any{
			"success":       true,
			"connected":     client != nil && client.IsConnected(),
			"logged_in":     client != nil && client.IsLoggedIn(),
			"chats":         chatCount,
			"messages":      messageCount,
			"media_msgs":    mediaCount,
			"db_size_bytes": dbBytes,
			"uptime_s":      int64(time.Since(processStart).Seconds()),
		}
		if oldestTS.Valid {
			out["oldest_message_ts"] = oldestTS.String
		}
		if newestTS.Valid {
			out["newest_message_ts"] = newestTS.String
		}
		writeJSON(w, http.StatusOK, out)
	})

	// GET /api/reactions?target_message_id=&chat_jid= - list reactions on a message.
	mux.HandleFunc("/api/reactions", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		tid := r.URL.Query().Get("target_message_id")
		cjid := r.URL.Query().Get("chat_jid")
		if tid == "" || cjid == "" {
			http.Error(w, "target_message_id and chat_jid required", http.StatusBadRequest)
			return
		}
		rows, err := messageStore.db.Query(
			`SELECT sender, emoji, timestamp FROM reactions
			 WHERE target_message_id = ? AND chat_jid = ?
			 ORDER BY timestamp ASC`,
			tid, cjid,
		)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		defer rows.Close()
		out := []map[string]any{}
		for rows.Next() {
			var sender, emoji string
			var ts time.Time
			if err := rows.Scan(&sender, &emoji, &ts); err == nil {
				out = append(out, map[string]any{
					"sender":    sender,
					"emoji":     emoji,
					"timestamp": ts.Format(time.RFC3339),
				})
			}
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":           true,
			"target_message_id": tid,
			"chat_jid":          cjid,
			"reactions":         out,
		})
	})

	// GET /api/media/info?message_id=&chat_jid= - return media metadata
	// without downloading. Cheap alternative to download_media for tools
	// that only need mime/size/filename to decide.
	mux.HandleFunc("/api/media/info", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		mid := r.URL.Query().Get("message_id")
		cjid := r.URL.Query().Get("chat_jid")
		if mid == "" || cjid == "" {
			http.Error(w, "message_id and chat_jid required", http.StatusBadRequest)
			return
		}
		var mediaType, filename string
		var fileLength uint64
		if err := messageStore.db.QueryRow(
			`SELECT media_type, filename, COALESCE(file_length, 0)
			 FROM messages WHERE id = ? AND chat_jid = ?`,
			mid, cjid,
		).Scan(&mediaType, &filename, &fileLength); err != nil {
			if err == sql.ErrNoRows {
				http.Error(w, "message not found", http.StatusNotFound)
				return
			}
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if mediaType == "" {
			writeJSON(w, http.StatusOK, map[string]any{
				"success": true, "message_id": mid, "chat_jid": cjid, "has_media": false,
			})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":    true,
			"message_id": mid,
			"chat_jid":   cjid,
			"has_media":  true,
			"media_type": mediaType,
			"filename":   filename,
			"mime":       mimeForFilename(filename, mediaType),
			"size_bytes": fileLength,
		})
	})

	// #73: POST /api/chat/delete {chat_jid, delete_media?} - delete a chat
	// via appstate.BuildDeleteChat. Needs the last message's timestamp +
	// key; fetched from messages.db.
	mux.HandleFunc("/api/chat/delete", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req struct {
			ChatJID     string `json:"chat_jid"`
			DeleteMedia bool   `json:"delete_media"`
		}
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		// Grab the last message so appstate can identify what we're
		// deleting from. If the chat has no messages we synthesize an
		// empty key so the patch still fires.
		var lastID string
		var lastFromMe bool
		var lastTS time.Time
		row := messageStore.db.QueryRow(
			`SELECT id, is_from_me, timestamp FROM messages
			 WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT 1`,
			req.ChatJID,
		)
		_ = row.Scan(&lastID, &lastFromMe, &lastTS)
		if lastTS.IsZero() {
			lastTS = time.Now()
		}
		key := &waCommon.MessageKey{
			ID:        proto.String(lastID),
			FromMe:    proto.Bool(lastFromMe),
			RemoteJID: proto.String(req.ChatJID),
		}
		patch := appstate.BuildDeleteChat(jid, lastTS, key, req.DeleteMedia)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// Optimistic local delete: remove the chat's messages + chat row.
		if _, err := messageStore.db.Exec(`DELETE FROM messages WHERE chat_jid = ?`, req.ChatJID); err != nil {
			fmt.Printf("local chat delete (messages) failed for %s: %v\n", req.ChatJID, err)
		}
		if _, err := messageStore.db.Exec(`DELETE FROM chats WHERE jid = ?`, req.ChatJID); err != nil {
			fmt.Printf("local chat delete (chat row) failed for %s: %v\n", req.ChatJID, err)
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Chat deleted"})
	})

	// #140: GET /api/qr - return the current pairing QR as image/png.
	// Loopback / docker-net only. If the client is already logged in the
	// QR file is stale; return 404. This lets a browser show the QR for
	// re-pairing without needing terminal access to the container.
	mux.HandleFunc("/api/qr", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if client != nil && client.IsLoggedIn() {
			http.Error(w, "already logged in - no QR active", http.StatusNotFound)
			return
		}
		f, err := os.Open("store/qr.png")
		if err != nil {
			http.Error(w, "QR not available - bridge is not currently in a pairing window", http.StatusNotFound)
			return
		}
		defer f.Close()
		w.Header().Set("Content-Type", "image/png")
		w.Header().Set("Cache-Control", "no-store")
		if _, err := io.Copy(w, f); err != nil {
			fmt.Printf("qr copy failed: %v\n", err)
		}
	})

	// --- N10 labels (WA Business) ----------------------------------------

	// POST /api/labels/edit - create / rename / delete a label.
	mux.HandleFunc("/api/labels/edit", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req LabelEditRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.LabelID == "" {
			http.Error(w, "label_id required", http.StatusBadRequest)
			return
		}
		patch := appstate.BuildLabelEdit(req.LabelID, req.LabelName, req.LabelColor, req.Delete)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Label edited"})
	})

	// POST /api/labels/chat - add / remove a label on a chat.
	mux.HandleFunc("/api/labels/chat", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req LabelChatRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.LabelID == "" || req.ChatJID == "" {
			http.Error(w, "label_id and chat_jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		patch := appstate.BuildLabelChat(jid, req.LabelID, req.Labeled)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Chat label updated"})
	})

	// POST /api/labels/message - add / remove a label on a message.
	mux.HandleFunc("/api/labels/message", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req LabelMessageRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.LabelID == "" || req.ChatJID == "" || req.MessageID == "" {
			http.Error(w, "label_id, chat_jid, message_id required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		patch := appstate.BuildLabelMessage(jid, req.LabelID, req.MessageID, req.Labeled)
		if err := client.SendAppState(r.Context(), patch); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Message label updated"})
	})

	// POST /api/group/leave - exit a group.
	mux.HandleFunc("/api/group/leave", func(w http.ResponseWriter, r *http.Request) {
		if !requireLoggedIn(w) {
			return
		}
		var req GroupLeaveRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.GroupJID == "" {
			http.Error(w, "group_jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.GroupJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad group_jid: %v", err), http.StatusBadRequest)
			return
		}
		if err := client.LeaveGroup(r.Context(), jid); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Left group"})
	})

	// POST /api/send_location - share a static map pin in a chat.
	mux.HandleFunc("/api/send_location", func(w http.ResponseWriter, r *http.Request) {
		var req SendLocationRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.Recipient == "" {
			http.Error(w, "recipient required", http.StatusBadRequest)
			return
		}
		recipient, err := parseRecipient(req.Recipient)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad recipient: %v", err), http.StatusBadRequest)
			return
		}
		msg := &waProto.Message{
			LocationMessage: &waProto.LocationMessage{
				DegreesLatitude:  proto.Float64(req.Latitude),
				DegreesLongitude: proto.Float64(req.Longitude),
				Name:             proto.String(req.Name),
				Address:          proto.String(req.Address),
			},
		}
		if _, err := client.SendMessage(r.Context(), recipient, msg); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: "Location sent"})
	})

	// POST /api/set_disappearing - set the ephemeral message timer for a chat.
	// Seconds == 0 disables; common values: 86400 (24h), 604800 (7d), 7776000 (90d).
	mux.HandleFunc("/api/set_disappearing", func(w http.ResponseWriter, r *http.Request) {
		var req SetDisappearingRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.ChatJID == "" {
			http.Error(w, "chat_jid required", http.StatusBadRequest)
			return
		}
		chat, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad chat_jid: %v", err), http.StatusBadRequest)
			return
		}
		// SetDisappearingTimer requires a settingsTimestamp - whatsmeow uses it
		// for app-state ordering. time.Now() is fine for an external setter.
		if err := client.SetDisappearingTimer(r.Context(), chat, time.Duration(req.Seconds)*time.Second, time.Now()); err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("Disappearing timer set to %ds", req.Seconds)})
	})

	// POST /api/create_poll - send a poll to a chat. Voting later is a separate
	// flow (needs the original poll's encryption context) - deferred to a
	// follow-up batch.
	mux.HandleFunc("/api/create_poll", func(w http.ResponseWriter, r *http.Request) {
		var req CreatePollRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.Recipient == "" || req.Name == "" || len(req.Options) < 2 {
			http.Error(w, "recipient, name, and at least 2 options required", http.StatusBadRequest)
			return
		}
		recipient, err := parseRecipient(req.Recipient)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad recipient: %v", err), http.StatusBadRequest)
			return
		}
		sel := req.SelectableOptionsCount
		if sel < 1 {
			sel = 1
		}
		poll := client.BuildPollCreation(req.Name, req.Options, sel)
		resp, err := client.SendMessage(r.Context(), recipient, poll)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// Persist our own poll. whatsmeow doesn't echo our sends back as
		// events.Message, so without this the poll we just created is absent
		// from our history and its votes arrive referencing an id we've never
		// heard of - unresolvable. Non-fatal: the poll is already sent.
		if messageStore != nil {
			sender := ""
			if client.Store != nil && client.Store.ID != nil {
				sender = client.Store.ID.User
			}
			var rawProto []byte
			if b, mErr := proto.Marshal(poll); mErr == nil {
				rawProto = b
			}
			_ = messageStore.StoreChat(recipient.String(), "", resp.Timestamp)
			if err := messageStore.StoreMessage(
				resp.ID, recipient.String(), sender, req.Name, resp.Timestamp, true,
				"", "", "", nil, nil, nil, 0, rawProto, false,
			); err != nil {
				fmt.Printf("poll persist failed for %s: %v\n", resp.ID, err)
			}
			if err := messageStore.StorePoll(resp.ID, recipient.String(), req.Name,
				req.Options, sel, resp.Timestamp); err != nil {
				fmt.Printf("poll options persist failed for %s: %v\n", resp.ID, err)
			}
		}
		writeJSON(w, http.StatusOK, GenericResponse{
			Success: true,
			Message: fmt.Sprintf("Poll sent (message_id: %s)", resp.ID),
		})
	})

	// GET /api/poll/results?message_id=&chat_jid= - tally a poll.
	//
	// Joins polls (option -> hash, recorded at creation) against poll_votes
	// (voter -> hash, recorded as votes arrive). LEFT JOIN so options with zero
	// votes still appear - a tally that silently omits the losing option is
	// worse than useless.
	mux.HandleFunc("/api/poll/results", func(w http.ResponseWriter, r *http.Request) {
		msgID := r.URL.Query().Get("message_id")
		chatJID := r.URL.Query().Get("chat_jid")
		if msgID == "" || chatJID == "" {
			http.Error(w, "message_id and chat_jid required", http.StatusBadRequest)
			return
		}
		rows, err := messageStore.db.Query(
			`SELECT p.option_index, p.option_name, p.name, p.selectable_count,
			        COUNT(v.voter_jid),
			        COALESCE(GROUP_CONCAT(v.voter_jid, '|'), '')
			 FROM polls p
			 LEFT JOIN poll_votes v
			   ON v.poll_message_id = p.message_id
			  AND v.poll_chat_jid   = p.chat_jid
			  AND v.option_hash     = p.option_hash
			 WHERE p.message_id = ? AND p.chat_jid = ?
			 GROUP BY p.option_index, p.option_name, p.name, p.selectable_count
			 ORDER BY p.option_index`,
			msgID, chatJID,
		)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		defer rows.Close()

		type optionResult struct {
			Option string   `json:"option"`
			Votes  int      `json:"votes"`
			Voters []string `json:"voters"`
		}
		var (
			results  []optionResult
			pollName string
			selCount int
		)
		for rows.Next() {
			var (
				idx, votes int
				optName    string
				name       sql.NullString
				sel        sql.NullInt64
				voters     string
			)
			if err := rows.Scan(&idx, &optName, &name, &sel, &votes, &voters); err != nil {
				writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
				return
			}
			pollName = name.String
			selCount = int(sel.Int64)
			var vs []string
			if voters != "" {
				vs = strings.Split(voters, "|")
			}
			results = append(results, optionResult{Option: optName, Votes: votes, Voters: vs})
		}
		if len(results) == 0 {
			writeJSON(w, http.StatusNotFound, GenericResponse{
				Success: false,
				Message: "no poll found with that message_id/chat_jid (only polls seen since this bridge started tracking them are known)",
			})
			return
		}
		uniq := map[string]struct{}{}
		for _, o := range results {
			for _, v := range o.Voters {
				uniq[v] = struct{}{}
			}
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"success":                  true,
			"message_id":               msgID,
			"chat_jid":                 chatJID,
			"name":                     pollName,
			"selectable_options_count": selCount,
			"total_voters":             len(uniq),
			"options":                  results,
		})
	})

	// POST /api/profile_picture - get the URL of a JID's profile picture.
	// Batch α N12: cached by picture_id. events.Picture invalidates a row so
	// the next call refetches. Cuts repeated CDN hits for tools that keep
	// showing avatars.
	mux.HandleFunc("/api/profile_picture", func(w http.ResponseWriter, r *http.Request) {
		var req ProfilePictureRequest
		if !parsePOSTJSON(w, r, &req) {
			return
		}
		if req.JID == "" {
			http.Error(w, "jid required", http.StatusBadRequest)
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil {
			http.Error(w, fmt.Sprintf("bad jid: %v", err), http.StatusBadRequest)
			return
		}
		// Only cache full-size images (Preview=false); previews are cheap enough
		// that caching adds no value.
		if !req.Preview {
			var cachedID, cachedURL string
			if row := messageStore.db.QueryRow(
				`SELECT picture_id, url FROM profile_pictures WHERE jid = ?`, req.JID,
			); row != nil {
				if err := row.Scan(&cachedID, &cachedURL); err == nil && cachedURL != "" {
					writeJSON(w, http.StatusOK, ProfilePictureResponse{
						Success: true, URL: cachedURL, Type: "image", ID: cachedID,
					})
					return
				}
			}
		}
		params := &whatsmeow.GetProfilePictureParams{Preview: req.Preview}
		info, err := client.GetProfilePictureInfo(r.Context(), jid, params)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, ProfilePictureResponse{Success: false, Message: err.Error()})
			return
		}
		if info == nil {
			writeJSON(w, http.StatusNotFound, ProfilePictureResponse{Success: false, Message: "No profile picture"})
			return
		}
		if !req.Preview {
			if _, err := messageStore.db.Exec(
				`INSERT OR REPLACE INTO profile_pictures (jid, picture_id, url, direct_path, fetched_at)
				 VALUES (?, ?, ?, ?, ?)`,
				req.JID, info.ID, info.URL, info.DirectPath, time.Now(),
			); err != nil {
				fmt.Printf("profile_pictures cache write failed: %v\n", err)
			}
		}
		writeJSON(w, http.StatusOK, ProfilePictureResponse{
			Success: true,
			URL:     info.URL,
			Type:    info.Type,
			ID:      info.ID,
		})
	})

	// GET /api/message_receipts?message_id=&chat_jid= - return receipt columns
	// for a single sent message. Batch α G2.
	mux.HandleFunc("/api/message_receipts", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		mid := r.URL.Query().Get("message_id")
		cjid := r.URL.Query().Get("chat_jid")
		if mid == "" || cjid == "" {
			http.Error(w, "message_id and chat_jid required", http.StatusBadRequest)
			return
		}
		var dAt, rAt, pAt sql.NullTime
		row := messageStore.db.QueryRow(
			`SELECT delivered_at, read_at, played_at FROM messages WHERE id = ? AND chat_jid = ?`,
			mid, cjid,
		)
		if err := row.Scan(&dAt, &rAt, &pAt); err != nil {
			if err == sql.ErrNoRows {
				writeJSON(w, http.StatusNotFound, GenericResponse{Success: false, Message: "message not found"})
				return
			}
			writeJSON(w, http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		out := map[string]any{"success": true, "message_id": mid, "chat_jid": cjid}
		if dAt.Valid {
			out["delivered_at"] = dAt.Time.Format(time.RFC3339)
		}
		if rAt.Valid {
			out["read_at"] = rAt.Time.Format(time.RFC3339)
		}
		if pAt.Valid {
			out["played_at"] = pAt.Time.Format(time.RFC3339)
		}
		writeJSON(w, http.StatusOK, out)
	})

	// Bind on all interfaces INSIDE the container. The unauthenticated
	// /api/send and /api/download endpoints are protected by NOT publishing
	// this port to any public host interface (see docker-compose.yml: the
	// bridge uses `expose`, not `ports`, so :8080 is reachable only by the
	// mcp container over the private docker network). Override with
	// WA_BRIDGE_BIND if you ever run the bridge directly on a host, where
	// 127.0.0.1 is the safe choice.
	bindHost := os.Getenv("WA_BRIDGE_BIND")
	if bindHost == "" {
		bindHost = "0.0.0.0"
	}
	serverAddr := fmt.Sprintf("%s:%d", bindHost, port)
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Explicit server with timeouts so a slow/hung client can't pin a goroutine
	// forever (http.ListenAndServe defaults are no timeouts at all).
	srv := &http.Server{
		Addr:              serverAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       60 * time.Second,
		WriteTimeout:      5 * time.Minute, // long enough for large media uploads
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

// saveQRPNG renders the pairing code as a scannable PNG image at the given path.
// Each QR module is scaled up to a block of pixels so a phone camera can read it.
func saveQRPNG(code, path string) error {
	c, err := qr.Encode(code, qr.L)
	if err != nil {
		return err
	}
	const scale = 8  // pixels per QR module
	const border = 4 // quiet-zone modules
	size := c.Size
	dim := (size + 2*border) * scale
	img := image.NewRGBA(image.Rect(0, 0, dim, dim))
	// Fill white background.
	for y := 0; y < dim; y++ {
		for x := 0; x < dim; x++ {
			img.Set(x, y, color.White)
		}
	}
	// Draw black modules.
	for my := 0; my < size; my++ {
		for mx := 0; mx < size; mx++ {
			if c.Black(mx, my) {
				for dy := 0; dy < scale; dy++ {
					for dx := 0; dx < scale; dx++ {
						img.Set((mx+border)*scale+dx, (my+border)*scale+dy, color.Black)
					}
				}
			}
		}
	}
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return png.Encode(f, img)
}

// runHealthcheck is a self-contained probe used as the container's
// HEALTHCHECK. Distroless has no shell, so we re-exec the same binary with
// `-healthcheck`; it hits the local /api/health and exits 0 if connected,
// 1 otherwise. Fast and dependency-free.
func runHealthcheck() {
	c := http.Client{Timeout: 3 * time.Second}
	resp, err := c.Get("http://127.0.0.1:8080/api/health")
	if err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: %v\n", err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(os.Stderr, "healthcheck: HTTP %d\n", resp.StatusCode)
		os.Exit(1)
	}
	var body struct {
		Connected bool `json:"connected"`
		LoggedIn  bool `json:"logged_in"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		fmt.Fprintf(os.Stderr, "healthcheck: decode: %v\n", err)
		os.Exit(1)
	}
	if !body.Connected || !body.LoggedIn {
		fmt.Fprintf(os.Stderr, "healthcheck: connected=%v logged_in=%v\n", body.Connected, body.LoggedIn)
		os.Exit(1)
	}
	// 0 exit - container is healthy
}

func main() {
	// Healthcheck mode: re-exec'd by docker's HEALTHCHECK with `-healthcheck`.
	// Exits 0/1 without starting the long-running services.
	if len(os.Args) >= 2 && os.Args[1] == "-healthcheck" {
		runHealthcheck()
		return
	}

	// Set up logger. Level is env-tunable because whatsmeow logs the
	// interesting protocol detail at DEBUG - e.g. whether a request for an
	// unavailable message actually reached the phone ("Requested message %s
	// from phone"). At INFO you see the symptom and none of the cause.
	// BRIDGE_WA_LOG_LEVEL=DEBUG to investigate; leave unset for INFO (DEBUG is
	// very chatty on a busy account).
	waLevel := os.Getenv("BRIDGE_WA_LOG_LEVEL")
	if waLevel == "" {
		waLevel = "INFO"
	}
	logger := waLog.Stdout("Client", waLevel, true)
	logger.Infof("Starting WhatsApp client... (whatsmeow log level: %s)", waLevel)

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", waLevel, true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information.
	// Newer whatsmeow returns (nil, nil) for a fresh store instead of
	// sql.ErrNoRows, so handle both the error and the nil-device cases.
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}
	if deviceStore == nil {
		// No existing device in the store, create a fresh one to pair.
		deviceStore = container.NewDevice()
		logger.Infof("Created new device")
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()

	// Start periodic WAL checkpoints. 5-minute interval matches typical write
	// cadence; tied to a context cancelled on shutdown.
	checkpointCtx, cancelCheckpoints := context.WithCancel(context.Background())
	defer cancelCheckpoints()
	messageStore.startWALCheckpoints(checkpointCtx, 5*time.Minute)

	// Start the media reaper. Default 14d TTL on downloaded media under store/,
	// disable with BRIDGE_MEDIA_TTL_DAYS=0.
	ttlDays := 14
	if v := os.Getenv("BRIDGE_MEDIA_TTL_DAYS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			ttlDays = n
		}
	}
	startMediaReaper(checkpointCtx, "store", time.Duration(ttlDays)*24*time.Hour)

	// Backfill chat names that are currently just the JID local-part (i.e.
	// raw numbers in the UI) using whatsmeow's Store.Contacts. Runs after a
	// short delay so any initial OfflineSyncCompleted / fresh PushName events
	// have populated the contacts cache first. Also fills push_name for chats
	// whose canonical name was set by an older single-field backfill.
	go func() {
		select {
		case <-checkpointCtx.Done():
			return
		case <-time.After(15 * time.Second):
			// #244: merge any @lid chats left over from earlier runs BEFORE
			// backfilling names - once merged there are fewer rows to walk.
			migrateLIDChats(client, messageStore, logger)
			backfillChatNames(client, messageStore, logger)
			backfillPushNames(client, messageStore, logger)
			// Batch omicron: group_participants backfill moved to a
			// manual /api/backfill/group_participants trigger. The
			// automatic 94k-row GROUP BY was serialising with the writer
			// and freezing the process for minutes.
		}
	}()

	// Setup event handling for messages and history sync.
	// The handler body is wrapped in a panic recover - whatsmeow dispatches events
	// synchronously on a single goroutine, so a panic inside ANY case would crash
	// the entire bridge and force a re-pair. The recover scopes the blast radius to
	// the single bad event.
	// All known event types are handled (cases that warn-log rather than act) so
	// operationally interesting events (Disconnected, TemporaryBan, ClientOutdated,
	// StreamReplaced, etc.) surface in journalctl instead of being silently dropped.
	client.AddEventHandler(func(evt interface{}) {
		defer func() {
			if r := recover(); r != nil {
				logger.Errorf("event handler panic: %v (event type=%T)", r, evt)
			}
		}()
		switch v := evt.(type) {
		case *events.Message:
			handleMessage(client, messageStore, v, logger)

		case *events.HistorySync:
			// Run history sync on its own goroutine so a slow GetGroupInfo /
			// GetContact lookup inside it cannot block live event dispatch.
			go func(hs *events.HistorySync) {
				defer func() {
					if r := recover(); r != nil {
						logger.Errorf("handleHistorySync panic: %v", r)
					}
				}()
				handleHistorySync(client, messageStore, hs, logger)
			}(v)

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please scan QR code to log in again")

		// Adopted from upstream PR #273: route MediaRetry responses to any
		// download call waiting on this specific message ID.
		case *events.MediaRetry:
			mediaRetryMutex.Lock()
			ch, ok := mediaRetryChans[string(v.MessageID)]
			mediaRetryMutex.Unlock()
			if ok {
				select {
				case ch <- v:
				default:
				}
			}

		// Operationally important events that the original handler silently dropped.
		// Logging them at WARN turns "messages stopped arriving with no explanation"
		// into "look at the log and you see why."
		case *events.Disconnected:
			logger.Warnf("Disconnected from WhatsApp; whatsmeow will auto-reconnect")
		case *events.StreamReplaced:
			logger.Warnf("Stream replaced - another client took over this device session")
		case *events.StreamError:
			logger.Warnf("Stream error: code=%s", v.Code)
		case *events.TemporaryBan:
			logger.Warnf("Temporary ban from WhatsApp: code=%v expires=%v", v.Code, v.Expire)
		case *events.ConnectFailure:
			logger.Warnf("Connect failure: reason=%v message=%s", v.Reason, v.Message)
		case *events.ClientOutdated:
			logger.Warnf("whatsmeow client outdated - rebuild bridge with latest go.mau.fi/whatsmeow")
		case *events.KeepAliveTimeout:
			logger.Warnf("Keepalive timeout (errors=%d, last_success=%v)", v.ErrorCount, v.LastSuccess)
		case *events.OfflineSyncCompleted:
			logger.Infof("Offline sync completed (count=%d)", v.Count)
		case *events.UndecryptableMessage:
			logger.Warnf("Undecryptable message from %s (unavailable=%v)", v.Info.Sender, v.IsUnavailable)

		// Batch α G2: Receipt persistence. WhatsApp fires events.Receipt for
		// delivered / read / played on messages we sent. We UPDATE the
		// matching rows so tools can surface delivery state.
		case *events.Receipt:
			metricEventsReceived.WithLabelValues("receipt").Inc()
			var col string
			switch v.Type {
			case types.ReceiptTypeDelivered:
				col = "delivered_at"
			case types.ReceiptTypeRead, types.ReceiptTypeReadSelf:
				col = "read_at"
			case types.ReceiptTypePlayed:
				col = "played_at"
			default:
				col = ""
			}
			if col != "" {
				chat := v.Chat.String()
				ts := v.Timestamp
				for _, id := range v.MessageIDs {
					if _, err := messageStore.db.Exec(
						`UPDATE messages SET `+col+` = ? WHERE id = ? AND chat_jid = ? AND `+col+` IS NULL`,
						ts, id, chat,
					); err != nil {
						logger.Warnf("receipt UPDATE failed for %s/%s (%s): %v", chat, id, col, err)
					}
				}
			}

		// Batch iota: persist chat-state changes so read-side tools can
		// query them without round-tripping to WhatsApp.
		case *events.Pin:
			metricEventsReceived.WithLabelValues("pin").Inc()
			pinned := 0
			if v.Action != nil && v.Action.GetPinned() {
				pinned = 1
			}
			if _, err := messageStore.db.Exec(
				`UPDATE chats SET is_pinned = ? WHERE jid = ?`,
				pinned, v.JID.String(),
			); err != nil {
				logger.Warnf("pin state update failed: %v", err)
			}
		case *events.Mute:
			metricEventsReceived.WithLabelValues("mute").Inc()
			muted := 0
			var endTS *time.Time
			if v.Action != nil {
				if v.Action.GetMuted() {
					muted = 1
				}
				if ts := v.Action.GetMuteEndTimestamp(); ts != 0 {
					t := time.Unix(ts/1000, 0).UTC()
					endTS = &t
				}
			}
			if _, err := messageStore.db.Exec(
				`UPDATE chats SET is_muted = ?, mute_end_ts = ? WHERE jid = ?`,
				muted, endTS, v.JID.String(),
			); err != nil {
				logger.Warnf("mute state update failed: %v", err)
			}
		case *events.Archive:
			metricEventsReceived.WithLabelValues("archive").Inc()
			archived := 0
			if v.Action != nil && v.Action.GetArchived() {
				archived = 1
			}
			if _, err := messageStore.db.Exec(
				`UPDATE chats SET is_archived = ? WHERE jid = ?`,
				archived, v.JID.String(),
			); err != nil {
				logger.Warnf("archive state update failed: %v", err)
			}
		case *events.MarkChatAsRead:
			metricEventsReceived.WithLabelValues("mark_chat_as_read").Inc()
			unread := 0
			if v.Action != nil && !v.Action.GetRead() {
				unread = 1
			}
			if _, err := messageStore.db.Exec(
				`UPDATE chats SET mark_unread = ? WHERE jid = ?`,
				unread, v.JID.String(),
			); err != nil {
				logger.Warnf("mark_unread state update failed: %v", err)
			}

		// Batch α N12: invalidate cached profile picture on change.
		case *events.Picture:
			metricEventsReceived.WithLabelValues("picture").Inc()
			jid := v.JID.String()
			if _, err := messageStore.db.Exec(`DELETE FROM profile_pictures WHERE jid = ?`, jid); err != nil {
				logger.Warnf("profile_pictures invalidate failed for %s: %v", jid, err)
			}
		}
	})

	// Create channel to track connection success
	connected := make(chan bool, 1)

	// Connect to WhatsApp
	if client.Store.ID == nil {
		// No ID stored, this is a new client, need to pair with phone
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}

		// Print QR code for pairing with phone
		for evt := range qrChan {
			if evt.Event == "code" {
				fmt.Println("\nScan this QR code with your WhatsApp app:")
				// Full-block rendering is larger and far easier for a phone
				// camera to read than the cramped half-block variant.
				qrterminal.GenerateWithConfig(evt.Code, qrterminal.Config{
					Level:     qrterminal.L,
					Writer:    os.Stdout,
					BlackChar: qrterminal.BLACK,
					WhiteChar: qrterminal.WHITE,
					QuietZone: 2,
				})
				// Also save a PNG so you can open it and scan at full size if
				// the terminal QR is hard to read.
				if err := saveQRPNG(evt.Code, "store/qr.png"); err == nil {
					if abs, aerr := filepath.Abs("store/qr.png"); aerr == nil {
						fmt.Printf("\nOr open this image and scan it: %s\n", abs)
						fmt.Println("(run:  open " + abs + ")")
					}
				}
			} else if evt.Event == "success" {
				connected <- true
				break
			}
		}

		// Wait for connection
		select {
		case <-connected:
			fmt.Println("\nSuccessfully connected and authenticated!")
		case <-time.After(3 * time.Minute):
			logger.Errorf("Timeout waiting for QR code scan")
			return
		}
	} else {
		// Already logged in, just connect
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
		connected <- true
	}

	// Wait a moment for connection to stabilize
	time.Sleep(2 * time.Second)

	if !client.IsConnected() {
		logger.Errorf("Failed to establish stable connection")
		return
	}

	fmt.Println("\n✓ Connected to WhatsApp! Type 'help' for commands.")

	// Start REST API server
	// #242: WHATSAPP_BRIDGE_PORT env override; falls back to 8080 on any
	// parse failure or out-of-range value.
	port := 8080
	if v := os.Getenv("WHATSAPP_BRIDGE_PORT"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n < 65536 {
			port = n
		} else {
			fmt.Printf("WHATSAPP_BRIDGE_PORT=%q is invalid, falling back to 8080\n", v)
		}
	}
	startRESTServer(client, messageStore, port)

	// Create a channel to keep the main goroutine alive
	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)

	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")

	// Wait for termination signal
	<-exitChan

	fmt.Println("Disconnecting...")
	// Disconnect client
	client.Disconnect()
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil && existingName != "" {
		// Chat exists with a name, use that. The previous "Using existing chat
		// name for X: Y" Infof fired on every single message - dominated log
		// volume in busy groups. Gated behind BRIDGE_LOG_MESSAGES=1.
		if os.Getenv("BRIDGE_LOG_MESSAGES") == "1" {
			logger.Infof("Using existing chat name for %s: %s", chatJID, existingName)
		}
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Using group name: %s", name)
	} else {
		// This is an individual contact - or an @lid identifier, which since
		// WhatsApp's "linked identifiers" migration is the default for many
		// 1:1 chats. resolveContactName tries PushName -> FullName ->
		// FirstName -> BusinessName before falling back to the JID local-part
		// (which is what shows up as a bare number in the UI).
		if resolved := resolveContactName(client, jid); resolved != "" {
			name = resolved
		} else if sender != "" {
			name = sender
		} else {
			name = jid.User
		}

		if os.Getenv("BRIDGE_LOG_MESSAGES") == "1" {
			logger.Infof("Using contact name: %s", name)
		}
	}

	return name
}

// resolveContactNames returns BOTH name flavours separately so callers can
// store them in distinct columns and let the client present whichever
// combination it prefers. Either value may be "":
//
//	saved = your address-book label (FullName, then FirstName, then BusinessName)
//	push  = the contact's self-set display name (PushName from WhatsApp envelope)
//
// USER ASK 2026-06-25 v2 (separate fields): WhatsApp's app shows e.g. "Smith ~Jo"
// to convey both pieces; storing them split means MCP tools can return them as
// distinct JSON fields and the client picks the formatting.
func resolveContactNames(client *whatsmeow.Client, jid types.JID) (saved, push string) {
	contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
	if err != nil {
		return "", ""
	}
	saved = contact.FullName
	if saved == "" {
		saved = contact.FirstName
	}
	if saved == "" {
		saved = contact.BusinessName
	}
	push = contact.PushName
	return saved, push
}

// resolveContactName returns the single best display name for a JID.
// Now derived from resolveContactNames - saved preferred, push as fallback.
func resolveContactName(client *whatsmeow.Client, jid types.JID) string {
	saved, push := resolveContactNames(client, jid)
	if saved != "" {
		return saved
	}
	return push
}

// backfillChatNames runs once after connect. Walks the chats table for rows
// whose name is just the JID local-part (numeric @lid or bare number - the
// "raw" state that shows up as a number in MCP tool output) and tries to
// resolve a real human name via Store.Contacts. The Store.Contacts cache is
// populated by whatsmeow as messages flow in, so this typically works for
// contacts you've recently received messages from.
//
// Idempotent: re-running only touches rows that are STILL purely-numeric.
// Logs a one-line summary; non-fatal on any individual failure.
func backfillChatNames(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	rows, err := store.db.Query(
		`SELECT jid FROM chats WHERE name GLOB '[0-9]*' AND name NOT LIKE '%@%'`,
	)
	if err != nil {
		logger.Warnf("backfillChatNames: query failed: %v", err)
		return
	}
	defer rows.Close()

	var candidates []string
	for rows.Next() {
		var j string
		if err := rows.Scan(&j); err == nil {
			candidates = append(candidates, j)
		}
	}
	if len(candidates) == 0 {
		return
	}

	resolved := 0
	for _, jidStr := range candidates {
		jid, err := types.ParseJID(jidStr)
		if err != nil {
			continue
		}
		// Skip groups (they have their own resolution path via GetGroupInfo).
		if jid.Server == "g.us" {
			continue
		}
		saved, push := resolveContactNames(client, jid)
		if saved == "" && push == "" {
			continue
		}
		// Canonical `name` = saved if present, else push. push column is set
		// unconditionally to whatever the contact self-reports (may be empty
		// for contacts that haven't messaged yet).
		newName := saved
		if newName == "" {
			newName = push
		}
		if _, err := store.db.Exec(
			`UPDATE chats SET name = ?, push_name = ? WHERE jid = ?`,
			newName, push, jidStr,
		); err == nil {
			resolved++
		}
	}
	fmt.Printf("Chat name backfill: resolved %d / %d candidates\n",
		resolved, len(candidates))
}

// backfillPushNames is a second-pass backfill that adds push_name to chats
// whose `name` is already populated (e.g. from the prior single-field
// backfill before we added the column). Idempotent - skips rows that
// already have a push_name set.
func backfillPushNames(client *whatsmeow.Client, store *MessageStore, logger waLog.Logger) {
	rows, err := store.db.Query(
		`SELECT jid FROM chats WHERE (push_name IS NULL OR push_name = '') AND jid NOT LIKE '%@g.us'`,
	)
	if err != nil {
		return
	}
	defer rows.Close()
	var jids []string
	for rows.Next() {
		var j string
		if err := rows.Scan(&j); err == nil {
			jids = append(jids, j)
		}
	}
	if len(jids) == 0 {
		return
	}
	resolved := 0
	for _, jidStr := range jids {
		jid, err := types.ParseJID(jidStr)
		if err != nil {
			continue
		}
		_, push := resolveContactNames(client, jid)
		if push == "" {
			continue
		}
		if _, err := store.db.Exec(
			`UPDATE chats SET push_name = ? WHERE jid = ? AND (push_name IS NULL OR push_name = '')`,
			push, jidStr,
		); err == nil {
			resolved++
		}
	}
	fmt.Printf("Push name backfill: resolved %d / %d candidates\n",
		resolved, len(jids))
}

// Handle history sync events.
//
// Writes are batched: one BEGIN/COMMIT per ~500 inserts, instead of one
// implicit BEGIN/COMMIT per row. At synchronous=NORMAL each commit fsyncs
// once, so collapsing 500 rows into 1 fsync is 500x fewer disk syncs.
// Audit benched the pre-batch version at ~minutes for a fresh 77k pair;
// the batched version should land in single-digit seconds.
//
// Per-message INFO logging is gated behind BRIDGE_LOG_MESSAGES=1 (default
// off) since the firehose dominates log volume during a history sync burst
// and leaks message bodies to disk-rotated logs. We always print the
// per-batch summary so progress is still visible.
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))

	verbose := os.Getenv("BRIDGE_LOG_MESSAGES") == "1"
	const batchSize = 500

	ctx := context.Background()
	tx, err := messageStore.BeginTx(ctx)
	if err != nil {
		logger.Errorf("history sync: BeginTx failed: %v", err)
		return
	}
	pending := 0
	syncedCount := 0

	commit := func() {
		if tx == nil {
			return
		}
		if err := tx.Commit(); err != nil {
			logger.Errorf("history sync: Commit failed (rolling back): %v", err)
			_ = tx.Rollback()
		}
		tx = nil
		pending = 0
	}
	maybeFlush := func() {
		if pending >= batchSize {
			commit()
			t, err := messageStore.BeginTx(ctx)
			if err != nil {
				logger.Errorf("history sync: re-BeginTx failed: %v", err)
				return
			}
			tx = t
		}
	}

	for _, conversation := range historySync.Data.Conversations {
		if conversation.ID == nil {
			continue
		}
		chatJID := *conversation.ID
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}
		// #244: normalize LID chat to its PN equivalent so history sync
		// writes into the same row as live messages.
		jid = resolveToPN(client, jid)
		chatJID = jid.String()

		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		messages := conversation.Messages
		if len(messages) == 0 {
			continue
		}
		latestMsg := messages[0]
		if latestMsg == nil || latestMsg.Message == nil {
			continue
		}
		ts := latestMsg.Message.GetMessageTimestamp()
		if ts == 0 {
			continue
		}
		chatTS := time.Unix(int64(ts), 0).UTC()

		if tx != nil {
			_ = messageStore.StoreChatTx(tx, chatJID, name, chatTS)
		}
		pending++
		maybeFlush()

		for _, msg := range messages {
			if msg == nil || msg.Message == nil {
				continue
			}

			var content string
			if msg.Message.Message != nil {
				if conv := msg.Message.Message.GetConversation(); conv != "" {
					content = conv
				} else if ext := msg.Message.Message.GetExtendedTextMessage(); ext != nil {
					content = ext.GetText()
				}
			}

			var mediaType, filename, url string
			var mediaKey, fileSHA256, fileEncSHA256 []byte
			var fileLength uint64
			if msg.Message.Message != nil {
				mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
			}

			if verbose {
				logger.Infof("Message content: %v, Media Type: %v", content, mediaType)
			}

			if content == "" && mediaType == "" {
				continue
			}

			var sender string
			isFromMe := false
			if msg.Message.Key != nil {
				if msg.Message.Key.FromMe != nil {
					isFromMe = *msg.Message.Key.FromMe
				}
				if !isFromMe && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
					// #244: normalize per-message participant to PN as well.
					sender = resolveToPNStr(client, *msg.Message.Key.Participant)
				} else if isFromMe {
					sender = client.Store.ID.User
				} else {
					sender = jid.User
				}
			} else {
				sender = jid.User
			}

			msgID := ""
			if msg.Message.Key != nil && msg.Message.Key.ID != nil {
				msgID = *msg.Message.Key.ID
			}

			ts := msg.Message.GetMessageTimestamp()
			if ts == 0 {
				continue
			}
			msgTS := time.Unix(int64(ts), 0).UTC()

			if tx == nil {
				continue
			}
			// N1: marshal the inner *waE2E.Message so history-sync rows can also
			// support forward_message / poll voting / re-download later.
			var hsRawProto []byte
			if msg.Message != nil && msg.Message.Message != nil {
				if b, mErr := proto.Marshal(msg.Message.Message); mErr == nil {
					hsRawProto = b
				}
			}
			// History sync never passes through events.Message, so nothing has
			// unwrapped this yet - the envelope is still on the proto and we
			// detect it directly.
			err = messageStore.StoreMessageTx(tx,
				msgID, chatJID, sender, content, msgTS, isFromMe,
				mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, hsRawProto,
				isViewOnceProto(msg.GetMessage().GetMessage()),
			)
			if err != nil {
				logger.Warnf("Failed to store history message: %v", err)
				continue
			}
			syncedCount++
			pending++
			if verbose {
				if mediaType != "" {
					logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
						msgTS.Format("2006-01-02 15:04:05"), sender, chatJID, mediaType, filename, content)
				} else {
					logger.Infof("Stored message: [%s] %s -> %s: %s",
						msgTS.Format("2006-01-02 15:04:05"), sender, chatJID, content)
				}
			}
			maybeFlush()
		}
	}
	commit() // final batch

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
}

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Local rand.Rand seeded per call. rand.Seed is deprecated since Go 1.20
	// and the global source isn't safe under concurrent audio sends - two
	// goroutines calling rand.Float64 simultaneously could race on the
	// shared state. A local Rand is both correct and free.
	r := rand.New(rand.NewSource(int64(duration)))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (r.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
