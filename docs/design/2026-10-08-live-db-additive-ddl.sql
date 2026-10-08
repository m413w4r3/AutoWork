-- Additive DDL for the live DB (generated from ORM diff f92cc18 -> aa71936)
BEGIN;
-- NEW COLUMN production_repair_decisions.criticality_policy_version
ALTER TABLE production_repair_decisions ADD COLUMN criticality_policy_version VARCHAR(64);
-- NEW COLUMN production_repair_decisions.criticality_reason
ALTER TABLE production_repair_decisions ADD COLUMN criticality_reason VARCHAR(64);
-- NEW COLUMN production_repair_decisions.failure_code
ALTER TABLE production_repair_decisions ADD COLUMN failure_code VARCHAR(128);
-- NEW COLUMN production_repair_decisions.source_criticality
ALTER TABLE production_repair_decisions ADD COLUMN source_criticality VARCHAR(32);
-- CHANGED CHECK production_repair_decisions.ck_production_repair_action_compatibility
ALTER TABLE production_repair_decisions DROP CONSTRAINT ck_production_repair_action_compatibility;
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_action_compatibility CHECK (((issue_kind IN ('rejected_indicator', 'rejected_rule') AND action IN ('include', 'exclude', 'replace')) OR (issue_kind = 'supplemental_source_unarchived' AND action = 'continue_without_source') OR (issue_kind = 'source_extraction_failed' AND action = 'continue_without_source')));
-- NEW CHECK production_repair_decisions.ck_production_repair_criticality_policy_version
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_criticality_policy_version CHECK (source_criticality IS NULL OR (criticality_policy_version IS NOT NULL AND char_length(btrim(criticality_policy_version)) > 0));
-- NEW CHECK production_repair_decisions.ck_production_repair_criticality_reason
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_criticality_reason CHECK (criticality_reason IS NULL OR criticality_reason IN ('core_tier', 'primary_role', 'counter_analysis', 'redundant_corroboration', 'corroboration_uncovered', 'content_duplicate', 'coverage_unknown'));
-- CHANGED CHECK production_repair_decisions.ck_production_repair_issue_kind
ALTER TABLE production_repair_decisions DROP CONSTRAINT ck_production_repair_issue_kind;
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_issue_kind CHECK (issue_kind IN ('rejected_indicator', 'rejected_rule', 'supplemental_source_unarchived', 'source_extraction_failed'));
-- NEW CHECK production_repair_decisions.ck_production_repair_source_criticality
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_source_criticality CHECK (source_criticality IS NULL OR source_criticality IN ('critical', 'secondary'));
-- NEW CHECK production_repair_decisions.ck_production_repair_source_criticality_pair
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_source_criticality_pair CHECK ((source_criticality IS NULL) = (criticality_reason IS NULL));
-- NEW CHECK production_repair_decisions.ck_production_repair_source_extraction_diagnostic
ALTER TABLE production_repair_decisions ADD CONSTRAINT ck_production_repair_source_extraction_diagnostic CHECK (issue_kind <> 'source_extraction_failed' OR action <> 'continue_without_source' OR (reason IS NOT NULL AND char_length(btrim(reason)) > 0 AND source_criticality IS NOT NULL AND criticality_reason IS NOT NULL AND failure_code IS NOT NULL AND char_length(btrim(failure_code)) > 0 AND criticality_policy_version IS NOT NULL AND char_length(btrim(criticality_policy_version)) > 0));
-- NEW TABLE production_source_additions
CREATE TABLE production_source_additions (
	id UUID NOT NULL, 
	addition_id UUID NOT NULL, 
	request_id UUID NOT NULL, 
	production_run_id UUID NOT NULL, 
	subject_id UUID NOT NULL, 
	source_key VARCHAR(64) NOT NULL, 
	version INTEGER NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	origin VARCHAR(32) NOT NULL, 
	record JSONB NOT NULL, 
	projection JSONB, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_source_addition_version UNIQUE (addition_id, version), 
	CONSTRAINT uq_source_addition_request_source_version UNIQUE (production_run_id, request_id, source_key, version), 
	CONSTRAINT ck_source_addition_version CHECK (version >= 1), 
	CONSTRAINT ck_source_addition_source_key CHECK (char_length(source_key) = 64 AND source_key ~ '^[0-9a-f]{64}$'), 
	CONSTRAINT ck_source_addition_state CHECK (state IN ('candidate', 'selected', 'archived', 'extracted', 'ready', 'refused')), 
	CONSTRAINT ck_source_addition_origin CHECK (origin IN ('discovery', 'operator_url', 'manual_archive')), 
	FOREIGN KEY(production_run_id) REFERENCES production_runs (id) ON DELETE CASCADE, 
	FOREIGN KEY(subject_id) REFERENCES subjects (id) ON DELETE RESTRICT
);
CREATE INDEX ix_production_source_additions_request_latest ON production_source_additions (production_run_id, request_id, source_key, version);
CREATE INDEX ix_production_source_additions_run_created ON production_source_additions (production_run_id, created_at);
-- NEW TRIGGER (append-only journal, same as baseline _TRIGGERS)
CREATE TRIGGER trg_production_source_additions_append_only BEFORE UPDATE OR DELETE ON production_source_additions FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation();
COMMIT;
