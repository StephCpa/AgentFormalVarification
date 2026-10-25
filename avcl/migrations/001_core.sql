CREATE TABLE schema_version(version integer PRIMARY KEY CHECK(version=1));
INSERT INTO schema_version VALUES(1);
CREATE TABLE authority_guard(
 tenant_id text PRIMARY KEY, epoch bigint NOT NULL CHECK(epoch>0), active_policy text NOT NULL,
 revision bigint NOT NULL DEFAULT 0, quarantined boolean NOT NULL, next_id bigint NOT NULL,
 signing_key bytea NOT NULL, contract_digest text NOT NULL
);
CREATE TABLE policy_snapshot(
 tenant_id text REFERENCES authority_guard, digest text NOT NULL, envelope text NOT NULL,
 PRIMARY KEY(tenant_id,digest)
);
CREATE TABLE capability(tenant_id text REFERENCES authority_guard, id text NOT NULL, payload text NOT NULL, PRIMARY KEY(tenant_id,id));
CREATE TABLE evidence(tenant_id text REFERENCES authority_guard, id text NOT NULL, payload text NOT NULL, PRIMARY KEY(tenant_id,id));
CREATE TABLE approval(tenant_id text REFERENCES authority_guard, id text NOT NULL, payload text NOT NULL, PRIMARY KEY(tenant_id,id));
CREATE TABLE health(tenant_id text REFERENCES authority_guard, id text NOT NULL, payload text NOT NULL, PRIMARY KEY(tenant_id,id));
CREATE TABLE budget_account(
 tenant_id text REFERENCES authority_guard, id text NOT NULL, limit_amount numeric NOT NULL,
 spent numeric NOT NULL, pending numeric NOT NULL, breached boolean NOT NULL,
 PRIMARY KEY(tenant_id,id),
 CHECK(limit_amount>=0 AND spent>=0 AND pending>=0),
 CHECK(limit_amount=trunc(limit_amount) AND spent=trunc(spent) AND pending=trunc(pending))
);
CREATE TABLE operation(
 tenant_id text REFERENCES authority_guard, id text NOT NULL, subject text NOT NULL,
 state text NOT NULL, settlement text NOT NULL, revision bigint NOT NULL, payload text NOT NULL,
 PRIMARY KEY(tenant_id,id), CHECK(state IN ('Prepared','Dispatched','Unknown','Confirmed','RejectedNoEffect','PartialEffect','AbortedBeforeDispatch')),
 CHECK(settlement IN ('Unsettled','Partial','Final','Disputed'))
);
CREATE TABLE reservation(
 tenant_id text NOT NULL, operation_id text NOT NULL, account_id text NOT NULL,
 upper_bound numeric NOT NULL, charged numeric NOT NULL, remaining numeric NOT NULL,
 PRIMARY KEY(tenant_id,operation_id,account_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES operation,
 FOREIGN KEY(tenant_id,account_id) REFERENCES budget_account,
 CHECK(upper_bound>=0 AND charged>=0 AND remaining>=0),
 CHECK(upper_bound=trunc(upper_bound) AND charged=trunc(charged) AND remaining=trunc(remaining))
);
CREATE TABLE permit(
 tenant_id text NOT NULL, id text NOT NULL, operation_id text NOT NULL, status text NOT NULL,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,operation_id),
 FOREIGN KEY(tenant_id,operation_id) REFERENCES operation,
 CHECK(status IN ('Live','Consumed','Aborted'))
);
CREATE TABLE business_action(
 tenant_id text NOT NULL, business_key text NOT NULL, operation_id text NOT NULL,
 PRIMARY KEY(tenant_id,business_key), FOREIGN KEY(tenant_id,operation_id) REFERENCES operation
);
CREATE TABLE request_dedup(
 tenant_id text NOT NULL, subject text NOT NULL, endpoint text NOT NULL, request_id text NOT NULL,
 digest text NOT NULL, operation_id text NOT NULL,
 PRIMARY KEY(tenant_id,subject,endpoint,request_id), FOREIGN KEY(tenant_id,operation_id) REFERENCES operation
);
CREATE TABLE observation(
 tenant_id text NOT NULL, id text NOT NULL, operation_id text NOT NULL, payload text NOT NULL,
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,operation_id) REFERENCES operation
);
CREATE TABLE observation_receipt(
 tenant_id text REFERENCES authority_guard, digest text NOT NULL, payload text NOT NULL,
 PRIMARY KEY(tenant_id,digest)
);
CREATE TABLE observation_application(
 tenant_id text NOT NULL, digest text NOT NULL, applied boolean NOT NULL, error_code text,
 CHECK((applied AND error_code IS NULL) OR (NOT applied AND error_code IS NOT NULL)),
 PRIMARY KEY(tenant_id,digest), FOREIGN KEY(tenant_id,digest) REFERENCES observation_receipt
);
CREATE TABLE outbox(
 tenant_id text NOT NULL, operation_id text NOT NULL, kind text NOT NULL DEFAULT 'Send',
 status text NOT NULL, generation bigint NOT NULL DEFAULT 0, worker text, lease_until bigint,
 attempts bigint NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant_id,operation_id,kind), FOREIGN KEY(tenant_id,operation_id) REFERENCES operation,
 CHECK(status IN ('Pending','InFlight','NeedsReconcile','Done'))
);
CREATE TABLE send_attempt(
 tenant_id text NOT NULL, operation_id text NOT NULL, generation bigint NOT NULL,
 worker text NOT NULL, request_digest text NOT NULL, binding_digest text NOT NULL, admitted_at bigint NOT NULL,
 PRIMARY KEY(tenant_id,operation_id,generation), FOREIGN KEY(tenant_id,operation_id) REFERENCES operation
);
CREATE TABLE obligation(
 tenant_id text NOT NULL, operation_id text NOT NULL, kind text NOT NULL DEFAULT 'Reconcile',
 due bigint NOT NULL, owner text NOT NULL, status text NOT NULL,
 PRIMARY KEY(tenant_id,operation_id,kind), FOREIGN KEY(tenant_id,operation_id) REFERENCES operation,
 CHECK(status IN ('Open','Fulfilled'))
);
CREATE TABLE audit_event(
 tenant_id text REFERENCES authority_guard, sequence bigint NOT NULL, payload text NOT NULL,
 PRIMARY KEY(tenant_id,sequence)
);
CREATE INDEX outbox_pending ON outbox(tenant_id,status,lease_until);
CREATE INDEX obligation_due ON obligation(tenant_id,status,due);
CREATE FUNCTION deny_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN RAISE EXCEPTION 'immutable record'; END;
$$;
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_event FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER receipt_immutable BEFORE UPDATE OR DELETE ON observation_receipt FOR EACH ROW EXECUTE FUNCTION deny_mutation();
CREATE TRIGGER policy_immutable BEFORE UPDATE OR DELETE ON policy_snapshot FOR EACH ROW EXECUTE FUNCTION deny_mutation();
