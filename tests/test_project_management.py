import copy
import os
import tempfile
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import engine
from domain_pricing import observed_quote
from test_domain_pricing import OfflineTests


class ProjectManagementTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.projects = self.root / 'projects'
        self.projects.mkdir()
        for item in (patch('engine.DATA', self.root), patch('engine.PROJECTS', self.projects),
                     patch('engine.migrate_local_projects'), patch('engine.stored_key', return_value=''),
                     patch('engine.fetch_json', side_effect=AssertionError('No research requests')),
                     patch('godaddy_pricing.GoDaddyPricing', side_effect=AssertionError('No pricing requests'))):
            item.start()
            self.patches.append(item)
        self.original = self.fixture('New brand project', 150)
        self.active = self.fixture('sample research project', 100)
        self.original_filename = self.original['id'] + '.json'
        self.active_filename = self.active['id'] + '.json'
        engine.save_project(self.original)
        engine.save_project(self.active)
        engine.save_settings(last_project=self.active['id'])

    def tearDown(self):
        super().tearDown()
        self.temp.cleanup()

    def fixture(self, title, count):
        p = engine.new_project(title)
        p['names'] = [f'Candidate{i:03d}' for i in range(count)]
        p['reviews'] = {name.casefold(): {'decision': 'Keep' if i % 2 else 'Reject',
                                         'decision_source': 'manual', 'notes': 'Keep original evidence'}
                        for i, name in enumerate(p['names'])}
        p['runs'] = [{'id': 'historical-run', 'results': []}]
        p['domain_pricing'] = {
            'candidate000.com': observed_quote('candidate000.com', 'Registered', True, '9950', '19.98', 'Manual evidence'),
            'candidate001.com': {'provider': 'GoDaddy', 'source': 'GoDaddy v3 availability API',
                                 'availability': 'Available', 'acquisition_price': '15.00',
                                 'currency': 'USD', 'checked_at': engine.now(), 'verified': True,
                                 'total_verified': False, 'price_kind': 'registration'},
        }
        p['domain_pricing_history'] = {'candidate001.com': [{'source': 'Prior evidence', 'acquisition_price': '20'}]}
        return p

    def database_bytes(self):
        return {path.name: path.read_bytes() for path in self.projects.glob('*.json')}

    def launch(self):
        app = AppTest.from_file(str(engine.ROOT / 'app.py')).run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def select(self, app, filename):
        return next(x for x in app.selectbox if x.label == 'Saved projects').set_value(filename).run(timeout=30)

    def click(self, app, label):
        app = next(x for x in app.button if x.label == label).click().run(timeout=30)
        self.assertFalse(app.exception)
        return app

    def confirm(self, app):
        app = next(x for x in app.checkbox if x.label.startswith('I confirm switching')).check().run(timeout=30)
        return self.click(app, 'Confirm database switch')

    def test_discovery_exact_filenames_counts_mtime_and_title_read_only(self):
        before = self.database_bytes()
        path = self.projects / self.original_filename
        os.utime(path, (1700000000, 1700000000))
        saved, warnings = engine.discover_projects()
        self.assertFalse(warnings)
        self.assertEqual(set(saved), {self.original_filename, self.active_filename})
        entry = saved[self.original_filename]
        self.assertEqual(entry['candidate_count'], 150)
        self.assertEqual(entry['title'], 'New brand project')
        label = engine.project_display_label(entry)
        self.assertIn(self.original_filename, label)
        self.assertIn('150 candidates', label)
        self.assertIn('2023-11-14', label)
        self.assertEqual(self.database_bytes(), before)

    def test_duplicate_titles_and_ids_are_listed_by_filename_not_collapsed(self):
        duplicate = copy.deepcopy(self.original)
        engine.write_json(self.projects / 'copy-of-original.json', duplicate)
        saved, warnings = engine.discover_projects()
        self.assertFalse(warnings)
        self.assertEqual(len(saved), 3)
        self.assertIn('copy-of-original.json', saved)
        self.assertEqual(saved['copy-of-original.json']['project']['id'], self.original['id'])

    def test_corrupt_projects_and_metadata_report_errors_without_overwriting(self):
        bad = self.projects / 'bad.json'
        bad.write_text('invalid json', encoding='utf-8')
        settings = self.root / 'settings.json'
        settings.write_text('invalid metadata', encoding='utf-8')
        before = self.database_bytes()
        saved, warnings = engine.discover_projects()
        self.assertEqual(len(saved), 2)
        self.assertTrue(any('bad.json' in warning for warning in warnings))
        self.assertTrue(any('metadata' in warning for warning in warnings))
        with self.assertRaises(ValueError):
            engine.save_project_display_name(self.active_filename, 'Friendly')
        self.assertEqual(self.database_bytes(), before)
        self.assertEqual(settings.read_text(), 'invalid metadata')

    def test_friendly_names_are_metadata_only_and_keep_other_settings(self):
        before = self.database_bytes()
        timestamps = {path.name: path.stat().st_mtime_ns for path in self.projects.glob('*.json')}
        engine.save_settings(unrelated={'value': 'keep'})
        engine.save_project_display_name(self.original_filename, 'Project A')
        engine.save_project_display_name(self.active_filename, 'Project B')
        settings = engine.load_settings()
        self.assertEqual(settings['unrelated'], {'value': 'keep'})
        self.assertEqual(settings['last_project'], self.active['id'])
        saved, warnings = engine.discover_projects()
        self.assertFalse(warnings)
        self.assertEqual(saved[self.original_filename]['display_name'], 'Project A')
        self.assertIn('Project: New brand project', engine.project_display_label(saved[self.original_filename]))
        engine.save_project_display_name(self.original_filename, 'Renamed display only')
        self.assertEqual(engine.discover_projects()[0][self.original_filename]['display_name'], 'Renamed display only')
        engine.save_project_display_name(self.original_filename, '')
        self.assertEqual(engine.discover_projects()[0][self.original_filename]['display_name'], '')
        self.assertEqual(self.database_bytes(), before)
        self.assertEqual({path.name: path.stat().st_mtime_ns for path in self.projects.glob('*.json')}, timestamps)

    def test_invalid_names_paths_and_metadata_write_failure_preserve_files(self):
        before = self.database_bytes()
        for filename in ('../evil.json', '..\\evil.json', 'bad:stream.json', 'not-json.txt'):
            with self.assertRaises(ValueError):
                engine.save_project_display_name(filename, 'Name')
        for value in ('x' * 121, 'Name\nline', None):
            with self.assertRaises(ValueError):
                engine.save_project_display_name(self.active_filename, value)
        with patch('engine.write_json', side_effect=OSError('not writable')):
            with self.assertRaises(OSError):
                engine.save_project_display_name(self.active_filename, 'No save')
        self.assertEqual(self.database_bytes(), before)

    def test_exact_filename_save_and_resume_never_creates_renamed_database(self):
        custom = 'original-candidate-database.json'
        engine.write_json(self.projects / custom, self.original)
        before = (self.projects / self.original_filename).read_bytes()
        engine.save_settings(last_project=self.original['id'], last_project_filename=custom)
        self.assertEqual(engine.resume_project_path().name, custom)
        self.assertEqual(engine.resume_project()['names'], self.original['names'])
        changed = engine.load_project(self.projects / custom)
        changed['brief'] = 'Explicitly saved user edit'
        engine.save_project(changed, filename=custom)
        self.assertEqual((self.projects / self.original_filename).read_bytes(), before)
        self.assertEqual(engine.load_project(self.projects / custom)['brief'], 'Explicitly saved user edit')
        self.assertEqual(engine.load_project(self.projects / custom)['domain_pricing'], self.original['domain_pricing'])

    def test_ui_discovery_naming_and_active_identity_preserve_every_database(self):
        before = self.database_bytes()
        app = self.launch()
        selector = next(x for x in app.selectbox if x.label == 'Saved projects')
        self.assertTrue(any(self.original_filename in option and '150 candidates' in option for option in selector.options))
        self.assertTrue(any(self.active_filename in option and '100 candidates' in option for option in selector.options))
        self.assertTrue(any('Active database: sample research project' in x.value for x in app.info))
        app = self.select(app, self.original_filename)
        next(x for x in app.text_input if x.label == 'Friendly display name').set_value('Project A')
        app = self.click(app, 'Save display name')
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        self.assertEqual(engine.discover_projects()[0][self.original_filename]['display_name'], 'Project A')
        self.assertEqual(self.database_bytes(), before)

    def test_switch_requires_confirmation_and_changes_only_last_project_metadata(self):
        before = self.database_bytes()
        app = self.select(self.launch(), self.original_filename)
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        app = self.click(app, 'Load selected project')
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        self.assertTrue(next(x for x in app.button if x.label == 'Confirm database switch').disabled)
        app = self.confirm(app)
        self.assertEqual(app.session_state.project['id'], self.original['id'])
        self.assertEqual(app.session_state.project_filename, self.original_filename)
        self.assertEqual(app.session_state.project['domain_pricing'], self.original['domain_pricing'])
        self.assertEqual(app.session_state.project['reviews'], self.original['reviews'])
        self.assertEqual(engine.load_settings()['last_project_filename'], self.original_filename)
        restarted = self.launch()
        self.assertEqual(restarted.session_state.project['id'], self.original['id'])
        self.assertEqual(self.database_bytes(), before)

    def test_dirty_changes_are_not_lost_until_explicit_confirmation_and_cancel_keeps_them(self):
        before = self.database_bytes()
        app = self.launch()
        app.session_state.project['reviews']['candidate000']['notes'] = 'Unsaved working edit'
        app = self.select(app, self.original_filename)
        self.assertTrue(any('unsaved changes' in x.value for x in app.warning))
        app = self.click(app, 'Load selected project')
        app = self.click(app, 'Cancel database switch')
        self.assertEqual(app.session_state.project['reviews']['candidate000']['notes'], 'Unsaved working edit')
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        app = self.click(app, 'Load selected project')
        app = self.confirm(app)
        self.assertEqual(app.session_state.project['id'], self.original['id'])
        self.assertEqual(self.database_bytes(), before)

    def test_failed_saves_and_failed_switch_leave_active_edits_open(self):
        app = self.launch()
        with patch('engine.write_json', side_effect=OSError('save failed')):
            app = next(x for x in app.number_input if x.label == 'Maximum domain acquisition price (USD)').set_value(50.0).run()
        self.assertEqual(app.session_state.project['domain_budget'], 50.0)
        app = self.select(app, self.original_filename)
        self.assertTrue(any('unsaved changes' in x.value for x in app.warning))
        app = self.click(app, 'Load selected project')
        with patch('engine.write_json', side_effect=OSError('settings failed')):
            app = self.confirm(app)
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        self.assertEqual(app.session_state.project['domain_budget'], 50.0)
        self.assertTrue(any('Could not switch databases' in x.value for x in app.error))

    def test_two_loaded_sessions_cannot_silently_overwrite_project(self):
        path = self.projects / self.active_filename
        first, second = engine.load_project(path), engine.load_project(path)
        second_before = copy.deepcopy(second)
        first['reviews']['candidate000']['notes'] = 'First session saved evidence'
        engine.save_project(first)
        saved = path.read_bytes()
        second['reviews']['candidate000']['notes'] = 'Second session recoverable edit'
        with self.assertRaises(engine.ProjectConflict):
            engine.save_project(second)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(second['updated_at'], second_before['updated_at'])
        self.assertEqual(second['reviews']['candidate000']['notes'], 'Second session recoverable edit')
        self.assertEqual(second['domain_pricing_history'], second_before['domain_pricing_history'])
        self.assertNotIn('_revision', json.loads(saved))
        with self.assertRaises(engine.ProjectConflict):
            engine.save_project(dict(first))

    def test_simultaneous_saves_have_exactly_one_winner(self):
        path = self.projects / self.active_filename
        projects = [engine.load_project(path), engine.load_project(path)]
        barrier = Barrier(2)

        def attempt(index):
            project = projects[index]
            project['brief'] = f'Session {index}'
            barrier.wait(timeout=5)
            try:
                engine.save_project(project)
                return True
            except engine.ProjectConflict:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(results), 1)
        winner = results.index(True)
        self.assertEqual(engine.load_project(path)['brief'], f'Session {winner}')
        self.assertFalse(path.with_suffix('.json.lock').exists())

    def test_existing_lock_and_deleted_database_cannot_be_silently_overwritten(self):
        path = self.projects / self.active_filename
        project = engine.load_project(path)
        saved = path.read_bytes()
        timestamp = project['updated_at']
        project['brief'] = 'Recoverable local edit'
        lock = path.with_suffix('.json.lock')
        lock.write_text('Isolated interrupted-save fixture', encoding='utf-8')
        with self.assertRaises(engine.ProjectConflict):
            engine.save_project(project)
        self.assertEqual(path.read_bytes(), saved)
        self.assertTrue(lock.exists())
        lock.unlink()
        path.unlink()
        with self.assertRaises(engine.ProjectConflict):
            engine.save_project(project)
        self.assertFalse(path.exists())
        self.assertEqual(project['updated_at'], timestamp)
        self.assertEqual(project['brief'], 'Recoverable local edit')

    def test_interrupted_process_lock_blocks_saves_until_explicit_recovery(self):
        path = self.projects / self.active_filename
        project = engine.load_project(path)
        saved = path.read_bytes()
        revision = project._revision
        project['brief'] = 'Recoverable local edit'
        script = ("import os, sys; from pathlib import Path; import engine; "
                  "lock = engine.project_write_lock(Path(sys.argv[1])); "
                  "lock.__enter__(); os._exit(0)")
        subprocess.run([sys.executable, '-c', script, str(path)], cwd=engine.ROOT,
                       check=True, timeout=15, capture_output=True)
        lock = path.with_suffix('.json.lock')
        self.assertTrue(lock.exists())
        with self.assertRaises(engine.ProjectConflict):
            engine.save_project(project)
        self.assertTrue(lock.exists())
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(project._revision, revision)
        self.assertEqual(project['brief'], 'Recoverable local edit')
        lock.unlink()
        engine.save_project(project)
        self.assertEqual(engine.load_project(path)['brief'], 'Recoverable local edit')
        self.assertFalse(lock.exists())

    def test_failed_atomic_replace_keeps_saved_bytes_revision_and_recoverable_edits(self):
        path = self.projects / self.active_filename
        project = engine.load_project(path)
        saved = path.read_bytes()
        timestamp, revision = project['updated_at'], project._revision
        project['reviews']['candidate000']['notes'] = 'Unsaved recoverable evidence'
        with patch('engine.os.replace', side_effect=OSError('Disk write refused')):
            with self.assertRaises(OSError):
                engine.save_project(project)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(project['updated_at'], timestamp)
        self.assertEqual(project._revision, revision)
        self.assertEqual(project['reviews']['candidate000']['notes'], 'Unsaved recoverable evidence')
        self.assertFalse(list(self.projects.glob('*.tmp')))
        self.assertFalse(list(self.projects.glob('*.lock')))
        engine.save_project(project)
        self.assertEqual(engine.load_project(path)['reviews'], project['reviews'])

    def test_ui_conflict_keeps_edits_and_offers_backup_and_confirmed_reload(self):
        app = self.launch()
        path = self.projects / self.active_filename
        external = engine.load_project(path)
        external['reviews']['candidate000']['notes'] = 'Other session evidence'
        engine.save_project(external)
        saved = path.read_bytes()
        app = next(x for x in app.number_input if x.label == 'Maximum domain acquisition price (USD)').set_value(50.0).run()
        app = app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(any('another session' in x.value for x in app.error))
        self.assertEqual(app.session_state.project['domain_budget'], 50)
        self.assertNotIn('domain_budget', app.session_state.project_saved_snapshot)
        self.assertTrue(any(x.label == 'Download unsaved project backup' for x in app.get('download_button')))
        self.assertEqual(path.read_bytes(), saved)
        app = self.click(app, 'Reload saved database')
        self.assertEqual(app.session_state.project['domain_budget'], 50)
        app = self.confirm(app)
        self.assertNotIn('domain_budget', app.session_state.project)
        self.assertEqual(app.session_state.project['reviews']['candidate000']['notes'], 'Other session evidence')
        self.assertNotIn('save_error', app.session_state)
        self.assertEqual(path.read_bytes(), saved)

    def test_settings_failure_does_not_misreport_a_successful_project_save(self):
        app = self.launch()
        write = engine.write_json

        def fail_settings(path, value):
            if Path(path).name == 'settings.json':
                raise OSError('Settings refused')
            return write(path, value)

        with patch('engine.write_json', side_effect=fail_settings):
            app = next(x for x in app.number_input if x.label == 'Maximum domain acquisition price (USD)').set_value(50.0).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error)
        self.assertTrue(any('Project saved, but' in x.value for x in app.warning))
        self.assertEqual(engine.load_project(self.projects / self.active_filename)['domain_budget'], 50)
        self.assertEqual(app.session_state.project_saved_snapshot, app.session_state.project)
        self.assertNotIn('save_error', app.session_state)

    def test_new_project_cancel_does_not_create_database(self):
        before = self.database_bytes()
        app = self.click(self.launch(), 'New project')
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        app = self.click(app, 'Cancel database switch')
        self.assertEqual(self.database_bytes(), before)

    def test_switch_loads_selected_file_again_without_rewriting_other_files(self):
        app = self.select(self.launch(), self.original_filename)
        app = self.click(app, 'Load selected project')
        updated = copy.deepcopy(self.original)
        updated['brief'] = 'Saved by another session after selecting'
        engine.save_project(updated)
        app = self.confirm(app)
        self.assertEqual(app.session_state.project['brief'], updated['brief'])
        self.assertEqual(app.session_state.project['domain_pricing'], self.original['domain_pricing'])

    def test_confirmation_is_reset_after_cancel(self):
        app = self.click(self.select(self.launch(), self.original_filename), 'Load selected project')
        app = next(x for x in app.checkbox if x.label.startswith('I confirm switching')).check().run()
        app = self.click(app, 'Cancel database switch')
        app = self.click(app, 'Load selected project')
        self.assertFalse(next(x for x in app.checkbox if x.label.startswith('I confirm switching')).value)
        self.assertTrue(next(x for x in app.button if x.label == 'Confirm database switch').disabled)

    def test_new_project_settings_failure_can_retry_without_rewriting_created_database(self):
        before = self.database_bytes()
        app = self.click(self.launch(), 'New project')
        write = engine.write_json

        def fail_settings(path, value):
            if Path(path).name == 'settings.json':
                raise OSError('settings failed')
            return write(path, value)

        with patch('engine.write_json', side_effect=fail_settings):
            app = self.confirm(app)
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        pending = app.session_state.pending_project_switch
        self.assertTrue(pending['created'])
        new_path = self.projects / pending['filename']
        new_bytes = new_path.read_bytes()
        app = self.click(app, 'Confirm database switch')
        self.assertEqual(app.session_state.project_filename, new_path.name)
        self.assertEqual(new_path.read_bytes(), new_bytes)
        self.assertTrue(all((self.projects / name).read_bytes() == data for name, data in before.items()))

    def test_corrupt_settings_block_switch_without_losing_display_metadata(self):
        app = self.click(self.select(self.launch(), self.original_filename), 'Load selected project')
        settings = self.root / 'settings.json'
        settings.write_text('corrupt metadata', encoding='utf-8')
        app = self.confirm(app)
        self.assertEqual(app.session_state.project['id'], self.active['id'])
        self.assertEqual(settings.read_text(), 'corrupt metadata')

    def test_restored_new_destination_preserves_all_backup_evidence(self):
        before = self.database_bytes()
        app = self.launch()
        restored = copy.deepcopy(self.original)
        restored['id'] = engine.new_project()['id']
        filename = restored['id'] + '.json'
        app.session_state.pending_project_switch = {'project': restored, 'filename': filename, 'create': True}
        app = app.run(timeout=30)
        app = self.confirm(app)
        saved = engine.load_project(self.projects / filename)
        for field in ('names', 'reviews', 'runs', 'domain_pricing', 'domain_pricing_history'):
            self.assertEqual(saved[field], self.original[field])
        self.assertEqual(app.session_state.project_filename, filename)
        self.assertTrue(all((self.projects / name).read_bytes() == data for name, data in before.items()))
