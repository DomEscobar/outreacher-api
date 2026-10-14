"""Deterministic job-local evaluate/repair. Never trusts model feedback as policy."""
from collections import Counter
from models import Lead, evaluate_lead, profile_key

VERDICTS = ('duplicate', 'invalid', 'schema', 'unsupported_fields', 'profile_provenance', 'scope', 'role', 'quota')
MAX_REPAIRS = 6
MAX_REPAIR_PASSES = 2


class CandidateState:
    def __init__(self):
        self.pending = {}
        self.repaired_count = 0

    def feedback(self, exclusions):
        # These entries are DATA, not free-form reflection or executable instructions.
        items = []
        for key, entry in list(self.pending.items()):
            if key in exclusions or entry['offered'] >= MAX_REPAIR_PASSES:
                del self.pending[key]
                exclusions.add(key)
                continue
            entry['offered'] += 1
            partial = dict(entry['partial'])
            partial['evidence'] = partial['evidence'][:3]
            items.append({'profile_url': key, 'verdict': entry['verdict'],
                          'gaps': entry['gaps'], 'observed_partial': partial})
        return items

    def evaluate(self, result, observations, exclusions, criteria, limit):
        counts = Counter(v['verdict'] for v in result._candidate_verdicts)
        fresh = []
        seen = set(exclusions)
        for proposed in result.leads:
            key = profile_key(proposed.linkedin_url)
            if key in seen:
                counts['duplicate'] += 1
                continue
            old = self.pending.get(key)
            lead = proposed
            if old:
                # Carry only facts already validated against actual tool observations.
                # Retained evidence still passes the identical gate against retained
                # successful observations; neither feedback nor model claims are proof.
                value = proposed.model_dump()
                for field in ('current_role', 'company', 'company_website'):
                    if value[field] is None and old['partial'].get(field):
                        value[field] = old['partial'][field]
                evidence = value['evidence'] + old['partial']['evidence']
                value['evidence'] = list({str(e): e for e in evidence}.values())[:10]
                lead = Lead.model_validate(value)
            partial, verdict, gaps = evaluate_lead(lead, observations, seen, criteria.titles, criteria.keywords)
            if verdict == 'accepted' and len(fresh) < limit:
                fresh.append(partial)
                seen.add(key)
                exclusions.add(key)
                self.pending.pop(key, None)
                self.repaired_count += int(old is not None)
                continue
            if verdict == 'accepted': verdict = 'quota'  # supported but beyond remaining requested target
            counts[verdict] += 1
            # Supported, definitively nonmatching role is not a repair target. Missing sector
            # evidence is repairable (the agent can still research the firm's own website).
            permanent = verdict in {'duplicate', 'quota'} or (verdict == 'role' and lead.current_role is not None)
            if permanent:
                exclusions.add(key)
                self.pending.pop(key, None)
            elif partial and (key in self.pending or len(self.pending) < MAX_REPAIRS):
                self.pending[key] = {'partial': partial, 'verdict': verdict, 'gaps': gaps,
                                     'offered': old['offered'] if old else 0}
            seen.add(key)  # Candidate-local duplicates in this envelope never count twice.
        return fresh, {key: counts[key] for key in VERDICTS}
