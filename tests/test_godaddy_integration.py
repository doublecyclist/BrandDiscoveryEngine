import copy
import csv
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from streamlit.testing.v1 import AppTest

import domain_pricing as dp
import engine
import godaddy_pricing as gd
from test_domain_pricing import OfflineTests, godaddy_response


def result(domain='example.com', inventory='REGISTRY', price=1500, fees=None):
    annual = {'term': 'YEAR', 'period': 1, 'price': {'currencyCode': 'USD', 'value': price},
              'renewalPrice': {'currencyCode': 'USD', 'value': 1998}}
    if fees is not None:
        annual['fees'] = fees
    return {'domain': domain, 'available': True, 'definitive': True, 'inventory': inventory, 'prices': [annual]}


class GoDaddyIntegrationTests(OfflineTests):
    def test_quoted_budget_boundaries_remain_provisional(self):
        for price, expected in [(1500, dp.PROVISIONAL), (9900, dp.PROVISIONAL),
                                (10000, dp.PROVISIONAL), (10001, dp.OVER_BUDGET),
                                (10100, dp.OVER_BUDGET)]:
            record = gd.pricing_record('example.com', result(price=price))
            self.assertEqual(dp.classification(record), expected)
            self.assertFalse(record['total_verified'])
            self.assertNotEqual(dp.classification(record), dp.AFFORDABLE)
        record = gd.pricing_record('example.com', result(price=1500))
        self.assertEqual(dp.classification(record, 10), dp.OVER_BUDGET)

    def test_premium_resale_is_registered_and_fee_included(self):
        fees = [{'type': 'ONE_TIME_PREMIUM_DOMAIN_PURCHASE',
                 'fee': {'currencyCode': 'USD', 'value': 995000}}]
        record = gd.pricing_record('example.com', result(inventory='PREMIUM', price=2299, fees=fees))
        self.assertEqual(record['availability'], 'Registered')
        self.assertTrue(record['purchase_available'])
        self.assertTrue(record['premium'])
        self.assertEqual(record['registration_price'], '22.99')
        self.assertEqual(record['premium_purchase_fee'], '9950.00')
        self.assertEqual(record['acquisition_price'], '9972.99')
        self.assertEqual(record['annual_renewal_price'], '19.98')
        self.assertEqual(dp.classification(record), dp.OVER_BUDGET)

    def test_premium_incomplete_price_never_counts_as_affordable(self):
        for fees in (None, [], [{'fee': {'currencyCode': 'EUR', 'value': 2000}}], [{}]):
            record = gd.pricing_record('example.com', result(inventory='PREMIUM', fees=fees))
            self.assertNotEqual(dp.classification(record), dp.AFFORDABLE)
            self.assertIsNone(record['acquisition_price'])
        # A known component over budget is sufficient to reject, even if another fee is unknown.
        record = gd.pricing_record('example.com', result(inventory='PREMIUM', price=10100))
        self.assertEqual(dp.classification(record), dp.OVER_BUDGET)
        record = gd.pricing_record('example.com', result(inventory='REGISTRY_PREMIUM', price=995000))
        self.assertEqual(dp.classification(record), dp.OVER_BUDGET)
        record = gd.pricing_record('example.com', result(inventory='REGISTRY_PREMIUM', price=0))
        self.assertIsNone(record['acquisition_price'])
        self.assertEqual(dp.classification(record), dp.PREMIUM_UNKNOWN)

    def test_unavailable_unknown_and_error_cases(self):
        for changes in ({'available': False}, {'prices': []}, {'inventory': 'UNKNOWN'},
                        {'error': {'name': 'ERROR_UNKNOWN', 'message': 'mock-godaddy-token'}},
                        {'prices': [{'term': 'YEAR', 'period': 2,
                                     'price': {'currencyCode': 'USD', 'value': 100}}]}):
            record = gd.pricing_record('example.com', result() | changes)
            self.assertNotEqual(dp.classification(record), dp.AFFORDABLE)
            self.assertIsNone(record['acquisition_price'])
            self.assertNotIn('mock-godaddy-token', json.dumps(record))
        record = gd.pricing_record('example.com', result() | {'available': False})
        self.assertFalse(record['purchase_available'])
        self.assertEqual(record['availability'], 'Unavailable')
        self.assertEqual(dp.classification(record), dp.RESALE_UNKNOWN)
        data = result()
        data['prices'].append(copy.deepcopy(data['prices'][0]))
        self.assertFalse(gd.pricing_record('example.com', data)['verified'])

    def test_missing_renewal_and_non_usd_prices_are_never_invented(self):
        data = result()
        del data['prices'][0]['renewalPrice']
        record = gd.pricing_record('example.com', data)
        self.assertIsNone(record['annual_renewal_price'])
        self.assertEqual(dp.classification(record), dp.PROVISIONAL)
        for code, value, expected in [('EUR', 1500, '15.00'), ('JPY', 1500, '1500'),
                                      ('KWD', 1500, '1.500'), ('XXX', 1500, None)]:
            data = result()
            data['prices'][0]['price'] = {'currencyCode': code, 'value': value}
            record = gd.pricing_record('example.com', data)
            self.assertEqual(record['currency'], code)
            self.assertEqual(record['registration_price'], expected)
            self.assertIsNone(record['annual_renewal_price'])  # USD renewal cannot be mixed.
            self.assertNotEqual(dp.classification(record), dp.AFFORDABLE)
            p = engine.new_project('Currency')
            p['domain_pricing'] = {'example.com': record}
            self.assertIsNone(dp.pricing_fields(p, 'Example')['Registration price (USD)'])

    def test_final_all_in_manual_confirmation_and_legacy_quotes(self):
        legacy = dp.observed_quote('example.com', 'Available', False, '99', '20', 'Current listing')
        self.assertEqual(dp.classification(legacy), dp.PROVISIONAL)
        # Missing mandatory charges can move a near-budget quote over the ceiling.
        final = dp.observed_quote('example.com', 'Available', False, '101', '20', 'Final total',
                                  total_verified=True)
        self.assertEqual(dp.classification(final), dp.OVER_BUDGET)
        for price in ('15', '99', '100'):
            final = dp.observed_quote('example.com', 'Available', False, price, '20', 'Final total',
                                      total_verified=True)
            self.assertEqual(dp.classification(final), dp.AFFORDABLE)
        self.assertEqual(dp.classification(final | {'checked_at': '2020-01-01T00:00:00+00:00'}), dp.UNAVAILABLE)

    @patch('godaddy_pricing.requests.Session.post')
    def test_batch_endpoint_payload_secret_free_records_no_purchase(self, post):
        post.return_value = godaddy_response(['example.com', 'other.com'])
        records = gd.GoDaddyPricing().check_batch(['example.com', 'other.com'])
        self.assertEqual(post.call_count, 1)
        call = post.call_args
        self.assertEqual(call.args, ('https://api.godaddy.com/v3/domains/check-availability',))
        self.assertEqual(call.kwargs['json'], {'domains': ['example.com', 'other.com'], 'optimizeFor': 'ACCURACY'})
        self.assertEqual(call.kwargs['timeout'], (4, 12))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer mock-godaddy-token')
        self.assertNotIn('mock-godaddy-token', json.dumps(records))
        with self.assertRaises(dp.PricingError):
            gd.GoDaddyPricing().check_batch(['example.com'] * 26)

    @patch('godaddy_pricing.requests.Session.post')
    def test_errors_and_rate_limits_stop_without_retry_or_secret_leaks(self, post):
        for status in (401, 402, 403, 429, 500, 302):
            post.reset_mock()
            post.return_value = Mock(status_code=status, headers={'Retry-After': '60'}, text='mock-godaddy-token')
            with self.assertRaises(dp.PricingError) as raised:
                gd.GoDaddyPricing().check_batch(['example.com'])
            self.assertIn(str(status), str(raised.exception))
            self.assertNotIn('mock-godaddy-token', str(raised.exception))
            self.assertEqual(post.call_count, 1)
        for error in (requests.Timeout('mock-godaddy-token'), requests.ConnectionError('mock-godaddy-token')):
            post.side_effect = error
            with self.assertRaises(dp.PricingError) as raised:
                gd.GoDaddyPricing().check_batch(['example.com'])
            self.assertNotIn('mock-godaddy-token', str(raised.exception))

    @patch('godaddy_pricing.requests.Session.post')
    def test_quota_exhaustion_stops_next_batch(self, post):
        post.return_value = godaddy_response(['example.com'])
        post.return_value.headers = {'RateLimit-Remaining': '0'}
        provider = gd.GoDaddyPricing()
        provider.check_batch(['example.com'])
        with self.assertRaisesRegex(dp.PricingError, 'quota'):
            provider.check_batch(['other.com'])
        self.assertEqual(post.call_count, 1)

    @patch('godaddy_pricing.sleep')
    @patch('godaddy_pricing.monotonic', side_effect=[10.0, 10.1, 12.5])
    @patch('godaddy_pricing.requests.Session.post')
    def test_requests_are_paced(self, post, clock, sleep):
        post.return_value = godaddy_response(['example.com'])
        provider = gd.GoDaddyPricing()
        provider.check_batch(['example.com'])
        provider.check_batch(['example.com'])
        self.assertAlmostEqual(sleep.call_args.args[0], 2.4)

    @patch('godaddy_pricing.requests.Session.post')
    def test_invalid_batch_no_partial_update(self, post):
        for data in ({'domains': [result()]}, [], {'items': []}, {'items': [result(), result()]},
                     {'items': [result(domain='unexpected.com')]}):
            post.return_value = Mock(status_code=200, headers={}, json=Mock(return_value=data))
            with self.assertRaises(dp.PricingError):
                gd.GoDaddyPricing().check_batch(['example.com'])
        post.return_value = Mock(status_code=200, headers={}, json=Mock(side_effect=ValueError('mock-godaddy-token')))
        with self.assertRaises(dp.PricingError):
            gd.GoDaddyPricing().check_batch(['example.com'])
        # A valid domain-level error is explicitly persisted as unknown, not silently dropped.
        post.return_value = Mock(status_code=200, headers={}, json=Mock(return_value={'items': [
            {'domain': 'example.com', 'error': {'name': 'ERROR_UNKNOWN', 'message': 'mock-godaddy-token'}}]}))
        record = gd.GoDaddyPricing().check_batch(['example.com'])['example.com']
        self.assertFalse(record['verified'])
        self.assertIn('domain-level error', record['message'])
        post.return_value = Mock(status_code=200, headers={}, json=Mock(return_value={'items': [result(), result()]}))
        with self.assertRaisesRegex(dp.PricingError, 'duplicate'):
            gd.GoDaddyPricing().check_batch(['example.com', 'other.com'])

    @patch('godaddy_pricing.requests.Session.post')
    def test_preserves_namecheap_cache_manual_overrides_and_research(self, post):
        p = engine.new_project('Provider cache')
        p['names'] = ['Example', 'Manual']
        p['reviews'] = {'example': {'decision': 'Keep', 'decision_source': 'manual'},
                        'manual': {'decision': 'Maybe', 'decision_source': 'manual'}}
        p['runs'] = [{'id': 'historical', 'results': []}]
        previous = {'provider': 'Namecheap', 'domain': 'example.com', 'checked_at': engine.now(),
                    'acquisition_price': '20', 'message': 'Original tariff'}
        manual = dp.observed_quote('manual.com', 'Registered', True, '9950', '20', 'Manual source')
        p['domain_pricing'] = {'example.com': previous, 'manual.com': manual}
        original = copy.deepcopy(p)
        self.assertEqual(dp.plan_prices(p, provider='GoDaddy')[1], ['example.com'])
        post.return_value = godaddy_response(['example.com'])
        with tempfile.TemporaryDirectory() as root:
            def persist():
                engine.save_project(p, root)
                return True
            dp.check_prices(p, ['example.com', 'manual.com'], gd.GoDaddyPricing(), persist)
            loaded = engine.load_project(engine.project_path(p['id'], root))
            self.assertEqual(loaded['reviews'], original['reviews'])
            self.assertEqual(loaded['runs'], original['runs'])
            self.assertEqual(loaded['domain_pricing']['manual.com'], original['domain_pricing']['manual.com'])
            self.assertEqual(loaded['domain_pricing_history']['example.com'], [previous])
            self.assertEqual(dp.plan_prices(loaded, provider='GoDaddy')[1], [])
            self.assertNotIn('mock-godaddy-token', json.dumps(loaded))
            self.assertNotIn('mock-godaddy-token', engine.csv_bytes(loaded).decode('utf-8-sig'))

    @patch('godaddy_pricing.requests.Session.post')
    def test_same_provider_refresh_preserves_prior_quotes_and_unknown_results(self, post):
        project = engine.new_project('Refresh history')
        project['names'] = ['Example']
        project['reviews'] = {'example': {'decision': 'Keep', 'decision_source': 'manual'}}
        project['runs'] = [{'id': 'historical', 'results': []}]
        previous = gd.pricing_record('example.com', result(price=919))
        project['domain_pricing'] = {'example.com': copy.deepcopy(previous)}
        original = copy.deepcopy(project)
        provider = gd.GoDaddyPricing()
        history = [previous]
        with tempfile.TemporaryDirectory() as root:
            def persist():
                engine.save_project(project, root)
                return True

            for response in (result() | {'available': False}, result() | {'prices': []},
                             result(price=1998)):
                post.return_value = Mock(status_code=200, headers={},
                                         json=Mock(return_value={'items': [response]}))
                dp.check_prices(project, ['example.com'], provider, persist)
                saved = engine.load_project(engine.project_path(project['id'], root))
                self.assertEqual(saved['domain_pricing_history']['example.com'], history)
                self.assertEqual(saved['reviews'], original['reviews'])
                self.assertEqual(saved['runs'], original['runs'])
                self.assertEqual(saved['names'], original['names'])
                current = saved['domain_pricing']['example.com']
                if not response['prices'] or not response['available']:
                    self.assertIsNone(current['acquisition_price'])
                    self.assertNotEqual(dp.classification(current), dp.PROVISIONAL)
                else:
                    self.assertEqual(current['acquisition_price'], '19.98')
                history.append(copy.deepcopy(current))
            self.assertEqual(post.call_count, 3)

    @patch('godaddy_pricing.requests.Session.post')
    def test_partial_batch_failure_keeps_success_and_stops(self, post):
        p = engine.new_project('Partial')
        domains = [f'candidate{i}.com' for i in range(59)]
        p['names'] = [f'Candidate{i}' for i in range(59)]
        p['reviews'] = {n.casefold(): {'decision': 'Keep', 'decision_source': 'manual'} for n in p['names']}
        before = copy.deepcopy(p)
        post.side_effect = [godaddy_response(domains[:25]), Mock(status_code=403, headers={})]
        progress = Mock()
        with tempfile.TemporaryDirectory() as root:
            def persist():
                engine.save_project(p, root)
                return True
            with self.assertRaises(dp.PricingError):
                dp.check_prices(p, domains, gd.GoDaddyPricing(), persist, progress=progress)
            loaded = engine.load_project(engine.project_path(p['id'], root))
            self.assertEqual(len(loaded['domain_pricing']), 25)
            self.assertEqual(loaded['reviews'], before['reviews'])
            self.assertEqual(loaded['runs'], before['runs'])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(progress.call_args.args, (25, 59))

    @patch('godaddy_pricing.requests.Session.post')
    def test_successful_progress_and_save_failure_bounds_requests(self, post):
        domains = [f'candidate{i}.com' for i in range(59)]
        p = engine.new_project('Progress')
        post.side_effect = [godaddy_response(domains[:25]), godaddy_response(domains[25:50]),
                            godaddy_response(domains[50:])]
        progress = Mock()
        self.assertEqual(dp.check_prices(p, domains, gd.GoDaddyPricing(), lambda: True, progress=progress), 59)
        self.assertEqual([call.args for call in progress.call_args_list], [(0, 59), (25, 59), (50, 59), (59, 59)])
        post.reset_mock()
        post.side_effect = None
        post.return_value = godaddy_response(domains[:25])
        with self.assertRaisesRegex(dp.PricingError, 'save'):
            dp.check_prices(engine.new_project('Save failure'), domains, gd.GoDaddyPricing(), lambda: False)
        self.assertEqual(post.call_count, 1)


class GoDaddyAppIntegrationTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for item in (patch('engine.PROJECTS', self.root), patch('engine.DATA', self.root),
                     patch('engine.migrate_local_projects'), patch('engine.stored_key', return_value=''),
                     patch('engine.fetch_json', side_effect=AssertionError('No research requests')),
                     patch('godaddy_pricing.sleep')):
            item.start()
            self.patches.append(item)
        self.project = engine.new_project('150-name test project')
        self.project['names'] = [f'Candidate{i:03d}' for i in range(150)]
        self.project['reviews'] = {n.casefold(): {'decision': 'Maybe' if i < 59 else 'Reject',
                                                 'decision_source': 'manual', 'notes': 'Do not change'}
                                   for i, n in enumerate(self.project['names'])}
        self.project['runs'] = [{'id': 'saved-history', 'results': []}]
        engine.save_project(self.project)

    def tearDown(self):
        super().tearDown()
        self.temp.cleanup()

    def launch(self):
        app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    @patch('domain_pricing.requests.get', side_effect=AssertionError('Namecheap must not be called'))
    @patch('godaddy_pricing.requests.Session.post')
    def test_59_of_150_batch_progress_cache_restarts_and_filters(self, post, get):
        app = self.launch()
        self.assertTrue(any('59 domains will be checked' in x.value for x in app.caption))
        post.assert_not_called()
        domains = dp.plan_prices(self.project, provider='GoDaddy')[1]
        post.side_effect = [godaddy_response(domains[:25]), godaddy_response(domains[25:50], price=10100),
                            godaddy_response(domains[50:])]
        app = next(x for x in app.button if x.label == 'Check Domain Prices').click().run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual([len(c.kwargs['json']['domains']) for c in post.call_args_list], [25, 25, 9])
        self.assertEqual(len(app.session_state.project['names']), 150)
        self.assertEqual(len(app.session_state.project['domain_pricing']), 59)
        self.assertEqual(app.session_state.project['reviews'], self.project['reviews'])
        self.assertEqual(app.session_state.project['runs'], self.project['runs'])
        self.assertTrue(any('provisional' in x.value.lower() for x in app.warning))
        app = next(x for x in app.selectbox if x.label == 'Show names').set_value('Shortlist (Keep + Investigate)').run()
        table = next(x for x in app.dataframe if 'Candidate name' in x.value.columns)
        self.assertEqual(len(table.value), 59)
        self.assertEqual(set(table.value['Budget classification']), {dp.PROVISIONAL, dp.OVER_BUDGET})
        self.assertIn('Premium/resale purchase fee (USD)', table.value.columns)
        self.assertIn('Price reliability', table.value.columns)
        app = next(x for x in app.selectbox if x.label == 'Show names').set_value('Affordable Domains').run()
        self.assertFalse(any('Candidate name' in x.value.columns for x in app.dataframe))
        app.run()
        restarted = self.launch()
        self.assertEqual(post.call_count, 3)
        self.assertEqual(len(restarted.session_state.project['domain_pricing']), 59)
        self.assertTrue(next(x for x in restarted.button if x.label == 'Check Domain Prices').disabled)
        saved = engine.load_project(engine.project_path(self.project['id'], self.root))
        self.assertEqual(saved['reviews'], self.project['reviews'])
        self.assertEqual(saved['runs'], self.project['runs'])
        self.assertNotIn('mock-godaddy-token', json.dumps(saved))
        rows = list(csv.DictReader(io.StringIO(engine.csv_bytes(saved).decode('utf-8-sig'))))
        self.assertEqual(len(rows), 150)
        self.assertNotIn('mock-godaddy-token', json.dumps(engine.workbook_data(saved)))
        get.assert_not_called()

    @patch('godaddy_pricing.requests.Session.post')
    def test_visible_provider_error_and_no_request_on_rerun(self, post):
        post.return_value = Mock(status_code=403, headers={}, text='mock-godaddy-token')
        app = self.launch()
        app = next(x for x in app.button if x.label == 'Check Domain Prices').click().run()
        self.assertTrue(any('HTTP 403' in x.value for x in app.error))
        self.assertFalse(app.exception)
        app.run()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(app.session_state.project['reviews'], self.project['reviews'])

    def test_final_total_confirmation_form_drives_affordable_filter(self):
        app = self.launch()
        next(x for x in app.selectbox if x.label == 'Listing candidate').set_value('Candidate000')
        next(x for x in app.selectbox if x.label == 'Listing registration availability').set_value('Available')
        next(x for x in app.selectbox if x.label == 'Listing premium status').set_value('No')
        next(x for x in app.text_input if x.label == 'Observed acquisition asking price (USD, optional)').set_value('100')
        next(x for x in app.text_input if x.label == 'Registrar/listing source and URL or reference').set_value('Verified final total')
        next(x for x in app.checkbox if x.label.startswith('I verified this')).check()
        next(x for x in app.checkbox if x.label.startswith('I verified the final')).check()
        app = next(x for x in app.button if x.label == 'Save observed listing').click().run()
        app = next(x for x in app.selectbox if x.label == 'Show names').set_value('Affordable Domains').run()
        table = next(x.value for x in app.dataframe if 'Candidate name' in x.value.columns)
        self.assertEqual(list(table['Candidate name']), ['Candidate000'])
        self.assertEqual(list(table['Acquisition price (USD)']), [100.0])
        self.assertEqual(app.session_state.project['reviews'], self.project['reviews'])
