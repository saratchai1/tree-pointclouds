#!/usr/bin/env python3
"""Bounded, local LAS screening -> immutable review candidates.

This is a new-survey screening adapter, not a re-run of the frozen V3.1 study.
Full-resolution points feed measurements; the bounded sample only finds seeds
and supplies display evidence. Existing learned scores are advisory transfer.
"""
from __future__ import annotations
import hashlib
import math
from pathlib import Path
import struct
from typing import Callable
import numpy as np
from scipy.ndimage import maximum_filter
from scipy.spatial import cKDTree
import review_learning as learning

VERSION = 'new-survey-screening-v0.1'
MIN_RECORD = (20, 28, 26, 34, 57, 63, 30, 36, 38, 59, 67)
SAMPLE_LIMIT = 200_000
MAX_SEEDS = 128
TUBE_LIMIT = 2_000_000


def header(path: Path) -> dict:
    """Validate LAS 1.0-1.4 layout; never interpret compressed bytes as XYZ."""
    size = path.stat().st_size
    with path.open('rb') as f:
        b = f.read(375)
    if len(b) < 227 or b[:4] != b'LASF':
        raise ValueError('ไฟล์นี้ไม่ใช่ LAS ที่รองรับ')
    version = (b[24], b[25])
    if version[0] != 1 or version[1] > 4:
        raise ValueError('รองรับ LAS 1.0–1.4 เท่านั้น')
    hsize = struct.unpack_from('<H', b, 94)[0]
    offset = struct.unpack_from('<I', b, 96)[0]
    fmt, compressed = b[104] & 63, bool(b[104] & 128)
    length = struct.unpack_from('<H', b, 105)[0]
    count = struct.unpack_from('<I', b, 107)[0]
    if version >= (1, 4):
        if len(b) < 375 or hsize < 375:
            raise ValueError('LAS 1.4 header ไม่ครบ')
        extended = struct.unpack_from('<Q', b, 247)[0]
        if extended:
            count = extended
    if fmt >= len(MIN_RECORD) or length < MIN_RECORD[fmt]:
        raise ValueError('LAS point format หรือ record length ไม่ถูกต้อง')
    if hsize < (235 if version == (1,3) else 227) or offset < hsize or offset > size or count < 1:
        raise ValueError('LAS header/count/offset ไม่ถูกต้อง')
    if not compressed and offset + count * length > size:
        raise ValueError('ไฟล์ LAS ไม่ครบตามจำนวนจุดใน header')
    scale = np.array(struct.unpack_from('<ddd', b, 131))
    translation = np.array(struct.unpack_from('<ddd', b, 155))
    bounds = struct.unpack_from('<dddddd', b, 179)
    if not np.isfinite([*scale, *translation, *bounds]).all() or np.any(scale <= 0):
        raise ValueError('LAS มี scale/พิกัดไม่ถูกต้อง')
    origin = np.array([(bounds[0]+bounds[1])/2, (bounds[2]+bounds[3])/2, bounds[5]])
    if bounds[0] <= bounds[1] or bounds[2] <= bounds[3] or bounds[4] <= bounds[5]:
        raise ValueError('LAS bounds ไม่ถูกต้อง')
    if max(bounds[0]-bounds[1], bounds[2]-bounds[3]) > 500:
        raise ValueError('รุ่นทดลองรองรับพื้นที่กว้างไม่เกิน 500 เมตรต่อไฟล์ กรุณาตัดเป็นแปลงย่อย')
    return dict(version=version, point_format=fmt, compressed=compressed,
                record_length=length, count=count, offset=offset,
                scale=scale.tolist(), translation=translation.tolist(),
                origin=origin.tolist(), size_bytes=size)


def chunks(path: Path, meta: dict, chunk_size: int = 250_000):
    origin = np.array(meta['origin'])
    if meta['compressed']:
        try:
            import laspy
        except ImportError as e:
            raise ValueError('LAZ ต้องติดตั้ง laspy[lazrs]; หรือ export เป็น LAS ก่อน') from e
        with laspy.open(path) as reader:
            for batch in reader.chunk_iterator(chunk_size):
                xyz = np.column_stack((batch.x, batch.y, batch.z)) - origin
                if not np.isfinite(xyz).all():
                    raise ValueError('Point cloud มีพิกัดไม่จำกัด')
                yield xyz
        return
    dtype = np.dtype({'names':['x','y','z'], 'formats':['<i4']*3,
                      'offsets':[0,4,8], 'itemsize':meta['record_length']})
    scale, offset = np.array(meta['scale']), np.array(meta['translation'])
    with path.open('rb') as f:
        f.seek(meta['offset'])
        remaining = meta['count']
        while remaining:
            n = min(chunk_size, remaining)
            data = f.read(n * meta['record_length'])
            if len(data) != n * meta['record_length']:
                raise ValueError('LAS ถูกตัดขณะอ่าน')
            batch = np.frombuffer(data, dtype=dtype)
            xyz = np.column_stack([batch[name] for name in ('x','y','z')]) * scale + offset - origin
            if not np.isfinite(xyz).all():
                raise ValueError('Point cloud มีพิกัดไม่จำกัด')
            yield xyz
            remaining -= n


