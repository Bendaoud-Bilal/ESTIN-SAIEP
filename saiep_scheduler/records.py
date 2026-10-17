"""Scheduling Decision Records + Metrics Emitter (invariants I-1, I-5).

Append-only JSONL, two stages per job: 'intent' at dispatch and 'outcome'
at termination (actual vs requested resources). This is the training stream
for the AI module, alongside spawn-requests.jsonl. Records are never edited.
"""
import json
import os
import threading
from datetime import datetime, timezone

TERMINAL_PHASES = {'Succeeded', 'Failed', 'Error'}


class DecisionLog:
    def __init__(self, path):
        self.path = path
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
