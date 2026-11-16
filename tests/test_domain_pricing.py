import copy
import csv
import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from streamlit.testing.v1 import AppTest

import domain_pricing as dp
import engine


ENV = {'NAMECHEAP_FREE_API_CONFIRMED': '1', 'NAMECHEAP_API_USER': 'test-user',
       'NAMECHEAP_API_KEY': 'test-secret', 'NAMECHEAP_CLIENT_IP': '203.0.113.1',
       'GODADDY_PAT': 'mock-godaddy-token'}
SOCKET_CONNECT = socket.socket.connect


def block_external_connect(sock, address):
    # Windows asyncio uses a loopback socket pair even in headless AppTest.
    if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return SOCKET_CONNECT(sock, address)
    raise AssertionError('External network blocked in tests')


def xml_response(body, status='OK'):
    return Mock(status_code=200, headers={}, content=(
        f'<ApiResponse xmlns="{dp.NS["nc"]}" Status="{status}">'
        f'<Errors/><CommandResponse>{body}</CommandResponse></ApiResponse>').encode())


def tariff_response(price='14.82', fee='0.18'):
    return xml_response(
        '<UserGetPricingResult><ProductType Name="DOMAIN">'
        f'<ProductCategory Name="REGISTER"><Product Name="com"><Price Duration="1" '
        f'DurationType="YEAR" YourPrice="{price}" YourAdditionalCost="{fee}" Currency="USD"/>'
        '</Product></ProductCategory><ProductCategory Name="RENEW"><Product Name="com">'
        '<Price Duration="1" DurationType="YEAR" YourPrice="19.80" YourAdditionalCost="0.18" '
        'Currency="USD"/></Product></ProductCategory></ProductType></UserGetPricingResult>')


def domain_response(domains, available='true', premium='false', price='0', renewal='0'):
    return xml_response(''.join(
        f'<DomainCheckResult Domain="{domain}" Available="{available}" ErrorNo="0" '
        f'IsPremiumName="{premium}" PremiumRegistrationPrice="{price}" '
        f'PremiumRenewalPrice="{renewal}" IcannFee="0" EapFee="0"/>'
        for domain in domains))


def godaddy_response(domains, price=1500):
    return Mock(status_code=200, headers={}, json=Mock(return_value={'items': [
        {'domain': domain, 'available': True, 'inventory': 'REGISTRY',
         'prices': [{'term': 'YEAR', 'period': 1, 'price': {'currencyCode': 'USD', 'value': price},
                     'renewalPrice': {'currencyCode': 'USD', 'value': 1998}}]}
        for domain in domains]}))


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch('requests.sessions.Session.request', side_effect=AssertionError('Network blocked in tests')),
            patch.object(socket.socket, 'connect', block_external_connect),
            patch.dict(os.environ, ENV),
            patch('domain_pricing.sleep'),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()


