-- fetch_url asks for approval unless its address came from the user or an earlier web result in
-- the same conversation; that check reads the conversation's earlier tool results.
CREATE INDEX IF NOT EXISTS idx_tool_executions_conversation ON tool_executions(conversation_id, tool_name);
