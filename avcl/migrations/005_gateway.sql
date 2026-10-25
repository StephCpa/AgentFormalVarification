ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 5);
UPDATE schema_version SET version=5;
CREATE TABLE api_credential(
 token_hash text PRIMARY KEY, id text NOT NULL UNIQUE, tenant_id text NOT NULL REFERENCES authority_guard,
 subject text NOT NULL, scopes text NOT NULL, expires bigint NOT NULL, revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE checked_request(
 tenant_id text REFERENCES authority_guard, id text NOT NULL, subject text NOT NULL,
 payload text NOT NULL, evidence_ref text NOT NULL, expires bigint NOT NULL,
 PRIMARY KEY(tenant_id,id)
);
CREATE TABLE api_request(
 tenant_id text NOT NULL, subject text NOT NULL, endpoint text NOT NULL, request_id text NOT NULL,
 digest text NOT NULL, operation_id text NOT NULL,
 PRIMARY KEY(tenant_id,subject,endpoint,request_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES operation DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER checked_immutable BEFORE UPDATE OR DELETE ON checked_request FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER api_request_immutable BEFORE UPDATE OR DELETE ON api_request FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE FUNCTION freeze_credential_identity() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF ROW(NEW.token_hash,NEW.id,NEW.tenant_id,NEW.subject,NEW.scopes,NEW.expires)
 IS DISTINCT FROM ROW(OLD.token_hash,OLD.id,OLD.tenant_id,OLD.subject,OLD.scopes,OLD.expires)
 OR (OLD.revoked AND NOT NEW.revoked) THEN
   RAISE EXCEPTION 'immutable credential identity';
 END IF;
 RETURN NEW;
 END;
$$;
CREATE TRIGGER credential_identity BEFORE UPDATE ON api_credential FOR EACH ROW EXECUTE FUNCTION freeze_credential_identity();
