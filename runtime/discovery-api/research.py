"""Bounded per-job accounting and candidate-local validation. No model calls here."""
import copy
import json
import time
from urllib.parse import parse_qsl, urlsplit
from pydantic import ValidationError
from models import DiscoveryResult, Lead, Observation, profile_key, public_url
from policy import ALLOWED_TOOLS
from evaluation import CandidateState, VERDICTS


class BackendError(Exception):
    def __init__(self, code): self.code = code


SAFE_ERRORS = frozenset({'timed_out', 'cancelled', 'output_limit', 'prompt_limit', 'invalid_stream',
                        'invalid_backend_result', 'invalid_result_schema', 'backend_exit', 'turn_limit', 'procedure_unavailable',
                        'search_not_configured'})


def safe_error(code):
    return code if code in SAFE_ERRORS else 'backend_failed'


class Ledger:
    def __init__(self, criteria, output_limit):
        self.started = time.monotonic()
        self.deadline = self.started + criteria.budget.seconds
        self.turn_limit = criteria.budget.max_turns
        self.turns = self.output_bytes = 0
        self.output_limit = output_limit
        self.iterations = []
        self.pass_tool_uses = 0

    def check(self, cancelled):
        if cancelled(): raise BackendError('cancelled')
        if time.monotonic() >= self.deadline: raise BackendError('timed_out')
        if self.output_bytes > self.output_limit: raise BackendError('output_limit')

    @property
    def remaining_turns(self): return self.turn_limit - self.turns


def parse_events(events):
    finals = [e for e in events if e.get('type') == 'result']
    if len(finals) != 1 or finals[0].get('exit_code') != 0 or not isinstance(finals[0].get('text'), str):
        raise BackendError('invalid_backend_result')
    observations = [Observation(e['output'], e['name']) for e in events if e.get('type') == 'tool_result'
                    and e.get('name') in ALLOWED_TOOLS and not e.get('is_error')
                    and isinstance(e.get('output'), str)]
    text = finals[0]['text'].strip()
    try:
        value = json.loads(text)
    except ValueError:
        # Skip complete objects, so nested lead objects cannot be alternative envelopes.
        candidates, offset = [], 0
        decoder = json.JSONDecoder()
        while offset < len(text):
            if text[offset] != '{':
                offset += 1
                continue
            try: obj, consumed = decoder.raw_decode(text[offset:])
            except ValueError:
                offset += 1
                continue
            if isinstance(obj, dict): candidates.append(obj)
            offset += consumed
        if len(candidates) != 1:
            raise BackendError('invalid_result_schema') from None
        value = candidates[0]
    # Strict outer contract first; a malformed envelope is never salvaged.
    if not isinstance(value, dict) or not isinstance(value.get('leads', []), list) or len(value.get('leads', [])) > 30:
        raise BackendError('invalid_result_schema')
    try:
        result = DiscoveryResult.model_validate({**value, 'leads': []})
    except Exception:
        raise BackendError('invalid_result_schema') from None
    raw = value.get('leads', [])
    rejected = normalized = 0
    for original in raw:
        item = copy.deepcopy(original)
        changed = False
        if isinstance(item, dict) and isinstance(item.get('company_website'), str):
            try: public_url(item['company_website'])
            except (ValueError, TypeError):
                item['company_website'] = None
                missing = item.get('missing_information', [])
                if isinstance(missing, list):
                    item['missing_information'] = list(dict.fromkeys(missing + ['company_website'])) if all(isinstance(x, str) for x in missing) else missing
                changed = True
        try: lead = Lead.model_validate(item)
        except (ValidationError, TypeError, ValueError) as exc:
            rejected += 1
            errors = exc.errors() if isinstance(exc, ValidationError) else []
            category = 'schema' if any(e['type'] == 'extra_forbidden' for e in errors) else 'invalid'
            result._candidate_verdicts.append({'verdict': category})
            continue
        result.leads.append(lead)
        normalized += int(changed)
    result._stats = {'candidate_count': len(raw), 'schema_rejected_count': rejected,
                     'normalized_website_count': normalized}
    return result, observations


def pass_data(criteria, iteration, batch_size):
    """Never rewrite supplied search URLs/opaque identifiers; page hint is separate data."""
    start = int(dict(parse_qsl(urlsplit(criteria.target_url()).query)).get('page', '1')) if criteria.source == 'linkedin' else 1
    return {'iteration': iteration, 'batch_size': batch_size,
            'normal_search_page': start + iteration - 1 if criteria.source == 'linkedin' else None,
            'role_focus': criteria.titles[(iteration - 1) % len(criteria.titles)] if criteria.titles else None}


