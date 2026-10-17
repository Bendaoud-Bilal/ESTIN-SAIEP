"""Scheduling Decision Records + Metrics Emitter (invariants I-1, I-5).

Append-only JSONL, two stages per job: 'intent' at dispatch and 'outcome'
at termination (actual vs requested resources). This is the training stream
for the AI module, alongside spawn-requests.jsonl. Records are never edited.

The local file stays the primary copy; when a RecordShipper is given, each
record is also written to MinIO as its own object (append-only: new key per
record, versioned bucket, no delete right for the hub).
"""
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from . import objectstore

log = logging.getLogger(__name__)

TERMINAL_PHASES = {'Succeeded', 'Failed', 'Error'}


def record_key(record):
    """decisions/YYYY/MM/DD/<ts>-<stage>-<id>.json — unique, time-ordered."""
    ts = datetime.fromisoformat(record['timestamp'])
    return (f"decisions/{ts:%Y/%m/%d}/{ts:%Y%m%dT%H%M%S.%fZ}-{record.get('stage', 'record')}"
            f"-{uuid.uuid4().hex[:12]}.json")


class RecordShipper:
    """Best-effort async upload of records to MinIO. Never blocks the caller:
    the local JSONL file is the source of truth if MinIO is unreachable."""

    def __init__(self, store, bucket=objectstore.RECORDS_BUCKET, attempts=3, backoff=1.0,
                 maxsize=1000):
        self.store, self.bucket = store, bucket
        self.attempts, self.backoff = attempts, backoff
        self._queue = queue.Queue(maxsize=maxsize)
        threading.Thread(target=self._worker, name='saiep-record-shipper', daemon=True).start()

    def ship(self, record):
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            log.warning('decision record not shipped to MinIO (queue full): %s', record.get('workflow'))

    def _upload(self, record):
        body = json.dumps(record, ensure_ascii=False).encode()
        key = record_key(record)
        for attempt in range(self.attempts):
            try:
                self.store.put(self.bucket, key, body, 'application/json')
                return True
            except objectstore.ObjectStoreError as e:
                if attempt == self.attempts - 1:
                    log.warning('decision record not shipped to MinIO: %s', e)
                    return False
                time.sleep(self.backoff * 2 ** attempt)

    def _worker(self):
        while True:
            record = self._queue.get()
            try:
                self._upload(record)
            finally:
                self._queue.task_done()

    def flush(self):
        self._queue.join()


class DecisionLog:
    def __init__(self, path, shipper=None):
        self.path = path
        self.shipper = shipper
        self._lock = threading.Lock()
        self._outcomes = self._load_outcomes()

    def _load_outcomes(self):
        seen = set()
        if os.path.exists(self.path):
            with open(self.path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get('stage') == 'outcome':
                        seen.add((rec.get('namespace'), rec.get('workflow')))
        return seen

    def _append(self, record):
        record = {'timestamp': datetime.now(timezone.utc).isoformat(), **record}
        with self._lock, open(self.path, 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
        if self.shipper is not None:
            self.shipper.ship(record)
        return record

    def intent(self, *, user, namespace, workflow, request, decision,
               recommendation=None, reason=''):
        return self._append({
            'stage': 'intent',
            'user': user,
            'namespace': namespace,
            'workflow': workflow,
            'request': request,
            'decision': decision,  # accepted | rejected
            'reason': reason,
            'ai_recommendation': recommendation,
        })

    def outcome(self, workflow_obj):
        """Record termination once per workflow. Returns the record or None."""
        meta = workflow_obj.get('metadata', {})
        status = workflow_obj.get('status', {})
        key = (meta.get('namespace'), meta.get('name'))
        if status.get('phase') not in TERMINAL_PHASES or key in self._outcomes:
            return None
        self._outcomes.add(key)
        return self._append({
            'stage': 'outcome',
            'namespace': key[0],
            'workflow': key[1],
            'phase': status.get('phase'),
            'message': status.get('message', ''),
            'started_at': status.get('startedAt'),
            'finished_at': status.get('finishedAt'),
            # Argo's measured usage (resource-seconds) — "actual" side of
            # actual-vs-requested for the AI module.
            'resources_duration': status.get('resourcesDuration', {}),
            'requested': {
                p['name']: p.get('value')
                for p in workflow_obj.get('spec', {}).get('arguments', {}).get('parameters', [])
                if p['name'] in ('cpu', 'memory', 'gpu')
            },
        })
