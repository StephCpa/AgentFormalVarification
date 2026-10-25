ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 4);
UPDATE schema_version SET version=4;
CREATE TABLE capacity_policy(
 tenant_id text PRIMARY KEY REFERENCES authority_guard,
 max_operations bigint NOT NULL CHECK(max_operations>=0),
 max_obligations bigint NOT NULL CHECK(max_obligations>=0),
 max_workflows bigint NOT NULL CHECK(max_workflows>=0)
);
-- Preserve all existing commitments on upgrade; never evict old obligations.
INSERT INTO capacity_policy
SELECT g.tenant_id,
 GREATEST(1000,(SELECT count(*) FROM operation o WHERE o.tenant_id=g.tenant_id AND o.settlement<>'Final')),
 GREATEST(1000,(SELECT count(*) FROM (
   SELECT o.id FROM operation o WHERE o.tenant_id=g.tenant_id AND o.settlement<>'Final'
   UNION SELECT b.operation_id FROM obligation b WHERE b.tenant_id=g.tenant_id AND b.resolution IS NULL
 ) pending)),
 GREATEST(1000,(SELECT count(*) FROM workflow_run w WHERE w.tenant_id=g.tenant_id AND w.status IN ('Running','WaitingIO')))
FROM authority_guard g;
ALTER TABLE workflow_run DROP CONSTRAINT workflow_run_status_check;
ALTER TABLE workflow_run ADD CHECK(status IN ('Running','WaitingIO','WaitingCapacity','Finished','Halted'));
