# AGANT Telegram newsroom, first delivery

## Intent and existing project

Extend Sepide's existing `agant-hermes-bot` on Hostless with the editorial
workflow discussed in chat: a source-backed Persian draft with its original
photo, optional humor, revisions and explicit owner-approved channel posting.
The repository pins Hermes to `v2026.9.24`; that source exposes
`PluginContext.register_platform_handler("telegram", factory)` before core
Telegram handlers. Reuse it and the existing bot token, without another poller.

## Approaches

1. Native Hermes plugin: reuse the bot, model and research tools; scoped native
   handlers own approval and publication. Selected as the implementation default
   because the conversation already concerns extending Hermes.
2. Independent newsroom bot: clearer service separation, but requires another
   Telegram bot and deployment. Keep this option available for a later football
   service if Sepide chooses it.
3. Make-only flow: useful for delivery, but requires additional state and inbound
   callback handling for conversational revisions. Do not use for this delivery.

## First delivery

- Disabled unless `AGANT_NEWSROOM_ENABLED=true`; no deployment or live Telegram
  messages during development.
- One configured editorial group and one destination channel, both numeric IDs.
- Only configured editor user IDs can create, revise, select or reject drafts.
  Only the configured owner user ID can publish or reconcile an uncertain send.
- The agent gets create/read/revise/refresh tools, never a publish tool. Identity
  comes from Hermes session context; button/reply identity comes from Telegram.
- Each draft holds a title, plain body, optional humor body, a canonical original
  X source and optional `pbs.twimg.com` photo URL. Source attribution is included
  in the published caption. Images are original source assets, never generated.
- Show one photo/text preview with buttons to switch plain/humor, publish the
  visible revision, or reject. Ordinary replies reach Hermes for language edits;
  reply `ارسال` directs the owner to the revision-bound button on the original
  preview; reply `رد` rejects the anchored draft. Only the button publishes.
- SQLite stores drafts, revisions, selected variant, publication receipts and
  decision events. The operator must put its path on durable storage before use.
- Unique X status IDs stop resubmission of the same post. This does not
  claim cross-source semantic deduplication or automatic factual verification.
- Compare-and-swap transitions prevent stale buttons, simultaneous publish
  clicks and edits during sending from publishing another revision.
- Per-draft nonblocking OS locks span preview updates and sends. Reconciliation
  cannot reset a send while its sender is active, including in another process.
- A gateway hook looks up replies by their original message ID and injects the
  current draft ID/revision, even when Telegram delivers only a partial quote.
- Explicit replacement previews repair deleted preview messages, increment the
  revision and rebind the message ID. Tokens are redacted from httpx URL logs.
- Telegram has no send idempotency key. A timeout, missing receipt or a process
  death during sending remains uncertain and blocks further sends. An owner
  reconciliation command records an observed channel message ID or explicitly
  records that the owner checked and found no post before re-enabling approval.

## Boundaries and constraints

Use Python 3.11 and existing HTTP/Telegram dependencies; preserve pinned image
inputs. Add no second polling process and no changes to runtime deployment
configuration. The plugin enforces its workflow, not a sandbox around every
existing Hermes terminal or messaging tool.

Scheduled collection, source subscriptions, cross-source deduplication,
Instagram reporting and the coding-agent task queue are subsequent deliveries.
The operator will supply the editorial group ID, test channel ID, owner ID and
durable database location before activation. Live end-to-end verification needs
that configured environment; offline tests do not establish deployment success.
