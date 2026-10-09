# เรียนรู้จากคำตัดสินเดิมของผู้ตรวจ — รุ่นแรก

## สิ่งที่เพิ่ม

`review_learning.py` เป็นโมดูลแยกจาก V3.1 ที่นำคำตัดสินเดิมมาฝึก
classifier จริง บันทึก coefficients เป็น JSON และโหลดกลับมาคัดกรอง candidate
ของงานสำรวจอื่นที่มี feature schema เดียวกันได้ ไม่ใช่แค่ blacklist Tree ID.

โมเดลรุ่นแรกมีสถานะ **ADVISORY_ONLY** ไม่แก้ผลเดิม ไม่ deploy และไม่รับรอง
เส้นรอบวง งานส่วนรับ LAS ของแปลงใหม่และปุ่มในเว็บยังไม่ได้เชื่อมใน increment นี้.

## ใช้งาน

จาก branch ที่มีโมดูลนี้ (ฐานคือ `codex/public-lidar-extension`):

```sh
python -m venv .venv-feedback
# Windows: .venv-feedback\Scripts\activate
# macOS/Linux: source .venv-feedback/bin/activate
python -m pip install -r requirements-feedback-learning.txt
python -m unittest discover -s tests -p 'test_review_learning.py' -v
python scripts/review_learning.py train --output .feedback-learning
```

ไม่ต้องตรวจรายการเดิมใหม่ ตัวนำเข้าจะอ่านไฟล์เดิม:

- `annotations/phase1_75_pilot_review.json`
- `outputs/review_queue_v2_phase1_75_pilot.json`

ผลอยู่ที่ `.feedback-learning/`:

| ไฟล์ | ความหมาย |
|---|---|
| `dataset.json` | คำตอบที่จับคู่กับหลักฐานและ feature แล้ว |
| `report.json` | จำนวน label, ข้อมูลที่ไม่ใช้, ผลทดสอบแยกกลุ่ม และ baseline |
| `reports/<fingerprint>.json` | รายงานประจำชุดข้อมูล ไม่ทับรายงานของชุดอื่น |
| `<task>-<hash>.json` | โมเดลที่ฝึกแล้ว พร้อม coefficients/normalization |
| `<task>.latest.json` | ชี้โมเดลล่าสุดของ local run; ไม่ใช่ production registry |
| `suggestions.json` | คำแนะนำ พร้อมคำตอบเดิมของคนที่มีอำนาจเหนือโมเดล |
| `human-review-ledger.json` | ประวัติการสอนเพิ่มทั้งหมดแบบ append-only เชิงตรรกะ |

อย่า commit local runtime/ledger ใหม่หรือเผยแพร่ข้อมูลสำรวจใหม่โดยไม่มีการตรวจ
สิทธิ์และข้อมูลส่วนบุคคลก่อน โมเดลเป็น JSON ไม่มี pickle และ inference ไม่ต้อง
ติดตั้ง scikit-learn แต่การฝึกต้องใช้ dependencies ข้างต้น.

## คำตัดสินต้องตรงกับเรื่องที่สอน

โมเดล `stem_identity` เรียนจาก TRUE_MAIN_STEM เทียบกับราก/กิ่ง/พืชอื่น.
โมเดล `measurement_validity` ต้องใช้คำตอบ MEASUREMENT_CORRECT/INCORRECT
ที่ผูกกับวงวัดรุ่นนั้นโดยตรง ไม่แปลง TRUE_MAIN_STEM เป็นการรับรองตัวเลข.

DUPLICATE_OF ใช้รวมกลุ่มตัวอย่าง ไม่ใช่ตัวอย่างลำต้นผิด.
NOT_ENOUGH_INFORMATION ไม่ถูกนับเป็นผิด. Manual seed และ clean-height hint
ยังคงอยู่ในไฟล์เดิม ไม่ถูกยกเป็นวงวัดที่คนรับรอง. ข้อมูลที่อ้าง pipeline คนละรุ่น
หรือ feedback คนละ evidence hash จะถูกแจ้งใน audit ไม่ถูกสวมให้วงใหม่.

## สอนเพิ่มแล้วฝึกซ้ำในคำสั่งเดียว

```sh
python scripts/review_learning.py review --candidate C-0121 \
  --task stem_identity --label PROP_ROOT_OR_ROOT_ONLY \
  --output .feedback-learning
```

สำหรับการรับรอง/ปฏิเสธวงวัด ต้องเป็นการตัดสินของคนจริง เช่น:

```sh
python scripts/review_learning.py review --candidate C-0174 \
  --task measurement_validity --label MEASUREMENT_CORRECT \
  --output .feedback-learning
```

