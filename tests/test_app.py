import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import engine
from assessment import profile_for


class AppDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            patch('engine.PROJECTS', self.root),
            patch('engine.DATA', self.root),
            patch('engine.migrate_local_projects'),
            patch('engine.stored_key', return_value=''),
            patch('engine.fetch_json', side_effect=AssertionError('Unexpected research/API request')),
            patch('engine.requests.get', side_effect=AssertionError('Unexpected HTTP request')),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def fixture_project(self):
        project = engine.new_project('Decision persistence test')
        project['platform'] = 'Business / other'
        project['brief'] = 'A banking software business'
        project['names'] = ['Strong Beacon', 'Possible Beacon', 'Quiet Beacon']
        project['research_profile'] = {
            'terms': 'banking, software',
            'youtube': False,
            'gmail': False,
            'social': False,
            'podcasts': False,
        }
        profile = profile_for(project)
        results = []
        samples = {
            'Strong Beacon': [{
                'title': 'Strong Beacon',
                'description': 'Commercial banking software',
                'url': 'https://strongbeacon.example/',
            }],
            'Possible Beacon': [{
                'title': 'Possible Beacon',
                'description': 'A community group with no industry details',
                'url': 'https://possiblebeacon.example/',
            }],
            'Quiet Beacon': [],
        }
        for name in project['names']:
            results.append({
                'name': name,
                'checked_at': '2026-10-14T01:00:00Z',
                'targets': engine.default_targets(name),
                'profile': profile,
                'dns': {'domain': engine.slug(name) + '.com', 'status': 'No A record', 'message': 'Test data'},
                'registration': {'domain': engine.slug(name) + '.com', 'status': 'Potentially available',
                                 'message': 'Test data', 'url': 'https://registry.example/domain'},
                'checks': [{
                    'source': source,
                    'status': 'Completed',
                    'matches': samples[name],
                    'exact_count': len(samples[name]),
                    'url': 'https://google.example/search',
                    'message': 'Test data',
                } for source in ('Google', 'Google broad', 'Industry search')],
            })
        project['runs'] = [{'id': 'saved-research', 'results': results}]
        engine.save_project(project)
        return project

    def launch(self):
        return AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=10)

    @staticmethod
    def matching_names(app):
        return [item.value[3:] for item in app.markdown if item.value.startswith('## ')]

    @staticmethod
    def metric_values(app):
        return {item.label: item.value for item in app.metric[:4]}

    def shortlist_table(self, app):
        return next(item for item in app.dataframe if 'Candidate name' in item.value.columns)

    def test_reference_counts_and_selectable_shortlist(self):
        project = engine.new_project('150 candidate shortlist')
        project['names'] = [f'Candidate {index:03d}' for index in range(1, 151)]
        project['reviews'] = {
            name.casefold(): {
                'decision': 'Maybe' if index <= 59 else 'Reject',
                'decision_source': 'manual',
            }
            for index, name in enumerate(project['names'], start=1)
        }
        engine.save_project(project)
        app = self.launch()
        self.assertFalse(app.exception)
        app = next(item for item in app.selectbox if item.label == 'Show names').set_value(
            'Shortlist (Keep + Investigate)').run(timeout=30)
        table = self.shortlist_table(app)
        self.assertEqual(len(table.value), 59)
        self.assertEqual(set(table.value['Current decision']), {'Investigate'})
        self.assertEqual(self.metric_values(app), {
            'To review': '0', 'Keep': '0', 'Investigate': '59', 'Discard': '91',
        })
        self.assertEqual(list(table.value.columns), [
            'Candidate name', 'Current decision', 'Automatic conflict assessment',
            '.com registration status', 'Research completeness', 'Last research date',
            'Domain name', 'Registration availability', 'Premium status', 'Acquisition price (USD)',
            'Annual renewal price (USD)', 'Budget classification', 'Price-check date', 'Pricing source',
            'Pricing notes',
            'Registration price (USD)', 'Premium/resale purchase fee (USD)',
            'Known acquisition minimum (USD)', 'Taxes and mandatory charges', 'Price reliability',
            'Pricing currency', 'Registrar inventory',
        ])

        selected_name = table.value.iloc[1]['Candidate name']
        app.session_state[table.key] = {'selection': {'rows': [1]}}
        app = app.run(timeout=30)
        self.assertEqual(self.matching_names(app), [selected_name])

    def test_manual_shortlist_changes_are_immediate_and_persist_without_research(self):
        project = self.fixture_project()
        project['reviews']['possible beacon'] = {'decision': 'Maybe', 'decision_source': 'manual'}
        engine.save_project(project)
        app = self.launch()
        app = next(item for item in app.selectbox if item.label == 'Show names').set_value(
            'Shortlist (Keep + Investigate)').run(timeout=10)
        app = next(item for item in app.text_input if item.label == 'Find name').set_value(
            'Possible Beacon').run(timeout=10)
        candidate_key = 'possible beacon'
        app = next(item for item in app.button if item.key == f'keep_{candidate_key}_{project["id"]}').click().run(timeout=10)
        review = app.session_state.project['reviews'][candidate_key]
        self.assertEqual(review['decision'], 'Keep')
        self.assertEqual(review['decision_source'], 'manual')
        table = self.shortlist_table(app)
        self.assertEqual(list(table.value['Candidate name']), ['Possible Beacon'])
        self.assertEqual(list(table.value['Current decision']), ['Keep'])

        app = next(item for item in app.button if item.key == f'discard_{candidate_key}_{project["id"]}').click().run(timeout=10)
        self.assertFalse(any('Candidate name' in item.value.columns for item in app.dataframe))
        self.assertNotIn('Possible Beacon', self.matching_names(app))
        self.assertEqual(app.session_state.project['reviews'][candidate_key]['decision'], 'Reject')
        self.assertEqual(app.session_state.project['reviews'][candidate_key]['decision_source'], 'manual')
        saved_runs = app.session_state.project['runs']

        app = next(item for item in app.selectbox if item.label == 'Show names').set_value('Discard').run(timeout=10)
        self.assertEqual(self.matching_names(app), ['Possible Beacon'])
        restarted = self.launch()
        self.assertFalse(restarted.exception)
        self.assertEqual(restarted.session_state.project['reviews'][candidate_key]['decision'], 'Reject')
        self.assertEqual(restarted.session_state.project['reviews'][candidate_key]['decision_source'], 'manual')
        self.assertEqual(restarted.session_state.project['runs'], saved_runs)

    def test_screening_recommendations_never_save_decisions(self):
        project = self.fixture_project()
        path = self.root / (project['id'] + '.json')
        before = path.read_bytes()
        app = self.launch()
        self.assertFalse(app.exception)
        saved = app.session_state.project
        self.assertEqual(saved['reviews'], {})
        self.assertEqual(self.metric_values(app), {
            'To review': '3', 'Keep': '0', 'Investigate': '0', 'Discard': '0',
        })
        self.assertEqual(saved['runs'], project['runs'])

        for filter_name, expected_name in (
            ('Strong conflicts', 'Strong Beacon'),
            ('Possible overlaps', 'Possible Beacon'),
            ('Little evidence', 'Quiet Beacon'),
        ):
            filter_widget = next(item for item in app.selectbox if item.label == 'Show names')
            app = filter_widget.set_value(filter_name).run(timeout=10)
            self.assertEqual(self.matching_names(app), [expected_name])
        self.assertEqual(path.read_bytes(), before)

    def test_manual_override_survives_restart_and_auto_sync(self):
        project = self.fixture_project()
        app = self.launch()
        project_id = project['id']
        strong_key = 'strong beacon'
        app = next(item for item in app.selectbox if item.label == 'Show names').set_value('Strong conflicts').run(timeout=10)
        keep = next(item for item in app.button
                    if item.key == f'keep_{strong_key}_{project_id}')
        app = keep.click().run(timeout=10)
        review = app.session_state.project['reviews'][strong_key]
        self.assertEqual(review['decision'], 'Keep')
        self.assertEqual(review['decision_source'], 'manual')

        restarted = self.launch()
        self.assertFalse(restarted.exception)
        review = restarted.session_state.project['reviews'][strong_key]
        self.assertEqual(review['decision'], 'Keep')
        self.assertEqual(review['decision_source'], 'manual')
        self.assertEqual(self.metric_values(restarted), {
            'To review': '2', 'Keep': '1', 'Investigate': '0', 'Discard': '0',
        })
        self.assertEqual(restarted.session_state.project['runs'], project['runs'])

        selected = next(item for item in restarted.selectbox if item.label == 'Show names')
        restarted = selected.set_value('Discard').run(timeout=10)
        self.assertEqual(self.matching_names(restarted), [])
        search = next(item for item in restarted.text_input if item.label == 'Find name')
        restarted = search.set_value('Strong Beacon').run(timeout=10)
        restarted = next(item for item in restarted.selectbox if item.label == 'Show names').set_value('Keep').run(timeout=10)
        self.assertEqual(self.matching_names(restarted), ['Strong Beacon'])

    def test_manual_undecided_is_not_replaced_but_legacy_decision_is_preserved(self):
        project = engine.new_project('Provenance test')
        project['reviews'] = {
            'manual pending': {'decision': 'Undecided', 'decision_source': 'manual'},
            'legacy choice': {'decision': 'Keep'},
            'automatic choice': {'decision': 'Keep', 'decision_source': 'automatic'},
        }
        rows = [
            {'Name': 'Manual Pending', 'Conflict assessment': 'Strong conflict'},
            {'Name': 'Legacy Choice', 'Conflict assessment': 'Strong conflict'},
            {'Name': 'Automatic Choice', 'Automatic conflict assessment': 'Strong conflict',
             'Conflict assessment': 'Little evidence'},
            {'Name': 'No Evidence', 'Conflict assessment': 'Insufficient evidence'},
        ]
        before = deepcopy(project)
        self.assertFalse(engine.sync_automatic_decisions(project, rows))
        self.assertEqual(project, before)
        self.assertEqual(project['reviews']['manual pending']['decision'], 'Undecided')
        self.assertEqual(project['reviews']['legacy choice']['decision'], 'Keep')
        self.assertEqual(project['reviews']['automatic choice']['decision'], 'Keep')
        self.assertEqual(project['reviews']['automatic choice']['decision_source'], 'automatic')
        self.assertNotIn('no evidence', project['reviews'])

    def test_failed_research_checkpoint_stops_calls_and_keeps_unsaved_result(self):
        project = self.fixture_project()
        app = self.launch()
        app = next(item for item in app.toggle if item.label == 'Show project management and research tools').set_value(True).run()
        self.assertFalse(app.exception)
        app = next(item for item in app.radio if item.label == 'Research mode').set_value('All (force refresh)').run()
        save = engine.save_project

        def fail_result_checkpoint(working, *args, **kwargs):
            if len(working['runs']) > 1 and working['runs'][-1]['results']:
                raise OSError('Isolated checkpoint failure')
            return save(working, *args, **kwargs)

        result = deepcopy(project['runs'][0]['results'][0])
        with patch('engine.research_name', return_value=result) as research, patch(
                'engine.save_project', side_effect=fail_result_checkpoint) as saves:
            app = next(item for item in app.button if item.label == 'Research selected names').click().run(timeout=20)
        self.assertFalse(app.exception)
        research.assert_called_once()
        self.assertEqual(saves.call_count, 2)
        self.assertTrue(research.call_args.kwargs['force'])
        self.assertTrue(any('0 confirmed saved names' in item.value for item in app.error))
        self.assertEqual(app.session_state.project['runs'][-1]['results'], [result])
        saved = engine.load_project(self.root / (project['id'] + '.json'))
        self.assertEqual(saved['runs'][-1]['results'], [])
        app = app.run()
        self.assertTrue(any(item.label == 'Download unsaved project backup' for item in app.get('download_button')))

    def test_failed_initial_research_checkpoint_makes_no_provider_calls(self):
        self.fixture_project()
        app = self.launch()
        app = next(item for item in app.toggle if item.label == 'Show project management and research tools').set_value(True).run()
        self.assertFalse(app.exception)
        app = next(item for item in app.radio if item.label == 'Research mode').set_value('All (force refresh)').run()
        with patch('engine.research_name', side_effect=AssertionError('No provider calls')) as research, patch(
                'engine.save_project', side_effect=OSError('Isolated write failure')):
            app = next(item for item in app.button if item.label == 'Research selected names').click().run(timeout=20)
        self.assertFalse(app.exception)
        research.assert_not_called()
        self.assertTrue(any('Research did not start' in item.value for item in app.error))

    def test_failed_refresh_warning_exposes_original_evidence_date(self):
        project = self.fixture_project()
        failed = deepcopy(project['runs'][0]['results'][0])
        failed['checked_at'] = '2026-10-15T02:00:00Z'
        failed['checks'] = [dict(failed['checks'][0], status='Failed', exact_count=None, matches=[], message='Quota reached')]
        project['runs'].append({'id': 'failed-refresh', 'results': [failed]})
        engine.save_project(project)
        path = self.root / (project['id'] + '.json')
        before = path.read_bytes()
        app = self.launch()
        app = next(item for item in app.selectbox if item.label == 'Show names').set_value('Strong conflicts').run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Quota reached' in item.value and '2026-10-14' in item.value
                            and '2026-10-15' in item.value for item in app.warning))
        app = next(item for item in app.toggle if item.label == 'Show project management and research tools').set_value(True).run()
        self.assertFalse(app.exception)
        app = next(item for item in app.selectbox if item.label == 'Review a candidate').set_value('Strong Beacon').run()
        self.assertFalse(app.exception)
        self.assertTrue(any('Google: 2026-10-14T01:00:00Z' in item.value for item in app.text))
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
