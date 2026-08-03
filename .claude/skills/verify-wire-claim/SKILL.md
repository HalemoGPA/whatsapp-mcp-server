---
name: verify-wire-claim
description: Verify a claim about WhatsApp behaviour before writing it down. Use whenever about to state that a feature works, a flag is enforced, a message was view-once, a poll cap applies, or any assertion about what WhatsApp does. Also use when documenting a finding in docs/protocol-vs-app.md.
---

# Verifying a claim about WhatsApp

WhatsApp enforces most of what you see client-side. The app bets that what people
see is reality, and for almost everyone that bet is correct. It is not correct
here, because this server reads the wire.

**A rendering is evidence of what a client chose to draw. Nothing more.**

## Never accept these as evidence

| Source | Why not |
|---|---|
| A screenshot, or what the desktop app shows | Desktop and mobile enforce different things. A message once looked view-once on desktop for weeks while the inner flag was unset. |
| A comment in this repo | They rot. The two most confidently wrong statements in this codebase were comments that had been true when written. |
| A library's documentation | It describes the field's intent, not client behaviour. `selectableOptionCount` is documented as a maximum; no client enforces it above 1. |
| A tally or counter in the app | Five phones once showed five different poll tallies while the wire said one number. |
| "The endpoint returned success" | A poll vote returned `Vote cast` while the tally stayed at zero. |

## Accept these

1. The actual protobuf, with the field number.
2. A test you ran, with its output.
3. An observation from a real phone, on the receive path.

## The procedure

1. **Separate the three claims.** "The field exists", "the server accepts it",
   and "the recipient's client honours it" are different, and a change can
   satisfy one and fail the others. Establish each on its own.

2. **Test the send path.** Call the tool. Capture the response.

3. **Test the receive path on a real phone.** This is the step people skip and it
   is where the bugs are. Sending through our own API exercises code that wraps
   and hashes locally and never decrypts or normalises. Two real bugs (all six
   poll-creation variants, and LID-vs-phone-number on votes) were found in
   minutes by a phone after the server-side tests passed clean.

4. **Check the stored row.** Read what actually landed in the message store, not
   what the tool said it wrote.

## Recording it

Findings go in `docs/protocol-vs-app.md` with two markers:

**Confidence** - `VERIFIED` (tested here, with the test), `SOURCE` (read in the
protocol or library, not run), `SPECULATIVE` (inferred, not confirmed).

**Status** - `EXPOSED` (a tool uses it), `BRIDGE_ONLY` (the bridge can, no tool
does), `NOT_IMPLEMENTED` (deliberately not built).

Never upgrade a `SOURCE` to `VERIFIED` without an actual run. If you are not sure
which marker applies, it is `SPECULATIVE`.

## The line on capabilities that deceive

Some of what the wire permits works by **deceiving a recipient**: forged quoted
context, fabricated previews, spoofed attribution. That is different in kind from
**reading data already delivered to us**, which is the whole point of this
server. Both are technically available. Only one is the owner's own data.

Name which one you are doing. If it is the first, the answer is
`NOT_IMPLEMENTED`, and the reasoning goes in the document rather than the field
recipe.

## When the user contradicts your model

They are probably right. "Desktop says only once, phone views it forever" was
reported three times and explained away twice before it was recognised as the bug
it was. The user can see a surface this server cannot.
