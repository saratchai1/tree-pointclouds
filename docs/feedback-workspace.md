# ตรวจบนเว็บ → เก็บคำตอบ → เรียนรู้ → ใช้กับ LAS งานใหม่

## เปิดใช้งาน

ใช้ branch `feat/review-feedback-learning-20261009` ใน repo `saratchai1/tree-pointclouds`.
ต้องมี Python 3.11 ขึ้นไป (ทดสอบด้วย 3.13). Windows เปิด `start-feedback.cmd`;
macOS เปิด `start-feedback.command` หรือรัน `bash start-feedback.command` จากโฟลเดอร์ repo.
ครั้งแรกดาวน์โหลด dependencies เข้าสภาพแวดล้อมแยก ครั้งต่อไปเปิดได้โดยไม่ต้องติดตั้งซ้ำ.
หน้าต่างโปรแกรมต้องเปิดค้างไว้ ใช้ Ctrl+C เมื่อต้องการหยุด.

หน้าเริ่มต้นคือ `http://127.0.0.1:8095/`. นี่คือตัวประมวลผลบนเครื่องของผู้ใช้
ไม่ใช่การ deploy เปลี่ยนเว็บ Vercel เดิม และไม่สามารถใช้ SQLite บน serverless
ชั่วคราวแล้วอ้างว่าเป็นฐานข้อมูลถาวรได้. เปิดเฉพาะ loopback สำหรับผู้ใช้คนเดียว.
ยังไม่มีระบบสมาชิกหรือ backend ส่วนกลางสำหรับหลายคน/หลายเครื่อง.

## สิ่งที่ทำได้

- โหลดคำตรวจ pilot เดิมจาก annotations และ queue ที่มีอยู่ใน repo โดยไม่ให้ตรวจ 39 รายการใหม่.
- เปิด V3.1 จากลิงก์ใน workspace: ตัว server เพิ่มแผงสอนให้เฉพาะตอนเปิดบนเครื่อง
  โดยไม่แก้ไฟล์ viewer/ผลวัดที่แช่แข็งไว้. แผงตรวจสอบ record และ point evidence
  กับไฟล์ต้นฉบับก่อนเปิดปุ่ม บันทึกกับ evidence hash ของวงที่แสดงจริง.
- ปุ่มลำต้น/ราก/กิ่ง และปุ่มวงถูก/วงผิด เป็นคนละ target; ไม่เปลี่ยนคำยืนยัน
  ว่าเป็นลำต้นให้กลายเป็นการรับรองเส้นรอบวง.
- คำตอบใหม่เก็บใน SQLite transaction ก่อนเริ่มฝึกซ้ำจากคำตอบเก่าและใหม่.
  Refresh/ปิดเปิดโปรแกรมไม่ล้างคำตอบ. สองแท็บแก้ revision เดียวกันจะคืน 409
  แทนเขียนทับเงียบ ๆ. ส่ง event เดิมซ้ำไม่เพิ่มข้อมูลฝึกซ้ำ.
- โมเดลที่ฝึกเสร็จใช้แนะนำภายใน workspace เท่านั้น. งานใหม่เรียกใช้โมเดลที่
  บันทึกไว้ ไม่ต้องเทรนใหม่ทุกไฟล์. คำตอบของคนมาก่อนคะแนนโมเดล.
- เลือก LAS ใหม่ ระบุแปลง/งาน ยืนยัน XYZ หน่วยเมตร แล้วเริ่มประมวลผล.
  upload แบ่ง chunk 8 MB และ job queue บนเครื่อง สถานะ/CSV เก็บถาวร.
- แสดง point cloud ราย candidate และวงวัด 3D หมุน/ซูมได้ พร้อมหน้าตัด.

## การประมวลผล LAS ใหม่ — แยกจาก frozen V3.1

`scripts/feedback_cloud.py` เป็น screening adapter รุ่นทดลอง
`new-survey-screening-v0.1`. ไม่อ้างว่าได้รัน V3.1 เดิมกับแปลงใหม่โดยไม่เปลี่ยน
ขั้นตอน. อ่าน LAS 1.0–1.4 แบบไม่บีบอัดเป็น chunk; metadata/header layout และ
SHA-256 ผูกกับแต่ละงาน ไม่ล็อกที่ 118 ต้นหรือ source hash สมุทรสงคราม.

ค้นหา seeds จาก sample ที่จำกัด 200,000 จุด; ดึงจุดความละเอียดเต็มจาก source
อีกครั้งเป็น tube ราย candidate แล้ว fit บนระนาบตั้งฉากแกนลำต้น. วัด 1.30 ม.
ก่อน เมื่อไม่ผ่านเกณฑ์จึงค้นหาระดับ 1.40–4.00 ม. ด้วยขั้น 0.10 ม.
เส้นรอบวงเป็น fitted-circle perimeter ไม่ใช่การวัดผิวเปลือกจริงทุกส่วน.
ผลต่างระดับไม่เรียก DBH. ต้องมีข้อมูลพื้นและผิวลำต้นจริง; inferred ground
และการยืนยันหน่วยจากผู้ใช้ถูกระบุเป็นคำเตือนเสมอ.

