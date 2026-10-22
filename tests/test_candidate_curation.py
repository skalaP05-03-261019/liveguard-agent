"""Synthetic TSV only; source data or derivatives must not be committed in tests."""
import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from app.candidate_detection.curation import (LABELS, BUCKETS, ReviewSet, normalized_hash,
    prepare, read_source, bind_source, check_pair, export_cases, main)
from app.candidate_detection.evaluate import load_cases


def write_source(path, prefix, *, extra=()):
    rows=[]
    for label in BUCKETS:
        for number in range(3):
            rows.append({'문장':f'Synthetic {prefix} {label} {number}',
                         **{key:int(key==label) for key in LABELS}})
    rows.extend(extra)
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,delimiter='\t',fieldnames=['문장',*LABELS])
        writer.writeheader();writer.writerows(rows)


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.train,self.valid=self.folder/'train.tsv',self.folder/'valid.tsv'
        write_source(self.train,'train');write_source(self.valid,'valid')
        self.kwargs={'revision':'a'*40,'seed':'test-seed','per_bucket':1}

    def pack(self): return prepare(self.train,self.valid,**self.kwargs)

    def approve(self,review):
        data=review.model_dump(mode='json',by_alias=True)
        for item in data['items']:
            # Controlled explicit annotation, deliberately independent of TSV labels.
            item.update(status='APPROVED',expectedCandidate=False,expectedKinds=[],
                        reviewer='synthetic-reviewer',reviewedAt='2026-10-22T10:00:00+09:00',
                        rationale='Synthetic service expectation for transport tests')
        return ReviewSet.model_validate(data)

    def test_selection_deterministic_stratified_and_pending(self):
        dev,hold,summary=self.pack()
        again=self.pack()
        self.assertEqual(dev,again[0]);self.assertEqual(hold,again[1])
        self.assertEqual(len(dev.items),10);self.assertEqual(len(hold.items),10)
        self.assertEqual(set(summary['development']['primaryBuckets'].values()),{1})
        self.assertTrue(all(item.status=='PENDING' and item.expected_candidate is None for item in dev.items))
        self.assertFalse(summary['humanReviewCompleted'])
        check_pair(dev,hold)

    def test_valid_excludes_every_train_text_not_only_selected_train(self):
        _,train_rows=read_source(self.train)
        # Include all train rows with case/width/spacing variation in valid.
        extra=[]
        for row in train_rows:
            extra.append({'문장':'  '+row['text'].upper()+'  ',**row['labels']})
        write_source(self.valid,'valid',extra=extra)
        dev,hold,summary=self.pack()
        self.assertEqual(summary['confirmation']['excludedNormalizedOverlap'],30)
        self.assertFalse({x.normalized_sha256 for x in hold.items}&{r['normalizedSha256'] for r in train_rows})
        self.assertEqual(normalized_hash('ＦＯＯ  bar'),normalized_hash('foo\tBAR'))

    def test_authored_development_exclusion_and_duplicate_detection(self):
        _,rows=read_source(self.train)
        first=rows[0]
        write_source(self.train,'train',extra=[{'문장':rows[1]['text'],**rows[1]['labels']}])
        fixture=load_cases(Path(__file__).parent/'fixtures/phase2/metrics-smoke-cases.json')[0]
        fixture.input.target.text=first['text']
        dev,hold,summary=prepare(self.train,self.valid,exclude_cases=[fixture],**self.kwargs)
        self.assertNotIn(first['normalizedSha256'],{x.normalized_sha256 for x in dev.items})
        self.assertEqual(summary['development']['excludedNormalizedOverlap'],1)
        self.assertEqual(summary['development']['withinSourceDuplicates'],1)

    def test_insufficient_strata_and_invalid_schema_or_labels_are_rejected(self):
        with self.assertRaises(ValueError):prepare(self.train,self.valid,**{**self.kwargs,'per_bucket':4})
        original=self.train.read_text()
        self.train.write_text(original.replace('문장','wrong',1))
        with self.assertRaises(ValueError):self.pack()
        self.train.write_text(original.replace('\t1\t','\t2\t',1))
        with self.assertRaises(ValueError):self.pack()

    def test_source_edit_or_changed_text_labels_id_and_flags_cannot_export(self):
        dev,hold,_=self.pack();dev=self.approve(dev)
        for field,value in [('text','edited content'),('originalLabels',{key:0 for key in LABELS}),
                            ('id','forged'),('normalizedSha256','0'*64),('sourceFlags',['FORGED'])]:
            raw=dev.model_dump(mode='json',by_alias=True);raw['items'][0][field]=value
            changed=ReviewSet.model_validate(raw)
            with self.assertRaises(ValueError):export_cases(changed,hold,source=self.train,peer_source=self.valid)
        self.train.write_text(self.train.read_text()+'\n')
        with self.assertRaises(ValueError):bind_source(dev,self.train)

    def test_pending_draft_and_disagreement_do_not_become_service_labels(self):
        dev,hold,_=self.pack()
        data=dev.model_dump(mode='json',by_alias=True)
        for item in data['items']:
            item['proposal']={'kind':'AI_DRAFT','expectedCandidate':True,'expectedKinds':['GROUP_HATE'],
                              'rationale':'Synthetic draft, not reviewer approval'}
        draft=ReviewSet.model_validate(data)
        with self.assertRaises(ValueError):export_cases(draft,hold,source=self.train,peer_source=self.valid)
        data=self.approve(dev).model_dump(mode='json',by_alias=True);data['items'][0]['status']='DISAGREEMENT'
        with self.assertRaises(ValueError):export_cases(ReviewSet.model_validate(data),hold,source=self.train,peer_source=self.valid)

    def test_approval_requires_explicit_expectation_and_review_record(self):
        dev,_,_=self.pack();data=dev.model_dump(mode='json',by_alias=True)
        data['items'][0]['status']='APPROVED'
        with self.assertRaises(ValidationError):ReviewSet.model_validate(data)
        approved=self.approve(dev).model_dump(mode='json',by_alias=True)
        approved['items'][0]['expectedKinds']=['GROUP_HATE']
        with self.assertRaises(ValidationError):ReviewSet.model_validate(approved)

    def test_normalized_and_manual_family_cross_split_leaks_are_rejected(self):
        dev,hold,_=self.pack()
        for field in ('normalizedSha256','groupId','id'):
            raw=hold.model_dump(mode='json',by_alias=True)
            raw['items'][0][field]=dev.model_dump(mode='json',by_alias=True)['items'][0][field]
            with self.assertRaises(ValueError):check_pair(dev,ReviewSet.model_validate(raw))
        with self.assertRaises(ValueError):check_pair(dev,dev)

    def test_reviewed_export_retains_explicit_labels_and_reports_exclusion(self):
        dev,hold,_=self.pack();data=self.approve(dev).model_dump(mode='json',by_alias=True)
        data['items'][0]['status']='EXCLUDED'
        cases,manifest=export_cases(ReviewSet.model_validate(data),hold,source=self.train,peer_source=self.valid)
        self.assertEqual(len(cases),9)
        self.assertTrue(all(not c.expected_candidate and not c.input.context for c in cases))
        self.assertEqual(manifest['excluded'],[dev.items[0].id])
        self.assertFalse(manifest['enforcementAuthorized'])

    def test_cli_creates_private_files_and_refuses_existing_directory(self):
        out=self.folder/'private-pack'
        argv=['curation','prepare','--train',str(self.train),'--valid',str(self.valid),
              '--revision','a'*40,'--seed','test','--per-bucket','1','--out-dir',str(out)]
        with patch('sys.argv',argv):self.assertEqual(main(),0)
        self.assertEqual(out.stat().st_mode&0o777,0o700)
        for path in out.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
        before=(out/'development-review.json').read_bytes()
        with patch('sys.argv',argv),self.assertRaises(SystemExit):main()
        self.assertEqual(before,(out/'development-review.json').read_bytes())

    def test_no_partial_output_on_unreviewed_export(self):
        dev,hold,_=self.pack()
        for name,value in [('dev.json',dev),('hold.json',hold)]:
            (self.folder/name).write_text(value.model_dump_json(by_alias=True))
        out=self.folder/'export'
        argv=['curation','export','--review',str(self.folder/'dev.json'),'--peer',str(self.folder/'hold.json'),
              '--source',str(self.train),'--peer-source',str(self.valid),'--out-dir',str(out)]
        with patch('sys.argv',argv),self.assertRaises(SystemExit):main()
        self.assertFalse(out.exists())
