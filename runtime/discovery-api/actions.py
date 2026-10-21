"""Prioritized next-best-actions for agents and humans.

`build_actions` reads the stores and returns a small, ordered task list. Each item names the record, why it
needs attention, who should act (`agent` can call the endpoint, `human` must decide/send) and the exact API
call that resolves it. No personal content is added beyond ids, statuses and error codes.
"""
import time


def _action(kind, ref_type, ref_id, priority, why, actor='agent', suggested=None, due_at=None, **extra):
    return {'type': kind, 'ref_type': ref_type, 'ref_id': ref_id, 'priority': priority, 'why': why,
            'actor': actor, 'suggested_call': suggested, 'due_at': due_at, **extra}


def build_actions(service, discovery_store, campaign_store, now=None, kind=None, limit=200):
    now = now or time.time()
    actions = []
    paused = service.paused_until > now
    if paused:
        actions.append(_action('fix_session', 'outreach', 'outreach', 100,
                               'Automated browser work is paused after a challenge/login wall.',
                               actor='human', suggested={'method': 'POST', 'path': '/api/v1/outreach/resume'},
                               due_at=None, until=service.paused_until))

    for r in service.store.list(limit=10000):
        rid, status = r['id'], r['status']
        connect, connection = (r.get('connect') or {}), (r.get('connection') or {})
        base = '/api/v1/outreach/' + rid
        if status == 'awaiting_connection':
            if connect.get('state') in ('failed', 'manual'):
                actions.append(_action('send_connection', 'outreach', rid, 80,
                                       'The connection request was not sent (%s).' % (connect.get('error') or connect.get('state')),
                                       suggested={'method': 'POST', 'path': base + '/connect'}))
            elif connect.get('state') == 'not_sent' and not paused and (connect.get('next_attempt_at') or 0) <= now:
                actions.append(_action('send_connection', 'outreach', rid, 79,
                                       'The connection request is queued but not sent yet.',
                                       suggested={'method': 'POST', 'path': base + '/connect'},
                                       due_at=connect.get('next_attempt_at')))
            elif connection.get('next_check_at') and connection['next_check_at'] <= now and not paused:
                actions.append(_action('check_acceptance', 'outreach', rid, 40,
                                       'The acceptance check is due.',
                                       suggested={'method': 'POST', 'path': base + '/check-connection'},
                                       due_at=connection.get('next_check_at')))
        elif status == 'failed':
            actions.append(_action('retry_research', 'outreach', rid, 50,
                                   'Research or writing failed (%s).' % (r.get('error') or 'unknown'),
                                   suggested={'method': 'POST', 'path': base + '/accepted'}))
        elif status == 'ready':
            actions.append(_action('review_message', 'outreach', rid, 60,
                                   'A message draft is ready for human review and sending.',
                                   actor='human', suggested={'method': 'POST', 'path': base + '/sent'}))
        elif status == 'replied':
            actions.append(_action('handle_reply', 'outreach', rid, 70,
                                   'The contact answered; a human should respond.',
                                   actor='human', suggested=None))
        elif status == 'expired':
            actions.append(_action('review_expired', 'outreach', rid, 30,
                                   'The connection attempt expired without a confirmed acceptance.',
                                   actor='human', suggested={'method': 'POST', 'path': base + '/accepted'}))

    for j in discovery_store.list(limit=1000):
        if j['status'] in ('blocked', 'partial', 'failed', 'timed_out'):
            actions.append(_action('review_job', 'discovery', j['id'], 45,
                                   'Discovery job ended as %s (%s).' % (j['status'], (j.get('error') or 'no error code')),
                                   actor='human', due_at=j['updated']))

    for c in campaign_store.list(limit=1000) if campaign_store else []:
        if c['status'] == 'awaiting_review':
            actions.append(_action('review_campaign', 'campaign', c['id'], 65,
                                   'The campaign finished its automation and has drafts to review.',
                                   actor='human', suggested={'method': 'GET', 'path': '/api/v1/campaigns/' + c['id']}))
        elif c['status'] == 'failed':
            actions.append(_action('review_campaign', 'campaign', c['id'], 55,
                                   'The campaign failed (%s).' % (c.get('error') or 'unknown'),
                                   actor='human', suggested={'method': 'GET', 'path': '/api/v1/campaigns/' + c['id']}))

    if kind: actions = [a for a in actions if a['type'] == kind]
    actions.sort(key=lambda a: (-a['priority'], a['due_at'] or 0))
    return actions[:limit]
