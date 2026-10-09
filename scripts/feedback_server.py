#!/usr/bin/env python3
"""Single-operator, loopback-only persistent review workspace.

SQLite is a local desktop database, never a Vercel filesystem/database claim.
All state stays outside the public static directory. No production deployment.
"""
from __future__ import annotations
import argparse
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlparse
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import review_learning as rl

ROOT=Path(__file__).resolve().parents[1]
MAX_BODY=2*1024*1024
CHUNK=8*1024*1024
MAX_UPLOAD=10*1024**3

def now():return datetime.now(timezone.utc).isoformat()
def json_text(value):return rl.canonical(value).decode('utf8')
def fail(code,message):raise HTTPException(code,message)
def identifier(value):
    if not isinstance(value,str) or not 1<=len(value)<=120 or any(ord(c)<32 for c in value):
        fail(422,'ชื่อแปลง/งานไม่ถูกต้อง')
    return value

class Store:
    def __init__(self,path:Path):
        self.path=path;path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, row TEXT NOT NULL, display TEXT NOT NULL,
                source TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                snapshot_id TEXT NOT NULL REFERENCES snapshots(id), revision INTEGER NOT NULL,
                payload TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(snapshot_id,revision));
            CREATE TABLE IF NOT EXISTS training(id TEXT PRIMARY KEY, status TEXT NOT NULL, through_seq INTEGER,
                report TEXT, error TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                progress TEXT, error TEXT, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY, offset INTEGER NOT NULL, size INTEGER NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=30)
        c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA synchronous=FULL')
        try:
            with c:yield c
        finally:c.close()
    def add(self,row,display,source):
        sid=rl.digest({k:row[k] for k in ('site_id','survey_id','candidate_id','evidence_hash')})
        with self.db() as c:
            c.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)',
                      (sid,json_text(row),json_text(display),source,now()))
        return sid
    def get(self,sid):
        with self.db() as c:
            r=c.execute('SELECT * FROM snapshots WHERE id=?',(sid,)).fetchone()
            events=c.execute('SELECT revision,payload FROM reviews WHERE snapshot_id=? ORDER BY seq',(sid,)).fetchall()
        if not r:fail(404,'ไม่พบหลักฐาน')
        row=json.loads(r['row']);event_list=[json.loads(e['payload']) for e in events]
        data={'records':[row]};rl.apply_feedback(data,event_list)
        return dict(id=sid,row=row,display=json.loads(r['display']),source=r['source'],
                    revision=events[-1]['revision'] if events else 0,review_history=event_list)
    def review(self,body):
        sid=body.get('snapshot_id');task=body.get('task');label=body.get('label')
        if not isinstance(task,str) or not isinstance(label,str) or task not in rl.LABELS or label not in {*rl.LABELS[task],'NOT_ENOUGH_INFORMATION'}:
            fail(422,'ชนิดคำตอบไม่ถูกต้อง')
        event_id=body.get('event_id');expected=body.get('revision');hash_=body.get('evidence_hash')
        if not isinstance(sid,str) or not isinstance(hash_,str):fail(422,'snapshot/evidence hash ไม่ถูกต้อง')
        try:uuid.UUID(event_id)
        except (ValueError,TypeError,AttributeError):fail(422,'ต้องมี event_id แบบ UUID')
        if type(expected) is not int or expected<0:fail(422,'revision ไม่ถูกต้อง')
        note=body.get('note','')
        if not isinstance(note,str) or len(note)>2000:fail(422,'หมายเหตุต้องไม่เกิน 2000 ตัวอักษร')
        signature=rl.digest({k:body.get(k) for k in ('snapshot_id','task','label','revision','evidence_hash','note')})
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT payload,revision FROM reviews WHERE event_id=?',(event_id,)).fetchone()
            if previous:
                event=json.loads(previous['payload'])
                if event['request_hash']!=signature:fail(409,'event_id ถูกใช้กับคำตอบอื่นแล้ว')
                return dict(event=event,revision=previous['revision'],duplicate=True)
            raw=c.execute('SELECT row,display FROM snapshots WHERE id=?',(sid,)).fetchone()
            if not raw:fail(404,'ไม่พบหลักฐานที่ตรวจ')
            row=json.loads(raw['row']);display=json.loads(raw['display'])
            if hash_!=row['evidence_hash']:fail(409,'หลักฐานเปลี่ยนแล้ว กรุณาโหลดวงปัจจุบันก่อน')
            current=c.execute('SELECT COALESCE(MAX(revision),0) FROM reviews WHERE snapshot_id=?',(sid,)).fetchone()[0]
            if current!=expected:fail(409,'มีคำตอบใหม่จากหน้าต่างอื่น กรุณาโหลดก่อนแก้')
            if task=='measurement_validity' and not display.get('measurement'):
                fail(422,'ไม่มีวงวัดในหลักฐานนี้ จึงรับรองตัวเลขไม่ได้')
            event={k:row[k] for k in ('site_id','survey_id','candidate_id','evidence_hash','feature_schema')}
            event.update(event_id=event_id,task=task,label=label,timestamp=now(),request_hash=signature,
                         note=note,reviewer='LOCAL_OPERATOR',field_verified=False)
            c.execute('INSERT INTO reviews(event_id,snapshot_id,revision,payload,created) VALUES(?,?,?,?,?)',
                      (event_id,sid,current+1,json_text(event),event['timestamp']))
        return dict(event=event,revision=current+1,duplicate=False)
    def dataset(self):
        with self.db() as c:
            rows=[json.loads(r['row']) for r in c.execute('SELECT row FROM snapshots ORDER BY created,id')]
            events=[json.loads(r['payload']) for r in c.execute('SELECT payload FROM reviews ORDER BY seq')]
            seq=c.execute('SELECT COALESCE(MAX(seq),0) FROM reviews').fetchone()[0]
        data=dict(records=rows,feature_schema=rl.SCHEMA,feature_names=list(rl.FEATURES),schema_version=1)
        rl.apply_feedback(data,events)
        # Entire sites stay together, including repeated surveys and V2/V3 snapshots.
        # One site => no independent held-out metric, not fabricated accuracy.
        parents={}
        def find(key):
            parents.setdefault(key,key)
            if parents[key]!=key:parents[key]=find(parents[key])
            return parents[key]
        def join(a,b):
            a,b=find(a),find(b);parents[max(a,b)]=min(a,b)
        for row in rows:
            site='site:'+row['site_id'];find(site)
            if row.get('source_las_sha256'):join(site,'source:'+row['source_las_sha256'])
        for row in rows:row['group_id']=find('site:'+row['site_id'])
        # Multiple snapshots must not amplify repeated stem-identity labels.
        seen=set()
        for row in reversed(rows):
            key=(row['site_id'],row['survey_id'],row['candidate_id'])
            if key in seen:row['targets']['stem_identity']=None
            elif row['targets']['stem_identity'] in (0,1):seen.add(key)
        return data,seq

