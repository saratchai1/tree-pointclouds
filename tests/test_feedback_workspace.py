import copy
import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import unittest
import uuid
import numpy as np
from fastapi.testclient import TestClient
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import review_learning as rl
import feedback_cloud as cloud
from feedback_server import Store, Workspace, create_app


def write_las(path,version=2,lean=0.,radius=.06):
    rng=np.random.default_rng(4)
    ground=np.array([[x,y,0.] for x in np.arange(-1,1.01,.04) for y in np.arange(-1,1.01,.04)])
    tree=[]
    axis=np.array([lean,0.,1.]);axis/=np.linalg.norm(axis)
    u=np.array([axis[2],0.,-axis[0]]);v=np.array([0.,1.,0.])
    for h in np.arange(.5,4.21,.025):
        for a in np.linspace(0,2*math.pi,40,endpoint=False):
            tree.append(np.array([lean*h,0.,h])+radius*(math.cos(a)*u+math.sin(a)*v)+rng.normal(0,.0007,3))
    xyz=np.vstack([ground,tree]);size=375 if version==4 else 227;fmt=6 if version==4 else 0;length=30 if fmt==6 else 20
    h=bytearray(size);h[:4]=b'LASF';h[24:26]=bytes([1,version]);struct.pack_into('<H',h,94,size);struct.pack_into('<I',h,96,size)
    h[104]=fmt;struct.pack_into('<H',h,105,length);struct.pack_into('<I',h,107,0 if version==4 else len(xyz))
    if version==4:struct.pack_into('<Q',h,247,len(xyz))
    struct.pack_into('<ddd',h,131,.0001,.0001,.0001);struct.pack_into('<ddd',h,155,0,0,0)
    lo=xyz.min(0);hi=xyz.max(0);struct.pack_into('<dddddd',h,179,hi[0],lo[0],hi[1],lo[1],hi[2],lo[2])
    dtype=np.dtype({'names':['x','y','z'],'formats':['<i4']*3,'offsets':[0,4,8],'itemsize':length})
    raw=np.zeros(len(xyz),dtype=dtype)
    for i,n in enumerate(['x','y','z']):raw[n]=np.rint(xyz[:,i]/.0001).astype('<i4')
    path.write_bytes(h+raw.tobytes());return xyz


def row(i=1,site='A',positive=None):
    return dict(site_id=site,survey_id='survey',candidate_id=f'C-{i:04d}',evidence_hash=rl.digest([site,i]),
        group_id=f'{site}/{i}',feature_schema=rl.SCHEMA,features=[.5+i*.01]*len(rl.FEATURES),
        targets=dict(stem_identity=positive,measurement_validity=None),
        human_labels=dict(stem_identity='TRUE_MAIN_STEM' if positive==1 else ('BRANCH' if positive==0 else None),measurement_validity=None),
        baseline_stem_like=False,missing_feature_count=0,pipeline_version='test')

def body(sid,r,revision=0,label='TRUE_MAIN_STEM',task='stem_identity'):
    return dict(snapshot_id=sid,evidence_hash=r['evidence_hash'],revision=revision,event_id=str(uuid.uuid4()),label=label,task=task)

class CloudTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def test_las_12_and_14_chunk_count(self):
        for v in (2,4):
            p=self.root/f'{v}.las';xyz=write_las(p,version=v);m=cloud.header(p)
            arr=np.concatenate(list(cloud.chunks(p,m,chunk_size=173)))
            self.assertEqual(len(arr),len(xyz));np.testing.assert_allclose(arr+np.array(m['origin']),xyz,atol=.000051)
    def test_bad_magic_and_truncated_rejected(self):
        p=self.root/'bad.las';p.write_bytes(b'x'*300)
        with self.assertRaises(ValueError):cloud.header(p)
        write_las(p);p.write_bytes(p.read_bytes()[:-50])
        with self.assertRaises(ValueError):cloud.header(p)
    def test_zero_scale_rejected(self):
        p=self.root/'x.las';write_las(p);b=bytearray(p.read_bytes());struct.pack_into('<d',b,131,0);p.write_bytes(b)
        with self.assertRaises(ValueError):cloud.header(p)
    def test_unknown_units_rejected(self):
        with self.assertRaises(ValueError):cloud.process(self.root/'x.las',self.root/'out','a','s','degrees')
    def test_circle_fits_without_model_approval(self):
        rng=np.random.default_rng(2);theta=np.linspace(0,2*math.pi,300)
        xy=np.column_stack([.07*np.cos(theta),.07*np.sin(theta)])+rng.normal(0,.0003,(300,2))
        fit=cloud.circle(xy,rng);self.assertAlmostEqual(fit['radius'],.07,delta=.002)
        self.assertGreater(fit['coverage'],300)
    def test_full_resolution_new_job_not_118_frozen_trees(self):
        p=self.root/'new.las';write_las(p);before=p.read_bytes()
        data,display,report=cloud.process(p,self.root/'out','NEW','2026','metres')
        self.assertGreater(len(data['records']),0);self.assertLess(len(data['records']),10)
        best=min((d['measurement'] for d in display.values()),key=lambda m:abs(m['diameter_cm']-12))
        self.assertAlmostEqual(best['circumference_cm'],2*math.pi*.06*100,delta=2.)
        self.assertEqual(best['height_agl_m'],1.3);self.assertIsNotNone(best['dbh_cm'])
        self.assertFalse(best['field_verified']);self.assertEqual(p.read_bytes(),before)
        self.assertFalse(list((self.root/'out').glob('tube-*.bin')))
        self.assertIn('NEW_PREPROCESSING_NOT_INDEPENDENTLY_VALIDATED',report['warnings'])
    def test_leaning_cylinder_perpendicular_fit(self):
        p=self.root/'lean.las';write_las(p,lean=.10)
        meta=cloud.header(p);points=np.concatenate(list(cloud.chunks(p,meta)))
        s=cloud.screen_tube(points,points,np.array([.13,0.]),-meta['origin'][2])
        self.assertIsNotNone(s);self.assertAlmostEqual(s['measurement']['diameter_cm'],12,delta=1.)
        plane=s['measurement']['plane'];self.assertAlmostEqual(np.dot(plane['axis_direction'],plane['basis_u']),0,places=5)

class StoreTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite';self.store=Store(self.path);self.row=row();self.sid=self.store.add(self.row,dict(measurement={'radius_m':.1}),'test')
    def tearDown(self):self.tmp.cleanup()
    def test_persistent_after_reopen_and_no_duplicates(self):
        self.store.review(body(self.sid,self.row));again=Store(self.path)
        self.assertEqual(again.get(self.sid)['revision'],1);self.assertEqual(again.get(self.sid)['row']['targets']['stem_identity'],1)
        self.assertEqual(again.add(self.row,{},'test'),self.sid)
    def test_same_event_id_is_idempotent(self):
        request=body(self.sid,self.row);a=self.store.review(request);b=self.store.review(request)
        self.assertFalse(a['duplicate']);self.assertTrue(b['duplicate']);self.assertEqual(len(self.store.get(self.sid)['review_history']),1)
    def test_event_id_collision_and_revision_conflict_rejected(self):
        from fastapi import HTTPException
        request=body(self.sid,self.row);self.store.review(request)
        request['label']='BRANCH'
        with self.assertRaises(HTTPException) as caught:self.store.review(request)
        self.assertEqual(caught.exception.status_code,409)
        with self.assertRaises(HTTPException):self.store.review(body(self.sid,self.row))
    def test_stale_evidence_is_rejected(self):
        from fastapi import HTTPException
        request=body(self.sid,self.row);request['evidence_hash']='other'
        with self.assertRaises(HTTPException):self.store.review(request)
        self.assertEqual(self.store.get(self.sid)['revision'],0)
    def test_unknown_is_not_negative(self):
        self.store.review(body(self.sid,self.row,label='NOT_ENOUGH_INFORMATION'))
        self.assertIsNone(self.store.get(self.sid)['row']['targets']['stem_identity'])
    def test_identity_is_not_measurement_approval(self):
        self.store.review(body(self.sid,self.row))
        self.assertIsNone(self.store.get(self.sid)['row']['targets']['measurement_validity'])
    def test_validity_has_separate_target(self):
        self.store.review(body(self.sid,self.row,task='measurement_validity',label='MEASUREMENT_INCORRECT'))
        r=self.store.get(self.sid)['row'];self.assertEqual(r['targets']['measurement_validity'],0);self.assertIsNone(r['targets']['stem_identity'])
    def test_missing_ring_cannot_be_validated(self):
        from fastapi import HTTPException
        r=row(2);sid=self.store.add(r,dict(measurement=None),'test')
        with self.assertRaises(HTTPException):self.store.review(body(sid,r,task='measurement_validity',label='MEASUREMENT_CORRECT'))
    def test_site_grouping_prevents_cross_survey_leakage(self):
        r=row(2,positive=1);r['survey_id']='later';self.store.add(r,{},'test')
        data,_=self.store.dataset();self.assertEqual(len({r['group_id'] for r in data['records']}),1)
    def test_same_source_cannot_cross_folds_under_different_site_names(self):
        a=row(2,site='A',positive=1);b=row(3,site='B',positive=0)
        a['source_las_sha256']=b['source_las_sha256']='identical-source'
        self.store.add(a,{},'test');self.store.add(b,{},'test')
        data,_=self.store.dataset();self.assertEqual(len({r['group_id'] for r in data['records']}),1)
    def test_original_decisions_and_changes_survive(self):
        self.store.review(body(self.sid,self.row));self.store.review(body(self.sid,self.row,revision=1,label='BRANCH'))
        value=self.store.get(self.sid);self.assertEqual(len(value['review_history']),2);self.assertEqual(value['row']['targets']['stem_identity'],0)

