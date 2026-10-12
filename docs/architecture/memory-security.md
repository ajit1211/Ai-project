# Memory security

How the memory engine ([memory-engine.md](memory-engine.md)) keeps memories private, keeps
secrets out, and keeps stored text from steering the model. Every rule here has a test
(see [memory-testing.md](../development/memory-testing.md)).

## Isolation

| Rule | How |
| --- | --- |
| Organizations never see each other's memories | Every query filters on the organization from the verified token; composite foreign keys keep a memory's team and conversation inside its organization |
| A private memory is its owner's alone | `conversation` and `user` memories are visible only to their owner, also to administrators: `memory.manage_all` reaches shared memories, never private ones |
| Team memories stay in the team | Visible to the team's members (from the database, not the request) and `memory.manage_all` holders; writable by their author and `memory.manage_all` |
| Organization memories are deliberate | Readable by every member; created and changed only with `memory.manage_all` |
| Nothing is promoted automatically | Extraction writes the user's private memories only; a team or organization memory is never derived from a private one, and the other way round |
| The client chooses nothing about identity | Request bodies are closed schemas: no owner, organization, role or permission field; a team must be the caller's, a conversation must be the caller's own |
| Unauthorized looks like absent | Another member's private memory, another team's or another organization's memory is 404 on every route, including search and approve |
| Background jobs keep the caller's scope | Extraction and summaries run with the principal of the turn that triggered them and read only that conversation |
| Retrieval grants nothing | Recall uses the same visibility predicate as the API, applied in SQL before ranking; a memory never changes what the user may do |

## Secrets and sensitive data

- **Never stored, by anyone**: text that looks like a password, passphrase, PIN, one-time
  code, API key, access token, JWT or private key (`policy.refuse_secret`). This applies to
  memories typed in by hand as well; the API answers 422.
- **Never stored automatically**: anything the extraction model marks sensitive or files
  under health, finance, credentials, identity, contact, beliefs or sexuality, and anything
  matching a personal identifier (card numbers with a valid Luhn check, IBANs, national ids,
  email addresses). The user may still write such a memory down on purpose, as their own act.
- **Not stored as fact without evidence**: inferences, low-confidence items and claims not
  grounded in the user's own words wait as candidates for the user's approval.
- **The user decides**: automatic extraction and automatic saving can be turned off; every
  memory can be edited or deleted; *Clear my memories* deletes every private memory at once.

## Prompt injection through memory

Memory text is user-influenced data and is treated as such everywhere:

- In the reply context, memories and the summary sit in their own messages after NEXA's
  protected instructions, each introduced as stored data that is not instructions. The
  protected instructions are never edited or extended (a test checks the first message is
  byte for byte the configured prompt).
- The extraction and summarization prompts carry the conversation as delimited data inside a
  single user turn, under fixed instructions, so a message cannot pose as instructions to the
  extractor or the summarizer.
- Model output is untrusted: extraction and summary answers must match a JSON schema, are
  validated again on our side, and pass the policy above before anything is stored.

## Memory poisoning

A planted "fact" is the main risk of automatic memory. The engine limits it by: storing only
statements the user made, grounded in their own message; holding every inference, every
conflict with a hand-written memory and every low-confidence item for approval; never
storing the assistant's words as the user's; superseding rather than silently overwriting,
so the previous version remains visible; and keeping candidates and rejected proposals out
of retrieval entirely. A rejected proposal is remembered by its hash and not proposed again.

## Deletion

| Action | Effect |
| --- | --- |
| Delete a memory | The row is deleted; it is never recalled again |
| Reject a candidate | Kept as `rejected` (hidden from recall) so it is not proposed again; purged after 30 days |
| Clear my memories | Every private memory of the caller in this organization is deleted; shared memories stay |
| Delete a conversation | Its conversation memories, summaries and extraction records are deleted; memories that came from it keep their text and lose the reference |
| Delete a team | Its team memories are deleted |
| Delete the user or the organization | Everything they own goes with them (cascade) |

What deletion does not reach: a summary is a record of a conversation, so a statement made in
a conversation stays in that conversation's messages and summary until the conversation is
deleted. Usage ledger rows hold no text and are kept.

## Privacy of operations

Logs, metrics and the admin carry ids, counts, codes and timings, never memory, message or
summary text. Memories are not cached anywhere, so no cache can serve one member's memories
to another; the catalog cache from Phase 04 holds no user data.