class Workspace:
    def __init__(self,root:Path,state:Path,auto_train=True):
        self.root=root.resolve();self.state=state.resolve()
        public=(self.root/'site/public').resolve()
        if self.state==public or public in self.state.parents:
            raise ValueError('ห้ามเก็บฐานข้อมูลหรือไฟล์ดิบใน public directory')
        self.state.mkdir(parents=True,exist_ok=True)
        self.store=Store(self.state/'feedback.sqlite3');self.token=secrets.token_urlsafe(32)
        self.auto_train=auto_train;self.signal=threading.Event();self.stop=threading.Event()
        self.upload_lock=threading.Lock();self.training_lock=threading.Lock()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cloud-job')
        self.worker=None
    def bootstrap(self):
        queue=self.root/rl.DEFAULT_QUEUE;annotations=self.root/rl.DEFAULT_ANNOTATIONS
        if queue.is_file() and annotations.is_file():
            data=rl.load_dataset(self.root)
            entries={e.get('candidate_id'):e for e in rl.read_json(queue)['entries']}
            for row in data['records']:
                entry=entries[row['candidate_id']]
                self.store.add(row,dict(legacy_entry=entry,measurement=None,
                    viewer='/viewer-v2-review/'),source='legacy-pilot')
    def start(self):
        self.lock_file=(self.state/'.workspace.lock').open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                self.lock_file.seek(0);self.lock_file.write(b'0');self.lock_file.flush();self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e:
            self.lock_file.close()
            raise RuntimeError('Workspace นี้เปิดอยู่แล้ว ห้ามเปิดซ้ำด้วย data directory เดียวกัน') from e
        with self.store.db() as c:
            c.execute("UPDATE jobs SET status='INTERRUPTED',error='โปรแกรมหยุดก่อนงานเสร็จ ไฟล์ต้นฉบับยังอยู่' WHERE status IN ('QUEUED','RUNNING')")
            c.execute("UPDATE training SET status='INTERRUPTED' WHERE status='RUNNING'")
        self.bootstrap()
        self.worker=threading.Thread(target=self.training_loop,name='review-training',daemon=True);self.worker.start()
        with self.store.db() as c:
            n=c.execute('SELECT COALESCE(MAX(seq),0) FROM reviews').fetchone()[0]
            done=c.execute("SELECT COALESCE(MAX(through_seq),0) FROM training WHERE status='COMPLETE'").fetchone()[0]
        if self.auto_train and n>done:self.signal.set()
    def close(self):
        self.stop.set();self.signal.set()
        if self.worker:self.worker.join(timeout=60)
        self.executor.shutdown(wait=True,cancel_futures=True)
        if hasattr(self,'lock_file') and not self.lock_file.closed:self.lock_file.close()
    def models(self):
        result={}
        with self.store.db() as c:
            rows=c.execute("SELECT value FROM meta WHERE key='models'").fetchone()
        if rows:
            for task,path in json.loads(rows[0]).items():
                p=(self.state/path).resolve()
                if self.state not in p.parents:raise ValueError('Invalid model path')
                model=rl.read_json(p)
                if model['task']==task:result[task]=model
        if 'stem_identity' not in result:
            p=self.root/'models/review-learning/v1/model.json'
            if p.is_file():result['stem_identity']=rl.read_json(p)
        return result
    def suggestions(self,row):
        return {task:rl.suggest(model,row) for task,model in self.models().items()}
    def train(self):
        with self.training_lock:
            data,seq=self.store.dataset();tid=uuid.uuid4().hex;directory=self.state/'training'/tid
            with self.store.db() as c:c.execute('INSERT INTO training VALUES(?,?,?,?,?,?)',(tid,'RUNNING',seq,None,None,now()))
            try:
                report=rl.train_and_save(data,directory)
                report['validation_policy']='LEAVE_ONE_SITE_FAMILY_OUT; same-site and shared-source snapshots never cross folds'
                report['automatic_release_enabled']=False
                paths={}
                for task in rl.LABELS:
                    p=directory/(task+'.latest.json')
                    if p.is_file():paths[task]=str((directory/rl.read_json(p)['model_file']).relative_to(self.state))
                with self.store.db() as c:
                    c.execute('UPDATE training SET status=?,report=? WHERE id=?',('COMPLETE',json_text(report),tid))
                    c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('models',json_text(paths)))
                return dict(id=tid,status='COMPLETE',report=report)
            except Exception as e:
                with self.store.db() as c:c.execute('UPDATE training SET status=?,error=? WHERE id=?',('FAILED',str(e)[:500],tid))
                return dict(id=tid,status='FAILED',error=str(e)[:500])
    def training_loop(self):
        while not self.stop.is_set():
            if not self.signal.wait(.5):continue
            self.signal.clear()
            if self.stop.is_set():break
            self.train()
    def register_v31(self,body):
        base=self.root/'site/public/viewer-v3-full-las/data'
        if not (base/'measurements.json').is_file():fail(404,'ไม่พบชุดข้อมูล V3.1 บนเครื่องนี้')
        payload=rl.read_json(base/'measurements.json');cid=body.get('tree_id')
        record=next((r for r in payload['records'] if r['tree_id']==cid),None)
        if not record:fail(404,'ไม่พบ Tree ID')
        index=rl.read_json(base/'evidence-index.json')
        file=(base/index['trees'][cid]).resolve()
        if base.resolve() not in file.parents:fail(422,'Invalid evidence path')
        evidence=rl.read_json(file)['evidence'][cid]
        if body.get('record')!=record or body.get('evidence')!=evidence:
            fail(409,'วงหรือ point cloud ที่หน้าเว็บแสดงไม่ตรงกับ snapshot; โหลดหน้าใหม่ก่อนสอน')
        selected=record.get('selected_candidate') or record.get('best_review_candidate')
        full={}
        if selected:
            mapping={'radius_m':'radius_m','angular_coverage_deg':'angular_coverage_deg','fit_rmse_m':'fit_residual_m',
                'axis_uncertainty_m':'centreline_residual_p90_m','radius_stability_mad_m':'radius_residual_mad_m',
                'point_count':'point_count','inlier_count':'accepted_point_count','neighbouring_valid_slice_count':'valid_slice_count',
                'component_count':'connected_component_count'}
            full={f'full_{target}':selected.get(source) for source,target in mapping.items()}
        entry=dict(candidate_id=cid,full_metrics=full,position=record.get('location') or {},
                   track_source_height_count=(record.get('local_axis') or {}).get('supporting_slice_count'),
                   source_record=record,evidence_content_hash=rl.digest(evidence))
        queue=dict(algorithm_version=payload.get('algorithm_version','V3.1'),entries=[entry])
        data=rl.make_dataset(queue,[],rl.DEFAULT_SITE,rl.DEFAULT_SURVEY)
        source_hash=(((payload.get('source') or {}).get('source_las') or {}).get('sha256'))
        if source_hash:data['records'][0]['source_las_sha256']=source_hash
        measurement=None
        if selected:
            measurement=dict(height_agl_m=selected.get('height_agl_m'),radius_m=selected.get('radius_m'),
                plane=record.get('measurement_plane') or record.get('best_review_plane'),
                circumference_cm=record.get('circumference_cm'),candidate_circumference_cm=selected.get('circumference_cm'),
                status=record.get('status'),field_verified=False)
        display=dict(measurement=measurement,record=record,evidence=evidence,
                     viewer=f'/viewer-v3-full-las/?tree={cid}',adapter='V31_FULL_ONLY_TRANSFER')
        sid=self.store.add(data['records'][0],display,'v31')
        value=self.store.get(sid);value['suggestions']=self.suggestions(value['row'])
        return value
    def run_job(self,jid):
        with self.store.db() as c:
            r=c.execute('SELECT payload FROM jobs WHERE id=?',(jid,)).fetchone();payload=json.loads(r[0])
            c.execute("UPDATE jobs SET status='RUNNING' WHERE id=?",(jid,))
        def update(text):
            if self.stop.is_set():raise ValueError('โปรแกรมกำลังปิด งานนี้ยังไม่เสร็จ')
            with self.store.db() as c:c.execute('UPDATE jobs SET progress=? WHERE id=?',(text,jid))
        try:
            from feedback_cloud import process
            source=self.state/'uploads'/jid/'source.las'
            data,displays,report=process(source,self.state/'jobs'/jid,payload['site_id'],payload['survey_id'],payload['units'],update)
            ids=[self.store.add(row,displays[row['candidate_id']],source='job:'+jid) for row in data['records']]
            suggestions=[self.suggestions(row) for row in data['records']]
            report['source_filename']=payload['filename']
            payload.update(report=report,snapshots=ids,suggestions=suggestions)
            with self.store.db() as c:c.execute('UPDATE jobs SET status=?,payload=?,progress=? WHERE id=?',
                ('COMPLETE',json_text(payload),'เสร็จแล้ว: ผลเรขาคณิตยังไม่ใช่ field verification',jid))
        except Exception as e:
            with self.store.db() as c:c.execute('UPDATE jobs SET status=?,error=? WHERE id=?',('FAILED',str(e)[:700],jid))

