import copy
import csv
import io
import tempfile
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import domain_pricing as dp
import engine
from godaddy_pricing import pricing_record
from test_domain_pricing import OfflineTests
from test_godaddy_integration import result


class PricingResultsTests(OfflineTests):
    def fixture(self):
        project = engine.new_project('100-name pricing interface test')
        project['names'] = [f'Candidate{i:03d}' for i in range(100)]
        project['reviews'] = {n.casefold(): {'decision': 'Keep' if i % 2 else 'Reject',
                                             'decision_source': 'manual'}
                              for i, n in enumerate(project['names'])}
        project['runs'] = [{'id': 'historical', 'results': []}]
        records = {}
        for i, name in enumerate(project['names'][:70]):
            domain = engine.slug(name) + '.com'
            data = result(domain=domain, price=1500 + i * 100)
            if i < 16:
                data['prices'][0]['price']['value'] = 1500 + (15 - i) * 100
            elif i < 22:
                data['prices'][0]['price']['value'] = 10100
            else:
                data['available'] = False
            records[domain] = pricing_record(domain, data)
        project['domain_pricing'] = records
        return project

    def test_70_saved_quotes_counts_and_default_price_order(self):
        project = self.fixture()
        before = copy.deepcopy(project)
        rows = dp.cached_pricing_rows(project)
        self.assertEqual(len(rows), 70)
        counts = {view: len(dp.filter_pricing_rows(rows, view)) for view in dp.RESULT_FILTERS}
        self.assertEqual(counts, {'All checked domains': 70, 'Provisional under $100': 16,
                                  'Verified affordable': 0, 'Over budget': 6, 'Stale quotes': 0,
                                  'Unavailable or unknown price': 48})
        provisional = dp.filter_pricing_rows(rows)
        self.assertEqual(provisional[0]['Candidate names'], 'Candidate015')
        self.assertEqual(provisional[0]['Acquisition price (USD)'], 15.0)
        prices = [row['Acquisition price (USD)'] for row in provisional]
        self.assertEqual(prices, sorted(prices))
        self.assertTrue(all(row['Budget classification'] == dp.PROVISIONAL for row in rows[:16]))
        self.assertEqual(project, before)

    def test_search_matches_name_domain_notes_without_network(self):
        project = self.fixture()
        project['domain_pricing']['candidate015.com']['message'] = 'Special registrar note'
        rows = dp.cached_pricing_rows(project)
        for text in ('Candidate015', 'candidate015.COM', '  SPECIAL REGISTRAR  '):
            found = dp.filter_pricing_rows(rows, search=text)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]['Domain name'], 'candidate015.com')
        self.assertEqual(dp.filter_pricing_rows(rows, search='nonexistent'), [])
        with self.assertRaises(ValueError):
            dp.filter_pricing_rows(rows, 'Unsupported')

    def test_expired_provisional_quotes_keep_numeric_prices_and_saved_bytes(self):
        project = self.fixture()
        checked = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for record in project['domain_pricing'].values():
            record['checked_at'] = checked.isoformat()
        with tempfile.TemporaryDirectory() as root:
            engine.save_project(project, root)
            path = engine.project_path(project['id'], root)
            before = path.read_bytes()
            with patch('domain_pricing.datetime') as clock:
                clock.fromisoformat.side_effect = datetime.fromisoformat
                clock.now.return_value = checked + timedelta(seconds=dp.CACHE_SECONDS - 1)
                fresh_rows = dp.cached_pricing_rows(engine.load_project(path))
                self.assertEqual(len(dp.filter_pricing_rows(fresh_rows)), 16)
                self.assertEqual(len(dp.filter_pricing_rows(fresh_rows, 'Over budget')), 6)
                clock.now.return_value = checked + timedelta(seconds=dp.CACHE_SECONDS)
                expired_rows = dp.cached_pricing_rows(engine.load_project(path))
                self.assertEqual(len(dp.filter_pricing_rows(expired_rows)), 0)
                self.assertEqual(len(dp.filter_pricing_rows(expired_rows, 'Stale quotes')), 22)
                self.assertEqual(len(dp.filter_pricing_rows(expired_rows, 'Unavailable or unknown price')), 48)
                self.assertTrue(all(row['Price reliability'] == 'Stale' for row in expired_rows))
                self.assertEqual(
                    {row['Domain name']: row['Acquisition price (USD)'] for row in expired_rows},
                    {row['Domain name']: row['Acquisition price (USD)'] for row in fresh_rows})
                self.assertEqual(sum(row['Acquisition price (USD)'] is not None for row in expired_rows), 22)
            self.assertEqual(path.read_bytes(), before)

    def test_stale_unknown_and_verified_final_totals_are_separate(self):
        project = self.fixture()
        project['domain_pricing']['candidate000.com']['checked_at'] = '2020-01-01T00:00:00+00:00'
        project['domain_pricing']['candidate001.com'] = dp.observed_quote(
            'candidate001.com', 'Available', False, '100', '', 'Verified all-in listing', total_verified=True)
        rows = dp.cached_pricing_rows(project)
        self.assertEqual(len(dp.filter_pricing_rows(rows)), 14)
        verified = dp.filter_pricing_rows(rows, 'Verified affordable')
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]['Acquisition price (USD)'], 100.0)
        self.assertEqual(verified[0]['Price reliability'], 'Final total verified')
        self.assertEqual(len(dp.filter_pricing_rows(rows, 'Stale quotes')), 1)
        self.assertEqual(len(dp.filter_pricing_rows(rows, 'Unavailable or unknown price')), 48)
        project['domain_budget'] = 50
        self.assertEqual(dp.filter_pricing_rows(dp.cached_pricing_rows(project), 'Verified affordable'), [])

    def test_shared_domain_and_old_targets_remain_visible_without_duplicates(self):
        project = self.fixture()
        project['reviews']['candidate099']['targets'] = {'Domain': 'candidate000.com'}
        project['reviews']['candidate001']['targets'] = {'Domain': 'changed.com'}
        rows = dp.cached_pricing_rows(project)
        self.assertEqual(len(rows), 70)
        shared = next(row for row in rows if row['Domain name'] == 'candidate000.com')
        self.assertEqual(shared['Candidate names'], 'Candidate000; Candidate099')
        former = next(row for row in rows if row['Domain name'] == 'candidate001.com')
        self.assertEqual(former['Candidate names'], '(not in current candidate list)')

    def test_csv_shape_filtering_and_formula_safety(self):
        project = self.fixture()
        project['names'][15] = '=Candidate015'
        project['domain_pricing']['candidate015.com']['message'] = '  =HYPERLINK("example")'
        selected = dp.filter_pricing_rows(dp.cached_pricing_rows(project), search='candidate015.com')
        exported = list(csv.DictReader(io.StringIO(dp.pricing_results_csv(selected).decode('utf-8-sig'))))
        self.assertEqual(len(exported), 1)
        self.assertEqual(list(exported[0]), list(dp.RESULT_COLUMNS))
        self.assertEqual(exported[0]['Acquisition price (USD)'], '15.0')
        self.assertTrue(exported[0]['Candidate names'].startswith("'="))
        self.assertTrue(exported[0]['Pricing notes'].startswith("'"))
        self.assertEqual(exported[0]['Price reliability'], 'Provisional')
        self.assertNotIn('mock-godaddy-token', dp.pricing_results_csv(selected).decode())
        self.assertEqual(dp.pricing_results_csv([]).decode('utf-8-sig').strip(),
                         ','.join(dp.RESULT_COLUMNS))

    def test_consolidated_table_reruns_filters_and_search_preserve_saved_project(self):
        project = self.fixture()
        with tempfile.TemporaryDirectory() as root:
            path = engine.project_path(project['id'], root)
            engine.save_project(project, root)
            with patch('engine.PROJECTS', Path(root)), patch('engine.DATA', Path(root)), \
                    patch('engine.migrate_local_projects'), patch('engine.stored_key', return_value=''), \
                    patch('engine.fetch_json', side_effect=AssertionError('No brand research')):
                with patch('godaddy_pricing.GoDaddyPricing', side_effect=AssertionError('No provider calls')):
                    app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=30)
                    self.assertFalse(app.exception)
                    original_bytes = path.read_bytes()
                    view = next(x for x in app.selectbox if x.label == 'Show pricing results')
                    self.assertEqual(view.value, 'All checked domains')
                    pricing_table = next(x for x in app.dataframe if 'Candidate names' in x.value.columns)
                    self.assertEqual(list(pricing_table.proto.column_order)[:3],
                                     ['Candidate names', 'Acquisition price (USD)', 'Budget classification'])
                    self.assertEqual(pricing_table.proto.row_height, 44)
                    self.assertTrue(json.loads(pricing_table.proto.columns)['Candidate names']['pinned'])
                    def table():
                        return next(x.value for x in app.dataframe if 'Candidate names' in x.value.columns)
                    self.assertEqual(len(table()), 70)
                    self.assertEqual(table().iloc[0]['Acquisition price (USD)'], 15.0)
                    self.assertTrue(any('Checked domains: 70' in x.value for x in app.markdown))
                    app = view.set_value('All checked domains').run()
                    self.assertEqual(len(table()), 70)
                    app = next(x for x in app.text_input if x.label == 'Search pricing results').set_value('candidate015.com').run()
                    self.assertEqual(len(table()), 1)
                    self.assertEqual(table().iloc[0]['Candidate names'], 'Candidate015')
                    app = next(x for x in app.text_input if x.label == 'Search pricing results').set_value('').run()
                    app = next(x for x in app.selectbox if x.label == 'Show pricing results').set_value('Over budget').run()
                    self.assertEqual(len(table()), 6)
                    app = next(x for x in app.selectbox if x.label == 'Show pricing results').set_value('Verified affordable').run()
                    self.assertEqual(len(table()), 0)
                    app = next(x for x in app.selectbox if x.label == 'Show pricing results').set_value('Unavailable or unknown price').run()
                    self.assertEqual(len(table()), 48)
                    app.run()
                    self.assertEqual(app.session_state.project['reviews'], project['reviews'])
                    self.assertEqual(app.session_state.project['runs'], project['runs'])
                    self.assertEqual(app.session_state.project['names'], project['names'])
                    self.assertEqual(path.read_bytes(), original_bytes)

    def test_lanternmarker_structured_observation_survives_form_save_restart_and_reruns(self):
        project = self.fixture()
        project['names'][-1] = 'LanternMarker'
        project['reviews']['lanternmarker'] = project['reviews'].pop('candidate099')
        project['reviews']['lanternmarker']['notes'] = 'Observed asking price: $1,750. Preserve this evidence.'
        previous = pricing_record('lanternmarker.com', {'domain': 'lanternmarker.com', 'available': False})
        project['domain_pricing']['lanternmarker.com'] = previous
        with tempfile.TemporaryDirectory() as root:
            path = engine.project_path(project['id'], root)
            engine.save_project(project, root)
            original = copy.deepcopy(project)
            with patch('engine.PROJECTS', Path(root)), patch('engine.DATA', Path(root)), \
                    patch('engine.migrate_local_projects'), patch('engine.stored_key', return_value=''), \
                    patch('godaddy_pricing.GoDaddyPricing', side_effect=AssertionError('No provider calls')):
                app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=30)
                self.assertFalse(app.exception)
                next(x for x in app.selectbox if x.label == 'Listing candidate').set_value('LanternMarker')
                next(x for x in app.selectbox if x.label == 'Listing premium status').set_value('Yes')
                next(x for x in app.text_input if x.label == 'Observed acquisition asking price (USD, optional)').set_value('1750')
                next(x for x in app.text_input if x.label == 'Registrar/listing source and URL or reference').set_value('Offline registrar listing evidence')
                next(x for x in app.checkbox if x.label.startswith('I verified this current listing')).check()
                app = next(x for x in app.button if x.label == 'Save observed listing').click().run(timeout=30)
                self.assertFalse(app.exception)
                saved = engine.load_project(path)
                self.assertEqual(saved['reviews'], original['reviews'])
                self.assertEqual(saved['runs'], original['runs'])
                self.assertEqual(saved['names'], original['names'])
                self.assertEqual(saved['domain_pricing_history']['lanternmarker.com'], [previous])
                quote = saved['domain_pricing']['lanternmarker.com']
                self.assertTrue(quote['manual'])
                self.assertEqual(quote['acquisition_price'], '1750')
                self.assertIsNone(quote['annual_renewal_price'])
                self.assertEqual(dp.classification(quote), dp.OVER_BUDGET)
                self.assertNotIn('lanternmarker.com', dp.plan_prices(saved, refresh=True, provider='GoDaddy')[1])
                for domain, original_quote in original['domain_pricing'].items():
                    if domain != 'lanternmarker.com':
                        self.assertEqual(saved['domain_pricing'][domain], original_quote)
                before = path.read_bytes()
                app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=30)
                app = next(x for x in app.selectbox if x.label == 'Show pricing results').set_value('Over budget').run()
                app = next(x for x in app.text_input if x.label == 'Search pricing results').set_value('LanternMarker').run()
                table = next(x.value for x in app.dataframe if 'Candidate names' in x.value.columns)
                self.assertEqual(len(table), 1)
                self.assertEqual(table.iloc[0]['Candidate names'], 'LanternMarker')
                self.assertEqual(table.iloc[0]['Acquisition price (USD)'], 1750.0)
                self.assertEqual(table.iloc[0]['Budget classification'], dp.OVER_BUDGET)
                self.assertEqual(table.iloc[0]['Price reliability'], 'Provisional')
                self.assertEqual(path.read_bytes(), before)

    def test_empty_cache_has_headers_and_no_requests(self):
        project = engine.new_project('No cached quotes')
        self.assertEqual(dp.cached_pricing_rows(project), [])
        self.assertEqual(dp.filter_pricing_rows([]), [])

    def test_expired_quote_and_failed_refresh_render_without_network(self):
        project = engine.new_project('Stale display')
        project['names'] = ['Example']
        quote = pricing_record('example.com', result(price=1900))
        quote['checked_at'] = '2020-01-01T00:00:00+00:00'
        project['domain_pricing'] = {'example.com': quote}
        project['domain_pricing_attempts'] = {'example.com': {
            'checked_at': '2020-01-02T00:00:00+00:00', 'provider': 'GoDaddy',
            'status': 'Failed', 'error': 'Offline fixture failure'}}
        with tempfile.TemporaryDirectory() as root:
            engine.save_project(project, root)
            path = engine.project_path(project['id'], root)
            before = path.read_bytes()
            with patch('engine.PROJECTS', Path(root)), patch('engine.DATA', Path(root)), \
                    patch('engine.migrate_local_projects'), patch('engine.stored_key', return_value=''), \
                    patch('godaddy_pricing.GoDaddyPricing', side_effect=AssertionError('No provider calls')):
                app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=30)
                self.assertFalse(app.exception)
                table = next(x.value for x in app.dataframe if 'Candidate names' in x.value.columns)
                self.assertEqual(len(table), 1)
                self.assertEqual(table.iloc[0]['Acquisition price (USD)'], 19)
                self.assertEqual(table.iloc[0]['Latest refresh error'], 'Offline fixture failure')
                self.assertEqual(table.iloc[0]['Domain eligibility'], 'Investigate')
                self.assertEqual(table.iloc[0]['Price-check date'], quote['checked_at'])
                self.assertEqual(path.read_bytes(), before)
