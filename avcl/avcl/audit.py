"""Transactional per-tenant event allocation and append-only persistence."""
from .canonical import Reject
from .storage_codec import dumps,loads
from .types import Event
import re

MAX_SEQUENCE=2**63-1


def validate_event(event):
    def text(value,limit):
        if type(value) is not str or len(value)>limit:return False
        try:value.encode("utf-8",errors="strict")
        except UnicodeError:return False
        return True
    if (type(event) is not Event or type(event.sequence) is not int or not 1<=event.sequence<=MAX_SEQUENCE or
        not text(event.kind,128) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*",event.kind) is None or
        (event.operation_id is not None and (not text(event.operation_id,256) or not event.operation_id)) or
        type(event.details) is not tuple):raise Reject("INVALID_AUDIT_EVENT")
    keys=[]
    for pair in event.details:
        if (type(pair) is not tuple or len(pair)!=2 or not text(pair[0],128) or
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",pair[0]) is None or not text(pair[1],65536)):
            raise Reject("INVALID_AUDIT_EVENT")
        keys.append(pair[0])
    if keys!=sorted(set(keys)):raise Reject("INVALID_AUDIT_EVENT")
    return event


def decode_event(payload):return validate_event(loads(payload))


def encoded(events):return tuple(dumps(validate_event(event)) for event in events)


def check_prefix(k,*,readonly=False):
    baseline=k._persisted_event_encodings
    if len(k._events)<len(baseline) or (readonly and len(k._events)!=len(baseline)):
        raise Reject("AUDIT_HISTORY_MUTATION")
    try:current=encoded(k._events[:len(baseline)])
    except Reject:raise Reject("AUDIT_HISTORY_MUTATION") from None
    if current!=baseline:raise Reject("AUDIT_HISTORY_MUTATION")


def allocate(conn,tenant):
    row=conn.execute("UPDATE audit_counter SET last_sequence=last_sequence+1 "
                     "WHERE tenant_id=%s AND last_sequence<%s RETURNING last_sequence",
                     (tenant,MAX_SEQUENCE)).fetchone()
    if row is None:raise Reject("AUDIT_SEQUENCE_UNAVAILABLE")
    return row["last_sequence"]


def attach(conn,k,persisted=(),payloads=()):
    k._persisted_events=tuple(persisted)
    k._persisted_event_encodings=encoded(k._persisted_events)
    k._persisted_event_payloads=tuple(payloads)
    k._event_sequence_allocator=lambda:allocate(conn,k.tenant)


def hydrate(db,conn,k):
    counter=conn.execute("SELECT last_sequence FROM audit_counter WHERE tenant_id=%s",(k.tenant,)).fetchone()
    rows=sorted(db._rows(conn,"audit_event"),key=lambda r:r["sequence"])
    if counter is None or counter["last_sequence"]!=len(rows):raise Reject("CORRUPT_AUDIT_COUNTER")
    events=[]
    for expected,row in enumerate(rows,1):
        event=loads(row["payload"])
        if (row["sequence"]!=expected or type(event) is not Event or
            type(event.sequence) is not int or event.sequence!=expected):raise Reject("CORRUPT_AUDIT_SEQUENCE")
        events.append(validate_event(event))
    k._events=events
    attach(conn,k,events,(r["payload"] for r in rows))


def persist(conn,k):
    baseline=k._persisted_events
    check_prefix(k)
    tail=k._events[len(baseline):]
    for expected,event in enumerate(tail,len(baseline)+1):
        if type(event) is not Event or type(event.sequence) is not int or event.sequence!=expected:
            raise Reject("CORRUPT_AUDIT_SEQUENCE")
        validate_event(event)
    counter=conn.execute("SELECT last_sequence FROM audit_counter WHERE tenant_id=%s",(k.tenant,)).fetchone()
    if counter is None or counter["last_sequence"]!=len(k._events):raise Reject("CORRUPT_AUDIT_COUNTER")
    # Only newly allocated events are inserted; conflicting rows are errors.
    payloads=encoded(tail)
    for event,payload in zip(tail,payloads):
        conn.execute("INSERT INTO audit_event(tenant_id,sequence,payload) VALUES(%s,%s,%s)",
                     (k.tenant,event.sequence,payload))
    k._persisted_events=tuple(k._events)
    k._persisted_event_encodings+=payloads
    k._persisted_event_payloads+=payloads


def check_readonly(conn,k):
    check_prefix(k,readonly=True)
    row=conn.execute("SELECT last_sequence FROM audit_counter WHERE tenant_id=%s",(k.tenant,)).fetchone()
    if row is None or row["last_sequence"]!=len(k._persisted_events):raise Reject("CORRUPT_AUDIT_COUNTER")
