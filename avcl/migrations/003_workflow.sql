ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 3);
UPDATE schema_version SET version=3;
CREATE TABLE workflow_run(
 tenant_id text REFERENCES authority_guard, subject text NOT NULL, id text NOT NULL,
 workflow_digest text NOT NULL, contract_digest text NOT NULL, submission_digest text NOT NULL,
 program text NOT NULL, values_wire text NOT NULL, faults text NOT NULL, frame text NOT NULL,
 pc bigint NOT NULL DEFAULT 0, revision bigint NOT NULL DEFAULT 0,
 status text NOT NULL CHECK(status IN ('Running','WaitingIO','Finished','Halted')),
 failed_node text, error text, PRIMARY KEY(tenant_id,subject,id)
);
CREATE TABLE workflow_step(
 tenant_id text NOT NULL, subject text NOT NULL, run_id text NOT NULL, node_id text NOT NULL,
 sequence bigint NOT NULL, payload text NOT NULL,
 PRIMARY KEY(tenant_id,subject,run_id,node_id), UNIQUE(tenant_id,subject,run_id,sequence),
 FOREIGN KEY(tenant_id,subject,run_id) REFERENCES workflow_run
);
CREATE TABLE workflow_operation(
 tenant_id text NOT NULL, operation_id text NOT NULL, subject text NOT NULL, run_id text NOT NULL,
 PRIMARY KEY(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,subject,run_id) REFERENCES workflow_run
 -- Operation rows are written later in the same transaction by the kernel.
 ,FOREIGN KEY(tenant_id,operation_id) REFERENCES operation DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE workflow_io(
 tenant_id text NOT NULL, subject text NOT NULL, run_id text NOT NULL, node_id text NOT NULL,
 operation_id text NOT NULL, kind text NOT NULL CHECK(kind IN ('Send','Reconcile')),
 status text NOT NULL CHECK(status IN ('InFlight','Recorded','Uncertain')),
 worker text NOT NULL, generation bigint NOT NULL, lease_until bigint NOT NULL,
 PRIMARY KEY(tenant_id,subject,run_id,node_id),
 FOREIGN KEY(tenant_id,subject,run_id) REFERENCES workflow_run,
 FOREIGN KEY(tenant_id,operation_id) REFERENCES operation
);
CREATE TRIGGER workflow_step_immutable BEFORE UPDATE OR DELETE ON workflow_step FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE FUNCTION freeze_workflow_definition() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF ROW(NEW.tenant_id,NEW.subject,NEW.id,NEW.workflow_digest,NEW.contract_digest,NEW.submission_digest,NEW.program,NEW.values_wire,NEW.faults)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.subject,OLD.id,OLD.workflow_digest,OLD.contract_digest,OLD.submission_digest,OLD.program,OLD.values_wire,OLD.faults)
 OR NEW.pc<OLD.pc OR NEW.revision<=OLD.revision
 OR (OLD.status IN ('Finished','Halted') AND NEW.status<>OLD.status) THEN
   RAISE EXCEPTION 'immutable workflow definition or invalid checkpoint progression';
 END IF;
 RETURN NEW;
 END;
$$;
CREATE TRIGGER workflow_definition_immutable BEFORE UPDATE ON workflow_run FOR EACH ROW EXECUTE FUNCTION freeze_workflow_definition();
