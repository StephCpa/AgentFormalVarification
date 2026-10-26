ALTER TABLE schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE schema_version ADD CHECK(version BETWEEN 1 AND 9);
CREATE TABLE rejection_audit_policy (
 tenant_id text PRIMARY KEY REFERENCES authority_guard,
 total_limit bigint NOT NULL DEFAULT 10000 CHECK(total_limit BETWEEN 1 AND 1000000),
 subject_limit bigint NOT NULL DEFAULT 100 CHECK(subject_limit BETWEEN 1 AND 1000000),
 window_seconds bigint NOT NULL DEFAULT 60 CHECK(window_seconds BETWEEN 1 AND 86400),
 recorded bigint NOT NULL DEFAULT 0 CHECK(recorded>=0 AND recorded<=total_limit)
);
INSERT INTO rejection_audit_policy(tenant_id) SELECT tenant_id FROM authority_guard;
CREATE TABLE rejection_audit_subject (
 tenant_id text NOT NULL REFERENCES rejection_audit_policy,
 subject text NOT NULL,
 window_start bigint NOT NULL CHECK(window_start>=0),
 recorded bigint NOT NULL CHECK(recorded>0),
 PRIMARY KEY(tenant_id,subject)
);
UPDATE schema_version SET version=9;