def sample_cloud(path: Path, meta: dict, limit: int = SAMPLE_LIMIT):
    # Global stride, not chunk-dependent sampling; bounded by limit+1.
    stride = max(1, math.ceil(meta['count']/limit))
    result, seen = [], 0
    for xyz in chunks(path, meta):
        first = (-seen) % stride
        result.append(xyz[first::stride])
        seen += len(xyz)
    return np.concatenate(result), stride


def ground_surface(points: np.ndarray):
    """Local low quantile; explicit inferred-ground warning on every output."""
    cell = np.floor(points[:, :2]).astype(np.int64)
    cells, inverse = np.unique(cell, axis=0, return_inverse=True)
    order = np.argsort(inverse)
    cuts = np.r_[0, np.flatnonzero(np.diff(inverse[order]))+1, len(order)]
    values = np.array([np.quantile(points[order[a:b],2], .08)
                       for a,b in zip(cuts[:-1], cuts[1:])])
    tree = cKDTree(cells + .5)
    def at(xy):
        _, indices = tree.query(np.asarray(xy))
        return values[indices]
    return at


def find_seeds(points: np.ndarray, ground, maximum: int = MAX_SEEDS):
    hag = points[:,2] - ground(points[:,:2])
    xy0 = points[:,:2].min(axis=0)
    extent = points[:,:2].max(axis=0) - xy0
    cell = .1
    shape = np.ceil(extent/cell).astype(int)+2
    if int(shape[0])*int(shape[1]) > 5_000_000:
        raise ValueError('พื้นที่ sparse ใหญ่เกิน screening budget; แบ่งพื้นที่ก่อน')
    votes = np.zeros((shape[1],shape[0]),dtype=np.int32)
    for height in (.8,1.3,1.8,2.3,2.8):
        selected = points[np.abs(hag-height) <= .10,:2]
        if not len(selected):
            continue
        indices = np.floor((selected-xy0)/cell).astype(int)
        density = np.zeros_like(votes)
        np.add.at(density, (indices[:,1],indices[:,0]), 1)
        votes += np.minimum(density, 12)
    threshold = max(6, float(np.percentile(votes[votes>0],65))) if np.any(votes) else 6
    ys,xs = np.where((votes >= threshold) & (votes == maximum_filter(votes,size=5)))
    candidates = sorted(zip(xs,ys),key=lambda p:(-int(votes[p[1],p[0]]),p[0],p[1]))
    seeds = []
    for x,y in candidates:
        p = xy0 + (np.array([x,y])+.5)*cell
        if any(np.linalg.norm(p-q) < .4 for q in seeds):
            continue
        seeds.append(p)
        if len(seeds) > maximum:
            raise ValueError(f'พบ candidate เกิน {maximum}; แบ่งพื้นที่ก่อน ไม่ตัดทิ้งเงียบ ๆ')
    return seeds


def circle(xy: np.ndarray, rng, max_radius: float=.50):
    if len(xy)<20:
        return None
    fit = xy[::max(1,math.ceil(len(xy)/1800))]
    best = None
    for _ in range(96):
        a,b,c = fit[rng.choice(len(fit),3,replace=False)]
        matrix = 2*np.array([b-a,c-a])
        if abs(np.linalg.det(matrix))<1e-10:
            continue
        center = np.linalg.solve(matrix,np.array([b@b-a@a,c@c-a@a]))
        radius = float(np.linalg.norm(a-center))
        if not .02 <= radius <= max_radius or np.linalg.norm(center)>.40:
            continue
        errors = np.abs(np.linalg.norm(fit-center,axis=1)-radius)
        inside = errors <= max(.005,min(.02,radius*.09))
        score = int(inside.sum())
        if best is None or score > best[0]:
            best = (score,center,radius)
    if best is None:
        return None
    _,center,radius = best
    for _ in range(2):
        error = np.abs(np.linalg.norm(xy-center,axis=1)-radius)
        inside = error <= max(.005,min(.02,radius*.09))
        if inside.sum()<20:
            return None
        p = xy[inside]
        a,b,c = np.linalg.lstsq(np.column_stack([2*p,np.ones(len(p))]),np.sum(p*p,axis=1),rcond=None)[0]
        center = np.array([a,b]); r2=c+a*a+b*b
        if r2<=0:
            return None
        radius = math.sqrt(r2)
    if not .02<=radius<=max_radius or np.linalg.norm(center)>.40:
        return None
    errors = np.linalg.norm(xy-center,axis=1)-radius
    inside = np.abs(errors)<=max(.005,min(.02,radius*.09))
    if inside.sum()<20:
        return None
    theta = np.arctan2(*(xy[inside]-center)[:,::-1].T)
    bins = np.unique(np.clip(((theta+math.pi)/(2*math.pi)*36).astype(int),0,35))
    return dict(center=center, radius=radius, inliers=inside,
                coverage=float(len(bins)*10), rmse=float(np.sqrt(np.mean(errors[inside]**2))))


