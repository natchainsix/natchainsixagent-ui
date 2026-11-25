"""Revision-bound synthetic benchmark; never infer accuracy on unknown code."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REFERENCE = json.loads((ROOT / 'benchmark.json').read_text(encoding='utf-8'))


def fingerprint(files):
    payload = sorted((f['path'], f['content']) for f in files)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def evaluate(repo, files, skipped, findings, source):
    lines = {f['path']: f['content'].splitlines() for f in files}
    risks = [f for f in findings if f['status'] == 'NON_COMPLIANT']
    grounded = sum(f['evidence'].strip() in lines[f['file']][f['line_start']-1]
                   for f in findings if f['file'] in lines and 0 < f['line_start'] <= len(lines[f['file']]))
    result = {'accuracy': None, 'precision': None, 'recall': None, 'f1': None,
              'evidence_match': round(100*grounded/len(findings), 1) if findings else None,
              'reason': 'No exact labelled snapshot or incomplete coverage', 'source': source}
    ref = REFERENCE.get(repo)
    if skipped or not ref or fingerprint(files) != ref['fingerprint']:
        return result
    expected = set(ref['positive_paths']); controls = set(ref['controls'])
    actual = {f['file'] for f in risks}
    tp = len(expected & actual); fp = len(actual - expected); fn = len(expected - actual)
    tn = len(controls - expected - actual)
    def pct(a,b): return round(100*a/b,1) if b else None
    result.update(accuracy=pct(tp+tn,tp+tn+fp+fn), precision=pct(tp,tp+fp),
                  recall=pct(tp,tp+fn), f1=pct(2*tp,2*tp+fp+fn), tp=tp,fp=fp,fn=fn,tn=tn,
                  reason='Synthetic file-level reference benchmark; not legal accuracy')
    return result
