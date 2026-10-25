ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 7);
UPDATE schema_version SET version=7;
CREATE TABLE task_run(
 tenant_id text NOT NULL REFERENCES authority_guard, subject text NOT NULL, id text NOT NULL,
 task_key text NOT NULL, contract_digest text NOT NULL, expected_keys text NOT NULL,
 close_requested boolean NOT NULL DEFAULT false, sealed boolean NOT NULL DEFAULT false,
 status text NOT NULL DEFAULT 'Running' CHECK(status IN ('Running','Pending','Completed','Rejected','Escalated','Failed')),
 revision bigint NOT NULL DEFAULT 0, report text NOT NULL,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,subject,task_key),
 FOREIGN KEY(tenant_id,contract_digest) REFERENCES policy_snapshot
);
CREATE TABLE task_close_request(
 tenant_id text NOT NULL, subject text NOT NULL, task_id text NOT NULL, request_id text NOT NULL,
 report text NOT NULL, PRIMARY KEY(tenant_id,subject,task_id,request_id),
 FOREIGN KEY(tenant_id,task_id) REFERENCES task_run
);
CREATE TRIGGER task_close_immutable BEFORE UPDATE OR DELETE ON task_close_request FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE FUNCTION freeze_task_definition() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF ROW(NEW.tenant_id,NEW.subject,NEW.id,NEW.task_key,NEW.contract_digest,NEW.expected_keys)
 IS DISTINCT FROM ROW(OLD.tenant_id,OLD.subject,OLD.id,OLD.task_key,OLD.contract_digest,OLD.expected_keys)
 OR (OLD.close_requested AND NOT NEW.close_requested) OR (OLD.sealed AND NOT NEW.sealed)
 OR NEW.revision<OLD.revision THEN RAISE EXCEPTION 'immutable task scope or invalid progression'; END IF;
 RETURN NEW;
 END;
$$;
CREATE TRIGGER task_definition_immutable BEFORE UPDATE ON task_run FOR EACH ROW EXECUTE FUNCTION freeze_task_definition();
CREATE TRIGGER task_definition_no_delete BEFORE DELETE ON task_run FOR EACH ROW EXECUTE FUNCTION deny_mutation();
