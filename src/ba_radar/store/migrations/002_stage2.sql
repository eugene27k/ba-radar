-- Stage 2: verdict provenance and the derived values a digest was built from.
--
-- `relevance_score` keeps the model's *base* score. `adjusted_score` is the value
-- after source weight, indicator and multi-source bonuses, as persisted when the item
-- was scored and again when it was selected for a digest, so a resend renders the
-- same tiers and Stage 5 can see what the reader actually got. The three provenance
-- columns say which provider, model and prompt version produced the score.
-- Excerpts are still absent, deliberately (answers doc §0).

ALTER TABLE items ADD COLUMN scored_at      TEXT;
ALTER TABLE items ADD COLUMN adjusted_score INTEGER;
ALTER TABLE items ADD COLUMN llm_provider   TEXT;
ALTER TABLE items ADD COLUMN llm_model      TEXT;
ALTER TABLE items ADD COLUMN prompt_version TEXT;
