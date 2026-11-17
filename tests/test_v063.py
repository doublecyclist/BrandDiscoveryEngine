import unittest
from unittest.mock import patch
import engine
from assessment import default_profile, profile_for


class Version063Tests(unittest.TestCase):
    def test_environment_secret_precedence(self):
        with patch.dict('engine.os.environ', {'SERPAPI_KEY': 'hosted-key'}):
            self.assertEqual(engine.stored_key('SerpAPI'), 'hosted-key')

    def test_stale_industry_only_match_not_counted(self):
        name = 'HarborGlyph'
        project = engine.new_project('Sample business project')
        project['names'] = [name]
        project['platform'] = 'Business / other'
        project['research_profile'] = default_profile('Business / other') | {'terms': 'banking, software'}
        old_profile = profile_for(project).copy()
        row = {'name': name, 'checked_at': '2026-11-11T00:00:00Z', 'profile': old_profile, 'targets': engine.default_targets(name),
               'registration': {'domain': 'harborglyph.com', 'status': 'Potentially available', 'message': 'test'},
               'checks': [{'source': src, 'status': 'Completed', 'matches':
                           ([{'title': name, 'description': 'banking software', 'url': 'https://harborglyph.example/'}]
                            if src == 'Industry search' else []), 'message': 'test'}
                          for src in ('Google', 'Google broad', 'Industry search')]}
        project['runs'] = [{'id': 'run', 'results': [row]}]
        self.assertEqual(engine.export_rows(project)[0]['Conflict assessment'], 'Strong conflict')
        project['research_profile']['terms'] = 'legal, accounting'
        result = engine.export_rows(project)[0]
        self.assertEqual(result['Conflict assessment'], 'Insufficient evidence')
        self.assertEqual(result['Overlap leads'], 0)
        self.assertIn('Industry terms changed', result['Incomplete checks'])


if __name__ == '__main__':
    unittest.main()
