# 시연 시나리오

`showcase.py` 는 제품이 하는 일을 API 로 차례차례 보여 준다 — 정상 생성, 개인정보 마스킹, 차단, 대화, 임베딩,
배치 레인, 멱등성, 관리자 시야(추이·가드 미리보기·사건·감사). `--seed N` 은 대시보드를 채우는 트래픽이다.

```bash
# 저장소 안에서 (번들 SDK clients/client.py 를 그대로 쓴다)
python examples/showcase/showcase.py --url https://llmcc.example --token "$LCC_TOKEN" --admin-token "$LCC_ADMIN_TOKEN"
python examples/showcase/showcase.py --url https://llmcc.example --token "$LCC_TOKEN" --seed 30 --pace 1

# 발표 전 예행 — 로컬 데모(목 프로바이더). 배너의 서비스 토큰을 쓴다.
python -m app serve --demo
python examples/showcase/showcase.py --token lcc_... --only 2,3,4
```

발표 자리에서 어떤 순서로 무엇을 보여 줄지는 [docs/demo-script.md](../../docs/demo-script.md) 에 있다.
비밀(토큰)은 인자나 환경(`LCC_URL` · `LCC_TOKEN` · `LCC_ADMIN_TOKEN`)으로만 주고, 스크립트는 그것을 화면에 찍지 않는다.
