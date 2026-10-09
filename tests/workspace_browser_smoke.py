"""Manual browser integration test. Requires Playwright and a Chromium binary.
Run: python tests/workspace_browser_smoke.py --chromium /usr/bin/chromium
Uses synthetic LAS only; no production writes or public data uploads.
"""
import argparse
import base64
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tests'))
from feedback_server import create_app
from test_feedback_workspace import write_las
from playwright.sync_api import sync_playwright
import uvicorn

def main():
    p=argparse.ArgumentParser();p.add_argument('--chromium',default='/usr/bin/chromium');p.add_argument('--screenshot');a=p.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        root=Path(temp);shutil.copytree(ROOT/'site/public/feedback-workspace',root/'site/public/feedback-workspace')
        shutil.copytree(ROOT/'models',root/'models');fixture=root/'synthetic.las';write_las(fixture)
        state=root/'private';app=create_app(root,state);server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=18995,log_level='error'))
        from fastapi.testclient import TestClient
        client_context=TestClient(app); client=client_context.__enter__()
        results={};errors=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=a.chromium,headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1060});page.on('pageerror',lambda e:errors.append(str(e)))
                # Offline DOM + real ASGI handlers. No network navigation/policy changes.
                def local_request(source, payload):
                    method=payload.get('method','GET');kwargs={'headers':payload.get('headers',{})}
                    if payload.get('body64') is not None:kwargs['content']=base64.b64decode(payload['body64'])
                    r=client.request(method,payload['url'],**kwargs)
                    return {'status':r.status_code,'text':r.text}
                page.expose_binding('localTestRequest',local_request)
                html=(root/'site/public/feedback-workspace/index.html').read_text()
                html=html.replace('<link rel="stylesheet" href="style.css">','<style>'+(root/'site/public/feedback-workspace/style.css').read_text()+'</style>')
                html=html.replace('<script src="app.js"></script>','')
                shim="""window.fetch=async(url,options={})=>{
                  let body64=null;if(options.body!==undefined){let bytes=new Uint8Array(await new Blob([options.body]).arrayBuffer());let text='';for(const b of bytes)text+=String.fromCharCode(b);body64=btoa(text);}
                  let r=await window.localTestRequest({url:String(url),method:options.method||'GET',headers:options.headers||{},body64});return new Response(r.text,{status:r.status,headers:{'Content-Type':'application/json'}});
                };
                if(!crypto.randomUUID)crypto.randomUUID=()=> '10000000-1000-4000-8000-100000000000'.replace(/[018]/g,c=>(+c^crypto.getRandomValues(new Uint8Array(1))[0]&15>>+c/4).toString(16));"""
                def mount():
                    page.set_content(html);page.add_script_tag(content=shim);page.add_script_tag(content=(root/'site/public/feedback-workspace/app.js').read_text())
                mount();page.wait_for_function("document.querySelector('#learningStatus').textContent.includes('บันทึกคำตอบ')")
                page.locator('#site').fill('SYNTHETIC QA ONLY');page.locator('#file').set_input_files(str(fixture));page.locator('#metres').check();page.locator('#start').click()
                page.wait_for_function("document.querySelector('#jobs').textContent.includes('COMPLETE')",timeout=30000)
                page.locator('#list button').first.click();page.locator('#identity button').first.wait_for(state='visible')
                page.wait_for_function("!document.querySelector('#identity button').disabled")
                page.get_by_role('button',name='ลำต้นจริง',exact=True).click()
                page.wait_for_function("document.querySelector('#saveStatus').textContent.includes('บันทึกถาวรแล้ว')")
                page.get_by_role('button',name='วงวัดถูก',exact=True).click()
                page.wait_for_function("document.querySelector('#saveStatus').textContent.includes('บันทึกถาวรแล้ว')")
                results['reviews_saved']=page.evaluate("fetch('/api/status').then(r=>r.json()).then(s=>s.reviews)")
                assert results['reviews_saved']==2
                page.reload();mount();page.locator('#list button').first.click();page.wait_for_function("document.querySelector('#history').textContent.includes('2 ครั้ง')")
                assert 'active' in page.get_by_role('button',name='ลำต้นจริง',exact=True).get_attribute('class')
                assert 'active' in page.get_by_role('button',name='วงวัดถูก',exact=True).get_attribute('class')
                results['offline_remount_preserved_labels']=True
                page.wait_for_function("fetch('/api/status').then(r=>r.json()).then(s=>s.training?.status==='COMPLETE')",timeout=30000)
                results['automatic_retrain_completed']=True
                if a.screenshot:page.screenshot(path=a.screenshot,full_page=True)
                browser.close()
        finally:
            client_context.__exit__(None,None,None)
        assert not errors,errors
        from feedback_server import Store
        db=Store(state/'feedback.sqlite3');data,seq=db.dataset();assert seq==2
        results['asgi_shutdown_kept_database']=True;results['browser_errors']=errors;results['mode']='OFFLINE_DOM_WITH_REAL_ASGI_API'
        print(json.dumps(results,indent=2))
if __name__=='__main__':main()