ตัวอย่างคำสั่งไม่ใช่คำยืนยันว่า candidate ตัวนั้นวัดถูก ให้รันเฉพาะเมื่อตรวจ
หลักฐานของ snapshot ที่โปรแกรมอ่านแล้วเท่านั้น ทุกครั้งบันทึกคำตอบใหม่ก่อนฝึก
จากคำตอบเดิมทั้งหมด ถ้าฝึกไม่สำเร็จคำตอบยังอยู่ใน ledger ไม่หาย.

## ใช้กับข้อมูลใหม่โดยไม่เทรนใหม่ทุกไฟล์

```sh
python scripts/review_learning.py score \
  --model .feedback-learning/stem_identity-<hash>.json \
  --queue /path/to/new-survey-candidate-queue.json \
  --site-id NEW_SITE --survey-id NEW_SURVEY \
  --output /path/to/new-survey-feedback
```

`--queue` เป็นผล candidate extraction แบบเดียวกับ pilot queue ไม่ใช่ LAS.
adapter ต้องส่ง sampled_metrics/full_metrics/comparison_metrics ตาม schema
ในโค้ด ถ้าขาดเกินครึ่งของ 18 features จะ abstain ไม่เดาตัวเลข.
คะแนนไม่ใช่ probability ที่ calibrate แล้ว และต้นใหม่ต่างแปลงมี warning เสมอ.
การที่ model บอกน่าจะเป็นลำต้น ไม่ผ่านเกณฑ์เรขาคณิตให้โดยอัตโนมัติ.

ใช้ manifest เพื่อรวมหลายแปลง/หลายไฟล์ review โดยไม่ทับชุดเดิม:

```json
{"datasets":[{"site_id":"SITE_A","survey_id":"SURVEY_01",
"queue":"relative/candidates.json",
"annotations":["relative/review-original.json","relative/review-new.json"]}]}
```

ส่ง `--manifest manifest.json` ให้ train/review. Timestamp ต้องมี timezone.
คำตอบขัดแย้งในเวลาล่าสุดเดียวกันจะไม่ถูกใช้ฝึกจนกว่าจะมีคำตอบใหม่ที่ชัดเจน.

## การทดสอบที่ไม่หลอกตัวเอง

ใช้ LogisticRegression C=0.2, class_weight=balanced แบบกำหนดไว้ล่วงหน้า.
ใช้เฉพาะ geometry 18 features ไม่มี Tree ID, XYZ, label, reviewer note หรือ
ผลตัดสินของคนปะปนอยู่ใน input. Median imputation และ scaling fit ใหม่ใน
training fold เท่านั้น. ตรวจการ replay JSON ให้ตรงกับ estimator ทุกครั้ง.

leave-one-conservative-candidate-group-out รวม alias ที่สัมพันธ์กัน, track
เดียวกัน และ candidate ที่อยู่ห่างกันไม่เกิน 0.75 เมตรไว้ฝั่งเดียวกัน. นี่เป็น
การทดสอบภายใน survey ไม่ใช่ผลยืนยันข้ามแปลง. กลุ่มที่ train เหลือ class เดียว
จะถูกข้ามและแจ้ง ไม่แอบใช้ training prediction มาคิดคะแนน.

รายงาน confusion matrix, precision, recall, balanced accuracy และ false
positive ที่เกณฑ์ 0.5/0.8/0.9 เทียบ majority baseline. โมเดล identity ยังเทียบ
กับ STEM_LIKE ของ geometry เดิมด้วย. ไม่เลือก threshold จาก test แล้วอ้างเป็น
ผลอิสระ และไม่อ้างอัตราวัดสำเร็จว่าเป็น accuracy.

## ขอบเขตที่ยังเหลือ

1. เชื่อมปุ่ม review ในเว็บกับ persistent ledger/backend ไม่ใช้ localStorage
   อย่างเดียว และผูกวงที่ผู้ใช้กำลังมองกับ evidence hash ตอนบันทึก.
2. ให้ pipeline รับ LAS ใหม่ สร้าง candidate/feature ชุดเดียวกัน และเรียก
   โมเดลที่ผ่านการทดสอบโดยอัตโนมัติ; ตัว CLI scoring พร้อมเป็นจุดเชื่อมแล้ว.
3. เพิ่ม explicit measurement-correct/incorrect และตัวเลือกตำแหน่งที่แก้
   เพื่อฝึกตัวประเมินวงและตัวจัดอันดับตำแหน่ง ไม่บังคับให้ตรวจรายการเก่าซ้ำ.
4. ทดสอบข้ามแปลงและ calibrate ก่อนลดการตรวจด้วยคนหรือเปิด auto-release.

อ้างอิง: source snapshot `1b0654e6f1b6c62eef7c3d340b4167ab3f12c69f`.
