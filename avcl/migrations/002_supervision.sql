ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 2);
UPDATE schema_version SET version=2;
ALTER TABLE authority_guard ADD COLUMN last_clock bigint NOT NULL DEFAULT 0;
ALTER TABLE outbox ADD COLUMN wait_until bigint NOT NULL DEFAULT 0;
ALTER TABLE obligation DROP CONSTRAINT obligation_status_check;
UPDATE obligation SET status='Satisfied' WHERE status='Fulfilled';
ALTER TABLE obligation ADD CHECK(status IN ('Open','Claimed','Satisfied','Breached'));
ALTER TABLE obligation ADD COLUMN generation bigint NOT NULL DEFAULT 0;
ALTER TABLE obligation ADD COLUMN worker text;
ALTER TABLE obligation ADD COLUMN lease_until bigint;
ALTER TABLE obligation ADD COLUMN next_attempt bigint NOT NULL DEFAULT 0;
ALTER TABLE obligation ADD COLUMN trigger_event bigint;
ALTER TABLE obligation ADD COLUMN requirement_id text NOT NULL DEFAULT 'R10';
ALTER TABLE obligation ADD COLUMN breached_at bigint;
ALTER TABLE obligation ADD COLUMN resolved_at bigint;
ALTER TABLE obligation ADD COLUMN resolution text;
ALTER TABLE obligation ADD COLUMN evidence_ref text;
-- Older schemas did not record resolution time. Preserve it as unknown.
UPDATE obligation SET resolution='LegacyFinal' WHERE status='Satisfied';
CREATE TABLE handoff_receiver(
 tenant_id text REFERENCES authority_guard, id text NOT NULL, expires bigint NOT NULL,
 PRIMARY KEY(tenant_id,id)
);
CREATE TABLE handoff_receipt(
 tenant_id text NOT NULL, receiver text NOT NULL, receipt_id text NOT NULL,
 operation_id text NOT NULL, accepted_at bigint NOT NULL,
 PRIMARY KEY(tenant_id,receiver,receipt_id),
 FOREIGN KEY(tenant_id,receiver) REFERENCES handoff_receiver,
 FOREIGN KEY(tenant_id,operation_id) REFERENCES operation
);
CREATE TRIGGER handoff_immutable BEFORE UPDATE OR DELETE ON handoff_receipt FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE FUNCTION preserve_obligation_deadline() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
 IF NEW.due IS DISTINCT FROM OLD.due OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
 OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.kind IS DISTINCT FROM OLD.kind
 OR NEW.trigger_event IS DISTINCT FROM OLD.trigger_event THEN
   RAISE EXCEPTION 'immutable obligation trigger/deadline';
 END IF;
 IF OLD.breached_at IS NOT NULL AND (NEW.breached_at IS DISTINCT FROM OLD.breached_at OR NEW.status<>'Breached') THEN
   RAISE EXCEPTION 'breach history cannot be erased';
 END IF;
 RETURN NEW;
 END;
$$;
CREATE TRIGGER obligation_history BEFORE UPDATE ON obligation FOR EACH ROW EXECUTE FUNCTION preserve_obligation_deadline();
