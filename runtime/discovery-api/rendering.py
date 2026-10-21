"""Render API JSON from independently accepted leads, never model free-form text."""
from response_models import AcceptedResult


def render_accepted(criteria, result, leads, rejected_unqualified_count=0):
    payload = {'leads': leads, 'returned_count': len(leads), 'requested_count': criteria.limit,
               'missing_count': criteria.limit - len(leads),
               'metadata': {'source': criteria.source, 'outcome': result.outcome,
                            'blocked_reason': result.blocked_reason,
                            'qualified_count': len(leads),
                            'rejected_count': result._stats.get('rejected_count', len(result.leads) - len(leads)),
                            'rejected_unqualified_count': result._stats.get('rejected_unqualified_count', rejected_unqualified_count),
                            **{k: v for k, v in result._stats.items() if k != 'rejected_count'},
                            'evidence_validation': 'verbatim_observed_tool_output',
                            'fit_reason_is_inference': True, 'contact_permission_established': False}}
    # Fail before persistence if runtime counters or accepted lead rendering drift
    # from the API contract (not on the subsequent HTTP readback).
    return AcceptedResult.model_validate(payload).model_dump(mode='json')


def render_search(criteria, qualified, review, stats):
    """source=search: leads at or above min_verification count toward the limit; the rest only on request."""
    counts = stats['verification_counts']
    payload = {'leads': qualified + review, 'returned_count': len(qualified) + len(review),
               'requested_count': criteria.limit, 'missing_count': max(0, criteria.limit - len(qualified)),
               'metadata': {'source': 'search', 'outcome': 'completed' if len(qualified) >= criteria.limit else 'partial',
                            'blocked_reason': None, 'qualified_count': len(qualified),
                            'rejected_count': max(0, sum(counts.values()) - len(qualified) - len(review)),
                            'candidate_count': stats['candidate_count'], 'verification_counts': counts,
                            'review_count': counts.get('review', 0), 'cost_usd': stats['cost_usd'],
                            'min_verification': criteria.min_verification,
                            'expanded_keywords': stats.get('expanded_keywords', []),
                            'expanded_titles': stats.get('expanded_titles', []),
                            'expanded_facets': stats.get('expanded_facets', []), 'regions': stats.get('regions', []),
                            'skipped_known': stats.get('skipped_known', 0), 'demoted_queries': stats.get('demoted_queries', 0),
                            'excluded_known_company': stats.get('excluded_known_company', 0),
                            'excluded_by_registry': stats.get('excluded_by_registry', 0),
                            'excluded_same_company': stats.get('excluded_same_company', 0),
                            'max_per_company': criteria.max_per_company,
                            'provider_calls': stats['provider_calls'], 'llm_calls': stats['llm_calls'],
                            'provider_errors': stats.get('provider_errors', {}), 'call_seconds': stats.get('call_seconds', {}),
                            'queries': stats['queries'], 'people_queries': stats.get('people_queries', 0),
                            'stale_rejected': stats.get('stale_rejected', 0), 'firms_checked': stats['firms_checked'],
                            'strategies': stats['strategies'], 'qualified_by_strategy': stats.get('qualified_by_strategy', {}),
                            'elapsed_seconds': stats['elapsed_seconds'],
                            'stop_reason': stats['stop_reason'],
                            'evidence_validation': 'verbatim_fetched_source_text',
                            'fit_reason_is_inference': True, 'contact_permission_established': False}}
    return AcceptedResult.model_validate(payload).model_dump(mode='json')
