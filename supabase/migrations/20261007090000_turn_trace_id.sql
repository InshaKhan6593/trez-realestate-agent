-- Link each turn (the business record) to its Langfuse trace, so the
-- dashboard's "why" on a bot message can open the full trace (§15).
alter table turns add column langfuse_trace_id text;
