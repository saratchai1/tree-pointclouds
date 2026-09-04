#!/usr/bin/env python3
import argparse, json, math
from pathlib import Path
import numpy as np
from scipy.ndimage import maximum_filter
from scipy.spatial import cKDTree

GRID=0.08
SEED_PERCENTILE=70.0
RADIAL_MIN=0.008
RADIAL_MAX=0.125
RADIAL_BINS=40
BANDS=((0.35,0.70),(0.65,1.00),(0.95,1.30),(1.25,1.60),(1.55,1.90))

def load_positions(data):
    chunks=[]
    for p in sorted(Path(data).glob('positions-*.glbin')):
        a=np.fromfile(p,dtype='<f4').reshape(-1,3)
        chunks.append(a)
    return np.concatenate(chunks).astype(np.float64)

def seeds(points,ground):
    xmin,ymin=np.min(points[:,:2],axis=0);xmax,ymax=np.max(points[:,:2],axis=0)
    nx=int(math.ceil((xmax-xmin)/GRID))+1;ny=int(math.ceil((ymax-ymin)/GRID))+1
    ix=np.clip(((points[:,0]-xmin)/GRID).astype(np.int32),0,nx-1);iy=np.clip(((points[:,1]-ymin)/GRID).astype(np.int32),0,ny-1);cell=iy*nx+ix
    h=points[:,2]-ground
    counts=[]
    for lo,hi in BANDS:
        m=(h>=lo)&(h<hi)
        c=np.bincount(cell[m],minlength=nx*ny).reshape(ny,nx).astype(np.float32)
        counts.append(maximum_filter(c,size=3,mode='constant'))
    C=np.stack(counts)
    presence=(C>0).sum(axis=0)
    strength=np.sqrt(C.sum(axis=0))*presence
    mask=presence>=4
    if not mask.any(): return np.empty((0,2))
    vals=strength[mask]
    thr=np.percentile(vals,SEED_PERCENTILE)
    mx=maximum_filter(strength,size=5,mode='constant')
    yy,xx=np.where(mask&(strength==mx)&(strength>=thr))
    order=np.argsort(strength[yy,xx])[::-1]
    out=[]
    for k in order:
        p=np.array([xmin+(xx[k]+0.5)*GRID,ymin+(yy[k]+0.5)*GRID])
        if out and np.min(np.linalg.norm(np.asarray(out)-p,axis=1))<0.28:continue
        out.append(p)
        if len(out)>=1200:break
    return np.asarray(out)

def axis_from_bands(local,ground,seed):
    h=local[:,2]-ground
    centers=[];zs=[]
    for lo,hi in ((0.4,0.7),(0.7,1.0),(1.0,1.3),(1.3,1.6),(1.6,1.9)):
        m=(h>=lo)&(h<hi)&(np.linalg.norm(local[:,:2]-seed,axis=1)<=0.16)
        if m.sum()<1:continue
        centers.append(np.median(local[m,:2],axis=0));zs.append((lo+hi)/2)
    if len(centers)<4:return None
    centers=np.asarray(centers);zs=np.asarray(zs)
    A=np.column_stack([zs,np.ones(len(zs))]);cx=np.linalg.lstsq(A,centers[:,0],rcond=None)[0];cy=np.linalg.lstsq(A,centers[:,1],rcond=None)[0]
    pred=np.column_stack([A@cx,A@cy]);err=np.linalg.norm(centers-pred,axis=1)
    direction=np.array([cx[0],cy[0],1.0]);direction/=np.linalg.norm(direction)
    if abs(direction[2])<0.72:return None
    return cx,cy,float(np.median(err)),direction,len(centers)

def eval_seed(points,tree,seed,global_ground):
    idx=tree.query_ball_point(seed,0.28)
    if len(idx)<12:return None
    local=points[idx]
    near=local[np.linalg.norm(local[:,:2]-seed,axis=1)<=0.20]
    if len(near)<10:return None
    ground=float(np.percentile(near[:,2],3.0));ground=float(np.clip(ground,global_ground-0.30,global_ground+0.40))
    ax=axis_from_bands(local,ground,seed)
    if ax is None:return None
    cx,cy,center_err,direction,band_count=ax
    h=local[:,2]-ground
    m=(h>=0.50)&(h<=1.75)
    q=local[m]
    if len(q)<8:return None
    zrel=q[:,2]-ground
    axis_xy=np.column_stack([cx[0]*zrel+cx[1],cy[0]*zrel+cy[1]])
    r=np.linalg.norm(q[:,:2]-axis_xy,axis=1)
    bins=np.linspace(RADIAL_MIN,RADIAL_MAX,RADIAL_BINS);hist=np.zeros(len(bins)-1)
    for i in range(len(hist)):
        lo,hi=bins[i],bins[i+1]
        hist[i]=np.sum((r>=lo)&(r<hi))/max(hi*hi-lo*lo,1e-6)
    if hist.max()<=0:return None
    bi=int(np.argmax(hist));radius=float((bins[bi]+bins[bi+1])/2)
    tol=max(0.008,min(0.018,radius*0.35))
    shell=np.abs(r-radius)<=tol
    if shell.sum()<5:return None
    shell_h=zrel[shell]
    persistent=sum(np.any((shell_h>=lo)&(shell_h<hi)) for lo,hi in BANDS)
    breast=np.sum(shell & (np.abs(zrel-1.30)<=0.18))
    if persistent<3 or breast<1:return None
    diam=radius*200
    flags=[]
    if shell.sum()<8:flags.append('SPARSE_SAMPLE')
    if persistent<4:flags.append('LIMITED_VERTICAL_PERSISTENCE')
    if center_err>0.055:flags.append('AXIS_CENTER_UNSTABLE')
    if diam>18:flags.append('LARGE_STRUCTURE_RISK')
    if diam<1.5:flags.append('BELOW_EFFECTIVE_RESOLUTION')
    if not flags and shell.sum()>=8 and persistent>=4 and center_err<=0.055 and 1.5<=diam<=18:
        status='A_SMALL_STEM_INDICATIVE'
    elif diam<=20 and persistent>=3 and center_err<=0.10:
        status='B_SMALL_STEM_LOW_CONFIDENCE'
    else: status='C_REJECT'
    return dict(x=float(seed[0]),y=float(seed[1]),groundZ=ground,diameterCm=diam,radiusM=radius,shellPoints=int(shell.sum()),breastSupportPoints=int(breast),persistentBands=int(persistent),axisCenterResidualM=center_err,verticality=float(abs(direction[2])),status=status,flags=flags)

