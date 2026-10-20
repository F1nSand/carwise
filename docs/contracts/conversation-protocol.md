# Conversation Protocol v1

Status: implementation in progress. This contract is the boundary between the
conversation API, the Agent run API, and the chat UI.

## Identity and ownership

- `conversation_id` is the stable public identity of one conversation and maps
  to exactly one existing Agent context (`sessions.session_id`).
- A conversation can contain multiple logical turns, runs, retries, resume
  attempts, and immutable report versions.
- The legacy `runs.session_id` remains a browser/history grouping value. It is
  not a conversation key and must not be used to merge contexts.
- The server derives `owner_id` from the authenticated request principal. A
  client-supplied owner is ignored/rejected.
- When both `conversation_id` and `context_id` are supplied, they must resolve
  to the same owned context.

## Durable resources

`sessions` remains the context store and gains conversation metadata:

- `title`, `title_source`, `selected_report_id`, `conversation_revision`.
- Conversation creation is idempotent by `(owner_id, command_id)` and creates
  an empty owned context without requiring a run.

`conversation_entries` is the user-visible timeline, not model history or raw
`run_events`. Each entry has `entry_id`, `conversation_id`, monotonically
increasing `conversation_seq`, `kind`, optional `run_id`/`turn_id`, stable
`source_key`, JSON payload and timestamp. `(conversation_id, source_key)` and
`(conversation_id, conversation_seq)` are unique.

`conversation_commands` records idempotent conversation-level writes using
`(owner_id, operation, command_id)`, a request hash, the conversation, accepted
operation/run, and replayable response. It is needed for empty-conversation
creation because legacy `client_requests` requires a run foreign key.

`report_artifacts` stores immutable report content and context text, evidence
manifest, dataset version, originating run, version and optional parent report.
An originating run and a `(conversation_id, version)` each identify one
artifact. Existing `runs.result_json.report` remains a compatibility projection.

## Read API

- `POST /v1/conversations`: create an empty conversation for the current owner;
  accept an idempotency command ID.
- `GET /v1/conversations?cursor=...`: return an owner-scoped paginated list with
  title, update time, latest entry preview, active run/status, pending-input
  marker, and latest report reference.
- `GET /v1/conversations/{conversation_id}`: return metadata, revision, active
  run and selected/latest report references.
- `GET /v1/conversations/{conversation_id}/entries?before_seq=...`: return
  paginated typed entries and a timeline high-water sequence.
- `GET /v1/reports/{report_id}`: return immutable report content and its
  conversation, originating run, version, and parent report after owner checks.

Missing or foreign-owned conversations/reports return the same not-found
behavior. Pagination order is stable by `conversation_seq` (entries) and
`updated_at` plus `conversation_id` (conversation list).

## Timeline entry kinds

The initial public kinds are `user_message`, `assistant_message`,
`execution_group`, `question`, `question_answer`, `report_card`, and `run_status`.
Payloads are allowlisted projections; they must not expose raw prompts, hidden
reasoning, checkpoints, owner IDs, or unredacted tool arguments/results.
Report cards reference a stable `report_id`, `version`, and originating run.
An `execution_group` is stored with the terminal run when public runtime events
or a runtime snapshot are available. It may contain a compact plan and tool activity list (tool name, safe
status, step identity, bounded note/error summary, and duration); raw arguments,
full tool outputs, and private reasoning are dropped. Failed runs keep this
execution summary with an error status so the history remains inspectable.
Schema v10 backfills the same safe summary for historical runs from their events
or persisted runtime snapshot.

An accepted user message is persisted with its command acceptance. A successful
run persists its validated assistant answer and report card in the same
completion transaction as the immutable report and terminal `done` state.
The historical execution group is written in that same transaction when runtime
events or a runtime snapshot are available. Candidate `final` events are
provisional and never create a success entry.

## Write and steering behavior

`POST /v1/conversations/{conversation_id}/messages` is the planned single chat
send contract. In one acceptance transaction the server checks the owned
conversation and active run, then accepts exactly one of:

- a new turn/run if no run is active;
- steering input for the current run if it is running or waiting for user input.

The `(owner_id, operation, command_id)` key is idempotent across completion
races. The response includes `accepted_kind`, `conversation_id`, `run_id` when
applicable, and the durable `entry_id`. Structured answers to an explicit
Agent question continue to use the existing run-input contract.

This endpoint is introduced after the read APIs and storage layer. Legacy
`/v1/runs`, `/input`, `/messages`, retry, recover, history, and run-ID report
routes remain compatible during migration.

## Version boundaries

Public run snapshot/event schema, conversation API schema, report schema, and
internal SQLite/checkpoint versions are independent version domains. A version
number is never globally rewritten to make two payloads appear compatible.
