import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import godaddy_pricing_test as gd


def fixture(domain='harborglyph.com', inventory='REGISTRY', price=919):
    return {'domain': domain, 'available': True, 'inventory': inventory,
            'prices': [{'term': 'YEAR', 'period': 1,
                        'price': {'currencyCode': 'USD', 'value': price},
                        'renewalPrice': {'currencyCode': 'USD', 'value': 2299}}]}


class GoDaddyProbeTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network blocked'))
        self.network.start()

    def tearDown(self):
        self.network.stop()

    def test_minor_units_and_missing_fields(self):
        row = gd.summarize('harborglyph.com', fixture())
        self.assertEqual(row['registration_price']['amount'], '9.19')
        self.assertEqual(row['annual_renewal_price']['amount'], '22.99')
        self.assertFalse(row['budget_verified'])
        self.assertFalse(row['premium'])
        self.assertEqual(gd.money({'currencyCode': 'JPY', 'value': 100})['amount'], '100')
        self.assertEqual(gd.money({'currencyCode': 'KWD', 'value': 1234})['amount'], '1.234')
        self.assertIsNone(gd.money({'currencyCode': 'XXX', 'value': 1234})['amount'])
        data = fixture()
        del data['prices'][0]['renewalPrice']
        del data['inventory']
        row = gd.summarize('harborglyph.com', data)
        self.assertIsNone(row['annual_renewal_price'])
        self.assertIsNone(row['premium'])

    def test_premium_resale_fees_are_separate_and_added(self):
        data = fixture(inventory='PREMIUM')
        data['prices'][0]['fees'] = [{'type': 'ONE_TIME_PREMIUM_DOMAIN_PURCHASE',
                                     'fee': {'currencyCode': 'USD', 'value': 995000}}]
        row = gd.summarize('harborglyph.com', data)
        self.assertTrue(row['premium'])
        self.assertTrue(row['resale_listing'])
        self.assertEqual(row['premium_purchase_fees'][0]['amount'], '9950.00')
        self.assertEqual(row['indicative_acquisition_excluding_tax_and_icann'], '9959.19')
        self.assertTrue(gd.summarize('harborglyph.com', fixture(inventory='REGISTRY_PREMIUM'))['premium'])

    def test_unavailable_and_multiyear_not_standard_or_annual(self):
        row = gd.summarize('harborglyph.com', {'domain': 'harborglyph.com', 'available': False})
        self.assertFalse(row['available'])
        self.assertIsNone(row['registration_price'])
        self.assertIsNone(row['indicative_acquisition_excluding_tax_and_icann'])
        data = fixture()
        data['prices'][0]['period'] = 2
        data['prices'][0]['firstTermPrice'] = {'currencyCode': 'USD', 'value': 100}
        row = gd.summarize('harborglyph.com', data)
        self.assertIsNone(row['registration_price'])
        self.assertIsNone(row['annual_renewal_price'])

    def test_invalid_domain_response_money_and_fee(self):
        for data in ([], {'domain': 'wrong.com'}, fixture() | {'error': {'message': 'secret'}},
                     fixture() | {'available': 'true'}, fixture() | {'prices': {}},
                     fixture() | {'prices': [{'term': 'YEAR', 'period': 1, 'fees': [{}]}]}):
            with self.assertRaises(gd.ProbeError):
                gd.summarize('harborglyph.com', data)
        for value in (True, -1, 9.19, '919'):
            with self.assertRaises(gd.ProbeError):
                gd.money({'currencyCode': 'USD', 'value': value})

    @patch('godaddy_pricing_test.requests.Session.get')
    def test_only_two_or_three_get_requests_correct_endpoint_no_secret_output(self, get):
        domains = gd.DEFAULT_DOMAINS
        get.side_effect = [Mock(status_code=200, json=Mock(return_value=fixture(domain))) for domain in domains]
        results = gd.probe(domains, 'test-token-secret')
        self.assertEqual(get.call_count, 3)
        self.assertNotIn('test-token-secret', json.dumps(results))
        for call in get.call_args_list:
            self.assertEqual(call.args[0], gd.ENDPOINT)
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer test-token-secret')
            self.assertEqual(call.kwargs['timeout'], (4, 12))
            self.assertFalse(call.kwargs['allow_redirects'])

    @patch('godaddy_pricing_test.requests.Session.get')
    def test_errors_stop_without_retry_or_leaking_body(self, get):
        for status in (401, 402, 403, 429, 500, 302):
            get.reset_mock()
            get.return_value = Mock(status_code=status, text='test-token-secret')
            with self.assertRaises(gd.ProbeError) as error:
                gd.probe(gd.DEFAULT_DOMAINS, 'test-token-secret')
            self.assertIn(str(status), str(error.exception))
            self.assertNotIn('test-token-secret', str(error.exception))
            self.assertEqual(get.call_count, 1)
            get.return_value.json.assert_not_called()
        for error in (requests.Timeout('test-token-secret'), requests.ConnectionError('test-token-secret')):
            get.side_effect = error
            with self.assertRaises(gd.ProbeError) as raised:
                gd.probe(gd.DEFAULT_DOMAINS, 'test-token-secret')
            self.assertNotIn('test-token-secret', str(raised.exception))
        get.side_effect = None
        get.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError('test-token-secret')))
        with self.assertRaises(gd.ProbeError):
            gd.probe(gd.DEFAULT_DOMAINS, 'test-token-secret')

    @patch('godaddy_pricing_test.requests.Session.get')
    def test_small_scope_and_token_required_before_network(self, get):
        for domains in ([], ['one.com'], ['a.com', 'b.com', 'c.com', 'd.com'], ['a.com', 'bad.net'],
                        ['a.com', 'A.COM']):
            with self.assertRaises(gd.ProbeError):
                gd.probe(domains, 'secret')
        for token in ('', 'with spaces', 'with\nnewline'):
            with self.assertRaises(gd.ProbeError):
                gd.probe(gd.DEFAULT_DOMAINS, token)
        get.assert_not_called()

    def test_local_token_environment_precedence_and_ignored_file(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'godaddy_pricing_test.py'
            token_file = script.parent / '.env.godaddy'
            token_file.write_text('# local only\nGODADDY_PAT="file-token"\n', encoding='utf-8')
            with patch('godaddy_pricing_test.__file__', str(script)), patch.dict(os.environ, {'GODADDY_PAT': ''}):
                self.assertEqual(gd.local_token(), 'file-token')
                with patch.dict(os.environ, {'GODADDY_PAT': 'env-token'}):
                    self.assertEqual(gd.local_token(), 'env-token')
                token_file.write_text('OTHER=never-display-this', encoding='utf-8')
                with self.assertRaises(gd.ProbeError) as raised:
                    gd.local_token()
                self.assertNotIn('never-display-this', str(raised.exception))

    @patch('godaddy_pricing_test.probe')
    def test_cli_dry_run_does_not_read_token_or_send_requests(self, probe):
        with patch('sys.argv', ['godaddy_pricing_test.py']), patch('godaddy_pricing_test.local_token') as token:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gd.main(), 0)
            token.assert_not_called()
            probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
