# WhatsApp Protocol vs App UI

A reference for what the WhatsApp *protocol* (as exposed by whatsmeow) allows that the
*app UI* does not. For the repo owner and future Claude sessions. Practical, not academic.

whatsmeow version underpinning every source citation below:
`v0.0.0-20260622185415-5f04eac6dbbb`. Line numbers drift a few lines between patch
versions; treat them as "look near here", not exact.

---

## The governing insight

**WhatsApp enforces almost everything CLIENT-SIDE. The server rarely checks.**

So when "the app won't let me" do something, that almost always means *the app won't let
me* - not *this is impossible*. The rules that stop you are usually rendering conventions
and composer restrictions baked into the official client, not validation on Meta's server.

A different, honest client (this bridge) can emit the wire message directly and skip the
composer's rules. Whether the *result* is meaningful then depends on two things the source
code cannot prove:

1. Does Meta's server accept the stanza?
2. Does the *recipient's* client render it the way you hoped?

The one hard server-side exception found so far is **incoming view-once** (and it was
patched Nov 2024 - see caveats). A short list of the other genuinely server-enforced
things is in "Where the server DOES enforce" near the end. That short list matters more
than the long one.

## How to read the confidence marker

Every finding carries one. Never mistake "the proto field exists" for "this works".

| Marker | Meaning |
|---|---|
| **[VERIFIED]** | Observed working end-to-end (live test or run against the real module). Trust it. |
| **[SOURCE]** | Proven from code: the field/method exists and the bridge/library will emit it. Says nothing about whether the server accepts it or the recipient renders it. |
| **[SPECULATIVE]** | The wire capability is real but the *effect* (server acceptance and/or recipient rendering) is untested and may not work. |

MCP status per finding:

- **EXPOSED** - reachable through an MCP tool today.
- **BRIDGE_ONLY** - the bridge has partial plumbing but no MCP tool, or it drops the result.
- **NOT_IMPLEMENTED** - would need Go work in the bridge plus a new tool.

---

## Worked example: the poll-cap saga

This is the perfect illustration of the governing insight, so it goes first.

`PollCreationMessage.selectableOptionsCount` (proto field 4) is a plain `uint32`. You would
think setting it to 2 caps voters at two selections. It does not.

The app never even exposes a number. Its poll composer has a single **boolean** toggle,
"Allow multiple answers":

- Off  -> `selectableOptionsCount = 1`, rendered as radio buttons ("Select one").
- On   -> rendered as checkboxes ("Select one or more").

There is no app path to author a cap of exactly 2. And when you force cap=2 on the wire, the
app reads it as "greater than 1" and draws checkboxes with no numeric limit at all.

What actually happened in a live test **[VERIFIED]**: a cap-2 poll, 5 real users, each ticked
all 3 options.

- Every client accepted all 3 ticks per user. The cap was ignored.
- The two phones observing disagreed with each other: one showed a `1 / 3 / 3` tally, another
  showed `2 / 3 / 2`, while the true wire state was `5 / 5 / 5`.
- **The bridge was the only accurate observer**, because it counts the decrypted
  `PollUpdateMessage`s directly instead of trusting a phone's local rendering.

Why the cap cannot be forced:

- The vote payload `PollVoteMessage.selectedOptions` (field 1) is an **unbounded repeated**
  field. No cardinality constraint anywhere in `waE2E`.
- Votes are **E2E-encrypted** (`EncryptPollVote`, `msgsecret.go:348`). The server sees only
  ciphertext, so it *cannot* count selections even if it wanted to.
- The only reason cap=1 "works" is that the honest client draws radio buttons - a UI-level
  mutual exclusion. Nothing validates the payload. A modified client (including this bridge's
  own `vote_in_poll`) can submit multiple hashes against a cap=1 poll and nothing server-side
  stops it.

**Bottom line: only cap=1 is genuinely enforced, and only against people using the real app.**
A cap of N>1 is cosmetic. If you truly need to constrain choice, use cap=1 or decompose into
N separate cap=1 polls.

> The tool docstrings in `tools.py` (`create_poll` ~740-753, `vote_in_poll` ~1006-1013)
> currently claim a cap of N "holds everyone on the normal app to N". **That is false for
> N>1** and should be corrected: only cap=1 constrains app users; any cap>1 renders as
> "Select one or more" and is not enforced; tallies can disagree between phones. The
> docstring's "Verified" line only ever proved the bridge *ignores* the cap, not that the
> app *honours* it.

---

## Polls

| # | Capability | App | Protocol | Conf | MCP |
|---|---|---|---|---|---|
| 1 | Force a selection cap of N>1 | boolean toggle only | impossible; cap=1 is the only binding value | VERIFIED | EXPOSED (cosmetic) |
| 2 | Cap > option count -> silent cap=0 | n/a | whatsmeow resets to 0, not a clamp | VERIFIED | EXPOSED (footgun) |
| 3 | Quiz mode (right answer / score) | none | `PollType` enum + `CorrectAnswer` field | SOURCE | NOT_IMPLEMENTED |
| 4 | Poll expiry / anonymous / add-option | none | `EndTime`, `HideParticipantName`, `AllowAddOption` | SOURCE | NOT_IMPLEMENTED |
| 5 | Fabricated result snapshot | none | plaintext `PollResultSnapshotMessage` | SOURCE | NOT_IMPLEMENTED |
| 6 | Edit a poll after sending | none | `POLL_EDIT` machinery (receive-only in whatsmeow) | SOURCE | NOT_IMPLEMENTED |
| 7 | Retract a vote (empty selection) | deselect in app | empty `selectedOptions` is a full retraction | SOURCE | EXPOSED |
| 8 | Duplicate/whitespace option collisions | app allows dup text | options keyed by `sha256(name)` only | VERIFIED | EXPOSED (footgun) |
| 9 | Unbounded option/question length | 255 / 100 char caps | proto strings unbounded | SOURCE | EXPOSED |

### 1. The cap - see the saga above. **[VERIFIED, EXPOSED]**
`create_poll(recipient, name, options, selectable_options_count=1)` is the only cap that
binds. Any other value is cosmetic. Even cap=1 is client-side trust.

