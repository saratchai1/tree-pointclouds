/* Served only by the loopback workspace. Frozen V3.1 files are not modified. */
(() => {'use strict';
const panel=document.createElement('section');panel.className='panel';panel.style.padding='16px';
const heading=document.createElement('h2');heading.textContent='ตรวจแล้วสอนระบบ — บันทึกบนเครื่องนี้';
const status=document.createElement('p'),frame=document.createElement('iframe');frame.title='บันทึกคำตัดสินของวงปัจจุบัน';frame.style.cssText='width:100%;height:440px;border:0;display:none';panel.append(heading,status,frame);
(document.querySelector('#detailViewer')||document.querySelector('main')||document.body).after(panel);
let token='',lastRecord=null,lastEvidence=null,generation=0,busy=false;
async function poll(){
 if(typeof state==='undefined'||!state.current||!state.evidence||state.evidence.error){
 frame.style.display='none';status.textContent='รอ point cloud และวงวัดของต้นที่เลือกให้โหลดครบ';lastRecord=null;return;}
 const record=state.current,evidence=state.evidence;
 if(record===lastRecord&&evidence===lastEvidence)return;
 lastRecord=record;lastEvidence=evidence;const mine=++generation;
 frame.style.display='none';status.textContent='ตรวจว่า snapshot ตรงกับวงและจุดที่หน้าเว็บแสดง…';
 try{
 if(!token){const res=await fetch('/api/session',{cache:'no-store'});if(!res.ok)throw new Error('ต้องเปิดผ่าน start-feedback');token=(await res.json()).token;}
 const res=await fetch('/api/v31/snapshot',{method:'POST',headers:{'Content-Type':'application/json','X-Workspace-Token':token},body:JSON.stringify({tree_id:record.tree_id,record,evidence})});
 const data=await res.json();if(!res.ok)throw new Error(data.detail||`HTTP ${res.status}`);
 if(mine!==generation||state.current!==record||state.evidence!==evidence)return;
 frame.src=`/feedback-workspace/?embed=1&snapshot=${encodeURIComponent(data.id)}`;frame.style.display='block';status.textContent='คำตอบจะผูกกับ Tree ID วงวัด และหลักฐานที่เห็นชุดนี้เท่านั้น';
 }catch(e){if(mine===generation){status.textContent='ยังบันทึกไม่ได้: '+e.message;frame.style.display='none';}}
}
const interval=setInterval(poll,400);window.addEventListener('pagehide',()=>clearInterval(interval));
})();