class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'site/public').mkdir(parents=True)
        self.app=create_app(self.root,self.root/'state',auto_train=False);self.ws=self.app.state.workspace
        self.ctx=TestClient(self.app);self.client=self.ctx.__enter__();self.token=self.client.get('/api/session').json()['token'];self.headers={'X-Workspace-Token':self.token}
        self.row=row();self.sid=self.ws.store.add(self.row,dict(measurement={'radius_m':.1}),'test')
    def tearDown(self):self.ctx.__exit__(None,None,None);self.tmp.cleanup()
    def test_writes_require_token(self):
        self.assertEqual(self.client.post('/api/reviews',json=body(self.sid,self.row)).status_code,403)
    def test_cross_origin_and_dns_rebinding_blocked(self):
        self.assertEqual(self.client.get('/api/session',headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/session',headers={'Host':'evil.example'}).status_code,403)
    def test_static_cannot_read_private_db(self):
        self.assertEqual(self.client.get('/state/feedback.sqlite3').status_code,404)
        self.assertEqual(self.client.get('/%2e%2e/state/feedback.sqlite3').status_code,404)
    def test_json_invalid_nan_no_side_effect(self):
        r=self.client.post('/api/reviews',content='{"label":NaN}',headers=self.headers)
        self.assertEqual(r.status_code,422);self.assertEqual(self.ws.store.get(self.sid)['revision'],0)
    def test_wrong_json_shapes_are_validation_errors_not_server_errors(self):
        for change in ({'task':[]},{'label':{}},{'snapshot_id':[]}):
            request=body(self.sid,self.row);request.update(change)
            self.assertEqual(self.client.post('/api/reviews',json=request,headers=self.headers).status_code,422)
    def test_http_review_get_export(self):
        response=self.client.post('/api/reviews',json=body(self.sid,self.row),headers=self.headers)
        self.assertTrue(response.json()['persisted']);self.assertEqual(self.client.get('/api/snapshots/'+self.sid).json()['revision'],1)
        self.assertEqual(len(self.client.get('/api/export').json()['events']),1)
    def test_incomplete_upload_no_job(self):
        upload=self.client.post('/api/uploads',json=dict(filename='x.las',size=400,site_id='A',survey_id='s',units='metres'),headers=self.headers).json()
        self.assertEqual(self.client.post('/api/uploads/'+upload['id']+'/complete',json={},headers=self.headers).status_code,409)
        self.assertEqual(self.client.get('/api/jobs').json(),[])
    def test_chunk_offset_and_filename_path_safety(self):
        u=self.client.post('/api/uploads',json=dict(filename='../../x.las',size=300,site_id='A',survey_id='s',units='metres'),headers=self.headers).json()
        path='/api/uploads/'+u['id'];self.assertEqual(self.client.put(path+'?offset=0',content=b'x'*100,headers=self.headers).status_code,200)
        self.assertEqual(self.client.put(path+'?offset=0',content=b'x'*100,headers=self.headers).status_code,409)
        self.assertEqual(self.client.get(path).json()['offset'],100)
        self.assertFalse((self.root/'x.las').exists())
    def test_two_instances_cannot_share_runtime(self):
        second=Workspace(self.root,self.root/'state')
        with self.assertRaises(RuntimeError):second.start()
        second.close()
    def test_real_upload_through_scoring_and_csv(self):
        p=self.root/'test.las';write_las(p);raw=p.read_bytes()
        u=self.client.post('/api/uploads',json=dict(filename=p.name,size=len(raw),site_id='NEW',survey_id='s',units='metres'),headers=self.headers).json()
        self.assertEqual(self.client.put('/api/uploads/'+u['id']+'?offset=0',content=raw,headers=self.headers).status_code,200)
        r=self.client.post('/api/uploads/'+u['id']+'/complete',json={},headers=self.headers);self.assertEqual(r.status_code,200,r.text)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            job=self.client.get('/api/jobs').json()[0]
            if job['status'] in ('COMPLETE','FAILED'):break
            time.sleep(.05)
        self.assertEqual(job['status'],'COMPLETE',job)
        self.assertGreater(job['payload']['report']['candidate_count'],0)
        self.assertEqual(self.client.get('/api/jobs/'+u['id']+'/measurements.csv').status_code,200)
        self.assertEqual(self.client.post('/api/uploads/'+u['id']+'/complete',json={},headers=self.headers).status_code,200)
    def test_v31_exact_display_snapshot_and_stale_rejection(self):
        base=self.root/'site/public/viewer-v3-full-las/data';base.mkdir(parents=True)
        rec={'tree_id':'TREE_0017','selected_candidate':{'radius_m':.1,'height_agl_m':1.3},'measurement_plane':{'center_xyz':[0,0,1.3]},'circumference_cm':62.83}
        evidence={'tube_sample_xyz':[[0,0,1.3]],'focus_height_agl_m':1.3}
        rl.atomic_write(base/'measurements.json',dict(algorithm_version='V3.1',records=[rec]))
        rl.atomic_write(base/'evidence-index.json',dict(trees={'TREE_0017':'evidence-000.json'}))
        rl.atomic_write(base/'evidence-000.json',dict(evidence={'TREE_0017':evidence}))
        body_={'tree_id':'TREE_0017','record':rec,'evidence':evidence}
        r=self.client.post('/api/v31/snapshot',json=body_,headers=self.headers);self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['row']['candidate_id'],'TREE_0017')
        body_['record']={**rec,'circumference_cm':99}
        self.assertEqual(self.client.post('/api/v31/snapshot',json=body_,headers=self.headers).status_code,409)
    def test_retrain_preserves_legacy_and_task_boundaries(self):
        for i in range(2,8):self.ws.store.add(row(i,positive=i%2),dict(measurement={'radius_m':.1}),'old')
        self.ws.store.review(body(self.sid,self.row))
        trained=self.ws.train();self.assertEqual(trained['status'],'COMPLETE',trained)
        tasks=trained['report']['tasks'];self.assertEqual(tasks['stem_identity']['labelled_rows'],7)
        self.assertEqual(tasks['measurement_validity']['labelled_rows'],0)
        self.assertEqual(tasks['stem_identity']['evaluated_rows'],0) # one site => no invented test accuracy
        self.assertIn('stem_identity',self.ws.models())
        model_id=self.ws.models()['stem_identity']['model_id']
        self.ws.store.review(body(self.sid,self.row,revision=1,label='BRANCH'));self.ws.train()
        self.assertNotEqual(self.ws.models()['stem_identity']['model_id'],model_id)
        self.assertEqual(self.ws.models()['stem_identity']['usage_mode'],'ADVISORY_ONLY')

if __name__=='__main__':unittest.main()
