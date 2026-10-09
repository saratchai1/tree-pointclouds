(() => {'use strict';
const byId=id=>document.getElementById(id), query=new URLSearchParams(location.search);
let token='',current=null,items=[],loadGeneration=0,saving=false,poll=null;
let yaw=-.5,pitch=.28,zoom=1,drag=null;
const labels={TRUE_MAIN_STEM:'ลำต้นจริง',PROP_ROOT_OR_ROOT_ONLY:'ราก',BRANCH:'กิ่ง',OTHER_VEGETATION:'พืชอื่น',NOT_ENOUGH_INFORMATION:'ยังไม่แน่ใจ',MEASUREMENT_CORRECT:'วงวัดถูก',MEASUREMENT_INCORRECT:'วงวัดผิด'};
if(query.has('embed'))document.body.classList.add('embedded');
byId('survey').value=new Date().toISOString().slice(0,10);
async function api(path,options={}){
 const response=await fetch(path,{...options,cache:'no-store',headers:{'X-Workspace-Token':token,...options.headers}});
 const data=await response.json();if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);return data;
}
function post(path,data){return api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});}
function message(text,error=false){byId('saveStatus').textContent=text;byId('saveStatus').classList.toggle('error',error);}
function disable(){for(const b of document.querySelectorAll('.buttons button'))b.disabled=saving||!current||(b.closest('#validity')&&!current.display.measurement);}
async function load(id){const generation=++loadGeneration;current=null;disable();try{
 const item=await api(`/api/snapshots/${encodeURIComponent(id)}`);if(generation!==loadGeneration)return;
 current=item;render();disable();
}catch(e){if(generation===loadGeneration)message(e.message,true);}}
function render(){
 if(!current)return;const row=current.row,display=current.display,m=display.measurement;
 byId('candidate').textContent=row.candidate_id;byId('siteName').textContent=`${row.site_id} · ${row.survey_id}`;
 byId('measurement').textContent=m?`เส้นรอบวง ${m.circumference_cm??'ยังไม่ปล่อยค่า'} ซม. · ระดับ ${m.height_agl_m??'—'} ม. · ${m.status??'วงทดลอง'}`:'ข้อมูลตรวจลำต้นเดิม — ยังไม่มีวงที่ผูกกับปุ่มรับรองตัวเลข';
 const advice=Object.values(current.suggestions||{}).map(s=>s.human_label?`ใช้คำตอบของคุณ: ${labels[s.human_label]||s.human_label}`:s.score==null?'โมเดลยังมีข้อมูลไม่พอ':`คะแนนแนะนำ ${s.score.toFixed(3)} (${s.task})`);
 byId('suggestion').textContent=advice.join(' · ')+' · ไม่ใช่ความน่าจะเป็นที่สอบเทียบแล้ว';
 for(const [group,task]of [['identity','stem_identity'],['validity','measurement_validity']])
 for(const b of byId(group).querySelectorAll('button'))b.classList.toggle('active',row.human_labels[task]===b.dataset.label);
 byId('history').textContent=`ประวัติการบันทึกบนเครื่อง: ${current.review_history.length} ครั้ง · รุ่นคำตอบ ${current.revision}`;
 if(current.review_history.length)message('บันทึกไว้แล้ว โหลดซ้ำจากฐานข้อมูลบนเครื่อง');else message('คำตอบเดิมยังอยู่ เลือกคำตอบเพื่อเพิ่มหรือแก้ไข');
 byId('original').hidden=!display.viewer;if(display.viewer)byId('original').href=display.viewer;
 byId('original').textContent='เปิดหลักฐานเดิม';draw();
}
async function review(task,label){
 if(!current||saving)return;const snapshot=current;const id=snapshot.id;saving=true;disable();message('กำลังบันทึก…');
 try{const result=await post('/api/reviews',{snapshot_id:id,evidence_hash:snapshot.row.evidence_hash,revision:snapshot.revision,event_id:crypto.randomUUID(),task,label});
 if(current?.id===id){await load(id);message(result.persisted?'บันทึกถาวรแล้ว · ส่งคำตอบเข้าสู่การฝึกซ้ำแล้ว':'ยังไม่ได้บันทึก',!result.persisted);}
 }catch(e){message(`ยังไม่ได้บันทึก: ${e.message}`,true);if(current?.id===id){current=null;disable();setTimeout(()=>load(id),1200);}}
 finally{saving=false;disable();}
}
for(const [group,task]of [['identity','stem_identity'],['validity','measurement_validity']])for(const b of byId(group).querySelectorAll('button'))b.addEventListener('click',()=>review(task,b.dataset.label));
function list(){const q=byId('search').value.toLowerCase();byId('list').replaceChildren();for(const item of items){
 if(!`${item.candidate_id} ${item.site_id}`.toLowerCase().includes(q))continue;
 const b=document.createElement('button');b.textContent=`${item.candidate_id} · ${item.site_id}`;b.onclick=()=>load(item.id);byId('list').append(b);
}}
async function refresh(){
 const s=await api('/api/status');byId('learningStatus').textContent=`บันทึกคำตอบใหม่แล้ว ${s.reviews} ครั้ง · หลักฐาน ${s.snapshots} รายการ`;
 const t=s.training;let line=Object.values(s.models).join(' / ')||'ยังไม่มีโมเดล';
 if(t)line+=` · การฝึก: ${t.status}${t.error?' · '+t.error:''}`;
 if(t?.report?.tasks){const n=t.report.tasks.measurement_validity?.labelled_rows||0;line+=` · คำตอบเรื่องวงวัดที่ใช้ได้ ${n} รายการ`;}
 byId('modelStatus').textContent=line;
 if(!query.has('embed')){items=await api('/api/snapshots');list();const jobs=await api('/api/jobs');byId('jobs').replaceChildren();
 for(const job of jobs){const div=document.createElement('div'),title=document.createElement('strong'),text=document.createElement('p');title.textContent=`${job.payload.filename} · ${job.status}`;text.textContent=job.error||job.progress||'';div.append(title,text);
 if(job.status==='COMPLETE'){const link=document.createElement('a');link.href=`/api/jobs/${job.id}/measurements.csv`;link.textContent=`ดาวน์โหลด CSV · ${job.payload.report.candidate_count} candidates`;div.append(link);}
 byId('jobs').append(div);}}
}
byId('train').onclick=async()=>{try{await post('/api/train',{});byId('modelStatus').textContent='เข้าคิวฝึกจากคำตอบเก่าและใหม่รวมกัน';}catch(e){message(e.message,true);}};
byId('search').oninput=list;
byId('start').onclick=async()=>{
 const file=byId('file').files[0];if(!file){byId('uploadStatus').textContent='เลือกไฟล์ก่อน';return;}
 if(!byId('metres').checked){byId('uploadStatus').textContent='ต้องยืนยันหน่วยเมตรก่อน ไม่สามารถเดาสเกลให้ได้';return;}
 byId('start').disabled=true;byId('progress').hidden=false;byId('uploadStatus').classList.remove('error');
 try{const session=await post('/api/uploads',{filename:file.name,size:file.size,site_id:byId('site').value,survey_id:byId('survey').value,units:'metres'});
 let offset=0;
 while(offset<file.size){const end=Math.min(offset+session.chunk_size,file.size),body=file.slice(offset,end);let success=false;
 for(let attempt=0;attempt<3&&!success;attempt++){
 try{const r=await api(`/api/uploads/${session.id}?offset=${offset}`,{method:'PUT',body});if(r.offset!==end)throw new Error('upload offset mismatch');offset=end;success=true;}
 catch(e){const status=await api(`/api/uploads/${session.id}`);if(status.offset===end){offset=end;success=true;}else if(status.offset!==offset||attempt===2)throw e;}}
 byId('progress').value=100*offset/file.size;byId('uploadStatus').textContent=`ส่งเข้าเครื่องประมวลผล ${Math.round(100*offset/file.size)}%`;
 }
 await post(`/api/uploads/${session.id}/complete`,{});byId('uploadStatus').textContent='ไฟล์ครบแล้ว เข้าคิวค้นหาต้นไม้และวัดจากจุดความละเอียดเต็ม';await refresh();
 }catch(e){byId('uploadStatus').textContent=`ไม่สำเร็จ: ${e.message}`;byId('uploadStatus').classList.add('error');}
 finally{byId('start').disabled=false;}
};
function draw(){
 const display=current?.display||{},points=display.points||display.evidence?.tube_sample_xyz||[],m=display.measurement,plane=m?.plane;
 const canvas=byId('cloud');canvas.width=Math.max(1,canvas.clientWidth);canvas.height=Math.max(1,canvas.clientHeight);const ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);
 const center=plane?.center_xyz||(points.length?points[0]:[0,0,0]);
 const project=p=>{const a=p.map((v,i)=>v-center[i]);const x=a[0]*Math.cos(yaw)-a[1]*Math.sin(yaw),d=a[0]*Math.sin(yaw)+a[1]*Math.cos(yaw);return[canvas.width/2+x*90*zoom,canvas.height/2-(a[2]*Math.cos(pitch)+d*Math.sin(pitch))*90*zoom];};
 ctx.fillStyle='#93b39f';for(const p of points){const [x,y]=project(p);ctx.fillRect(x,y,2,2);}
 if(plane&&Number.isFinite(m.radius_m)){ctx.strokeStyle='#72ddff';ctx.lineWidth=3;ctx.beginPath();for(let i=0;i<=80;i++){const a=i/80*2*Math.PI;const p=center.map((v,j)=>v+m.radius_m*(Math.cos(a)*plane.basis_u[j]+Math.sin(a)*plane.basis_v[j]));const[x,y]=project(p);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);}ctx.stroke();}
 if(!points.length){ctx.fillStyle='#a7c5b4';ctx.font='16px system-ui';ctx.fillText('เปิดหลักฐานเดิมเพื่อดู point cloud ของรายการนี้',30,80);}
 const cross=byId('cross');cross.width=Math.max(1,cross.clientWidth);cross.height=Math.max(1,cross.clientHeight);const cc=cross.getContext('2d');cc.clearRect(0,0,cross.width,cross.height);
 if(plane&&Number.isFinite(m.radius_m)){
 const axis=plane.axis_direction,u=plane.basis_u,v=plane.basis_v,scale=90/Math.max(m.radius_m,.03);
 cc.fillStyle='#83cda5';for(const p of points){const q=p.map((x,i)=>x-center[i]);if(Math.abs(q.reduce((s,x,i)=>s+x*axis[i],0))>.05)continue;const x=q.reduce((s,a,i)=>s+a*u[i],0),y=q.reduce((s,a,i)=>s+a*v[i],0);cc.fillRect(cross.width/2+x*scale,cross.height/2-y*scale,3,3);}
 cc.strokeStyle='#72ddff';cc.lineWidth=2;cc.beginPath();cc.arc(cross.width/2,cross.height/2,m.radius_m*scale,0,2*Math.PI);cc.stroke();
 }
}
byId('cloud').onpointerdown=e=>{drag=[e.clientX,e.clientY];byId('cloud').setPointerCapture(e.pointerId);};
byId('cloud').onpointermove=e=>{if(!drag)return;yaw+=(e.clientX-drag[0])*.01;pitch=Math.max(-1.5,Math.min(1.5,pitch+(e.clientY-drag[1])*.008));drag=[e.clientX,e.clientY];draw();};
byId('cloud').onpointerup=()=>drag=null;byId('cloud').onpointercancel=()=>drag=null;
byId('cloud').addEventListener('wheel',e=>{e.preventDefault();zoom=Math.min(10,Math.max(.1,zoom*Math.exp(-e.deltaY*.001)));draw();},{passive:false});
(async()=>{disable();try{token=(await api('/api/session')).token;await refresh();if(query.get('snapshot'))await load(query.get('snapshot'));poll=setInterval(()=>refresh().catch(e=>{byId('modelStatus').textContent='การเชื่อมต่อหยุด: '+e.message;}),2500);}catch(e){byId('offline').hidden=false;byId('learningStatus').textContent='ยังไม่เชื่อมตัวประมวลผล';byId('start').disabled=true;byId('train').disabled=true;}})();
window.addEventListener('resize',draw);
window.addEventListener('pagehide',()=>clearInterval(poll));
})();
