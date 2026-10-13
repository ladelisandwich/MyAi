-- What the approval card showed (computer name, why it asks, warnings), so the Jobs page and other
-- devices show the same details as the chat.
ALTER TABLE tool_approvals ADD COLUMN details_json TEXT;