async def small_json(request):
    raw=bytearray()
    async for piece in request.stream():
        raw.extend(piece)
        if len(raw)>MAX_BODY:fail(413,'คำขอใหญ่เกินกำหนด')
    try:
        value=json.loads(raw,parse_constant=lambda v:(_ for _ in ()).throw(ValueError(v)))
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,UnicodeError):fail(422,'JSON ไม่ถูกต้อง')


def create_app(root=ROOT,state=None,auto_train=True):
    root=Path(root).resolve();state=Path(state or root/'.feedback-workspace').resolve()
    ws=Workspace(root,state,auto_train)
    @asynccontextmanager
    async def lifespan(app):
        ws.start()
        try:yield
        finally:ws.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None);app.state.workspace=ws
    @app.middleware('http')
    async def local_only(request,call_next):
        host=request.headers.get('host','')
        hostname=urlparse('//'+host).hostname
        if hostname not in ('localhost','127.0.0.1','::1','testserver'):
            return JSONResponse({'detail':'Loopback workspace only'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin!=f'http://{host}':
            return JSONResponse({'detail':'Cross-origin requests denied'},status_code=403)
        if request.headers.get('sec-fetch-site')=='cross-site':
            return JSONResponse({'detail':'Cross-site requests denied'},status_code=403)
        if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('x-workspace-token',''),ws.token):
            return JSONResponse({'detail':'Invalid workspace session; reload page'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='SAMEORIGIN'
        return response
    @app.get('/api/session')
    def session():return dict(token=ws.token,mode='LOCAL_SINGLE_OPERATOR',automatic_release_enabled=False)
    @app.get('/api/status')
    def status():
        with ws.store.db() as c:
            review_count=c.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
            snapshots=c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]
            training=c.execute('SELECT * FROM training ORDER BY created DESC LIMIT 1').fetchone()
        t=dict(training) if training else None
        if t and t['report']:t['report']=json.loads(t['report'])
        return dict(reviews=review_count,snapshots=snapshots,training=t,models={k:v.get('model_id') for k,v in ws.models().items()},
                    persistence='LOCAL_SQLITE',auto_retrain=auto_train,automatic_release_enabled=False)
    @app.get('/api/snapshots')
    def snapshots():
        with ws.store.db() as c:rows=c.execute('SELECT id,row,source FROM snapshots ORDER BY created DESC,id').fetchall()
        return [dict(id=r['id'],source=r['source'],candidate_id=json.loads(r['row'])['candidate_id'],
                     site_id=json.loads(r['row'])['site_id']) for r in rows]
    @app.get('/api/snapshots/{sid}')
    def snapshot(sid:str):
        value=ws.store.get(sid);value['suggestions']=ws.suggestions(value['row']);return value
    @app.post('/api/v31/snapshot')
    async def v31(request:Request):return ws.register_v31(await small_json(request))
    @app.post('/api/reviews')
    async def review(request:Request):
        result=ws.store.review(await small_json(request))
        if auto_train and not result['duplicate']:ws.signal.set()
        return {**result,'persisted':True,'training':'QUEUED' if auto_train else 'MANUAL'}
    @app.post('/api/train')
    def train():ws.signal.set();return dict(status='QUEUED')
    @app.get('/api/export')
    def export():
        with ws.store.db() as c:events=[json.loads(r[0]) for r in c.execute('SELECT payload FROM reviews ORDER BY seq')]
        return JSONResponse(dict(schema_version=1,events=events),headers={'Content-Disposition':'attachment; filename="feedback-history.json"'})
    @app.post('/api/uploads')
    async def begin_upload(request:Request):
        body=await small_json(request);size=body.get('size');name=body.get('filename','')
        if type(size)is not int or not 227<=size<=MAX_UPLOAD:fail(422,'ไฟล์ต้องมีขนาด 227 bytes–10 GB')
        if not isinstance(name,str) or not name.lower().endswith(('.las','.laz')):fail(422,'เลือก LAS หรือ LAZ')
        if body.get('units')!='metres':fail(422,'ต้องยืนยันหน่วยเมตร')
        site=identifier(body.get('site_id'));survey=identifier(body.get('survey_id'))
        if shutil.disk_usage(ws.state).free<size*2+100*1024**2:fail(507,'พื้นที่ดิสก์ไม่พอสำหรับไฟล์และผลประมวลผล')
        jid=uuid.uuid4().hex;directory=ws.state/'uploads'/jid;directory.mkdir(parents=True)
        (directory/'source.part').touch()
        data=dict(site_id=site,survey_id=survey,units='metres',filename=Path(name.replace('\\','/')).name)
        with ws.store.db() as c:c.execute('INSERT INTO uploads VALUES(?,?,?,?,?)',(jid,0,size,json_text(data),'UPLOADING'))
        return dict(id=jid,offset=0,chunk_size=CHUNK)
    @app.put('/api/uploads/{jid}')
    async def upload_chunk(jid:str,request:Request,offset:int):
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>CHUNK:fail(413,'แต่ละ chunk ต้องไม่เกิน 8 MB')
        if not content:fail(422,'chunk ว่าง')
        with ws.upload_lock,ws.store.db() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM uploads WHERE id=?',(jid,)).fetchone()
            if not row:fail(404,'ไม่พบ upload')
            if row['status']!='UPLOADING' or row['offset']!=offset:fail(409,'offset เปลี่ยนแล้ว; โหลดสถานะก่อนส่งซ้ำ')
            if offset+len(content)>row['size']:fail(413,'ข้อมูลเกินขนาดที่แจ้ง')
            path=ws.state/'uploads'/jid/'source.part'
            with path.open('r+b') as f:
                f.seek(offset);f.write(content);f.truncate();f.flush();os.fsync(f.fileno())
            c.execute('UPDATE uploads SET offset=? WHERE id=?',(offset+len(content),jid))
        return dict(id=jid,offset=offset+len(content))
    @app.get('/api/uploads/{jid}')
    def upload_status(jid:str):
        with ws.store.db() as c:r=c.execute('SELECT id,offset,size,status FROM uploads WHERE id=?',(jid,)).fetchone()
        if not r:fail(404,'ไม่พบ upload')
        return dict(r)
    @app.post('/api/uploads/{jid}/complete')
    def complete(jid:str):
        with ws.upload_lock,ws.store.db() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM uploads WHERE id=?',(jid,)).fetchone()
            if not row:fail(404,'ไม่พบ upload')
            if row['status']=='COMPLETE':return dict(id=jid,status='QUEUED_OR_COMPLETE')
            if row['offset']!=row['size']:fail(409,'ไฟล์ยังส่งไม่ครบ')
            source=ws.state/'uploads'/jid/'source.part'
            if not source.exists():source=source.with_name('source.las')
            from feedback_cloud import header
            try:header(source)
            except ValueError as e:fail(422,str(e))
            if source.name!='source.las':source.rename(source.with_name('source.las'))
            c.execute("UPDATE uploads SET status='COMPLETE' WHERE id=?",(jid,))
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(jid,'QUEUED',row['payload'],'เข้าคิว',None,now()))
        ws.executor.submit(ws.run_job,jid)
        return dict(id=jid,status='QUEUED')
    @app.get('/api/jobs')
    def jobs():
        with ws.store.db() as c:rows=c.execute('SELECT * FROM jobs ORDER BY created DESC').fetchall()
        return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]
    @app.get('/api/jobs/{jid}/measurements.csv')
    def csv_export(jid:str):
        import csv,io
        with ws.store.db() as c:r=c.execute('SELECT payload FROM jobs WHERE id=?',(jid,)).fetchone()
        if not r:fail(404,'ไม่พบงาน')
        out=io.StringIO();w=csv.writer(out);w.writerow(['candidate_id','height_m','circumference_cm','diameter_cm','status','field_verified'])
        for sid in json.loads(r[0]).get('snapshots',[]):
            value=ws.store.get(sid);m=value['display'].get('measurement') or {}
            w.writerow([value['row']['candidate_id'],m.get('height_agl_m'),m.get('circumference_cm'),m.get('diameter_cm'),m.get('status'),False])
        return HTMLResponse('\ufeff'+out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="measurements.csv"'})
    @app.get('/{path:path}')
    def static(path:str):
        if not path:path='feedback-workspace/index.html'
        if path.endswith('/'):path+='index.html'
        public=(root/'site/public').resolve();p=(public/path).resolve()
        if public not in p.parents or not p.is_file():fail(404,'ไม่พบหน้าเว็บ')
        if path=='viewer-v3-full-las/index.html':
            html=p.read_text('utf8');html=html.replace('</body>','<script src="/feedback-workspace/bridge.js"></script></body>')
            return HTMLResponse(html)
        return FileResponse(p)
    return app


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--state-dir',type=Path);p.add_argument('--port',type=int,default=8095);p.add_argument('--no-browser',action='store_true')
    a=p.parse_args()
    if not 1024<=a.port<=65535:p.error('port ต้องอยู่ระหว่าง 1024–65535')
    app=create_app(a.root,a.state_dir)
    if not a.no_browser:threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{a.port}/')).start()
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=a.port,workers=1)
if __name__=='__main__':main()
