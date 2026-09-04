#!/usr/bin/env python3
import argparse, csv, json, math
from pathlib import Path

import laspy
import numpy as np
from scipy.ndimage import maximum_filter
from scipy.spatial import cKDTree

BANDS=[(0.20,0.55),(0.50,0.85),(0.80,1.15),(1.10,1.45),(1.40,1.75)]
GRID=0.08
MAX_CANDIDATES=3500
MIN_SEP=0.30
RNG=np.random.default_rng(20260904)

def circle3(a,b,c):
    d=2*(a[0]*(b[1]-c[1])+b[0]*(c[1]-a[1])+c[0]*(a[1]-b[1]))
    if abs(d)<1e-10:return None
    aa=np.dot(a,a);bb=np.dot(b,b);cc=np.dot(c,c)
    ctr=np.array([(aa*(b[1]-c[1])+bb*(c[1]-a[1])+cc*(a[1]-b[1]))/d,
                  (aa*(c[0]-b[0])+bb*(a[0]-c[0])+cc*(b[0]-a[0]))/d])
    return ctr,float(np.linalg.norm(a-ctr))

def coverage(xy,c,bins=24):
    a=np.arctan2(xy[:,1]-c[1],xy[:,0]-c[0])
    occ=np.unique(np.clip(((a+np.pi)/(2*np.pi)*bins).astype(int),0,bins-1))
    return len(occ)/bins

def fit_circle(xy,seed,rmin=0.006,rmax=0.15):
    if len(xy)<10:return None
    fit=xy if len(xy)<=2500 else xy[::math.ceil(len(xy)/2500)]
    best=None
    for _ in range(450):
        s=fit[RNG.choice(len(fit),3,replace=False)]
        z=circle3(*s)
        if z is None:continue
        c,r=z
        if not (rmin<=r<=rmax) or np.linalg.norm(c-seed)>0.16:continue
        tol=float(np.clip(r*0.18,0.003,0.012))
        e=np.abs(np.linalg.norm(fit-c,axis=1)-r)
        m=e<=tol
        if m.sum()<8:continue
        cov=coverage(fit[m],c)
        score=m.sum()*(0.25+cov)/(0.01+r)
        if best is None or score>best[0]:best=(score,c,r)
    if best is None:return None
    _,c,r=best
    for _ in range(3):
        tol=float(np.clip(r*0.18,0.003,0.012))
        dist=np.linalg.norm(xy-c,axis=1)
        m=np.abs(dist-r)<=tol
        if m.sum()<8:break
        pts=xy[m];o=pts.mean(0);q=pts-o;x=q[:,0];y=q[:,1]
        A=np.column_stack([x,y,np.ones(len(pts))]);b=-(x*x+y*y)
        try:a,b1,cc=np.linalg.lstsq(A,b,rcond=None)[0]
        except np.linalg.LinAlgError:break
        cl=np.array([-a/2,-b1/2]);rs=np.dot(cl,cl)-cc
        if rs<=0:break
        nc=cl+o;nr=float(np.sqrt(rs))
        if not (rmin<=nr<=rmax) or np.linalg.norm(nc-seed)>0.18:break
        c,r=nc,nr
    tol=float(np.clip(r*0.18,0.003,0.012))
    err=np.linalg.norm(xy-c,axis=1)-r;m=np.abs(err)<=tol
    if m.sum()<8:return None
    return dict(center=c,radius=r,inliers=int(m.sum()),coverage=float(coverage(xy[m],c)),rmse=float(np.sqrt(np.mean(err[m]**2))))

def estimate_ground(path):
    vals=[]
    with laspy.open(path) as f:
        for pts in f.chunk_iterator(2_000_000):
            z=np.asarray(pts.z)
            vals.append(z[::50].astype(np.float32))
    z=np.concatenate(vals)
    return float(np.percentile(z,2.5)), [float(v) for v in np.percentile(z,[0.5,2.5,5,50,95,99.5])]