class PricingTests(OfflineTests):
    def quote(self, price, availability='Available', premium=False):
        return dp.observed_quote('example.com', availability, premium, price, '19.98', 'Test registrar listing',
                                 total_verified=bool(price))

    def test_manual_reobservation_deduplicates_equivalent_amounts(self):
        project = engine.new_project('Manual history')
        first = self.quote('15')
        dp.save_quote(project, 'example.com', first)
        again = copy.deepcopy(first)
        again.update(checked_at='2025-01-01T00:00:00+00:00', acquisition_price='15.00')
        dp.save_quote(project, 'example.com', again)
        self.assertNotIn('domain_pricing_history', project)
        changed = copy.deepcopy(again)
        changed['acquisition_price'] = '16'
        dp.save_quote(project, 'example.com', changed)
        self.assertEqual(project['domain_pricing_history']['example.com'], [again])
        changed['legacy_notes'] = 'Preserve older metadata'
        replacement = copy.deepcopy(changed)
        replacement.pop('legacy_notes')
        dp.save_quote(project, 'example.com', replacement)
        self.assertEqual(project['domain_pricing_history']['example.com'][-1]['legacy_notes'],
                         'Preserve older metadata')

    def test_exact_budget_boundaries_and_premium(self):
        for price, expected in [('15', dp.AFFORDABLE), ('99', dp.AFFORDABLE), ('100', dp.AFFORDABLE),
                                ('101', dp.OVER_BUDGET), ('9950', dp.OVER_BUDGET)]:
            with self.subTest(price=price):
                quote = self.quote(price, premium=price == '9950')
                self.assertEqual(dp.classification(quote), expected)
        self.assertEqual(dp.classification(self.quote('9950', 'Registered', True)), dp.OVER_BUDGET)
        self.assertEqual(dp.classification(self.quote('15'), 10), dp.OVER_BUDGET)
        self.assertEqual(dp.classification(self.quote('101'), 1000), dp.OVER_BUDGET)

    def test_unknown_prices_are_never_affordable(self):
        self.assertEqual(dp.classification(self.quote('', premium=True)), dp.PREMIUM_UNKNOWN)
        self.assertEqual(dp.classification(self.quote('', 'Registered', True)), dp.RESALE_UNKNOWN)
        self.assertEqual(dp.classification(self.quote('')), dp.UNAVAILABLE)
        for record in ({'availability': 'Available'}, {'availability': 'Potentially available'},
                       {'availability': 'Registered', 'acquisition_price': '15'},
                       {'premium': True, 'acquisition_price': '15'}):
            self.assertNotEqual(dp.classification(record), dp.AFFORDABLE)

    def test_unreliable_currency_stale_and_wrong_price_kind(self):
        for changes in ({'verified': False}, {'currency': 'EUR'}, {'source': ''},
                        {'checked_at': '2020-01-01T00:00:00+00:00'},
                        {'checked_at': 'invalid'}, {'checked_at': '2026-01-01'},
                        {'checked_at': '2999-01-01T00:00:00+00:00'},
                        {'availability': 'Registered', 'price_kind': 'registration'}):
            with self.subTest(changes=changes):
                self.assertNotEqual(dp.classification(self.quote('15') | changes), dp.AFFORDABLE)
        for value in ('NaN', 'Infinity', '-1', 'bad', True):
            self.assertNotEqual(dp.classification(self.quote('15') | {'acquisition_price': value}), dp.AFFORDABLE)

    def test_observed_listing_validation(self):
        for price in ('-1', 'NaN', 'Infinity', '$15', '9,950'):
            with self.assertRaises(ValueError):
                self.quote(price)
        with self.assertRaises(ValueError):
            dp.observed_quote('example.com', 'Registered', True, '15', '', '')
        with self.assertRaises(ValueError):
            dp.observed_quote('example.net', 'Registered', True, '15', '', 'source')

    def test_plan_reuses_cache_deduplicates_and_respects_targets_and_decisions(self):
        project = engine.new_project('Pricing')
        project['names'] = ['First', 'Second', 'Third', 'Invalid']
        project['reviews'] = {'first': {'decision': 'Keep', 'targets': {'Domain': 'EXAMPLE.COM'}},
                              'second': {'decision': 'Maybe', 'targets': {'Domain': 'example.com'}},
                              'third': {'decision': 'Reject'},
                              'invalid': {'decision': 'Keep', 'targets': {'Domain': 'bad.net'}}}
        project['domain_pricing'] = {'example.com': self.quote('15') | {'manual': False}}
        self.assertEqual(dp.plan_prices(project), (['example.com'], [], ['Invalid']))
        self.assertEqual(dp.plan_prices(project, refresh=True)[1], ['example.com'])
        self.assertEqual(dp.plan_prices(project, shortlisted=False)[1], ['third.com'])
        project['reviews']['first']['targets']['Domain'] = 'changed.com'
        self.assertEqual(dp.plan_prices(project)[1], ['changed.com'])
        self.assertEqual(dp.pricing_fields(project, 'First')['Budget classification'], dp.UNAVAILABLE)

    @patch('domain_pricing.requests.get')
    def test_api_checks_never_replace_manual_listing_even_when_stale(self, get):
        project = engine.new_project('Manual listing')
        project['names'] = ['Example']
        project['reviews'] = {'example': {'decision': 'Keep', 'decision_source': 'manual'}}
        record = self.quote('9950', 'Registered', True) | {'checked_at': '2020-01-01T00:00:00+00:00'}
        project['domain_pricing'] = {'example.com': record}
        self.assertEqual(dp.plan_prices(project, refresh=True)[1], [])
        self.assertEqual(dp.check_prices(project, ['example.com'], dp.NamecheapPricing(), lambda: True), 0)
        self.assertEqual(project['domain_pricing']['example.com'], record)
        self.assertNotEqual(dp.classification(record), dp.AFFORDABLE)
        get.assert_not_called()

    def test_old_projects_and_registered_research_evidence(self):
        project = engine.new_project('Old project')
        project['names'] = ['Example']
        project['runs'] = [{'results': [{'name': 'Example',
                                         'registration': {'domain': 'example.com', 'status': 'Registered'}}]}]
        original = copy.deepcopy(project)
        fields = dp.pricing_fields(project, 'Example')
        self.assertEqual(fields['Registration availability'], 'Registered')
        self.assertEqual(fields['Budget classification'], dp.RESALE_UNKNOWN)
        self.assertEqual(dp.budget_for(project), 100)
        self.assertEqual(project, original)
        project['reviews']['example'] = {'targets': {'Domain': 'changed.com'}}
        self.assertEqual(dp.pricing_fields(project, 'Example')['Registration availability'], 'Unknown')
        for value, expected in [(1000, 100), ('NaN', 100), (25, 25)]:
            self.assertEqual(dp.budget_for({'domain_budget': value}), expected)

    def test_existing_exports_include_prices_without_altering_research(self):
        project = engine.new_project('Pricing exports')
        project['names'] = ['Example']
        project['domain_pricing'] = {'example.com': self.quote('15')}
        original = copy.deepcopy(project)
        row = next(csv.DictReader(io.StringIO(engine.csv_bytes(project).decode('utf-8-sig'))))
        self.assertEqual(row['Budget classification'], dp.AFFORDABLE)
        self.assertEqual(row['Acquisition price (USD)'], '15.0')
        workbook = engine.workbook_data(project)
        self.assertIn('Budget classification', workbook[0]['columns'])
        self.assertEqual(project, original)

    @patch('domain_pricing.requests.get')
    def test_namecheap_standard_and_premium_quotes(self, get):
        provider = dp.NamecheapPricing()
        get.return_value = tariff_response()
        standard = provider.standard_prices()
        self.assertEqual(standard, {'REGISTER': '15.00', 'RENEW': '19.98'})
        get.return_value = domain_response(['example.com'])
        record = provider.check_batch(['example.com'], standard)['example.com']
        self.assertEqual(dp.classification(record), dp.PROVISIONAL)
        self.assertEqual(record['annual_renewal_price'], '19.98')
        for price, expected in [('15', dp.PROVISIONAL), ('99', dp.PROVISIONAL), ('100', dp.PROVISIONAL),
                                ('101', dp.OVER_BUDGET), ('9950', dp.OVER_BUDGET), ('0', dp.PREMIUM_UNKNOWN)]:
            get.return_value = domain_response(['example.com'], premium='true', price=price, renewal='19.98')
            quote = provider.check_batch(['example.com'], standard)['example.com']
            self.assertEqual(dp.classification(quote), expected)
        call = get.call_args
        self.assertEqual(call.kwargs['timeout'], (4, 12))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertNotIn('test-secret', json.dumps(record))
        self.assertTrue(all(c.kwargs['params']['Command'] in (
            'namecheap.domains.check', 'namecheap.users.getPricing') for c in get.call_args_list))

    @patch('domain_pricing.sleep')
    @patch('domain_pricing.monotonic', side_effect=[10.0, 10.1, 11.3])
    @patch('domain_pricing.requests.get', return_value=tariff_response())
    def test_read_only_command_and_spacing(self, get, clock, sleep):
        provider = dp.NamecheapPricing()
        provider.request('namecheap.users.getPricing', Command='namecheap.domains.create')
        provider.request('namecheap.users.getPricing')
        self.assertEqual(get.call_args_list[0].kwargs['params']['Command'], 'namecheap.users.getPricing')
        self.assertAlmostEqual(sleep.call_args.args[0], 1.2)

    @patch('domain_pricing.requests.get')
    def test_premium_fees_and_missing_price_fields(self, get):
        provider = dp.NamecheapPricing()
        get.return_value = xml_response(
            '<DomainCheckResult Domain="example.com" Available="true" ErrorNo="0" IsPremiumName="true" '
            'PremiumRegistrationPrice="99.99" PremiumRenewalPrice="19.80" IcannFee="0.18" EapFee="0"/>')
        record = provider.check_batch(['example.com'], {})['example.com']
        self.assertEqual(record['acquisition_price'], '100.17')
        self.assertEqual(record['annual_renewal_price'], '19.98')
        self.assertEqual(dp.classification(record), dp.OVER_BUDGET)
        for attribute in ('PremiumRegistrationPrice="99.99"', 'IcannFee="0.18"', 'EapFee="0"'):
            get.return_value = Mock(status_code=200, content=get.return_value.content.replace(attribute.encode(), b''))
            record = provider.check_batch(['example.com'], {})['example.com']
            self.assertEqual(dp.classification(record), dp.PREMIUM_UNKNOWN)
        get.return_value = tariff_response(fee='NaN')
        self.assertNotIn('REGISTER', provider.standard_prices())

    @patch('domain_pricing.requests.get')
    def test_registered_prices_never_mistaken_for_registration_or_resale(self, get):
        get.return_value = domain_response(['example.com'], available='false', premium='true',
                                         price='9950', renewal='19.98')
        record = dp.NamecheapPricing().check_batch(['example.com'], {'REGISTER': '15'})['example.com']
        self.assertEqual(record['availability'], 'Registered')
        self.assertIsNone(record['acquisition_price'])
        self.assertEqual(dp.classification(record), dp.RESALE_UNKNOWN)

    @patch('domain_pricing.requests.get')
    def test_missing_unknown_and_domain_errors(self, get):
        provider = dp.NamecheapPricing()
        get.return_value = xml_response('')
        with self.assertRaisesRegex(dp.PricingError, 'omitted'):
            provider.check_batch(['example.com'], {})
        for attrs in ('Available="true" ErrorNo="1" IsPremiumName="false"',
                      'Available="true" ErrorNo="0"', 'Available="unknown" ErrorNo="0"'):
            get.return_value = xml_response(f'<DomainCheckResult Domain="example.com" {attrs}/>')
            quote = provider.check_batch(['example.com'], {'REGISTER': '15'})
            self.assertEqual(dp.classification(quote['example.com']), dp.UNAVAILABLE)

    @patch('domain_pricing.requests.get')
    def test_errors_stop_no_retries_no_secret_leaks(self, get):
        provider = dp.NamecheapPricing()
        cases = [
            Mock(status_code=429, headers={'Retry-After': '60'}),
            Mock(status_code=403), Mock(status_code=402), Mock(status_code=500),
            Mock(status_code=302), Mock(status_code=200, content=b'invalid xml'),
            xml_response('<Error Number="2011169">test-secret</Error>', status='ERROR'),
        ]
        for response in cases:
            get.reset_mock()
            get.return_value = response
            with self.assertRaises(dp.PricingError) as raised:
                provider.standard_prices()
            self.assertNotIn('test-secret', str(raised.exception))
            self.assertEqual(get.call_count, 1)
        for error in (requests.Timeout('test-secret'), requests.ConnectionError('test-secret')):
            get.side_effect = error
            with self.assertRaises(dp.PricingError) as raised:
                provider.standard_prices()
            self.assertNotIn('test-secret', str(raised.exception))
        with self.assertRaises(dp.PricingError):
            provider.request('namecheap.domains.create')
        with self.assertRaises(dp.PricingError):
            provider.check_batch(['example.com'] * 51, {})

    @patch('domain_pricing.requests.get')
    def test_access_confirmation_and_credentials_required_before_network(self, get):
        for overrides in ({'NAMECHEAP_FREE_API_CONFIRMED': ''}, {'NAMECHEAP_API_KEY': ''},
                          {'NAMECHEAP_CLIENT_IP': '::1'}):
            with patch.dict(os.environ, overrides), self.assertRaises(dp.PricingError):
                dp.NamecheapPricing()
        get.assert_not_called()

    @patch('domain_pricing.requests.get')
    def test_batch_persistence_restart_and_partial_failure_preserve_research_and_decisions(self, get):
        project = engine.new_project('Batch pricing')
        project['names'] = [f'Candidate{i}' for i in range(51)]
        project['reviews'] = {name.casefold(): {'decision': ('Keep', 'Maybe', 'Reject')[i % 3],
                                               'decision_source': 'manual', 'notes': 'keep this'}
                              for i, name in enumerate(project['names'])}
        project['runs'] = [{'id': 'old', 'results': []}]
        original = copy.deepcopy(project)
        domains = dp.plan_prices(project, shortlisted=False)[1]
        get.side_effect = [tariff_response(), domain_response(domains[:50]), domain_response(domains[50:])]
        with tempfile.TemporaryDirectory() as root:
            def persist():
                engine.save_project(project, root)
                return True
            self.assertEqual(dp.check_prices(project, domains, dp.NamecheapPricing(), persist), 51)
            self.assertEqual(get.call_count, 3)
            loaded = engine.load_project(engine.project_path(project['id'], root))
            self.assertEqual(loaded['reviews'], original['reviews'])
            self.assertEqual(loaded['runs'], original['runs'])
            self.assertEqual(len(loaded['domain_pricing']), 51)
            self.assertEqual(dp.plan_prices(loaded, shortlisted=False)[1], [])
            self.assertEqual(dp.classification(loaded['domain_pricing'][domains[0]]), dp.PROVISIONAL)
            get.side_effect = [domain_response(domains[:50]), requests.Timeout()]
            with self.assertRaises(dp.PricingError):
                dp.check_prices(project, domains, dp.NamecheapPricing(), persist)
            self.assertEqual(engine.load_project(engine.project_path(project['id'], root))['reviews'],
                             original['reviews'])
            self.assertEqual(get.call_count, 5)  # Cached tariff avoided another request.

    @patch('domain_pricing.requests.get')
    def test_save_failure_stops_before_more_requests(self, get):
        project = engine.new_project('Save failure')
        get.return_value = tariff_response()
        with self.assertRaisesRegex(dp.PricingError, 'save'):
            dp.check_prices(project, ['example.com'], dp.NamecheapPricing(), lambda: False)
        self.assertEqual(get.call_count, 1)


class PricingAppTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for item in (patch('engine.PROJECTS', self.root), patch('engine.DATA', self.root),
                     patch('engine.migrate_local_projects'), patch('engine.stored_key', return_value=''),
                     patch('engine.fetch_json', side_effect=AssertionError('Brand research is blocked'))):
            item.start()
            self.patches.append(item)
        self.project = engine.new_project('Pricing UI')
        self.project['names'] = ['Low', 'At Limit', 'High', 'Premium', 'Resale', 'Discarded']
        self.project['reviews'] = {n.casefold(): {'decision': 'Reject' if n == 'Discarded' else 'Keep',
                                                 'decision_source': 'manual'} for n in self.project['names']}
        self.project['runs'] = [{'id': 'historical-research', 'results': []}]
        engine.save_project(self.project)

    def tearDown(self):
        super().tearDown()
        self.temp.cleanup()

    def launch(self):
        app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=20)
        self.assertFalse(app.exception)
        return app

    @staticmethod
    def table(app):
        return next(x.value for x in app.dataframe if 'Candidate name' in x.value.columns)

    def test_affordable_filter_budget_and_restart_preserve_shortlist(self):
        prices = [('Low', '15', False, 'Available'), ('At Limit', '100', False, 'Available'),
                  ('High', '101', False, 'Available'), ('Premium', '', True, 'Available'),
                  ('Resale', '', True, 'Registered'), ('Discarded', '99', False, 'Available')]
        self.project['domain_pricing'] = {
            engine.slug(n) + '.com': dp.observed_quote(engine.slug(n) + '.com', availability, premium,
                                                       price, '', 'Registrar listing', total_verified=bool(price))
            for n, price, premium, availability in prices}
        engine.save_project(self.project)
        app = self.launch()
        app = next(x for x in app.selectbox if x.label == 'Show names').set_value('Affordable Domains').run()
        table = self.table(app)
        self.assertEqual(set(table['Candidate name']), {'Low', 'At Limit', 'Discarded'})
        self.assertEqual(set(table['Budget classification']), {dp.AFFORDABLE})
        app = next(x for x in app.number_input if x.label == 'Maximum domain acquisition price (USD)').set_value(50.0).run()
        self.assertEqual(list(self.table(app)['Candidate name']), ['Low'])
        app = next(x for x in app.selectbox if x.label == 'Show names').set_value('Shortlist (Keep + Investigate)').run()
        self.assertEqual(len(self.table(app)), 5)
        self.assertIn('High', set(self.table(app)['Candidate name']))
        restarted = self.launch()
        self.assertEqual(restarted.session_state.project['domain_budget'], 50)
        self.assertEqual(restarted.session_state.project['reviews'], self.project['reviews'])
        self.assertEqual(restarted.session_state.project['runs'], self.project['runs'])
        self.assertEqual(restarted.session_state.project['domain_pricing'], self.project['domain_pricing'])

    @patch('godaddy_pricing.requests.Session.post')
    def test_button_only_batch_check_and_reruns_never_fetch(self, get):
        app = self.launch()
        get.assert_not_called()
        self.assertTrue(any('5 domains will be checked' in x.value for x in app.caption))
        domains = dp.plan_prices(self.project)[1]
        get.return_value = godaddy_response(domains)
        app = next(x for x in app.button if x.label == 'Check Domain Prices').click().run(timeout=20)
        self.assertFalse(app.exception)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(app.session_state.project['domain_pricing']), 5)
        self.assertNotIn('discarded.com', app.session_state.project['domain_pricing'])
        app.run()
        app.run()
        restarted = self.launch()
        self.assertEqual(get.call_count, 1)
        self.assertTrue(next(x for x in restarted.button if x.label == 'Check Domain Prices').disabled)
        self.assertEqual(restarted.session_state.project['reviews'], self.project['reviews'])
        self.assertEqual(restarted.session_state.project['runs'], self.project['runs'])

    @patch('godaddy_pricing.requests.Session.post')
    def test_unconfigured_click_stops_with_visible_error_and_no_calls(self, get):
        with patch('godaddy_pricing.local_token', return_value=''):
            app = self.launch()
            app = next(x for x in app.button if x.label == 'Check Domain Prices').click().run()
            self.assertTrue(any('GoDaddy needs' in x.value for x in app.error))
            get.assert_not_called()

    def test_observed_resale_listing_form(self):
        app = self.launch()
        next(x for x in app.selectbox if x.label == 'Listing candidate').set_value('Resale')
        next(x for x in app.selectbox if x.label == 'Listing premium status').set_value('Yes')
        next(x for x in app.text_input if x.label == 'Observed acquisition asking price (USD, optional)').set_value('9950')
        next(x for x in app.text_input if x.label == 'Observed annual renewal price (USD, optional)').set_value('19.98')
        next(x for x in app.text_input if x.label == 'Registrar/listing source and URL or reference').set_value('Test listing reference')
        next(x for x in app.checkbox if x.label.startswith('I verified')).check()
        app = next(x for x in app.button if x.label == 'Save observed listing').click().run()
        self.assertFalse(app.exception)
        record = app.session_state.project['domain_pricing']['resale.com']
        self.assertEqual(record['availability'], 'Registered')
        self.assertEqual(record['annual_renewal_price'], '19.98')
        self.assertEqual(dp.classification(record), dp.OVER_BUDGET)
        self.assertEqual(app.session_state.project['reviews'], self.project['reviews'])


if __name__ == '__main__':
    unittest.main()