def screen_tube(points: np.ndarray, sampled: np.ndarray, seed: np.ndarray, ground: float):
    rng = np.random.default_rng(1729)
    observations = []
    for h in np.arange(.9,4.11,.2):
        slab = points[np.abs(points[:,2]-ground-h)<=.075]
        result = circle(slab[:,:2]-seed,rng)
        if result:
            observations.append((float(h),result))
    if len(observations)<4:
        return None
    zs = np.array([h for h,_ in observations]); centers=np.array([f['center'] for _,f in observations])
    design = np.column_stack([zs,np.ones(len(zs))])
    coef = np.linalg.lstsq(design,centers,rcond=None)[0]
    residual = np.linalg.norm(design@coef-centers,axis=1)
    axis_error = float(np.percentile(residual,90))
    axis=np.array([coef[0,0],coef[0,1],1.]);axis/=np.linalg.norm(axis)
    u=np.cross(axis,np.array([0.,1.,0.]));u/=np.linalg.norm(u);v=np.cross(axis,u)
    candidates=[]
    for h in np.round(np.arange(1.3,4.01,.1),2):
        center=np.r_[seed+np.array([h,1])@coef,ground+h]
        q=points-center; slab=q[np.abs(q@axis)<=.05]
        xy=np.column_stack([slab@u,slab@v]); fit=circle(xy,rng)
        if fit is None:
            continue
        measured_center=center+u*fit['center'][0]+v*fit['center'][1]
        candidates.append(dict(h=float(h),fit=fit,center=measured_center,axis=axis,u=u,v=v,xy=xy))
    if not candidates:
        return None
    for row in candidates:
        window=sorted(candidates,key=lambda q:abs(q['h']-row['h']))[:3]
        neighbours=[q['fit']['radius'] for q in window if abs(q['h']-row['h'])<=.201]
        r=row['fit']['radius'];mad=float(np.median(np.abs(np.array(neighbours)-np.median(neighbours))))
        row.update(mad=mad,neighbours=len(neighbours))
        row['ok']=(row['fit']['coverage']>=210 and row['fit']['inliers'].sum()>=40
                   and row['fit']['rmse']/r<=.12 and r<=.18
                   and len(neighbours)>=3 and mad/r<=.15 and axis_error<=.10)
    for row in candidates:
        if row['fit']['radius']>=.12 and any(q['ok'] and 0<q['h']-row['h']<=1.2 and q['fit']['radius']<.7*row['fit']['radius'] for q in candidates):
            row['ok']=False
    good=[r for r in candidates if r['ok']]
    focus=min(good,key=lambda r:r['h']) if good else max(candidates,key=lambda r:r['fit']['coverage'])
    fit=focus['fit'];h=focus['h'];r=fit['radius']
    sq=sampled-focus['center'];s= sq[np.abs(sq@axis)<=.05]
    sample_fit=circle(np.column_stack([s@u,s@v]),rng)
    full_metrics=dict(full_radius_m=r,full_angular_coverage_deg=fit['coverage'],
        full_fit_residual_m=fit['rmse'],full_centreline_residual_p90_m=axis_error,
        full_radius_residual_mad_m=focus['mad'],full_point_count=len(focus['xy']),
        full_accepted_point_count=int(fit['inliers'].sum()),full_valid_slice_count=focus['neighbours'])
    sampled_metrics={}
    if sample_fit:
        sampled_metrics=dict(sampled_radius_m=sample_fit['radius'],sampled_angular_coverage_deg=sample_fit['coverage'],
            sampled_fit_residual_m=sample_fit['rmse'],sampled_point_count=len(s))
    # Undefined components/track features stay missing; do not manufacture parity.
    plane=dict(center_xyz=focus['center'].tolist(),axis_direction=axis.tolist(),basis_u=u.tolist(),basis_v=v.tolist())
    display_points=points[::max(1,math.ceil(len(points)/1600))]
    return dict(sampled_metrics=sampled_metrics,full_metrics=full_metrics,
        measurement=dict(height_agl_m=h,diameter_cm=round(r*200,2),circumference_cm=round(2*math.pi*r*100,2),
            dbh_cm=round(r*200,2) if focus['ok'] and h==1.3 else None,
            status='AUTOMATIC_GEOMETRY_ESTIMATE' if focus['ok'] else 'REVIEW_CANDIDATE_ONLY',
            field_verified=False,protocol_final=False,plane=plane,radius_m=r,
            circumference_method='FITTED_CIRCLE',axis_error_m=axis_error,arc_coverage_deg=fit['coverage']),
        points=display_points.tolist(),profile=[dict(height_m=q['h'],radius_m=q['fit']['radius'],
            arc_coverage_deg=q['fit']['coverage'],geometry_pass=q['ok']) for q in candidates])


