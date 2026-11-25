import os
import unittest
from pathlib import Path
from unittest import mock
from fastapi import HTTPException
from agents import demo_review, validate
from app import _backend_report_to_scan, _match_backend_target, _validated_backend_url

ROOT=Path(__file__).resolve().parents[1]
class Tests(unittest.TestCase):
    def files(self,name):
        return [{'path':p.name,'content':p.read_text()} for p in (ROOT/'demo'/name).glob('*.py')]
    def test_demo(self):
        self.assertEqual(len(demo_review(self.files('quicklend'))),6)
        self.assertEqual(demo_review(self.files('safelend')),[])
    def test_invalid(self):
        with self.assertRaises(ValueError): validate({'findings':[{'status':'INVALID'}]},[], 'test')
    def test_backend_url_validation(self):
        with mock.patch.dict(os.environ, {'COMPLIANCE_API_URL': 'http://127.0.0.1:8002'}):
            self.assertEqual(_validated_backend_url('/api/targets'), 'http://127.0.0.1:8002/api/targets')
        with mock.patch.dict(os.environ, {'COMPLIANCE_API_URL': 'http://example.com:8002'}):
            with self.assertRaises(HTTPException) as ctx:
                _validated_backend_url('/api/targets')
            self.assertEqual(ctx.exception.status_code, 400)
        with mock.patch.dict(os.environ, {'COMPLIANCE_API_URL': ''}):
            with self.assertRaises(HTTPException) as ctx:
                _validated_backend_url('/api/targets')
            self.assertEqual(ctx.exception.status_code, 503)
    def test_backend_report_mapping(self):
        sha, scan = _backend_report_to_scan({
            'report_id': 'REP-1234ABCD',
            'units_triaged_in': 2,
            'findings': [{
                'status': 'non_compliant',
                'severity': 'critical',
                'title': 'Short withdrawal period',
                'location': {'file_path': 'withdrawal.py', 'start_line': 5},
                'fca_citations': [{'reference': 'CONC 4.2.5R'}],
                'cca_citations': [{'reference': 'CCA 1974 s.66A'}],
                'evidence_excerpt': 'withdrawal_days = 7',
                'remediation_guidance': 'Use 14 calendar days.',
            }],
        })
        self.assertEqual(sha, 'REP-1234ABCD')
        self.assertEqual(len(scan['findings']), 1)
        self.assertEqual(scan['findings'][0]['severity'], 'CRITICAL')
        self.assertEqual(scan['findings'][0]['status'], 'NON_COMPLIANT')
        self.assertIn('CONC 4.2.5R', scan['findings'][0]['regulation'])
        self.assertTrue(_match_backend_target('https://github.com/natchainsix/safelend-co', 'natchainsix/safelend-co', {'url': 'https://github.com/natchainsix/safelend-co'}))
