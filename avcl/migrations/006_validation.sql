ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 6);
UPDATE schema_version SET version=6;
CREATE TABLE order_snapshot(
 tenant_id text NOT NULL REFERENCES authority_guard, id text NOT NULL, subject text NOT NULL,
 order_id text NOT NULL, payload text NOT NULL, issued_at bigint NOT NULL, expires bigint NOT NULL,
 PRIMARY KEY(tenant_id,id), CHECK(expires>issued_at)
);
CREATE TABLE order_context_head(
 tenant_id text NOT NULL REFERENCES authority_guard, order_id text NOT NULL, snapshot_id text,
 PRIMARY KEY(tenant_id,order_id), FOREIGN KEY(tenant_id,snapshot_id) REFERENCES order_snapshot
);
CREATE TABLE candidate_validation(
 tenant_id text NOT NULL, subject text NOT NULL, request_id text NOT NULL, input_digest text NOT NULL,
 checked_ref text NOT NULL, evidence_ref text NOT NULL, context_ref text NOT NULL,
 validator_id text NOT NULL, validator_version text NOT NULL, issued_at bigint NOT NULL, expires bigint NOT NULL,
 PRIMARY KEY(tenant_id,subject,request_id), UNIQUE(tenant_id,evidence_ref),
 FOREIGN KEY(tenant_id,checked_ref) REFERENCES checked_request DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,evidence_ref) REFERENCES evidence DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,context_ref) REFERENCES order_snapshot, CHECK(expires>issued_at)
);
CREATE TRIGGER snapshot_immutable BEFORE UPDATE OR DELETE ON order_snapshot FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER validation_immutable BEFORE UPDATE OR DELETE ON candidate_validation FOR EACH ROW EXECUTE FUNCTION deny_mutation();
