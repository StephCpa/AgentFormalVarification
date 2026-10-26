-- Run in the migration transaction, in the same lock order as runtime writers.
LOCK TABLE authority_guard IN ACCESS EXCLUSIVE MODE;
LOCK TABLE audit_event IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
 IF EXISTS (
   SELECT tenant_id FROM audit_event GROUP BY tenant_id
   HAVING min(sequence)<>1 OR max(sequence)<>count(*)
 ) OR EXISTS (
   SELECT 1 FROM audit_event
   WHERE (payload::jsonb ->> '$record') IS DISTINCT FROM 'Event'
      OR (payload::jsonb #> '{fields,sequence}') IS DISTINCT FROM jsonb_build_object('$int',sequence::text)
 ) THEN RAISE EXCEPTION 'corrupt legacy audit sequence'; END IF;
END;
$$;
CREATE TABLE audit_counter (
 tenant_id text PRIMARY KEY REFERENCES authority_guard,
 last_sequence bigint NOT NULL DEFAULT 0 CHECK(last_sequence>=0)
);
INSERT INTO audit_counter(tenant_id,last_sequence)
 SELECT g.tenant_id,coalesce(max(e.sequence),0)
 FROM authority_guard g LEFT JOIN audit_event e USING(tenant_id) GROUP BY g.tenant_id;
CREATE FUNCTION require_audit_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE allocated bigint;
BEGIN
 SELECT last_sequence INTO allocated FROM audit_counter WHERE tenant_id=NEW.tenant_id;
 IF allocated IS NULL OR NEW.sequence<1 OR NEW.sequence>allocated
    OR (NEW.payload::jsonb ->> '$record') IS DISTINCT FROM 'Event'
    OR (NEW.payload::jsonb #> '{fields,sequence}') IS DISTINCT FROM jsonb_build_object('$int',NEW.sequence::text)
 THEN RAISE EXCEPTION 'audit event has no matching allocation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER audit_requires_allocation BEFORE INSERT ON audit_event FOR EACH ROW EXECUTE FUNCTION require_audit_allocation();
ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 10);
UPDATE schema_version SET version=10;