### 2. Cap larger than option count silently becomes 0. **[VERIFIED, EXPOSED]**
`BuildPollCreation` (whatsmeow `msgsecret.go:328-330`) clamps:
`if selectableOptionCount < 0 || selectableOptionCount > len(optionNames) { selectableOptionCount = 0 }`.
The bridge only guards the low end (`if sel < 1 { sel = 1 }`, main.go ~4382). So
`create_poll(options=[a,b,c], selectable_options_count=99)` sends `selectableOptionsCount=0`
on the wire - a value nobody asked for. Verified live: requested `-1/0/4/99` all -> wire 0;
`1/2/3` -> `1/2/3`; 100 options with sel=50 passes unclamped.
**Guidance: keep the value between 1 and len(options), or just pass 1.** What the app renders
for 0 is unproven and may differ across platforms; the local DB also records `selectable_count=0`
and `get_poll_results` would report it as if meaningful.

### 3. Quiz polls. **[SOURCE, NOT_IMPLEMENTED]**
`PollCreationMessage.PollType` (field 7, enum `POLL=0`/`QUIZ=1`) and `CorrectAnswer` (field 8)
exist; `PollResultSnapshotMessage.PollType` (field 4) implies results rendering is quiz-aware.
A QUIZ is semantically single-answer - the one construct where cap=1 would be a *semantic*
property rather than a UI accident. **Untested**: these fields most likely serve Channels /
Meta AI surfaces, not 1:1/group chats. A recipient app may fall back to a plain poll or show
nothing, and `correctAnswer` ships in **plaintext** to every recipient, so it is not secret
even if it renders. Would need Go work (hand-build `PollCreationMessage`; `BuildPollCreation`
never sets these).

### 4. Expiry, anonymity, add-option. **[SOURCE, NOT_IMPLEMENTED]**
`EndTime` (field 9), `HideParticipantName` (field 10), `AllowAddOption` (field 11).
`AllowAddOption` is corroborated by a whole wired transport
(`SecretEncryptedMessage_POLL_ADD_OPTION = 5`, decryptable by whatsmeow), so it is a real
feature somewhere. **All three are unproven end-to-end and are very likely client-side display
hints.** In particular `hideParticipantName` almost certainly does NOT hide anything
cryptographically - the voter's sender JID is on the message envelope regardless. Treat it as
a rendering request, never as anonymity. `endTime` is likewise unlikely to be server-enforced;
a modified client could vote past it.

### 5. Fake result snapshot. **[SOURCE, NOT_IMPLEMENTED]**
`PollResultSnapshotMessage` sits directly on `Message` (field 88; V3 field 115). It carries
`Name`, plaintext `PollVotes` (each is `OptionName` + `OptionVoteCount`), `ContextInfo`,
`PollType`. Note what is absent: no encKey, no msgsecret, no voter identities, no link to any
`PollUpdateMessage`. It is a bare, unencrypted assertion "option X has N votes" - the opposite
of a real vote. Any sender can state any counts. **Likeliest of all these to simply not render**
in a 1:1/group chat (probably a Channels construct). Do not assume you can fabricate a
convincing tally until someone sends one and looks.

### 6. Poll editing. **[SOURCE, NOT_IMPLEMENTED]**
Two independent signals it is a live feature: `SecretEncryptedMessage_POLL_EDIT` (type 4,
`EncSecretPollEdit`), and `PollUpdateMessageMetadata.LastEditStanzaID` (field 2) next to
`PollNameHash` (field 1) - every vote carries a pointer to the poll's last edit, which only
makes sense if polls get edited. **whatsmeow is receive-only**: it can `DecryptSecretEncryptedMessage`
for `POLL_EDIT` but ships no `EncryptPollEdit`/`BuildPollEdit`. Practical hole today: because
the bridge ignores `POLL_EDIT`, an edited poll silently keeps its stale question/options in the
local DB, and votes referencing a new `lastEditStanzaID` are recorded against the old text.

### 7. Vote retraction. **[SOURCE, EXPOSED]**
`vote_in_poll(..., option_names=[])` retracts. A `PollUpdateMessage` carries the voter's
*complete current selection*, not a delta, so an empty selection is a full retraction - no
separate "retract" type exists. Corollary: the wire exposes no vote history; each update
overwrites. The bridge logs history itself via DELETE-then-INSERT (`StorePollVote`,
main.go ~617-648) and stores its own votes by recomputing hashes locally, so a retraction
updates local state even if delivery fails.

### 8. Option hashing collisions. **[VERIFIED, EXPOSED]**
`HashPollOptions` is a pure `sha256.Sum256([]byte(option))` - no poll ID, no index, no salt.
Consequences, verified by running it:
- Duplicate option text -> one indistinguishable hash. `"Yes"` and `"Yes"` both hash to
  `85a39ab3...`. Nobody, including the honest app, can tell the votes apart.
- Option **order carries no wire meaning** - a vote is a bare digest; index is presentational.
- The same text yields the same digest in **every poll ever** (`"Pizza"` collides across
  unrelated polls). The hash protects nothing; only the surrounding `EncryptPollVote`
  ciphertext does.
- Whitespace/case are significant: `" Yes"` (`043e8d9b...`) != `"Yes"`, `"yes"` (`8a798890...`)
  != `"Yes"`. `vote_in_poll` option names must match byte-for-byte.

The bridge inherits the collision: PK `(message_id, chat_jid, option_hash)` + `INSERT OR REPLACE`,
so a duplicate-text poll stores ONE row (last index wins). Not a bridge bug - a property of
WhatsApp's design.

### 9. Length / count limits. **[SOURCE, EXPOSED]**
`Name` (field 2), `Option.OptionName` (field 1) are unbounded proto strings; `Options`
(field 3) is unbounded repeated. whatsmeow adds no ceiling - a 5000-char option marshalled
cleanly (5058 bytes), 100 options passed with no clamp. The bridge only checks
`len(options) >= 2` (main.go ~4373). **Marshalling is NOT delivery**: whether the server
accepts an over-length option or a 13th option is unproven; recipient apps may truncate or
fail to render. Worth one cheap test against your own second device before relying on it.
Forwarding a poll is also impossible in any shared-tally sense: each poll mints a fresh 32-byte
`msgSecret` and votes bind to that creation message's key, so any copy is a disjoint poll.
That is cryptographic, not a UI choice.

---

## Media