def accumulate(backend, criteria, previous, cancelled):
    ledger = Ledger(criteria, backend.max_output)
    exclusions = {profile_key(p) for p in previous}
    leads, all_observations = [], []
    candidates = CandidateState()
    rejection_counts = {key: 0 for key in VERDICTS}
    total_candidates = total_rejected = total_normalized = total_unqualified = total_out_of_scope = 0
    outcome, blocked_reason, stop_reason = 'partial', None, 'search_exhausted'
    iteration = 0
    while len(leads) < criteria.limit:
        if ledger.remaining_turns <= 0:
            stop_reason = 'turn_limit'
            break
        iteration += 1
        data = pass_data(criteria, iteration, min(3, criteria.limit - len(leads)))
        if iteration > 10 or (data['normal_search_page'] or 1) > 10:
            stop_reason = 'search_exhausted'
            break
        data['repair_candidates'] = candidates.feedback(exclusions)
        # Adaptive full CLI reservations, NOT tool calls masquerading as model turns.
        # Larger budgets leave room for per-candidate firm website research (search + extract).
        full_yield = ledger.iterations and ledger.iterations[-1]['accepted_count'] == ledger.iterations[-1]['requested_count'] and not data['repair_candidates']
        data['turn_reservation'] = min(ledger.remaining_turns,
            8 if criteria.budget.max_turns <= 30 else 14 if full_yield else 16)
        row = {'iteration': iteration, 'requested_count': data['batch_size'],
               'normal_search_page': data['normal_search_page'], 'accepted_count': 0,
               'candidate_count': 0, 'rejected_count': 0, 'normalized_website_count': 0,
               'rejected_unqualified_count': 0, 'rejected_out_of_scope_count': 0,
               'tool_use_count': 0, 'charged_turns': 0, 'outcome': 'failed', 'error': None,
               'rejection_counts': {key: 0 for key in VERDICTS}}
        before = ledger.turns
        ledger.pass_tool_uses = 0
        try:
            ledger.check(cancelled)
            result, observations, uses = backend.run_pass(criteria, exclusions, cancelled, ledger, data)
            row['tool_use_count'] = uses
            # Backend reserves its whole CLI cap, while recording actual tool events.
            row['charged_turns'] = ledger.turns - before
            # Retain exact successful observations across job-local repair passes.
            all_observations.extend(observations)
            fresh, verdicts = candidates.evaluate(result, all_observations, exclusions, criteria, criteria.limit - len(leads))
            row['rejection_counts'] = verdicts
            row['rejected_unqualified_count'] = verdicts['role']
            row['rejected_out_of_scope_count'] = verdicts['scope']
            stats = result._stats
            row.update(candidate_count=stats['candidate_count'],
                       rejected_count=stats['candidate_count'] - len(fresh),
                       normalized_website_count=stats['normalized_website_count'],
                       accepted_count=len(fresh), outcome=result.outcome)
            leads.extend(fresh)
            outcome, blocked_reason = result.outcome, result.blocked_reason
            if outcome == 'blocked': stop_reason = 'blocked'
            elif len(leads) == criteria.limit: stop_reason = 'target_reached'; outcome = 'completed'
            else: outcome = 'partial'
        except BackendError as exc:
            stop_reason = safe_error(exc.code)
            row['error'] = stop_reason
            row['charged_turns'] = ledger.turns - before
            if stop_reason == 'cancelled' or not leads: raise
            outcome = 'partial'
        except Exception:
            stop_reason = 'backend_failed'
            row['error'] = stop_reason
            if not leads: raise BackendError(stop_reason) from None
            outcome = 'partial'
        finally:
            row['tool_use_count'] = ledger.pass_tool_uses
            row['charged_turns'] = ledger.turns - before
            ledger.iterations.append(row)
            total_candidates += row['candidate_count']
            total_rejected += row['rejected_count']
            total_normalized += row['normalized_website_count']
            total_unqualified += row['rejected_unqualified_count']
            total_out_of_scope += row['rejected_out_of_scope_count']
            for key in VERDICTS: rejection_counts[key] += row['rejection_counts'][key]
        if stop_reason in {'blocked', 'target_reached'} or row['error']: break
    result = DiscoveryResult.model_validate({'outcome': outcome if stop_reason == 'blocked' else ('completed' if len(leads) == criteria.limit else 'partial'),
                                             'leads': leads, 'blocked_reason': blocked_reason if stop_reason == 'blocked' else None})
    result._stats = {'candidate_count': total_candidates, 'rejected_count': total_rejected,
                     'qualified_count': len(leads),
                     'rejected_unqualified_count': total_unqualified, 'rejected_out_of_scope_count': total_out_of_scope,
                     'normalized_website_count': total_normalized, 'iterations': ledger.iterations,
                     'iteration_count': len(ledger.iterations), 'tool_use_count': sum(p['tool_use_count'] for p in ledger.iterations),
                     'charged_turns': ledger.turns, 'reserved_turns': ledger.turns,
                     'budget_accounting': 'full_cli_reservation', 'measured_model_iterations': None,
                     'rejection_counts': rejection_counts, 'repaired_count': candidates.repaired_count,
                     'output_bytes': ledger.output_bytes,
                     'elapsed_seconds': round(time.monotonic() - ledger.started, 3), 'stop_reason': stop_reason}
    return result, all_observations