ค่า automatic เป็นผลคำนวณเรขาคณิตที่ยังไม่ยืนยันภาคสนาม. ค่าไม่ผ่านเกณฑ์
แสดงเป็น `REVIEW_CANDIDATE_ONLY`; ไม่เปลี่ยนเป็นผ่านเพราะ identity score สูง.
โมเดลเดิมใช้เป็น advisory transfer; preprocessing ใหม่ยังไม่ได้ validate
ข้ามแปลง. feature ที่ไม่มีหลักฐานจะเป็น missing ไม่คัดลอกค่า full ให้เป็น sample.
ป้าย HIGH/MEDIUM หรือ score ไม่ใช่ calibrated probability.

รุ่นนี้จำกัดไฟล์ 10 GB, กว้าง 500 เมตร, สูงสุด 128 seeds และ 2 ล้านจุด/tube.
เกิน budget จะแจ้งชัดเจน ไม่ตัด candidate เงียบ ๆ. การส่งไฟล์มี offset guard
แต่ยังไม่มี UI สำหรับ resume ข้ามการปิดหน้า; ไฟล์ที่ส่งไว้บางส่วนอยู่ใน runtime.
งานที่โปรแกรมหยุดกลางคันเป็น INTERRUPTED ไม่อ้างว่าสำเร็จ. ยังไม่มี job retry UI.

LAZ เป็นทางเลือก ต้องติดตั้ง `laspy[lazrs]>=2.5,<3` ใน environment นี้เอง.
รอบนี้ทดสอบ LAS 1.2/1.4 จริงที่สร้างจาก synthetic geometry ไม่ได้ทดสอบ LAZ.

## ที่เก็บข้อมูลและความปลอดภัย

ทุกอย่างอยู่ใต้ `.feedback-workspace/` ซึ่งถูก .gitignore:

- `feedback.sqlite3`: snapshots, append-only review events, revisions, jobs, model pointers.
- `uploads/<id>/source.las`: ไฟล์งานใหม่ บนดิสก์เครื่องนี้ ไม่เผยแพร่สาธารณะ.
- `jobs/<id>/`: queue, point evidence, provenance และผลวัด.
- `training/<id>/`: model JSON, dataset, evaluation และ suggestions ของแต่ละรอบ.

ห้ามตั้ง state-dir อยู่ใน `site/public`. Server ไม่เปิด route ให้ดาวน์โหลด raw
uploads/ฐานข้อมูล. Token ป้องกันคำขอเขียน, same-origin/Host checks, และ OS file
lock ป้องกันเปิดสองโปรแกรมบน state-dir เดียวกัน. ไม่รับ origin ต่างเว็บไซต์.
การสำรองทั้งงานให้ปิดโปรแกรมแล้วคัดลอก `.feedback-workspace/` ทั้งโฟลเดอร์.
ปุ่ม export ประวัติส่งออกเฉพาะ review events ไม่ใช่ backup ทั้งฐานข้อมูล.
ไม่มีการเปลี่ยน field_verified เป็น true จากปุ่มตรวจบนหน้าจอ.

## Evaluation และการทดสอบ

โมเดลยัง ADVISORY_ONLY. รักษาทั้ง site และ source checksum เดียวกันใน fold
เดียว แม้ upload เดิมภายใต้ชื่อแปลงต่างกัน. แปลงเดียวจึงไม่มีคะแนน held-out
ข้ามแปลง และไม่มีการแต่ง accuracy ขึ้นแทน. คำตอบไม่พอสำหรับ target ใด
แสดง NOT_ENOUGH_EXPLICIT_LABELS ไม่ปลอม labels หรือ weights.

รันเอง ไม่เพิ่ม CI/CD:

```sh
python -m pip install -r requirements-feedback-workspace.txt
python -m unittest discover -s tests -p 'test_review_learning.py' -v
python -m unittest discover -s tests -p 'test_feedback_workspace.py' -v
node --check site/public/feedback-workspace/app.js
node --check site/public/feedback-workspace/bridge.js
```

`tests/workspace_browser_smoke.py` เป็นการทดสอบ DOM ใน Chromium แบบ offline
เชื่อม real ASGI handlers ผ่าน test transport ไม่ใช่ทดสอบ live deployed network.
ทดสอบ upload → job → เลือก candidate → กดคำตอบสองชนิด → สร้างหน้าใหม่ →
อ่านคำตอบเดิม → automatic retrain. ไม่ได้รัน Windows/macOS launcher ในรอบนี้.
ต้องทดสอบไฟล์สำรวจใหม่จริงของผู้ใช้และ cross-site accuracy ก่อน production.
ไม่ merge/deploy หรือนำ raw data ขึ้น GitHub โดยไม่มีคำสั่ง.