| # | Capability | App | Protocol | Conf | MCP |
|---|---|---|---|---|---|
| 1 | Any audio sent as a voice note (PTT) | mic-record only | `PTT` is a sender-set bool; bridge hardcodes `true` | SOURCE (send) / SPECULATIVE (render) | EXPOSED |
| 2 | Send a normal audio FILE | app does it | expressible, but bridge sends `.mp3` as a document | SOURCE | NOT_IMPLEMENTED |
| 3 | Fake voice-note duration / waveform | derived from recording | `Seconds`/`Waveform` sender-set, not hashed | SOURCE | NOT_IMPLEMENTED |
| 4 | Thumbnail != real image | derived from image | `JPEGThumbnail` inline, not bound to media hash | SOURCE | NOT_IMPLEMENTED |
| 5 | GIF-playback on a long video | toggle on short clips only | `GifPlayback` bool, no length tie | SOURCE (field) / SPECULATIVE (render) | NOT_IMPLEMENTED |
| 6 | Stamp video as GIPHY/TENOR sourced | set by picker | `gifAttribution` free enum | SOURCE | NOT_IMPLEMENTED |
| 7 | Re-send media with no re-upload | forward is by-ref | url+mediaKey is a bearer token | SOURCE | EXPOSED |
| 8 | Sticker flags (animated/avatar/AI/lottie) | set by pack/avatar | all sender-set, never set by bridge | SOURCE | BRIDGE_ONLY |
| 9 | Caption an audio or sticker | no caption UI | **no such proto field - genuinely impossible** | SOURCE | n/a |
| 10 | File-size ceiling | ~16MB/2GB by type | whatsmeow imposes none; our 64 MiB is self-inflicted | SOURCE | EXPOSED |