def density_candidates(path,ground):
    with laspy.open(path) as f:
        xmin,ymin,_=f.header.mins;xmax,ymax,_=f.header.maxs
        nx=int(math.ceil((xmax-xmin)/GRID))+1;ny=int(math.ceil((ymax-ymin)/GRID))+1;n=nx*ny
        counts=np.zeros((len(BANDS),n),dtype=np.uint16)
        for pts in f.chunk_iterator(2_000_000):
            x=np.asarray(pts.x);y=np.asarray(pts.y);h=np.asarray(pts.z)-ground
            ix=np.clip(((x-xmin)/GRID).astype(np.int32),0,nx-1);iy=np.clip(((y-ymin)/GRID).astype(np.int32),0,ny-1)
            cell=iy*nx+ix
            for bi,(lo,hi) in enumerate(BANDS):
                m=(h>=lo)&(h<hi)
                if not m.any():continue
                u,c=np.unique(cell[m],return_counts=True)
                cur=counts[bi,u].astype(np.uint32)+c.astype(np.uint32)
                counts[bi,u]=np.minimum(cur,65535).astype(np.uint16)
        shaped=counts.reshape(len(BANDS),ny,nx)
        local=np.stack([maximum_filter(a,size=3,mode='constant') for a in shaped])
        score=np.min(local,axis=0)
        pos=score[score>0]
        if not len(pos):return np.empty((0,2)),dict(bounds=[xmin,xmax,ymin,ymax],grid=GRID,scoreThreshold=0)
        thr=max(2,float(np.percentile(pos,90)))
        mx=maximum_filter(score,size=5,mode='constant')
        yy,xx=np.where((score==mx)&(score>=thr))
        order=np.argsort(score[yy,xx])[::-1]
        seeds=[]
        tree=None
        for k in order:
            p=np.array([xmin+(xx[k]+0.5)*GRID,ymin+(yy[k]+0.5)*GRID])
            if seeds:
                if np.min(np.linalg.norm(np.asarray(seeds)-p,axis=1))<MIN_SEP:continue
            seeds.append(p)
            if len(seeds)>=MAX_CANDIDATES:break
        return np.asarray(seeds),dict(bounds=[float(xmin),float(xmax),float(ymin),float(ymax)],grid=GRID,scoreThreshold=float(thr),positiveCells=int(len(pos)))

def collect(path,seeds,ground):
    if len(seeds)==0:return []
    tree=cKDTree(seeds);parts=[[] for _ in range(len(seeds))]
    with laspy.open(path) as f:
        for pts in f.chunk_iterator(1_500_000):
            x=np.asarray(pts.x);y=np.asarray(pts.y);z=np.asarray(pts.z)
            m=(z>=ground-0.45)&(z<=ground+2.35)
            if not m.any():continue
            xyz=np.column_stack([x[m],y[m],z[m]])
            d,owner=tree.query(xyz[:,:2],k=1)
            keep=d<=0.24
            xyz=xyz[keep];owner=owner[keep]
            if not len(xyz):continue
            for i in np.unique(owner):
                a=xyz[owner==i]
                if len(a)>5000:a=a[::math.ceil(len(a)/5000)]
                parts[int(i)].append(a.astype(np.float32))
    out=[]
    for p in parts:
        if not p:out.append(np.empty((0,3),dtype=np.float32));continue
        a=np.concatenate(p)
        if len(a)>20000:a=a[::math.ceil(len(a)/20000)]
        out.append(a)
    return out

def evaluate(seed,pts,global_ground):
    if len(pts)<60:return None
    d=np.linalg.norm(pts[:,:2]-seed,axis=1)
    core=pts[d<=0.20]
    if len(core)<50:return None
    local_ground=float(np.percentile(core[:,2],2.5))
    local_ground=float(np.clip(local_ground,global_ground-0.30,global_ground+0.40))
    h=core[:,2]-local_ground
    col=core[(d[d<=0.20]<=0.14)&(h>=0.35)&(h<=1.65)]
    if len(col)<35 or np.ptp(col[:,2])<0.9:return None
    cov=np.cov(col.T);vals,vecs=np.linalg.eigh(cov);vertical=float(abs(vecs[2,np.argmax(vals)]))
    if vertical<0.65:return None
    fits=[]
    for hh in (0.60,0.90,1.20,1.30,1.50):
        m=(np.abs(h-hh)<=0.045)&(d[d<=0.20]<=0.18)
        xy=core[m,:2]
        f=fit_circle(xy,seed)
        if f is not None:f['height']=hh;fits.append(f)
    dbh=next((f for f in fits if abs(f['height']-1.30)<1e-6),None)
    if dbh is None or len(fits)<3:return None
    hs=np.array([f['height'] for f in fits]);cent=np.array([f['center'] for f in fits]);rad=np.array([f['radius'] for f in fits])
    A=np.column_stack([hs,np.ones(len(hs))]);cx=np.linalg.lstsq(A,cent[:,0],rcond=None)[0];cy=np.linalg.lstsq(A,cent[:,1],rcond=None)[0]
    pred=np.column_stack([A@cx,A@cy]);line=float(np.max(np.linalg.norm(cent-pred,axis=1)))
    rcv=float(np.std(rad)/max(np.mean(rad),1e-6))
    diam=dbh['radius']*200
    flags=[]
    if dbh['inliers']<16:flags.append('LIMITED_POINT_SUPPORT')
    if dbh['coverage']<0.30:flags.append('PARTIAL_COVERAGE')
    if line>0.075:flags.append('CENTERLINE_UNSTABLE')
    if rcv>0.38:flags.append('RADIUS_UNSTABLE')
    if diam>20:flags.append('LARGE_STRUCTURE_RISK')
    if diam<1.0:flags.append('BELOW_RESOLUTION_RISK')
    if not flags and dbh['inliers']>=22 and dbh['coverage']>=0.40 and len(fits)>=4 and vertical>=0.75:
        status='A_INDICATIVE'
    elif diam<=25 and line<=0.12 and rcv<=0.55 and dbh['coverage']>=0.20:
        status='B_LOW_CONFIDENCE'
    else:status='C_REJECT'
    return dict(x=float(dbh['center'][0]),y=float(dbh['center'][1]),groundZ=local_ground,dbhCm=float(diam),radiusM=float(dbh['radius']),circumferenceCm=float(2*np.pi*dbh['radius']*100),fitPoints=dbh['inliers'],coverage=dbh['coverage'],rmseM=dbh['rmse'],verticality=vertical,validatedSlices=len(fits),centerlineResidualM=line,radiusCv=rcv,status=status,flags=flags)

