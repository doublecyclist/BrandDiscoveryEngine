import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
import engine as e

class EngineTests(unittest.TestCase):
    def test_import_excel_styles_and_duplicates(self):
        for header in ['name', '"name"', 'NAME', ' Name ', '\ufeffname']:
            self.assertEqual(e.import_csv(header + '\n"Garden by the Sea"\n\nGarden by the Sea\n'), ['Garden by the Sea'])
        self.assertEqual(e.import_csv('Notes,CANDIDATE\nx,"Barns, Beaches"\n'), ['Barns, Beaches'])
        self.assertEqual(e.import_csv('Garden by the Sea\nMossy Trail\n'), ['Garden by the Sea', 'Mossy Trail'])

    def test_links_use_targets_and_respect_project_profile(self):
        p=e.new_project();name='Garden by the Sea';p['names']=[name]
        p['reviews'][name.casefold()]={'targets':{'Domain':'newfarmdomain.com','YouTube':'myfarmvideo','Instagram':'myfarmprofile'}}
        links=e.candidate_links(p,name)
        self.assertEqual(len(links.values()),len(set(links.values())))
        self.assertIn('newfarmdomain.com',links['Check domain at registrar'])
        self.assertTrue(links['YouTube — proposed handle'].endswith('@myfarmvideo'))
        self.assertTrue(links['Instagram — proposed profile'].endswith('/myfarmprofile/'))
        p['platform']='Business / other'
        links=e.candidate_links(p,name)
        self.assertEqual(len(links),3)

    def test_invalid_imports(self):
        for content in ['', 'name\n', 'Unknown,Heading\na,b\n']:
            with self.assertRaises(ValueError): e.import_csv(content)

    def test_pasted_tables_and_lists(self):
        self.assertEqual(e.import_pasted('| Name | Notes |\n| --- | --- |\n| Country Barn | test |\n'), ['Country Barn'])
        self.assertEqual(e.import_pasted('Name\n1. Country Barn\n- Beach Barn\nCountry Barn'), ['Country Barn','Beach Barn'])

    def test_gmail_validation_and_dots(self):
        p=e.new_project();p['names']=['Country Barn']
        p['reviews']['country barn']={'targets':{'Gmail':'country.barn'}}
        self.assertEqual(e.effective_status(p,'Country Barn','Gmail',None)[0],'Manual check required')
        p['reviews']['country barn']['targets']['Gmail']='country_barn'
        self.assertEqual(e.effective_status(p,'Country Barn','Gmail',None)[0],'Invalid Gmail username')

    def test_no_automatic_scoring(self):
        self.assertIsNone(e.rating_score({}))
        self.assertIsNone(e.rating_score({k: 0 for k in e.RATING_FIELDS}))
        self.assertEqual(e.rating_score({k: 4 for k in e.RATING_FIELDS}), 80)

    def test_project_isolation_and_preservation(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = e.new_project('A'), e.new_project('B')
            a['names'] = b['names'] = ['Garden by the Sea']
            a['reviews']['garden by the sea'] = {'notes': 'Keep these notes', 'decision': 'Keep'}
            a['runs'].append({'id': 'first', 'results': []})
            e.save_project(a, d); e.save_project(b, d)
            a['runs'].append({'id': 'second', 'results': []})
            e.save_project(a, d)
            loaded = e.load_project(e.project_path(a['id'], d))
            self.assertEqual(loaded['reviews']['garden by the sea']['notes'], 'Keep these notes')
            self.assertEqual(len(loaded['runs']), 2)
            self.assertEqual(e.load_project(e.project_path(b['id'], d))['runs'], [])
            with self.assertRaises(ValueError): e.project_path('../bad', d)

    @patch('engine.fetch_json', return_value=(None, 'Request timed out.'))
    def test_failed_search_is_not_zero(self, mock):
        result = e.search_check('Apple Podcasts', 'Garden by the Sea')
        self.assertEqual(result['status'], 'Failed')
        self.assertIsNone(result['exact_count'])

    @patch('engine.fetch_json')
    def test_title_matching_and_channel_search(self, mock):
        mock.return_value = ({'items': [{'snippet': {'title': 'Garden by the Sea'}, 'id': {'channelId': 'abc'}}, {'snippet': {'title': 'Farm Tips'}, 'id': {'channelId': 'xyz'}}]}, '')
        result = e.search_check('YouTube', 'Garden by the Sea', 'secret')
        self.assertEqual(result['exact_count'], 1)
        self.assertEqual(len(result['matches']), 2)
        self.assertEqual(mock.call_args_list[0].args[1]['type'], 'channel')
        self.assertNotIn('secret', json.dumps(result))

    @patch('engine.fetch_json', return_value=({'results': []}, ''))
    def test_successful_empty_and_missing_key(self, mock):
        self.assertEqual(e.search_check('Apple Podcasts', 'Name')['exact_count'], 0)
        self.assertEqual(e.search_check('YouTube', 'Name')['status'], 'Not checked')

    @patch('engine.fetch_json')
    def test_dns_failure_not_availability(self, mock):
        for data in [None, {'Status': 2}, {'Answer': []}]:
            mock.return_value = (data, '')
            self.assertEqual(e.dns_check('Name')['status'], 'Failed')
        mock.return_value = ({'Status': 3}, '')
        self.assertEqual(e.dns_check('Name')['status'], 'No A record')
        mock.return_value = ({'Status': 0, 'Answer': [{'type': 1, 'data': '1.2.3.4'}]}, '')
        self.assertEqual(e.dns_check('Name')['status'], 'Resolves')
        for data in ({'Status': 0, 'Answer': None}, {'Status': 0, 'Answer': [None]}):
            mock.return_value = (data, '')
            self.assertEqual(e.dns_check_domain('example.com')['status'], 'Failed')

    @patch('engine.requests.get', side_effect=requests.Timeout('url?key=secret'))
    def test_request_timeout_sanitizes_keys(self, mock):
        data, error = e.fetch_json('url?key=secret')
        self.assertIsNone(data)
        self.assertNotIn('secret', error)

    def test_export_and_changed_candidate_list(self):
        p = e.new_project()
        with self.assertRaises(ValueError): e.csv_bytes(p)
        p['names'] = ['=Bad', 'New Name']
        p['reviews']['=bad'] = {'notes': '=HYPERLINK("bad")', 'decision': 'Keep'}
        p['runs'] = [{'results': []}]
        rows = list(csv.DictReader(io.StringIO(e.csv_bytes(p).decode('utf-8-sig'))))
        bad = next(r for r in rows if r['Name'].startswith("'="))
        self.assertTrue(bad['Notes'].startswith("'="))
        self.assertEqual(rows[0]['Research'], 'Research incomplete')

    def test_unicode_partial_and_other_fields(self):
        self.assertNotEqual(e.normalize('The Daily Retreat ไทย'), e.normalize('The Daily Retreat'))
        self.assertEqual(e.classify_match('Farm Paradise', 'Farm Paradise: Farming Village')[0], 'Name in title')
        self.assertEqual(e.classify_match('Farm Paradise', 'Other title', 'Farm Paradise tips here')[1], 'Description')
        self.assertEqual(e.classify_match('Gardens & Shores', 'Gardens &amp; Shores')[0], 'Exact title')

    def test_brand_boundaries_and_punctuation(self):
        for title in ('HarborSketch', 'harborsketch', 'HARBORSKETCH', 'Harbor Sketch', 'Harbor-Sketch', 'Harbor & Sketch'):
            with self.subTest(title=title):
                self.assertEqual(e.classify_match('HarborSketch', title)[0], 'Exact title')
        for title in ('Smith & Harbor: Sketch History of Our Accounting Software', 'HarborSketching',
                      'Unrelated Accounting Company', 'PreHarborSketch'):
            with self.subTest(title=title):
                self.assertNotIn(e.classify_match('HarborSketch', title)[0], ('Exact title', 'Name in title'))
        self.assertEqual(e.classify_match('HarborSketch', 'HarborSketch: Banking tools')[0], 'Name in title')
        self.assertEqual(e.classify_match('HarborSketch', 'Article about Harbor Sketch software')[0], 'Name in title')
        self.assertFalse(e.brand_identity('HarborSketch', 'Article about Harbor Sketch software', 'https://publisher.example/'))

    @patch('engine.fetch_json')
    def test_explicit_empty_searches_complete_but_malformed_payloads_fail(self, fetch):
        fixtures = [('YouTube', {'pageInfo': {'totalResults': 0}}),
                    ('Apple Podcasts', {'resultCount': 0}),
                    ('Google', {'organic_results': []}),
                    ('Google', {'search_metadata': {'status': 'Success'},
                                'search_information': {'organic_results_state': 'Fully empty'}})]
        for source, payload in fixtures:
            with self.subTest(source=source, payload=payload):
                fetch.return_value = (payload, '')
                check = e.search_check(source, 'HarborSketch', 'offline-key')
                self.assertEqual(check['status'], 'Completed')
                self.assertEqual(check['exact_count'], 0)
                self.assertEqual(check['matches'], [])
        for source in ('YouTube', 'Apple Podcasts', 'Google'):
            for payload in ({}, {'unexpected': 'response'}, {'items': None}, {'organic_results': [None]}):
                with self.subTest(source=source, payload=payload):
                    fetch.return_value = (payload, '')
                    check = e.search_check(source, 'HarborSketch', 'offline-key')
                    self.assertEqual(check['status'], 'Failed')
                    self.assertIsNone(check['exact_count'])

    @patch('engine.requests.get')
    def test_provider_errors_remain_distinct_and_sanitized(self, get):
        for code, expected in ((401, 'API access denied'), (403, 'API access denied'),
                               (429, 'quota'), (500, 'HTTP 500'), (302, 'HTTP 302')):
            with self.subTest(code=code):
                get.return_value = Mock(status_code=code, json=Mock(side_effect=ValueError()))
                data, error = e.fetch_json('https://serpapi.com/search.json?api_key=never-print-this')
                self.assertIsNone(data)
                self.assertIn(expected, error)
                self.assertNotIn('never-print-this', error)
        get.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError()))
        self.assertIn('invalid', e.fetch_json('https://serpapi.com/search.json')[1].lower())
        get.side_effect = requests.ConnectionError('never-print-this')
        self.assertIn('connection', e.fetch_json('https://serpapi.com/search.json')[1].lower())

    @patch('engine.requests.get')
    def test_documented_serpapi_empty_envelope_is_not_an_error(self, get):
        envelope = {'error': "Google hasn't returned any results for this query.",
                    'search_metadata': {'status': 'Success'}}
        get.return_value = Mock(status_code=200, json=lambda: envelope)
        check = e.search_check('Google', 'HarborSketch', 'offline-key')
        self.assertEqual(check['status'], 'Completed')
        self.assertEqual(check['exact_count'], 0)
        for payload in (dict(envelope, search_metadata={'status': 'Error'}),
                        dict(envelope, organic_results=[{'title': 'Contradictory nonempty response'}]),
                        dict(envelope, error='Invalid API key: never-print-this'),
                        dict(envelope, error='Your account has run out of searches.')):
            get.return_value = Mock(status_code=200, json=lambda: payload)
            check = e.search_check('Google', 'HarborSketch', 'offline-key')
            self.assertEqual(check['status'], 'Failed')
            self.assertIsNone(check['exact_count'])
            self.assertNotIn('never-print-this', json.dumps(check))

    def test_priority_order_and_stale_confirmations(self):
        p = e.new_project(); p['names'] = ['Youtube Winner', 'Social Winner', 'Gmail Winner']
        for n in p['names']:
            targets = e.default_targets(n)
            p['reviews'][n.casefold()] = {'targets': targets, 'availability': {}}
        def set_status(n, platform, status):
            p['reviews'][n.casefold()]['availability'][platform] = {'status': status, 'target': e.targets_for(p,n)[platform]}
        set_status('Youtube Winner','YouTube','Confirmed available')
        set_status('Gmail Winner','Gmail','Confirmed available')
        for platform in ['Facebook','TikTok','Instagram','Domain']:
            set_status('Social Winner',platform,'Confirmed available')
        self.assertEqual([r['Name'] for r in e.export_rows(p)], ['Social Winner','Youtube Winner','Gmail Winner'])
        review = json.loads(json.dumps(p['reviews']['youtube winner']))
        review['targets']['YouTube'] = 'changedhandle'
        self.assertEqual(e.save_review(p,'Youtube Winner',review), ['YouTube'])
        self.assertEqual(e.effective_status(p,'Youtube Winner','YouTube',None)[0],'Unknown')

    def test_resume_and_key_separation(self):
        with tempfile.TemporaryDirectory() as d, patch('engine.DATA',Path(d)), patch('engine.PROJECTS',Path(d)/'projects'), patch('engine.migrate_local_projects'):
            p=e.new_project();p['names']=['Saved Name'];e.save_project(p);e.save_settings(last_project=p['id'])
            self.assertEqual(e.resume_project()['names'], ['Saved Name'])
            with patch('keyring.set_password') as put, patch('keyring.get_password',return_value='saved-secret'):
                e.remember_keys('yt-secret','web-secret')
                self.assertEqual(put.call_count,2)
                self.assertEqual(e.stored_key('YouTube'),'saved-secret')
            self.assertNotIn('secret',json.dumps(e.load_settings()))
            self.assertNotIn('secret',json.dumps(e.workbook_data(p)))

    def test_latest_per_candidate_and_full_export(self):
        p=e.new_project();p['names']=['First Name','New Name']
        row={'name':'First Name','checked_at':'old','dns':{'status':'No A record','message':'test','domain':'firstname.com'},'checks':[{'source':'Google','status':'Completed','url':'https://google.com','matches':[{'title':'First Name tips','url':'https://example.com','description':'saved description'}],'exact_count':0,'message':'test'}]}
        p['runs']=[{'id':'run1','results':[row]},{'id':'run2','results':[]}]
        export=e.workbook_data(p)
        self.assertEqual(len(export[2]['rows']),1)
        self.assertIn('saved description',export[2]['rows'][0])
        first=next(r for r in e.export_rows(p) if r['Name']=='First Name')
        self.assertEqual(first['Checked at'],'old')

    @patch('engine.fetch_json')
    def test_handle_found_and_failure_are_distinct(self,mock):
        mock.return_value=({'items':[{'id':'abc','snippet':{'title':'Taken'}}]},'')
        self.assertEqual(e.youtube_handle_check('myhandle','key')['status'],'Handle found')
        mock.return_value=({'items':[]},'')
        self.assertEqual(e.youtube_handle_check('myhandle','key')['status'],'No channel returned')
        mock.return_value=(None,'Request timed out.')
        self.assertEqual(e.youtube_handle_check('myhandle','key')['status'],'Failed')
        for payload in ({'pageInfo': []}, {'items': [None]}, {'items': [{'snippet': {}}]},
                        {'items': [{'id': 'abc', 'snippet': {'title': None}}]}):
            mock.return_value = (payload, '')
            self.assertEqual(e.youtube_handle_check('myhandle', 'key')['status'], 'Failed')

if __name__ == '__main__': unittest.main()
