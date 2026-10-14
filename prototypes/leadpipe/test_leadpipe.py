"""Offline checks of the deterministic helpers (synthetic data, no network)."""
import unittest
from unittest.mock import patch

import leadpipe as lp

PROFILE = 'https://www.linkedin.com/in/synthetic-person'
SITE = '### SOURCE https://synthetic.example\nTeam\nERIKA MUSTERFRAU. Rechtsanwältin. Partnerin.\nMAX BEISPIEL. Rechtsanwalt.\nImpressum: Musterfrau Rechtsanwälte PartG mbB, München'


def lead(name='Dr. Erika Musterfrau', role='Rechtsanwältin | Partnerin', company='Musterfrau Rechtsanwälte'):
    return {'name': name, 'current_role': role, 'company': company, 'linkedin_url': PROFILE, 'evidence': []}


class HelperTests(unittest.TestCase):
    def test_linkedin_profile_canonical(self):
        self.assertEqual(lp.linkedin_profile('https://de.linkedin.com/in/synthetic-person/de'), PROFILE)
        self.assertIsNone(lp.linkedin_profile('https://www.linkedin.com/company/synthetic'))

    def test_clean_company_drops_placeholders(self):
        for value in ('Selbstständig', 'meiner Kanzlei', ' Kanzlei ', ''):
            self.assertIsNone(lp.clean_company(value))
        self.assertEqual(lp.clean_company('Musterfrau Rechtsanwälte'), 'Musterfrau Rechtsanwälte')

    def test_name_matching_ignores_titles(self):
        self.assertTrue(lp.name_in('Dr. Erika Musterfrau, LL.M.', 'Erika Musterfrau – Partnerin'))
        self.assertFalse(lp.name_in('Erika Musterfrau', 'Erika Beispiel'))

    def test_role_quote_must_sit_near_the_person(self):
        self.assertTrue(lp.near('Erika Musterfrau', 'Rechtsanwältin. Partnerin.', SITE))
        far = SITE + '\n' + 'x' * 600 + '\nSenior Partner'
        self.assertFalse(lp.near('Max Beispiel', 'Senior Partner', far))

    def test_decision_maker_title_is_checked_on_the_quote(self):
        for text in ('Partner', 'Partnerin', 'Gründungspartner', 'Inhaber der Kanzlei', 'Geschäftsführender Gesellschafter',
                     'Managing Partner', 'Local Partner'):
            with self.subTest(text=text): self.assertTrue(lp.is_decision_maker_text(text))
        for text in ('Rechtsanwalt', 'Lawyer', 'Associate Partner', 'Senior Associate', 'Rechtsanwalt bei Muster & Partner mbB',
                     'Ansprechpartner', 'Musterfrau Partnerschaft mbB'):
            with self.subTest(text=text): self.assertFalse(lp.is_decision_maker_text(text))

    def test_site_score_needs_person_and_rejects_third_parties(self):
        l = lead()
        self.assertLess(lp.site_score(l, 'https://rocketreach.co/erika-musterfrau', 'Erika Musterfrau Partnerin'), 3)
        self.assertEqual(lp.site_score(l, 'https://musterfrau-legal.de/team', 'Max Beispiel only'), 0)
        self.assertGreaterEqual(lp.site_score(l, 'https://musterfrau-legal.de/team/erika-musterfrau', SITE), 3)
        self.assertLess(lp.site_score(l, 'https://unrelated-news.example/article', SITE), 3)
        # Directory with the person's slug in the path is still not the firm's website.
        self.assertLess(lp.site_score(l, 'https://anwaltsliste.example/erika-musterfrau', SITE), 3)
        # Short brand names and umlauts in domains (PwC Legal -> legal.pwc.de, Rödl -> roedl.com).
        pwc = lead(name='Erika Musterfrau', company='PwC Legal München')
        self.assertGreaterEqual(lp.site_score(pwc, 'https://legal.pwc.de/de/anwaelte/erika-musterfrau', SITE), 3)
        roedl = lead(name='Erika Musterfrau', company='Rödl & Partner')
        self.assertGreaterEqual(lp.site_score(roedl, 'https://www.roedl.com/de/team', SITE), 3)

    def test_verbatim_is_whitespace_and_case_tolerant_only(self):
        self.assertTrue(lp.verbatim('musterfrau  rechtsanwälte', SITE))
        self.assertFalse(lp.verbatim('Musterfrau Legal', SITE))


class FirmPagesTests(unittest.TestCase):
    IMPRINT = 'Impressum Musterfrau Rechtsanwälte PartG mbB, vertreten durch die Partnerin Erika Musterfrau'

    def run_pages(self, results, pages):
        with patch.object(lp, 'fc_search', return_value=[{'url': u} for u in results]),              patch.object(lp, 'fc_scrape', side_effect=lambda url: pages.get(url, '')):
            return lp.firm_pages(lead(), 'München')

    def test_only_firm_domains_are_fetched_and_imprint_is_fallback(self):
        site, pages = self.run_pages(['https://gelbeseiten.de/x', 'https://musterfrau-legal.de/'],
                                     {'https://musterfrau-legal.de/': 'Willkommen', 'https://musterfrau-legal.de/impressum': self.IMPRINT,
                                      'https://gelbeseiten.de/x': SITE})
        self.assertEqual(site, 'https://musterfrau-legal.de')
        self.assertEqual(pages[0][0], 'https://musterfrau-legal.de/impressum')

    def test_directories_only_gives_no_website(self):
        self.assertEqual(self.run_pages(['https://gelbeseiten.de/x', 'https://rocketreach.co/y'], {'https://gelbeseiten.de/x': SITE}), (None, []))