def process(path: Path, output: Path, site_id: str, survey_id: str,
            units: str, progress: Callable[[str],None] = lambda _:None):
    if units != 'metres':
        raise ValueError('ต้องยืนยันว่าพิกัด XYZ เป็นเมตรก่อนวัด')
    output.mkdir(parents=True,exist_ok=True)
    progress('ตรวจ LAS และ checksum')
    meta=header(path); hash_=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):hash_.update(block)
    checksum=hash_.hexdigest()
    progress('ค้นหาตำแหน่งจากตัวอย่างจุด')
    sampled,stride=sample_cloud(path,meta); ground=ground_surface(sampled)
    seeds=find_seeds(sampled,ground);grounds=[float(ground(p)) for p in seeds]
    files=[output/f'tube-{i:04d}.bin' for i in range(len(seeds))];counts=[0]*len(seeds)
    try:
        progress(f'ดึงจุดความละเอียดเต็มของ {len(seeds)} candidates')
        for xyz in chunks(path,meta):
            tree=cKDTree(xyz[:,:2])
            for i,(seed,g) in enumerate(zip(seeds,grounds)):
                ids=tree.query_ball_point(seed,1.)
                local=xyz[ids];local=local[(local[:,2]>=g-.25)&(local[:,2]<=g+4.4)]
                counts[i]+=len(local)
                if counts[i]>TUBE_LIMIT:
                    raise ValueError('จุดรอบ candidate มากเกิน memory budget; แบ่งพื้นที่หรือประมวลผลด้วย full pipeline')
                with files[i].open('ab') as f:local.astype('<f8').tofile(f)
        entries=[];displays={};skipped=[]
        for i,(seed,g) in enumerate(zip(seeds,grounds)):
            progress(f'วัดและเตรียมหลักฐาน {i+1}/{len(seeds)}')
            pts=np.fromfile(files[i],dtype='<f8').reshape(-1,3)
            near=sampled[np.linalg.norm(sampled[:,:2]-seed,axis=1)<=1.]
            result=screen_tube(pts,near,seed,g); cid=f'C-{i+1:04d}'
            if result is None:
                skipped.append(dict(candidate_id=cid,reason='INSUFFICIENT_STABLE_STEM_GEOMETRY',position=seed.tolist()))
                continue
            entry=dict(candidate_id=cid,item_type='CANDIDATE_EVIDENCE',position=dict(x=float(seed[0]),y=float(seed[1])),
                ground_z_m=g,sampled_metrics=result['sampled_metrics'],full_metrics=result['full_metrics'],
                candidate_geometry_status='UNVERIFIED_NEW_SURVEY',measurement=result['measurement'],
                source_las_sha256=checksum,preprocessing_version=VERSION,
                display_evidence_hash=learning.digest(dict(points=result['points'],profile=result['profile'])))
            entries.append(entry);displays[cid]=dict(points=result['points'],measurement=result['measurement'],profile=result['profile'])
        queue=dict(algorithm_version=VERSION,entries=entries,locked_input_hashes=dict(source_las=checksum))
        data=learning.make_dataset(queue,[],site_id,survey_id)
        for row in data['records']:row['source_las_sha256']=checksum
        warnings=['INFERRED_GROUND_NOT_FIELD_CHECKED','USER_DECLARED_METRE_COORDINATES',
                  'NEW_PREPROCESSING_NOT_INDEPENDENTLY_VALIDATED','MODEL_IS_ADVISORY_TRANSFER',
                  'NO_TREE_DETECTION_RECALL_ESTABLISHED']
        learning.atomic_write(output/'candidate-queue.json',queue)
        learning.atomic_write(output/'dataset.json',data)
        learning.atomic_write(output/'display.json',displays)
        report=dict(pipeline=VERSION,source_sha256=checksum,source_filename=path.name,
            meta=meta,sampling_stride=stride,seed_count=len(seeds),candidate_count=len(entries),
            skipped=skipped,warnings=warnings,field_verified=False,protocol_final=False)
        learning.atomic_write(output/'job-report.json',report)
        return data,displays,report
    finally:
        for file in files:file.unlink(missing_ok=True)