def suppress(rows):
    kept=[]
    rank={'A_INDICATIVE':2,'B_LOW_CONFIDENCE':1,'C_REJECT':0}
    rows=sorted(rows,key=lambda r:(rank[r['status']],r['fitPoints'],r['coverage']),reverse=True)
    for r in rows:
        p=np.array([r['x'],r['y']])
        if any(np.linalg.norm(p-np.array([q['x'],q['y']]))<0.22 for q in kept):continue
        kept.append(r)
    return kept

def stats(vals):
    if not vals:return None
    a=np.asarray(vals,float)
    return dict(n=len(a),meanCm=round(float(np.mean(a)),2),medianCm=round(float(np.median(a)),2),p25Cm=round(float(np.percentile(a,25)),2),p75Cm=round(float(np.percentile(a,75)),2),minCm=round(float(np.min(a)),2),maxCm=round(float(np.max(a)),2))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',required=True);ap.add_argument('--site',required=True);ap.add_argument('--output',required=True);args=ap.parse_args()
    path=Path(args.input);ground,zq=estimate_ground(path)
    seeds,det=density_candidates(path,ground)
    neigh=collect(path,seeds,ground)
    rows=[]
    for s,p in zip(seeds,neigh):
        r=evaluate(s,p,ground)
        if r is not None:rows.append(r)
    rows=suppress(rows)
    for i,r in enumerate(rows,1):r['treeId']=f"{args.site.upper()}-FULL-{i:04d}"
    A=[r['dbhCm'] for r in rows if r['status']=='A_INDICATIVE'];B=[r['dbhCm'] for r in rows if r['status']=='B_LOW_CONFIDENCE']
    with laspy.open(path) as f:
        hdr=dict(pointCount=int(f.header.point_count),pointFormat=str(f.header.point_format),version=str(f.header.version),mins=[float(v) for v in f.header.mins],maxs=[float(v) for v in f.header.maxs],scales=[float(v) for v in f.header.scales])
    payload=dict(siteId=args.site,measurementStatus='PRELIMINARY_FULL_LAS_SMALL_STEM_SCREENING',fieldVerified=False,source=path.name,header=hdr,globalGroundEstimateZ=ground,zSampleQuantiles=zq,detection=det,candidateSeedCount=int(len(seeds)),retainedCount=len(rows),statusCounts={k:sum(r['status']==k for r in rows) for k in ('A_INDICATIVE','B_LOW_CONFIDENCE','C_REJECT')},aStats=stats(A),aPlusBStats=stats(A+B),trees=rows,limitations=['No field-verified DBH reference supplied.','Global/local ground are estimated from point-cloud quantiles.','A_INDICATIVE is a geometry screening label, not formal MRV acceptance.'])
    Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:payload[k] for k in ('siteId','candidateSeedCount','retainedCount','statusCounts','aStats','aPlusBStats')},ensure_ascii=False))
if __name__=='__main__':main()