class ConfirmTierTests(unittest.TestCase):
    def run_confirm(self, answer, pages=(('https://synthetic.example', SITE),)):
        with patch.object(lp, 'firm_pages', return_value=('https://synthetic.example', list(pages))), \
             patch.object(lp, 'llm_json', return_value=answer):
            return lp.confirm(lead(), 'München', ['Rechtsanwalt'])

    BASE = {'person_on_site': True, 'firm_in_sector': True, 'quote_sector': 'Rechtsanwälte PartG mbB',
            'firm_name': 'Musterfrau Rechtsanwälte PartG mbB', 'quote_firm': 'Musterfrau Rechtsanwälte PartG mbB, München',
            'in_city': True, 'quote_city': 'München', 'listed_as_non_partner': False}

    def test_verified_when_role_quoted_next_to_name(self):
        r = self.run_confirm({**self.BASE, 'is_decision_maker': True, 'quote_role': 'Rechtsanwältin. Partnerin.'})
        self.assertEqual(r['tier'], 'verified')
        self.assertEqual(r['company'], 'Musterfrau Rechtsanwälte PartG mbB')
        self.assertTrue(r['city_confirmed'])

    def test_invented_role_quote_is_not_verified(self):
        r = self.run_confirm({**self.BASE, 'is_decision_maker': True, 'quote_role': 'Managing Partnerin und Gründerin'})
        self.assertEqual(r['tier'], 'plausible')  # namesake firm, but the role claim itself is unproven

    def test_website_listing_as_employee_goes_to_review(self):
        r = self.run_confirm({**self.BASE, 'is_decision_maker': False, 'listed_as_non_partner': True})
        self.assertEqual(r['tier'], 'review')

    def test_plain_lawyer_title_is_not_verified(self):
        # Holzinger/Sowa case: the site only says "Rechtsanwalt"; namesake firm -> plausible, not verified.
        r = self.run_confirm({**self.BASE, 'quote_role': 'MAX BEISPIEL. Rechtsanwalt.'})
        self.assertEqual(r['tier'], 'plausible')
        other = lead(name='Max Beispiel', company='Musterfrau Rechtsanwälte')
        with patch.object(lp, 'firm_pages', return_value=('https://synthetic.example', [('https://synthetic.example', SITE)])), \
             patch.object(lp, 'llm_json', return_value={**self.BASE, 'quote_role': 'MAX BEISPIEL. Rechtsanwalt.'}):
            self.assertEqual(lp.confirm(other, 'München', ['Rechtsanwalt'])['tier'], 'review')

    def test_wrong_llm_quote_rescued_only_by_title_next_to_name(self):
        # Arendt case: LLM says Geschäftsführer but quotes "..., Rechtsanwalt"; page has the title by the name.
        gf = '### SOURCE x\nGeschäftsführer: Dr. Erika Musterfrau, Rechtsanwältin\nImpressum Musterfrau Rechtsanwälte PartG mbB, München'
        answer = {**self.BASE, 'role_on_site': 'Geschäftsführerin', 'quote_role': 'Dr. Erika Musterfrau, Rechtsanwältin'}
        self.assertEqual(self.run_confirm(answer, pages=(('https://synthetic.example', gf),))['tier'], 'verified')
        far = ('### SOURCE x\nGeschäftsführer: Max Beispiel\n' + 'x' * 400 +
               '\nDr. Erika Musterfrau, Rechtsanwältin\nImpressum Musterfrau Rechtsanwälte PartG mbB')
        self.assertNotEqual(self.run_confirm(answer, pages=(('https://synthetic.example', far),))['tier'], 'verified')

    def test_counsel_on_site_goes_to_review_even_for_namesake(self):
        site = SITE.replace('Rechtsanwältin. Partnerin.', 'Rechtsanwältin | Counsel')
        r = self.run_confirm({**self.BASE, 'quote_role': 'Rechtsanwältin | Counsel'}, pages=(('https://synthetic.example', site),))
        self.assertEqual(r['tier'], 'review')

    def test_requested_sector_term_on_page_counts_as_sector_evidence(self):
        answer = {**self.BASE, 'quote_sector': 'not on the page', 'quote_role': 'Rechtsanwältin. Partnerin.'}
        self.assertEqual(self.run_confirm(answer)['tier'], 'verified')  # "Rechtsanwälte" is literally on the page
        with patch.object(lp, 'firm_pages', return_value=('https://synthetic.example', [('https://synthetic.example', SITE)])), \
             patch.object(lp, 'llm_json', return_value=answer):
            self.assertEqual(lp.confirm(lead(), 'München', ['Steuerberater'])['tier'], 'review')

    def test_no_website_goes_to_review(self):
        with patch.object(lp, 'firm_pages', return_value=(None, [])):
            r = lp.confirm(lead(company='Selbstständig'), 'München', ['Rechtsanwalt'])
        self.assertEqual((r['tier'], r['company']), ('review', None))


if __name__ == '__main__':
    unittest.main()