### 1. Any audio as a "voice note". **[SOURCE send / SPECULATIVE render, EXPOSED]**
`AudioMessage.PTT` (field 6) is a plain optional bool that describes nothing about how the
audio was produced. The bridge **hardcodes** `PTT: proto.Bool(true)` (main.go:1260) for any
opus upload, with no arg to disable it. `send_audio_message(recipient, media_data=<b64>,
filename="x.mp3")` transcodes to opus and forces PTT=true.
**What is proven:** the outbound proto sets PTT=true. **What is NOT proven:** that a recipient
actually sees a voice-note bubble, or that a long file (the "10-minute podcast you never
recorded") is accepted by the server and rendered as PTT rather than a normal audio attachment.
Server acceptance and client rendering were not tested.

### 2. The bridge cannot send a normal audio file. **[SOURCE, NOT_IMPLEMENTED]**
The send-side extension switch (main.go:1151) has exactly one audio case: `case "ogg"`. mp3/m4a/
opus fall through to `default:` -> `MediaDocument`. Combined with the hardcoded PTT=true, **no
code path emits an `AudioMessage` with PTT=false.** So an `.mp3` sent via `send_file` today
silently becomes a `DocumentMessage` - recipient sees a file card, not a play button. That is
our gap, not WhatsApp's; the app happily sends non-PTT audio. Fix: add mp3/m4a/opus to the
switch and thread a `ptt bool` to replace the hardcoded `proto.Bool(true)`.

### 3. Fake duration / waveform. **[SOURCE, NOT_IMPLEMENTED]**
`AudioMessage.Seconds` (field 5) and `Waveform` (field 19) are sender-populated and NOT covered
by `fileSHA256` (field 3), which hashes only the media bytes. A 3-second clip can declare
`Seconds=600` and ship an arbitrary 64-byte waveform. The bridge computes real values via
`analyzeOggOpus` and exposes no override. **Effect unproven** - recipient clients plausibly
re-derive duration from the opus stream on play. Do not assume a false `Seconds` survives.

### 4. Thumbnail that differs from the image. **[SOURCE, NOT_IMPLEMENTED]**
`ImageMessage.JPEGThumbnail`, `VideoMessage.JPEGThumbnail`, `DocumentMessage.JPEGThumbnail`
(all field 16) are inline JPEG bytes in the encrypted body. `ImageMessage.FileSHA256`
(**field 4** - not 3) covers only the uploaded media blob, not the thumbnail. `ThumbnailSHA256`
(field 27) hashes a *separately* uploaded thumbnail at `thumbnailDirectPath` (field 26), and
also does not bind the inline thumbnail to the full image. So the bubble preview and the
tapped-through image are two independent sender-chosen payloads. `grep JPEGThumbnail main.go` =
zero hits - the bridge never sets it, which also means **images we send today may show a blank
preview until downloaded** (a live cosmetic bug). Whether a recipient renders a mismatched
preview or re-renders from the full image on tap is untested.

### 5. gifPlayback on any-length video. **[SOURCE field / SPECULATIVE render, NOT_IMPLEMENTED]**
`VideoMessage.GifPlayback` (field 8) is an unconstrained bool with no tie to `Seconds` (field 5)
or file length. The app only offers the GIF toggle on short clips (roughly under ~6s, version-
dependent - treat as approximate). So the field is settable on a long video, **but** whether
the server accepts a multi-minute `gifPlayback=true` and whether the recipient autoplays it on
silent loop is untested and may well be gated. The short-clip case is app-exposed, so only the
long-video case is even a gap.

### 6. gifAttribution. **[SOURCE, NOT_IMPLEMENTED]**
`VideoMessage.gifAttribution` (field 19) is a free enum: `NONE=0`, `GIPHY=1`, `TENOR=2`,
`KLIPY=3`. Sender-asserted provenance with no proof. Low impact - most clients likely ignore it
for rendering (it may only drive an attribution badge in the GIF search UI). Do not claim a
visible effect. (For the record, the checked-in `.proto` does contain `KLIPY=3`; it is in sync
with the generated Go.)

### 7. Re-send media with no re-upload. **[SOURCE, EXPOSED]**
WhatsApp media blobs are AES-encrypted ciphertext at rest; the server never sees plaintext and
cannot bind a blob to a conversation. Anyone holding `url`/`directPath` + `mediaKey` can
reference it in a fresh message to a different chat. `forward_message(source_chat_jid,
message_id, target_jid)` already does exactly this: `/api/forward` loads the stored `raw_proto`,
`proto.Unmarshal`s it, and passes it straight to `SendMessage` - the URL/mediaKey/directPath
ride along verbatim, no `Upload` on that path. Requires `raw_proto` present in the DB (older
history-synced rows fall back to text-only). Caveats: media URLs expire server-side (time window
unmeasured); and the bridge **hardcodes** `IsForwarded=true`/`ForwardingScore=1`, so re-sending
media *without* the forwarded label, or with a chosen score, is NOT_IMPLEMENTED without Go work.

### 8. Sticker flags. **[SOURCE, BRIDGE_ONLY]**
`StickerMessage` carries `IsAnimated` (13), `IsAvatar` (19), `IsAiSticker` (20), `IsLottie` (21),
`Emojis` (25), `AccessibilityLabel` (22), `Premium` (24) - all sender-set, none proven by the
server. `send_sticker` exists and reaches `/api/send_sticker`, but the bridge sets only
url/directPath/mediaKey/mimetype/hashes/length (and omits Width/Height); `grep IsAnimated main.go`
= zero. So the docstring's "WhatsApp accepts static or animated WebP" is **unverified and suspect**
precisely because `IsAnimated` is never set - an animated WebP may render as a frozen first frame.
Do not repeat that docstring as fact. (Aside: whatsmeow keys stickers off `MediaImage`, so an
image upload and a sticker upload are key-compatible - the same blob can be referenced as either.)

### 9. Captioning audio/stickers - genuinely impossible. **[SOURCE, closed]**
This one is NOT a gap you can exploit. `AudioMessage` and `StickerMessage` have **no Caption
field at any number**. Only `ImageMessage` (Caption field 3), `VideoMessage` (field 7) and
`DocumentMessage` (field 20) carry captions, and the app exposes all three. A field absent from
the struct cannot be serialized, so this is a real negative capability. The closest reachable
approximation is a separate text message. `AudioMessage.AccessibilityLabel` (22) and
`StickerMessage.AccessibilityLabel`(22)/`Emojis`(25) are sender-set strings but are NOT captions
(likely screen-reader only, untested).

### 10. File size. **[SOURCE, EXPOSED]**
whatsmeow's `Upload` applies no client-side size check (`FileLength` is `uint64`); the real
ceiling is Meta's upload endpoint and we have not measured it. Our own `maxMediaBytes = 64 << 20`
(main.go ~2346, HTTP 413 over it) is **deliberate memory protection**, not an oversight: the
comment explains a larger in-memory upload would spike RSS from ~25MB to ~250MB and risk
OOMKill on a 512MB cgroup. Given the earlier OOM outage, **do not raise this const
without first switching to whatsmeow's streaming `UploadReader`** (io.Reader + temp file). Any
specific server-side ceiling (16MB/100MB/2GB) would be a guess.

---

## Messages

| # | Capability | App | Protocol | Conf | MCP |
|---|---|---|---|---|---|
| 1 | Forge a quoted-reply's content/author | quote real msgs only | `QuotedMessage`/`Participant`/`StanzaID` fully sender-supplied | SOURCE (wire) / SPECULATIVE (render) | BRIDGE_ONLY |
| 2 | Fake link preview | app fetches the real one | preview fields independent of URL | SOURCE (wire) / SPECULATIVE (render) | NOT_IMPLEMENTED |
| 3 | Delete-for-everyone past window / others' msgs | grayed after ~48h, own only | `BuildRevoke` has no time gate; takes any sender JID | SOURCE | EXPOSED |
| 4 | Edit past the ~15 min window | Edit hidden after ~15 min | `EditWindow` const is never enforced | SOURCE | EXPOSED |
| 5 | Custom disappearing timer | Off/24h/7d/90d only | raw seconds, no 1:1 validation | SOURCE | EXPOSED |
| 6 | @-mention non-members / all | picker = members only | `MentionedJID` unvalidated | SOURCE | EXPOSED |

### 1. Forged quoted-reply (classic "FakesApp" vector). **[SOURCE wire / SPECULATIVE render, NOT_IMPLEMENTED]**
The reply `ContextInfo` carries the quoted message, the quoted participant, and the quoted
message ID as independent, sender-controlled fields that need not reference any real message. So
the wire permits attributing a fabricated quote to someone who never sent it. This is the 2018
Check Point "FakesApp" vector; WhatsApp changed things afterward, and a current client may
validate against the real message ID or show a greyed "original message not available" bubble
instead of rendering the forged body, so the *effect* is unproven regardless.

**Assessed and deliberately not implemented.** Forging attribution deceives a recipient, which
is categorically different from reading data already delivered to us - the whole rest of this
server is the latter. The specific field layout and the one-line bridge change that would emit it
are intentionally omitted from this document. The finding is recorded so the boundary is
documented; the recipe is not, because writing the recipe down is the part that helps an attacker
and helps no legitimate use.

### 2. Fake link preview. **[SOURCE wire / SPECULATIVE render, NOT_IMPLEMENTED]**
`ExtendedTextMessage` carries the preview's title, description, and thumbnail as fields
independent of the URL the preview binds to, so nothing ties the card a recipient sees to where
the link actually goes - the ingredients of a phishing card. The bridge only ever sets the
message text, never the preview fields, and whether a current client renders a mismatched preview
without re-fetching is untested.

**Assessed and deliberately not implemented,** for the same reason as the forged quoted-reply
above: it works by deceiving a recipient. The exact field set is omitted here on purpose. Noting
that the vector exists is documentation; spelling out how to build the card is a phishing
how-to, so it stays out.

### 3. Delete-for-everyone: no client time gate, arbitrary sender. **[SOURCE, EXPOSED]**
`BuildRevoke(chat, sender, id)` builds a `ProtocolMessage{Type:REVOKE}` with **no time-window
check**; passing another user's JID as `sender` is the documented mechanism for a group admin to
revoke someone else's message. `delete_message(chat_jid, message_id, sender_jid=<other JID>,
confirm=True)` already sends the revoke for another JID, and revoking past the app's grayed-out
window is just calling it on an old message ID - **the library imposes zero gate; acceptance is
the server's call.** The server almost certainly rejects revoking a non-own message unless you
are admin, and enforces its own age limit (the tool docstring records an observed ~1h08m window,
far shorter than the app's 48h UI hint - so the *server* window is the real one). Not tested live.

### 4. Edit past the window. **[SOURCE, EXPOSED]**
`BuildEdit` has no timestamp gate. whatsmeow declares `const EditWindow = 20 * time.Minute` but
a full-tree grep shows it is **referenced nowhere** - a hint for callers, never checked. (The
app's ~15 min and the library's 20 min already disagree, underscoring these are advisory.)
`edit_message(chat_jid, message_id, new_text)` on an old message builds and sends regardless;
whether it takes effect is the server's decision. Note `BuildEdit` hardcodes `FromMe=true`, so
this path can only edit **your own** messages.

### 5. Custom disappearing-message timer. **[SOURCE, EXPOSED]**
For **1:1 chats**, `SetDisappearingTimer` sends `ProtocolMessage{Type:EPHEMERAL_SETTING,
EphemeralExpiration:uint32(seconds)}` with the raw value and **no range check**; the bridge
passes `req.Seconds` straight through. So `set_disappearing(chat_jid=<1:1>, seconds=3600)` sets
a 1-hour timer the app never offers (app only has Off/24h/7d/90d). For **groups** the timer goes
via an IQ the server can reject (`ErrInvalidDisappearingTimer`) - groups ARE server-validated,
1:1 is not (at the library layer; and for 1:1 the value rides inside an E2E `ProtocolMessage` the
server cannot inspect). Whether the recipient honours a non-standard duration vs snapping to a
preset is unproven.

### 6. @-mention arbitrary JIDs. **[SOURCE, EXPOSED]**
`ContextInfo.MentionedJID` (field 15) is a plain repeated string with **zero membership check**
in whatsmeow. `send_message(group_jid, 'hey ...', mentioned_jids=[...])` can list non-members or
enumerate every member to "@-mention everyone" (fetch via `list_group_members`). The mention is
cosmetic metadata driving the recipient-side highlight/notification-boost. Mentioning a non-member
only highlights a name to a party who does not receive the group's messages. The @all trick is
real but mention count may be capped/rate-limited server-side (untested), and spraying mentions is
a plausible spam/ban signal.

Also in this domain, **not** a capability: **a message's displayed timestamp is
server-authoritative.** The outgoing stanza carries no client `t`; both sender and recipient read
the bubble time from the server's `t` attribute. Payload timestamps that exist
(`ReactionMessage.SenderTimestampMS`, poll `senderTimestampMs`) drive ordering/dedup, not bubble
time. **You cannot backdate or postdate a message to another user's chat. [SOURCE]**

---

## Groups

Groups are unusually honest: nearly everything here is **server-enforced against admin status**.
The gaps below are mostly *automation* gaps (whatsmeow has the call, the bridge lacks a tool),
not app-hidden capabilities.

| # | Capability | Protocol | Conf | MCP |
|---|---|---|---|---|
| 1 | Edit name/topic/photo/description | `SetGroupName/Topic/Photo/Description` | SOURCE | NOT_IMPLEMENTED |
| 2 | Permission toggles (announce/locked/approval/add-mode) | `SetGroupAnnounce/Locked/JoinApprovalMode/MemberAddMode` | SOURCE | NOT_IMPLEMENTED |
| 3 | Approve/reject pending join requests | `Get/UpdateGroupRequestParticipants` | SOURCE | NOT_IMPLEMENTED |
| 4 | See per-participant add errors + invite codes | `UpdateGroupParticipants` returns `[]GroupParticipant` | SOURCE | BRIDGE_ONLY |
| 5 | Communities (parent groups, link/unlink, rosters) | `CreateGroup{IsParent}`, `Link/UnlinkGroup`, `GetSubGroups` | SOURCE | NOT_IMPLEMENTED |
| 6 | Join via embedded invite message | `GetGroupInfoFromInvite` / `JoinGroupWithInvite` | SOURCE | NOT_IMPLEMENTED |

### 1. Metadata edits. **[SOURCE, NOT_IMPLEMENTED]**
whatsmeow ships `SetGroupName`, `SetGroupTopic(jid,prevID,newID,topic)`, `SetGroupPhoto(jid,jpeg)`
(nil bytes removes it), `SetGroupDescription` (group.go ~303/335/348/1067). All send an iqSet the
server validates against admin rights. The bridge's group endpoints are only
create/participants/info/joined/invite_link/join/leave. `SetGroupPhoto` needs JPEG bytes or returns
`ErrInvalidImageFormat`.

### 2. Permission toggles. **[SOURCE, NOT_IMPLEMENTED]**
`SetGroupAnnounce` (only-admins-can-send), `SetGroupLocked` (edit-info admins-only),
`SetGroupJoinApprovalMode`, `SetGroupMemberAddMode('admin_add'|'all_member_add')` - each an iqSet
the server validates against admin status. These can also be set at creation via `ReqCreateGroup`
(`GroupAnnounce`/`GroupLocked`/`GroupMembershipApprovalMode`), which the bridge's `create_group`
ignores.

### 3. Join-request approval. **[SOURCE, NOT_IMPLEMENTED]**
`GetGroupRequestParticipants(jid)` returns `[]{JID, RequestedAt}`;
`UpdateGroupRequestParticipants(jid, jids, 'approve'|'reject')`. Only meaningful when join-approval
mode is on and you are admin.

### 4. Per-participant add errors + auto-invite. **[SOURCE, BRIDGE_ONLY]**
`UpdateGroupParticipants` add returns `[]GroupParticipant`, each with an `Error int` and, when a
direct add is refused (privacy/blocklist/blocked-you), an `AddRequest{Code, Expiration}` - a
personal invite the target can accept via `JoinGroupWithInvite`. The bridge **ignores the returned
slice entirely** (only checks `err`), so today the codes and per-add failures are dropped. The app
silently converts this to "couldn't be added, so an invite was sent". `JoinGroupWithInvite` is
itself unexposed, so the code cannot be acted on through the bridge yet.

### 5. Communities. **[SOURCE, NOT_IMPLEMENTED]**
`CreateGroup` with `GroupParent{IsParent:true}` makes a community (server auto-creates the
announcement subgroup); `GroupLinkedParent.LinkedParentJID` creates a group inside one;
`LinkGroup`/`UnlinkGroup` attach/detach; `GetSubGroups`; `GetLinkedGroupsParticipants` (union of
members). The bridge's `create_group` takes only `(subject, participants)` and cannot set
`IsParent`. Server enforces admin/ownership.

### 6. Join via embedded invite message. **[SOURCE, NOT_IMPLEMENTED]**
Distinct from the `chat.whatsapp.com` link path the bridge already has. When an admin sends an
invite *as a message* (common for approval-required or link-disabled groups),
`GroupInviteMessage` carries `groupJID`(1), `inviteCode`(**2**), `inviteExpiration`(**3**),
`groupName`(4), `caption`(6), `groupType`(8, enum DEFAULT/PARENT). `GetGroupInfoFromInvite` previews
it; `JoinGroupWithInvite(jid, inviter, code, expiration)` accepts it. Codes expire. The accept
stanza being sent does not guarantee an immediate join - approval-required groups may still gate it.

---

## Presence and receipts

This is where you get the most reliable *quiet* leverage, because presence and read receipts are
emitted only by explicit calls - viewing content emits nothing by itself.

| # | Capability | App | Protocol | Conf | MCP |
|---|---|---|---|---|---|
| 1 | Read with zero blue ticks, fire receipts on demand | global toggle; opening a chat marks read | read receipt only from explicit `MarkRead` | SOURCE | EXPOSED |
| 2 | Force a per-message blue tick while global is OFF | n/a - impossible | `MarkRead` downgrades to `read-self` | SOURCE | EXPOSED |
| 3 | Read/act with no online/last-seen footprint | using app emits presence | no `SendPresence` = no online, no last-seen advance | SOURCE (core) / SPECULATIVE (sub-claims) | EXPOSED |
| 4 | Log a contact's online/last-seen timeline | transient, unrecorded | `SubscribePresence` streams `events.Presence` | SOURCE | NOT_IMPLEMENTED |
| 5 | Emit typing/recording on demand | only while composing | `SendChatPresence` standalone chatstate | SOURCE | EXPOSED |

### 1. Read silently, tick on demand. **[SOURCE, EXPOSED]**
A read receipt is emitted **only** by an explicit `MarkRead` node (receipt.go:189). Merely
receiving/viewing a message emits an automatic *delivery* receipt, never a read receipt - there is
no coupling between "I opened the content" and "I sent a read receipt". `list_messages`/`get_message`
read from local SQLite and send nothing. `mark_read(chat_jid, [chosen ids], sender_jid?)` fires a
blue tick for ONLY those IDs. **The default MCP posture is total read-receipt suppression.** Caveat:
this only covers the MCP side - your *phone* still auto-marks read when you open the chat there.

### 2. You cannot force a tick while global receipts are OFF. **[SOURCE, EXPOSED]**
`MarkRead` inspects `GetPrivacySettings().ReadReceipts`; when it is `PrivacySettingNone` (or the
chat is a newsletter) it rewrites the receipt type from `read` to `read-self`, which syncs only to
your own devices and is never delivered to the sender. So the control is **asymmetric**: you can
*withhold* a tick per message (just don't call `mark_read`), but you cannot *grant* one per message
against a global "off". To make any blue tick work, global read receipts must be ON.
`get_privacy_settings()` reports the current value.

### 3. Read/act with no presence footprint. **[SOURCE core / SPECULATIVE sub-claims, EXPOSED]**
Online status and last-seen advancement are driven **entirely** by `SendPresence(available/
unavailable)` (presence.go:64). `grep` finds **no `SendPresence` call anywhere in the bridge**, so
all MCP read/act operations emit no online presence and do not advance last-seen *from this client*
- proven at source. Two sub-claims are only SPECULATIVE (server/recipient-side, untested):
(a) as a side effect the client auto-acks incoming with delivery type `inactive`, which the official
app supposedly does not render as the second gray tick - but on a live multi-device account your
*phone* still sends ACTIVE receipts, so the sender still sees "delivered"; (b) the exact server-side
last-seen guarantee. To *deliberately* fake "online" you would call `SendPresence(PresenceAvailable)`
on a timer - not wired to any tool. (Separately, the `send_presence` tool DOES emit a typing/chatstate
presence - a different presence type, see #5.)

### 4. Log a contact's presence timeline. **[SOURCE, NOT_IMPLEMENTED]**
`SubscribePresence(jid)` (presence.go:97) asks the server to stream `events.Presence`, each carrying
`From`, `Unavailable` (offline), `LastSeen`. Persisting these builds an activity/sleep-schedule log
the app never retains. Would need Go work (endpoint + event handler + read-back tool). **Important
tension:** whatsmeow's own doc says the server only streams others' presence if *you* are first
marked online via `SendPresence(PresenceAvailable)` - which directly breaks the zero-footprint
invisibility of #3 (you become visible and advance your own last-seen to watch them). Also needs a
privacy token, and `LastSeen` comes back as the zero value if the target hides last-seen (WhatsApp is
roughly reciprocal).

### 5. Typing / recording on demand. **[SOURCE, EXPOSED]**
`SendChatPresence(jid, composing|paused, media)` sends a standalone chatstate unrelated to any draft;
"recording" is `composing` with `media='audio'`. `send_presence(chat_jid, state='composing')` shows
"typing..."; add `media='audio'` for "recording audio..."; `state='paused'` clears it. Recipient
clients auto-expire a composing indicator after ~25s, so indefinite typing needs re-sends on a timer.
Whether a recipient reliably renders a chatstate from a companion device (vs the phone) is unverified.

---

## Rich / business message types

The pattern across this whole domain: `client.SendMessage(ctx, to, *waE2E.Message)` (send.go:185)
has **no field allow-list** - it E2E-encrypts and transmits whatever `Message` you build. So the
protocol/library layer is never what stops you. The block is **entirely server + recipient-side**,
and this is one of the few areas Meta HAS historically enforced server-side against non-Business
senders. **Default expectation: likely stripped or blank, pending a real test to a controlled
recipient.** None of these are exposed by the bridge.

| # | Type (Message field) | Conf | Note |
|---|---|---|---|
| 1 | Buttons (42) / List (36) | SPECULATIVE | Worked via Baileys ~2020-21; WhatsApp disabled for non-Business; ~2023+ often blank bubble. |
| 2 | Interactive (45): NativeFlow/carousel/CTA-URL | SPECULATIVE | Modern rich envelope; some subtypes reportedly still render in certain builds. |
| 3 | Template (25): hydrated 4-row + URL/call/quick-reply buttons | SPECULATIVE | Inbound rendering proven only for Business-API senders (DHL/banks/OTP). Receive != send. |
| 4 | Payments (16/22/23/24/44/124/125) | SPECULATIVE | Region-locked, KYC-backed rails. High ban-risk, low reward. |
| 5 | Product (30) / Order (38) | SPECULATIVE | Fields reference a business catalog/order the account does not own; a forged card is unlikely to be meaningful even if it reaches the recipient. |

All NOT_IMPLEMENTED. The bridge only ever *decodes* these inbound for their text (buttons/list/
template/interactive body); it never builds any of them, and `tools.py` has no tool for them. Any of
them would be a new Go endpoint plus a tool - trivial to construct the proto, but "the field exists"
and "whatsmeow will send it" both being true does NOT imply the message renders. Settle it only by
sending a live test to a controlled recipient and screenshotting the result.

---

## Profile, channels, metadata

| # | Capability | App | Protocol | Conf | MCP |
|---|---|---|---|---|---|
| 1 | Write privacy settings; 3 extra categories | one screen at a time | `SetPrivacySetting`, 10 categories | SOURCE | NOT_IMPLEMENTED |
| 2 | Channel write/interact suite | follow/react/mute/create | Follow/Unfollow/Mute/SendReaction/MarkViewed/Create | SOURCE | NOT_IMPLEMENTED |
| 3 | Fetch any channel's full history + media keys | must open/follow | `GetNewsletterMessages`, content is plaintext | SOURCE (core) / SPECULATIVE (without following) | NOT_IMPLEMENTED |
| 4 | Increment a channel post's view counter | +1 on open | `NewsletterMarkViewed` takes arbitrary server_ids | SOURCE | NOT_IMPLEMENTED |
| 5 | Set avatar from raw JPEG (no square crop) | forced crop + ~640px | `SetGroupPhoto(ownJID, bytes)`, no client processing | SOURCE (client) / SPECULATIVE (server keeps it) | NOT_IMPLEMENTED |
| 6 | About text with no length cap | 139 char cap | `SetStatusMessage`, no client cap | SOURCE | EXPOSED |
| 7 | Media status / per-post audience | app does both | status accepts any Message; whitelist honored not settable | SOURCE | BRIDGE_ONLY |
| 8 | React with any string (multi-emoji/text) | one emoji only | `ReactionMessage.Text` free-form | SOURCE (send) / SPECULATIVE (render) | EXPOSED |
| 9 | Choose the message ID | auto-generated | `SendRequestExtra.ID` verbatim | SOURCE | NOT_IMPLEMENTED |
| 10 | Inflate "Forwarded many times" score | auto from hop count | `forwardingScore` any uint32 | SOURCE | NOT_IMPLEMENTED |
| 11 | Labels on a consumer account; star messages | no labels UI (Business only) | app-state mutations | SOURCE (machinery) / SPECULATIVE (labels work on consumer) | EXPOSED |

### 1. Privacy write. **[SOURCE, NOT_IMPLEMENTED]**
whatsmeow `SetPrivacySetting` (privacysettings.go:63) issues an iqSet setting any category to any
value. The type table exposes **TEN** categories: the 7 the bridge surfaces read-only
(groupadd, last, status, profile, readreceipts, online, calladd) plus `messages` (all|contacts),
`defense` (on_standard|off), `stickers`. The bridge has **no write path at all**. The standard 7
are exactly what the app writes, so low risk; `messages`/`defense` are newer and could be rejected
on a linked device.

### 2. Channel write/interact suite. **[SOURCE, NOT_IMPLEMENTED]**
whatsmeow ships `FollowNewsletter`, `UnfollowNewsletter`, `NewsletterToggleMute`,
`NewsletterSendReaction`, `NewsletterMarkViewed`, `GetNewsletterMessages`,
`GetNewsletterMessageUpdates`, `CreateNewsletter`. The bridge exposes only list + info (read).
`NewsletterSendReaction` sets the reaction `code` to **any string** - not limited to the app's
channel-reaction palette (server may still normalize/reject off-palette; untested).

### 3. Any channel's full history, unencrypted. **[SOURCE core / SPECULATIVE without-follow, NOT_IMPLEMENTED]**
`GetNewsletterMessages(jid, {Count, Before})` issues a plain iqGet with only the channel JID.
`parseNewsletterMessages` reads each post from a `<plaintext>` node and `proto.Unmarshal`s it
straight into a `waE2E.Message`: **channel content has NO Signal session and NO message-level E2EE.**
Any image/video inside carries its `mediaKey` in that same cleartext proto, so the CDN blob is
trivially decryptable by anyone who fetched the post. **Proven:** the plaintext structure and that
the request carries no membership token. **Unproven:** that the *server* serves history for a channel
you do not follow (plausible since channels are public, but it may rate-limit or require the channel
be discoverable).

### 4. View-counter increment. **[SOURCE, NOT_IMPLEMENTED]**
`NewsletterMarkViewed(jid, []serverID)` sends a `type='view'` receipt over a caller-supplied list of
server IDs; its doc says it increments the view counter and is explicitly "not the same as marking
the channel as read". Decoupled from actually receiving the message. Whether the server dedupes views
per account (capping the effect at +1) is untested - do not claim inflation works without testing.

### 5. Avatar from raw JPEG. **[SOURCE client / SPECULATIVE server, NOT_IMPLEMENTED]**
whatsmeow has no dedicated `SetProfilePicture`; the own-avatar path is `SetGroupPhoto(ctx, ownJID,
jpegBytes)` (group.go:303), which posts the JPEG **verbatim** to `w:profile:picture` with zero
resize/crop/dimension validation (only maps `ErrIQNotAcceptable` -> `ErrInvalidImageFormat`). The app
forces a square crop and ~640px downscale. **Client-side no-processing is certain.** But WhatsApp
serves all profile pictures as small squares, which strongly suggests the *server* re-crops/downscales
- so a non-square or high-res avatar surviving end-to-end is SPECULATIVE and may be silently
re-processed or rejected. No bridge setter exists (only `get_profile_picture`).

### 6. About text length. **[SOURCE, EXPOSED]**
`set_status_message(message=...)` already works and sends the string verbatim (`SetStatusMessage`,
user.go:164) with **no client length cap**; the app caps input at 139 chars. Passing >139 chars or
newlines exercises the gap. The server may itself enforce 139 and truncate/reject - unverified. This
is a validation gap on an already-exposed tool, not a new capability.

### 7. Status: media + per-post audience. **[SOURCE, BRIDGE_ONLY]**
The status broadcast channel accepts any `waE2E.Message` sent to `StatusBroadcastJID`, and
`getStatusBroadcastRecipients` already honours a `StatusPrivacyTypeWhitelist`. But the bridge's
`/api/status/post` **hard-codes** `Message{Conversation: text}` (text only), and whatsmeow ships
`GetStatusPrivacy` but **no `SetStatusPrivacy`** - so the whitelist can be honoured but not set from
the bridge. Media status is plumbing work (build an image/video Message to `StatusBroadcastJID`);
subset targeting only works if the whitelist is already configured in the app. "Who viewed your
status" has no surfaced whatsmeow API.

### 8. Reactions accept any string. **[SOURCE send / SPECULATIVE render, EXPOSED]**
`ReactionMessage.Text` (field 2) is free-form. `BuildReaction` sets it with zero validation; the
bridge passes `req.Emoji` unchecked; `react_to_message` declares `emoji` as a plain `str`.
`react_to_message(chat_jid, message_id, emoji="multiple emoji or text", sender_jid=<author for group
msgs>)`; empty emoji clears **your** reaction. **Proven:** the client/bridge/MCP send whatever string
you give. **Unproven:** that the server accepts non-single-emoji payloads and the recipient renders
them (newer clients may sanitize to one grapheme). You **cannot** remove someone else's reaction -
reactions are keyed by (target message, reacting user = you); `Text=""` only clears your own.

### 9. Caller-chosen message ID. **[SOURCE, NOT_IMPLEMENTED]**
`SendMessage` accepts `SendRequestExtra{ID: <any string>}` and writes it verbatim as the stanza `id`.
`GenerateMessageID` documents the normal `"3EB0"+hex` web format, but the server accepts an arbitrary
id - you could forge that format, mint a Facebook-style numeric id, or attempt to collide an existing
id. The bridge never populates `SendRequestExtra`, so every send uses a random auto-id. One field to
thread through. Whether a colliding id survives (vs being deduped/dropped) is unverified; a well-formed
random id sends fine.

### 10. "Forwarded many times" score. **[SOURCE, NOT_IMPLEMENTED]**
`ContextInfo.forwardingScore` (field 21) is any `uint32`; clients render the double-arrow "Forwarded
many times" label once the score reaches ~4. `isForwarded` (field 22) is an independent bool - you
can flag any message as forwarded, or NOT flag a genuine forward. The bridge hardcodes a
forwarding score of 1 on the forward path, so the "many times" label is unreachable through
`forward_message` today, and it is left that way deliberately. The label is a pure
display-of-a-wire-int (the server cannot compute it - messages are E2E encrypted), so it fits the
client-side-trust pattern, but inflating it deceives a recipient, so the enabling detail is not
written out here.

### 11. Labels + stars on a consumer account. **[SOURCE machinery / SPECULATIVE labels-work, EXPOSED]**
`LabelEdit`, `LabelAssociationChat`, `LabelAssociationMessage`, `StarAction` are **app-state
mutations** - encrypted key-value records synced to your *own* linked devices, never delivered to
other chat participants. **Proven:** the builders, the bridge wiring, and the tools exist
(`edit_label`, `set_chat_label`, `set_message_label`, `star_message`), and star/label are personal
per-account metadata. **Unproven:** that creating/assigning a *label* actually works on a consumer
(non-Business) account - the code attempts the app-state patch, but consumer WhatsApp has no labels UI
on phone or web, so even if the server stores it nothing may render it. Starring is a genuine consumer
feature and is on firmer ground (but was not run live here). This answers "star/label: server or
local?" - **server-persisted personal app-state, not local-only and not shared.**

---

## Where the server DOES enforce

This is the short list, and it matters more than everything above. If a rule is on it, a modified
client cannot get around it.

| Rule | Evidence |
|---|---|
| **Incoming view-once** (the one hard exception; patched Nov 2024 - see caveats) | the original genuinely server-side media-visibility gate |
| **Group admin rights** - every admin op returns `401 not-authorized` / `403 forbidden` from the wire if you are not admin | `errors.go` `ErrIQNotAuthorized`/`ErrIQForbidden`; all group mutations route through `sendGroupIQ(iqSet)`; `GetGroupInviteLink` explicitly branches on the 401 |
| **Group subject length** - >25 chars returns `406 not-acceptable` | `ReqCreateGroup.Name` doc; `ErrIQNotAcceptable`. Rare server-enforced *content* limit. |
| **Group disappearing-timer** validated (unlike 1:1) | group branch wraps `ErrIQBadRequest` -> `ErrInvalidDisappearingTimer` |
| **Message bubble timestamp** - server-stamped `t`, no client field | `prepareMessageNode` sends no `t`; both sides read `ag.UnixTime("t")` |
| **Rich/business message rendering** - Meta has historically stripped Buttons/List/Template/Interactive from non-Business senders | server + recipient-side gate; see that section |
| **Payments** - real KYC-backed rails, region-locked | fabricated payment messages likely rejected/inert |

Why groups are the honest exception: group *state* lives server-side and the server is its only trust
anchor, so it *must* validate. Contrast with polls/reactions/receipts, where the content is E2E
encrypted and the server literally cannot inspect it - all it can do is relay bytes and let the
recipient client decide.

---

## Caveats - read before relying on any of this

- **"Recipient's client honours a flag" is a UI convention, not a guarantee.** Every SPECULATIVE
  finding above hinges on the recipient's app rendering something the way you intend. That is not a
  contract. It varies by app version and platform, and can change without notice. If a capability's
  value depends on what the *other* person sees, treat it as unproven until you screenshot it.
- **WhatsApp patches these.** The client-side-trust posture is a moving target. **Incoming view-once
  bypass was patched around Nov 2024** - the media is no longer downloadable after viewing the way it
  once was. Anything in this document could be closed by a server or client update. Re-verify before
  building on a SPECULATIVE item.
- **Ban risk on unofficial clients.** This bridge is not the official app. High-volume or abusive-
  looking patterns - spraying @all mentions, mass reactions, fabricated payment messages, view-count
  inflation, sending business/interactive types - are plausible spam/abuse signals and carry
  account-risk up to a ban. The account here is the user's own; a ban is a real cost.
- **Source != wire != render.** The recurring trap. "The proto field exists" (SOURCE) and "whatsmeow
  will send it" are both provable from code and both true for most findings here, yet neither implies
  the server accepts it or the recipient renders it. Keep the confidence marker in view.

## A word on ethics

These capabilities operate on the **user's own account and own data** - reading their own chats
quietly, setting their own profile, automating their own groups. That is the default and it is fine.

A few capabilities are different in kind because they would **deceive a recipient**: the **forged
quoted-reply** (attributing invented text to another person), the **fake link preview** (a card whose
title and thumbnail contradict the URL), and to a lesser degree an **inflated "forwarded many times"
score** and a **spoofed thumbnail**. Reading your own delivered messages is one thing; fabricating what
another person appears to have said or seen is another.

Every one of these is marked `NOT_IMPLEMENTED`, and for the impersonation vectors the field-level
recipe is deliberately withheld from this document even though the finding is recorded. The reasoning:
noting that a boundary exists is documentation that a maintainer needs; writing down the exact bytes to
cross it is an attacker's how-to that no legitimate use of this server requires. This is the project's
one editorial position, and it is load-bearing - it is the reason the rest of the document can be
trusted to describe reading, not deceiving.

---

## Debunked

One thing a reader might reasonably try that does **not** work:

- **"Set a poll cap of 2 to hold voters to two selections."** It does not bind. cap=2 renders as
  "Select one or more" and every client ignores it; only cap=1 is enforced (and only against the real
  app). See the poll-cap saga at the top. The `create_poll`/`vote_in_poll` docstrings currently claim
  otherwise and are wrong for N>1.
