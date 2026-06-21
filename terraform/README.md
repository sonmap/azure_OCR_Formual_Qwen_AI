# Azure Actuarial OCR Terraform

이 Terraform은 현재 로컬/VM Docker Compose 기반 OCR PoC를 Azure VM에 배포하기 위한 기본 구성입니다.

구성:
- Azure Linux VM 1대
- Docker / Docker Compose 설치
- frontend/backend/formula-ocr/qwen-vl 컨테이너 실행
- Azure AI Vision 계정 생성
- NSG로 8080, 8000, 22 포트 허용
- 선택적으로 로컬 앱 디렉터리를 VM에 업로드

## 사용 순서

1. Azure 로그인

```bash
az login
az account set --subscription "<subscription-id>"
```

2. 변수 파일 준비

```bash
cp terraform.tfvars.example terraform.tfvars
```

`terraform.tfvars`에서 `source_app_path`를 현재 앱 소스가 있는 경로로 맞춥니다.

예:

```hcl
source_app_path = "/home/son/actuarial-formula-page-ocr-local-regroup"
```

Windows에서 Terraform을 실행하면서 원격 Linux 앱을 그대로 가져오려면, 먼저 로컬에 app 디렉터리를 복사한 뒤 그 로컬 경로를 지정하는 방식을 권장합니다.

3. 배포

```bash
terraform init
terraform apply
```

4. 접속

```bash
terraform output app_url
```

## OCR 엔진 비교 구조

앱 화면에서 다음 엔진을 선택해 비교합니다.

- `formula-ocr`: 로컬 수식 OCR 컨테이너
- `qwen`: Qwen2-VL 컨테이너
- `azure-ai`: Azure AI Vision OCR

Terraform은 Azure AI Vision 리소스와 키를 만들고 VM 환경 파일에 주입합니다.

현재 PoC 앱에는 아래 선택값이 반영되어 있어야 합니다.

```text
formula  -> http://formula-ocr:9000/predict
qwen     -> http://qwen-vl:9100/predict
azure-ai -> AZURE_AI_ENDPOINT / AZURE_AI_KEY 직접 호출
```

이 작업에서 현재 PoC 서버의 앱 코드에는 `Azure AI` 선택 버튼과 Azure AI Vision Read API 어댑터를 반영했습니다.

## 수동 기동

Azure VM 접속 후:

```bash
cd /opt/actuarial-formula-page-ocr-local-regroup

./manual-start.sh base   # formula-ocr + backend + frontend
./manual-start.sh qwen   # Qwen까지 기동
```

Azure AI는 별도 컨테이너가 아니라 backend에서 Azure AI Vision API를 직접 호출합니다.

## 운영 주의

- Qwen2-VL CPU 추론은 느리고 메모리를 많이 씁니다. 기본 VM 크기는 `Standard_D8s_v5`로 잡았습니다.
- 디스크는 기본 128GB입니다.
- 공개 테스트용으로 8080을 열어 둡니다. 운영에서는 Application Gateway, Bastion, Private Endpoint, HTTPS를 추가해야 합니다.