def suppress(rows):
    rank={'A_SMALL_STEM_INDICATIVE':2,'B_SMALL_STEM_LOW_CONFIDENCE':1,'C_REJECT':0}
    rows=sorted(rows,key=lambda x:(rank[x['status']],x['persistentBands'],x['shellPoints']),reverse=True)
    out=[]
    for r in rows:
        p=np.array([r['x'],r['y']])
        if any(np.linalg.norm(p-np.array([q['x'],q['y']]))<0.24 for q in out):continue
        out.append(r)
    return out

def stats(v):
    if not v:return None
    a=np.asarray(v,float)
    return {'n':len(a),'meanCm':round(float(a.mean()),2),'medianCm':round(float(np.median(a)),2),'p25Cm':round(float(np.percentile(a,25)),2),'p75Cm':round(float(np.percentile(a,75)),2),'minCm':round(float(a.min()),2),'maxCm':round(float(a.max()),2)}

def main():
    global GRID,SEED_PERCENTILE,RADIAL_MIN,RADIAL_MAX,RADIAL_BINS
    ap=argparse.ArgumentParser();ap.add_argument('--data',required=True);ap.add_argument('--metadata',required=True);ap.add_argument('--site',required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--grid',type=float,default=GRID);ap.add_argument('--seed-percentile',type=float,default=SEED_PERCENTILE);ap.add_argument('--radial-min',type=float,default=RADIAL_MIN);ap.add_argument('--radial-max',type=float,default=RADIAL_MAX);ap.add_argument('--radial-bins',type=int,default=RADIAL_BINS)
    args=ap.parse_args();GRID=args.grid;SEED_PERCENTILE=args.seed_percentile;RADIAL_MIN=args.radial_min;RADIAL_MAX=args.radial_max;RADIAL_BINS=args.radial_bins
    pts=load_positions(args.data);meta=json.loads(Path(args.metadata).read_text())
    ground=float(np.percentile(pts[:,2],2.5));ss=seeds(pts,ground);tree=cKDTree(pts[:,:2]);rows=[]
    for s in ss:
        r=eval_seed(pts,tree,s,ground)
        if r:rows.append(r)
    rows=suppress(rows)
    for i,r in enumerate(rows,1):r['treeId']=f"{args.site.upper()}-SMALL-{i:04d}"
    A=[r['diameterCm'] for r in rows if r['status']=='A_SMALL_STEM_INDICATIVE'];B=[r['diameterCm'] for r in rows if r['status']=='B_SMALL_STEM_LOW_CONFIDENCE']
    out={'siteId':args.site,'status':'SAMPLED_SMALL_STEM_CYLINDER_SCREENING','parameters':{'gridM':GRID,'seedPercentile':SEED_PERCENTILE,'radialMinM':RADIAL_MIN,'radialMaxM':RADIAL_MAX,'radialBins':RADIAL_BINS},'sourceLas':meta.get('source'),'sourcePointCount':meta.get('sourcePointCount'),'viewerPointCount':len(pts),'samplingStride':meta.get('samplingStride'),'globalGroundZ':ground,'seedCount':len(ss),'retainedCount':len(rows),'statusCounts':{k:sum(r['status']==k for r in rows) for k in ('A_SMALL_STEM_INDICATIVE','B_SMALL_STEM_LOW_CONFIDENCE','C_REJECT')},'aStats':stats(A),'aPlusBStats':stats(A+B),'trees':rows,'limitations':['Browser sample only; not formal DBH.','Cylinder radius uses multi-height shell mode because per-slice circle fitting is too sparse for small stems.','Requires full-LAS or field validation for MRV.']}
    Path(args.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:out[k] for k in ('siteId','parameters','seedCount','retainedCount','statusCounts','aStats','aPlusBStats')},ensure_ascii=False))
if __name__=='__main__':main()
