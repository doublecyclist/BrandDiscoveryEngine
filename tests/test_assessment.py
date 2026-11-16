import json
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch
import requests
import engine as e
import domain_pricing as dp
from assessment import default_profile, profile_for
from research_queue import plan_research, research_state


class AssessmentTests(unittest.TestCase):
    def fixture(self, name='Banker Beacon', domain='Potentially available', matches=None):
        p=e.new_project('Example business');p['names']=[name];p['research_profile']=default_profile('Business / other')
        p['research_profile']['terms']='banking, software, consulting'
        row={'name':name,'checked_at':'2026-10-14T01:00:00Z','targets':e.default_targets(name),'profile':profile_for(p),
             'dns':{'domain':e.slug(name)+'.com','status':'No A record','message':'DNS only'},
             'registration':{'domain':e.slug(name)+'.com','status':domain,'message':'Registry evidence','url':'https://registry.example/domain'},
             'checks':[{'source':s,'status':'Completed','exact_count':0,'matches':matches or [],'url':'https://google.com','message':'Sample'} for s in ['Google','Google broad','Industry search']]}
        p['runs']=[{'id':'test','results':[row]}]
        return p,row

    def test_core_scoring_and_recommendation(self):
        p,row=self.fixture()
        result=e.export_rows(p)[0]
        self.assertEqual(result['Universal score'],60)
        self.assertEqual(result['Recommendation'],'Investigate')
        self.assertEqual(result['Project points'],0)
        self.assertEqual(result['Conflict assessment'],'Little evidence')
        row['registration']['status']='Registered'
        result=e.export_rows(p)[0]
        self.assertEqual(result['Domain points'],0)
        self.assertEqual(result['Recommendation'],'Investigate')

    def test_pricing_eligibility_budget_renewal_and_manual_decision(self):
        p, _ = self.fixture()
        p['reviews']['banker beacon'] = {'decision': 'Keep', 'decision_source': 'manual'}
        for price, renewal, verified, eligibility, recommendation in (
                ('100', '150', True, 'Eligible', 'Shortlist'),
                ('99', '150', False, 'Promising', 'Shortlist'),
                ('101', '1', True, 'Over budget', 'Investigate')):
            with self.subTest(price=price, verified=verified):
                p['domain_pricing'] = {'bankerbeacon.com': dp.observed_quote(
                    'bankerbeacon.com', 'Available', False, price, renewal, 'Offline fixture',
                    total_verified=verified)}
                row = e.export_rows(p)[0]
                self.assertEqual(row['Domain eligibility'], eligibility)
                self.assertEqual(row['Recommendation'], recommendation)
                self.assertEqual(row['Decision'], 'Keep')
                self.assertEqual(row['Annual renewal price (USD)'], float(renewal))
        self.assertEqual(p['reviews']['banker beacon']['decision'], 'Keep')

    def test_positive_availability_does_not_verify_unknown_stale_or_failed_pricing(self):
        p, row = self.fixture()
        p['reviews']['banker beacon'] = {'decision': 'Keep', 'decision_source': 'manual'}
        domain = 'bankerbeacon.com'
        good = dp.observed_quote(domain, 'Available', False, '19', '30',
                                 'Offline fixture', total_verified=True)
        cases = [
            ('unknown', {}, {}),
            ('stale', {domain: dict(good, checked_at='2020-01-01T00:00:00+00:00')}, {}),
            ('failed', {domain: good}, {domain: {'status': 'Failed', 'error': 'Timeout'}}),
            ('incomplete', {domain: good}, {domain: {'status': 'Incomplete', 'error': 'Missing total'}}),
        ]
        for label, quotes, attempts in cases:
            with self.subTest(label=label):
                p['domain_pricing'] = deepcopy(quotes)
                p['domain_pricing_attempts'] = deepcopy(attempts)
                assessed = e.export_rows(p)[0]
                self.assertEqual(assessed['Domain eligibility'], 'Investigate')
                self.assertNotEqual(assessed['Budget classification'], dp.AFFORDABLE)
                self.assertEqual(assessed['Recommendation'], 'Investigate')
                self.assertEqual(assessed['Decision'], 'Keep')
                self.assertEqual(p['reviews']['banker beacon']['decision'], 'Keep')

    def test_registered_without_resale_is_investigate_and_ranks_below_unknown(self):
        p, row = self.fixture(name='Registered Name', domain='Registered')
        p['names'].append('Unknown Name')
        unknown = dict(row, name='Unknown Name', targets=e.default_targets('Unknown Name'),
                       registration={'domain': 'unknownname.com', 'status': 'Potentially available', 'message': 'Registry evidence'})
        p['runs'][0]['results'].append(unknown)
        rows = e.export_rows(p)
        self.assertEqual([r['Name'] for r in rows], ['Unknown Name', 'Registered Name'])
        self.assertEqual(rows[1]['Recommendation'], 'Investigate')
        self.assertEqual(rows[1]['Budget classification'], dp.RESALE_UNKNOWN)

    def test_non_com_history_does_not_drive_or_trigger_screening(self):
        p, row = self.fixture()
        p['reviews']['banker beacon'] = {'targets': {'Domain': 'bankerbeacon.net'}, 'decision': 'Keep'}
        row['targets']['Domain'] = 'bankerbeacon.net'
        row['registration'] = {'domain': 'bankerbeacon.net', 'status': 'Confirmed available', 'message': 'Historical evidence'}
        p['domain_pricing'] = {'bankerbeacon.net': dp.observed_quote(
            'bankerbeacon.com', 'Available', False, '10', '20', 'Offline fixture', total_verified=True)}
        before = deepcopy(p)
        exported = e.export_rows(p)[0]
        self.assertEqual(exported['Domain eligibility'], 'Not eligible')
        self.assertEqual(exported['Domain points'], 0)
        self.assertEqual(exported['Recommendation'], 'Investigate')
        with patch('engine.search_check', return_value={'status': 'Completed', 'matches': []}), \
                patch('engine.dns_check_domain', side_effect=AssertionError('Non-com DNS lookup')), \
                patch('engine.registration_check', side_effect=AssertionError('Non-com registry lookup')):
            researched = e.research_name('Banker Beacon', targets=e.targets_for(p, 'Banker Beacon'),
                                         profile=profile_for(p))
        self.assertEqual(researched['registration']['status'], 'Not checked')
        self.assertEqual(p, before)

    def test_failed_or_old_data_never_proves_clear(self):
        p,row=self.fixture(domain='Check failed')
        row['checks'][0]['status']='Failed';row['checks'][0]['message']='Quota reached'
        result=e.export_rows(p)[0]
        self.assertEqual(result['Domain points'],0)
        self.assertIn('Quota reached',result['Incomplete checks'])
        self.assertEqual(result['Recommendation'],'Investigate')
        row.pop('registration');row.pop('profile')
        self.assertEqual(e.export_rows(p)[0]['Domain status'],'Not checked')
        self.assertEqual(e.export_rows(p)[0]['Conflict assessment'],'Insufficient evidence')

    def test_strong_brand_vs_incidental_name(self):
        m={'title':'Banker Beacon','description':'Commercial banking software platform','url':'https://bankerbeacon.example/'}
        p,row=self.fixture(matches=[m]);result=e.export_rows(p)[0]
        self.assertEqual(result['Conflict assessment'],'Strong conflict')
        self.assertEqual(result['Recommendation'],'Reject')
        self.assertEqual(result['Overlap leads'],1) # three searches, one unique page
        m['url']='https://news.example/story/1';m['description']='A person called Banker Beacon'
        self.assertEqual(e.export_rows(p)[0]['Conflict assessment'],'Possible overlap')

    def test_harborsketch_coincidental_phrase_is_not_a_strong_conflict(self):
        for title, expected in (
                ('Smith & Harbor: Sketch History of Our Accounting Software', 'Little evidence'),
                ('HarborSketch', 'Strong conflict'),
                ('Harbor-Sketch: Banking Software', 'Strong conflict'),
                ('An article about Harbor Sketch software', 'Possible overlap'),
                ('Unrelated Accounting Company', 'Little evidence')):
            with self.subTest(title=title):
                p, _ = self.fixture('HarborSketch', matches=[{
                    'title': title, 'description': 'Business banking software', 'url': 'https://accounting.example/'}])
                self.assertEqual(e.export_rows(p)[0]['Conflict assessment'], expected)

    def test_failed_refresh_retains_latest_success_per_source_and_raw_history(self):
        p, row = self.fixture(matches=[{'title': 'Banker Beacon', 'description': 'Banking software',
                                       'url': 'https://bankerbeacon.example/'}])
        failed = deepcopy(row)
        failed['checked_at'] = '2026-10-15T02:00:00Z'
        failed['checks'] = [{'source': 'Google', 'status': 'Failed', 'matches': [], 'message': 'Quota reached'}]
        failed['registration'] = {'domain': 'bankerbeacon.com', 'status': 'Check failed', 'message': 'Timeout'}
        failed.pop('dns')
        p['runs'].append({'id': 'failed-refresh', 'results': [failed]})
        before = deepcopy(p)
        effective = e.latest_results(p)['banker beacon']
        checks = {c['source']: c for c in effective['checks']}
        self.assertEqual(checks['Google']['status'], 'Completed')
        self.assertTrue(checks['Google']['retry_needed'])
        self.assertEqual(checks['Google']['checked_at'], row['checked_at'])
        self.assertEqual(checks['Google']['last_attempt']['checked_at'], failed['checked_at'])
        self.assertEqual(checks['Google broad']['matches'], row['checks'][1]['matches'])
        self.assertEqual(effective['registration']['status'], 'Potentially available')
        self.assertEqual(effective['dns']['checked_at'], row['checked_at'])
        assessed = e.export_rows(p)[0]
        self.assertEqual(assessed['Conflict assessment'], 'Strong conflict')
        self.assertIn('Quota reached', assessed['Incomplete checks'])
        self.assertIn(row['checked_at'], assessed['Research evidence dates'])
        self.assertIn(failed['checked_at'], assessed['Research refresh warnings'])
        self.assertEqual(assessed['Domain checked at'], row['checked_at'])
        self.assertEqual(research_state(p, 'Banker Beacon', effective), 'Incomplete')
        self.assertEqual(plan_research(p), ['Banker Beacon'])
        self.assertEqual(p, before)

    def test_later_completed_zero_results_supersede_old_conflicts(self):
        p, row = self.fixture(matches=[{'title': 'Banker Beacon', 'description': 'Banking software',
                                       'url': 'https://bankerbeacon.example/'}])
        empty = deepcopy(row)
        empty['checked_at'] = '2026-10-16T00:00:00Z'
        for check in empty['checks']:
            check['matches'] = []
        p['runs'].append({'id': 'valid-empty-refresh', 'results': [empty]})
        self.assertEqual(e.export_rows(p)[0]['Conflict assessment'], 'Little evidence')
        self.assertEqual(plan_research(p), [])

    @patch('engine.youtube_handle_check', side_effect=AssertionError('Disabled source'))
    @patch('engine.registration_check', side_effect=AssertionError('Completed registry repeated'))
    @patch('engine.dns_check_domain', side_effect=AssertionError('Completed DNS repeated'))
    @patch('engine.search_check')
    def test_retry_reuses_completed_checks_and_force_refresh_calls_every_source(self, search, dns, registry, handle):
        p, row = self.fixture()
        row['checks'][0].update(status='Failed', message='Timeout')
        previous = e.latest_results(p)['banker beacon']
        search.return_value = {'source': 'Google', 'status': 'Completed', 'matches': [], 'message': 'Zero results'}
        refreshed = e.research_name('Banker Beacon', profile=profile_for(p), previous=previous)
        search.assert_called_once_with('Google', 'Banker Beacon', '', '')
        self.assertEqual(refreshed['checks'][1]['checked_at'], row['checked_at'])
        self.assertEqual(refreshed['registration']['checked_at'], row['checked_at'])
        p['runs'].append({'id': 'retry', 'results': [refreshed]})
        self.assertEqual(plan_research(p), [])
        search.reset_mock()
        dns.side_effect = registry.side_effect = None
        dns.return_value = row['dns']
        registry.return_value = row['registration']
        e.research_name('Banker Beacon', profile=profile_for(p), previous=e.latest_results(p)['banker beacon'], force=True)
        self.assertEqual(search.call_count, 3)
        dns.assert_called_once()
        registry.assert_called_once()

    def test_changed_targets_do_not_inherit_other_domains_evidence(self):
        p, row = self.fixture()
        p['reviews']['banker beacon'] = {'targets': {'Domain': 'newbeacon.com'}}
        current = e.latest_results(p)['banker beacon']
        self.assertNotIn('registration', current)
        self.assertNotIn('dns', current)
        self.assertEqual(len(current['checks']), 3)
        self.assertIn('Domain registration', e.export_rows(p)[0]['Incomplete checks'])

    def test_retained_platform_dates_do_not_adopt_failed_refresh_time(self):
        p, row = self.fixture()
        p['research_profile'].update(youtube=True, social=True)
        row['profile'] = profile_for(p)
        row['checks'] += [{'source': source, 'status': 'Completed', 'matches': [], 'message': 'Completed sample'}
                          for source in ('YouTube', 'Facebook', 'TikTok', 'Instagram')]
        row['youtube_handle'] = {'target': e.targets_for(p, row['name'])['YouTube'],
                                 'status': 'Handle found', 'message': 'Existing channel', 'matches': []}
        failed = deepcopy(row)
        failed['checked_at'] = '2026-10-15T00:00:00Z'
        failed['checks'] = [dict(check, status='Failed', message='Timeout') for check in row['checks']]
        failed['youtube_handle'].update(status='Failed', message='Timeout')
        p['runs'].append({'id': 'failed-refresh', 'results': [failed]})
        assessed = e.export_rows(p)[0]
        self.assertEqual(assessed['YouTube status'], 'Conflict found')
        self.assertEqual(assessed['YouTube checked at'], row['checked_at'])
        self.assertEqual(assessed['Facebook checked at'], row['checked_at'])

    def test_core_outranks_optional_accounts(self):
        p,row=self.fixture('Core Winner')
        p['names'].append('Social Winner')
        other=dict(row,name='Social Winner',targets=e.default_targets('Social Winner'),registration={'status':'Registered','message':'test','domain':'socialwinner.com'})
        p['runs'][0]['results'].append(other)
        p['research_profile']=default_profile('YouTube / gaming') | {'terms':'banking, software, consulting'}
        for r in [row,other]:
            r['profile']=dict(p['research_profile'])
            r['checks']=list(r['checks'])+[{'source':'YouTube','status':'Completed','matches':[],'message':'test','url':'https://youtube.com','exact_count':0}]
            r['youtube_handle']={'status':'No channel returned','message':'test','matches':[]}
        targets=e.default_targets('Social Winner')
        p['reviews']['social winner']={'availability':{x:{'status':'Owned by me','target':targets[x]} for x in ['YouTube','Gmail','Facebook','TikTok','Instagram']}}
        self.assertEqual(e.export_rows(p)[0]['Name'],'Core Winner')

    def test_profile_change_and_target_change_lower_confidence(self):
        p,row=self.fixture()
        p['research_profile']['terms']='farming, gaming'
        self.assertIn('Industry terms changed',e.export_rows(p)[0]['Incomplete checks'])
        p['reviews']['banker beacon']={'targets':{'Domain':'otherdomain.com'}}
        self.assertEqual(e.export_rows(p)[0]['Domain points'],0)

    def test_manual_assessment_preserved(self):
        p,row=self.fixture(matches=[{'title':'Banker Beacon','description':'Banking software','url':'https://bankerbeacon.example/'}])
        p['reviews']['banker beacon']={'conflict_assessment':'Little evidence','conflict_evidence':'My own existing company'}
        r=e.export_rows(p)[0]
        self.assertEqual(r['Automatic conflict assessment'],'Strong conflict')
        self.assertEqual(r['Conflict assessment'],'Little evidence')
        self.assertEqual(r['Recommendation'],'Investigate')
        self.assertIn('My own existing company',r['Assessment basis'])

    @patch('engine.requests.get')
    def test_registry_distinguishes_record_and_structured_404(self,get):
        get.return_value=Mock(status_code=200,json=lambda:{'objectClassName':'domain','ldhName':'EXAMPLE.COM','status':['active']})
        self.assertEqual(e.registration_check('example.com')['status'],'Registered')
        get.return_value=Mock(status_code=404,json=lambda:{'errorCode':404})
        result=e.registration_check('example.com')
        self.assertEqual(result['status'],'Potentially available')
        self.assertIn('reservations',result['message'])
        get.return_value=Mock(status_code=404,json=lambda:{'message':'generic proxy error'})
        self.assertEqual(e.registration_check('example.com')['status'],'Check failed')

    @patch('engine.requests.get')
    def test_empty_registry_404_is_supported_only_for_trusted_endpoints(self, get):
        get.return_value = Mock(status_code=404, content=b'', json=Mock(side_effect=ValueError()))
        result = e.registration_check('example.com')
        self.assertEqual(result['status'], 'Potentially available')
        self.assertIn('Confirm sale eligibility', result['message'])
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        for code, content in ((404, b'<html>Proxy not found</html>'), (200, b''), (429, b''), (500, b'')):
            get.return_value = Mock(status_code=code, content=content, json=Mock(side_effect=ValueError()))
            self.assertEqual(e.registration_check('example.com')['status'], 'Check failed')
        get.return_value = Mock(status_code=200, json=lambda: {'objectClassName': 'domain', 'ldhName': 'other.com'})
        self.assertEqual(e.registration_check('example.com')['status'], 'Check failed')
        get.return_value = Mock(status_code=200, json=lambda: {'objectClassName': 'domain', 'ldhName': None})
        self.assertEqual(e.registration_check('example.com')['status'], 'Check failed')
        get.return_value = Mock(status_code=404, content=b'', json=Mock(side_effect=ValueError()))
        with patch('engine.fetch_json', return_value=({'services': [[['test'], ['https://registry.example/rdap/']]]}, '')):
            self.assertEqual(e.registration_check('example.test')['status'], 'Check failed')
        with patch('engine.fetch_json', return_value=({'services': [None]}, '')):
            self.assertEqual(e.registration_check('example.test')['status'], 'Check failed')
        get.return_value=Mock(status_code=429,json=lambda:{'errorCode':429})
        self.assertEqual(e.registration_check('example.com')['status'],'Check failed')
        get.side_effect=requests.Timeout()
        self.assertEqual(e.registration_check('example.com')['status'],'Check failed')

    @patch('engine.requests.get')
    def test_api_quota_and_auth_diagnostics(self,get):
        get.return_value=Mock(status_code=403,json=lambda:{'error':{'errors':[{'reason':'quotaExceeded'}]}})
        self.assertIn('quota',e.fetch_json('https://example.com')[1])
        get.return_value=Mock(status_code=403,json=lambda:{'error':{'errors':[{'reason':'accessNotConfigured'}]}})
        self.assertIn('API access denied',e.fetch_json('https://example.com')[1])

    @patch('engine.fetch_json',return_value=({'pageInfo':{'totalResults':0}},''))
    def test_valid_empty_handle_response(self,_):
        self.assertEqual(e.youtube_handle_check('bankerbeacon','secret')['status'],'No channel returned')

    def test_brand_name_header_imports_targets(self):
        p=e.new_project('Example business')
        e.merge_candidate_csv(p,b'Brand Name,Domain Candidate,YouTube Handle Candidate\nBanker Beacon,trybankerbeacon.com,@beaconvideo\n')
        self.assertEqual(p['names'],['Banker Beacon'])
        self.assertEqual(e.targets_for(p,'Banker Beacon')['Domain'],'trybankerbeacon.com')
        self.assertEqual(e.targets_for(p,'Banker Beacon')['YouTube'],'beaconvideo')

    def test_workbook_new_fields_and_evidence(self):
        p,row=self.fixture()
        data=e.workbook_data(p)
        for label in ['Universal score','Recommendation','Conflict assessment','Competing names','Incomplete checks']:
            self.assertIn(label,data[0]['columns'])
        self.assertIn('Domain registration',json.dumps(data[1]))
        self.assertNotIn('YouTube tier first',json.dumps(data[-1]))

if __name__=='__main__':unittest.main()
