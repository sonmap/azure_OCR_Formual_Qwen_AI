# 보험계리식 OCR · DB 저장 · 페이지별 조회 PoC

로컬 Docker 환경에서 PPTX/PDF/이미지 파일을 업로드하고, 페이지별로 텍스트/표/이미지 OCR 결과와 계리식 후보를 DB에 저장한 뒤 웹에서 조회하는 PoC입니다.

## 주요 기능

- PPTX 슬라이드별 페이지 저장
- PPTX 텍스트에서 `$$...$$` LaTeX 수식 자동 추출
- PPTX Table 셀 단위 저장
- PDF 페이지별 텍스트 추출 및 페이지 이미지 저장
- 이미지 수식 OCR 후보 저장
- SQLite DB 저장
- 페이지별 공식 후보 조회
- 공식 승인/반려 상태 관리
- 검색 기능

## 실행

```bash
unzip actuarial-formula-page-ocr-local.zip
cd actuarial-formula-page-ocr-local

docker compose down -v
docker compose build --no-cache
docker compose up
```

브라우저:

```text
http://localhost:8080
```

API 확인:

```bash
curl http://localhost:8000/health
curl http://localhost:8080/api/health
```

## 테스트 파일

기본 샘플:

```text
samples/sample_actuarial_formula.pptx
samples/formula_sample.png
```

웹에서 `sample_actuarial_formula.pptx`를 업로드하면 페이지별로 계리식 후보가 저장됩니다.

## DB 위치

```text
data/app.db
```

SQLite 확인:

```bash
sqlite3 data/app.db
.tables
select filename, page_count, status from documents;
select document_id, page_no, formula_seq, formula_title, latex, status from formula_blocks;
```

## 주요 테이블

| 테이블 | 설명 |
|---|---|
| documents | 업로드 문서 단위 |
| pages | 페이지/슬라이드 단위 텍스트와 이미지 |
| page_assets | 페이지 내 이미지/표/OCR 원문 |
| formula_blocks | 인식된 계리식 후보 |
| formula_validation_results | 문법/변수/검증 결과 |

## 수식 OCR 구조

이미지 수식 인식은 다음 순서로 동작합니다.

1. `FORMULA_OCR_URL` 환경변수가 있으면 외부 수식 OCR API 호출
2. `pix2tex`가 설치되어 있으면 pix2tex 사용
3. 없으면 Tesseract fallback 사용

Tesseract fallback은 수식을 LaTeX로 완벽하게 만들지 못합니다. 운영형으로 가려면 UniMERNet, pix2tex, PaddleOCR Formula Recognition 같은 특화모델을 별도 API로 붙이는 구조를 권장합니다.

예시:

```yaml
backend:
  environment:
    FORMULA_OCR_URL: http://formula-ocr:9000/predict
```

외부 수식 OCR API 응답 예시:

```json
{
  "latex": "\\text{1인당 순보험료} = \\frac{20,000,000,000}{100,000} = 200,000",
  "confidence": 0.92
}
```

## 운영 확장 방향

| PoC | 운영 |
|---|---|
| SQLite | PostgreSQL / Azure Database for PostgreSQL |
| Tesseract fallback | UniMERNet / pix2tex / PaddleOCR 수식 모델 |
| 로컬 Docker | Azure Container Apps / Azure Batch |
| 파일 volume | Blob Storage / ADLS Gen2 |
| 단순 승인 | 계리사 승인 Workflow |


## v0.3 멀티라인 수식 그룹핑 개선

OCR 결과가 아래처럼 여러 줄로 끊어지는 경우:

```text
E APVFNC(t, x)
=
E ANC(t,
x)
+
E ANC(t+1,
x+1)× e
- δ × lx +1/lx
```

`formula_parser.py`의 `group_broken_formula_lines()`가 연속된 수식 조각을 하나의 `multiline_formula_block`으로 묶습니다.
웹 화면의 문서 상세 상단에서 **수식 다시 묶기** 버튼을 누르면 기존 저장된 `formula_blocks`를 삭제하고 다시 생성합니다.

예상 보정 결과:

```text
E_{APVFNC}(t,x) = E_{ANC}(t,x) + E_{ANC}(t+1,x+1) × e^{-δ} × l_{x+1}/l_x + ...
```

운영 수준에서는 이 후처리 규칙에 회사 계리 기호 사전과 공식 패턴을 계속 추가해야 합니다.
