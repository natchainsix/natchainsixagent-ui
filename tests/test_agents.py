import unittest
from pathlib import Path
from agents import demo_review, validate
ROOT=Path(__file__).resolve().parents[1]
class Tests(unittest.TestCase):
    def files(self,name):
        return [{'path':p.name,'content':p.read_text()} for p in (ROOT/'demo'/name).glob('*.py')]
    def test_demo(self):
        self.assertEqual(len(demo_review(self.files('quicklend'))),6)
        self.assertEqual(demo_review(self.files('safelend')),[])
    def test_invalid(self):
        with self.assertRaises(ValueError): validate({'findings':[{'status':'INVALID'}]},[], 'test')
