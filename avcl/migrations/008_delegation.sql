ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 8);
CREATE TABLE delegation_subject (
    tenant_id text NOT NULL REFERENCES authority_guard(tenant_id),
    subject text NOT NULL,
    active boolean NOT NULL,
    PRIMARY KEY (tenant_id, subject)
);
CREATE TABLE delegation_grant (
    tenant_id text NOT NULL,
    capability_ref text NOT NULL,
    payload text NOT NULL,
    PRIMARY KEY (tenant_id, capability_ref),
    FOREIGN KEY (tenant_id, capability_ref) REFERENCES capability(tenant_id, id)
);
CREATE TABLE delegation_request (
    tenant_id text NOT NULL,
    subject text NOT NULL,
    request_id text NOT NULL,
    digest text NOT NULL,
    capability_ref text NOT NULL,
    PRIMARY KEY (tenant_id, subject, request_id),
    FOREIGN KEY (tenant_id, capability_ref) REFERENCES delegation_grant(tenant_id, capability_ref)
);
UPDATE schema_version SET version=8;
CREATE FUNCTION freeze_delegation_grant() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.tenant_id,NEW.capability_ref) IS DISTINCT FROM ROW(OLD.tenant_id,OLD.capability_ref)
       OR (NEW.payload::jsonb #- '{fields,revoked}') IS DISTINCT FROM (OLD.payload::jsonb #- '{fields,revoked}')
       OR jsonb_typeof(NEW.payload::jsonb #> '{fields,revoked}') IS DISTINCT FROM 'boolean'
       OR (OLD.payload::jsonb #>> '{fields,revoked}' = 'true' AND (NEW.payload::jsonb #>> '{fields,revoked}') IS DISTINCT FROM 'true')
    THEN RAISE EXCEPTION 'immutable delegation grant'; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER delegation_grant_immutable BEFORE UPDATE ON delegation_grant FOR EACH ROW EXECUTE FUNCTION freeze_delegation_grant();
CREATE TRIGGER delegation_grant_no_delete BEFORE DELETE ON delegation_grant FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER delegation_request_immutable BEFORE UPDATE OR DELETE ON delegation_request FOR EACH ROW EXECUTE FUNCTION deny_mutation();
