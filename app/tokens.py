"""입력 토큰 상한 추정 — **아무것도 임포트하지 않는 잎 모듈.**

여기 따로 있는 이유는 두 층이 같은 함수를 써야 하기 때문이다. `cost.py` 는
예약 금액을 낼 때 쓰고, `store.py` 는 잡을 만들 때 그 추정치를 **컬럼에
박아 두려고** 쓴다. `store.py` 는 어느 앱 모듈도 임포트하지 않는 바닥
레이어이고 `cost.py` 는 그 `store.py` 를 임포트하므로, 이 함수가 `cost.py`
에 있으면 순환이 된다.

**왜 컬럼에 박아 두나.** 스케줄러는 매 틱 스캔 창(기본 50)만큼 잡을 훑는다.
추정에 텍스트가 필요하면 그 창의 프롬프트 전량을 매 틱 읽어야 하고,
200KB 프롬프트에서는 그것만 60ms 다(실측). 스케줄러에 필요한 것은 텍스트가
아니라 **숫자 하나**다. 제출 시 한 번 재서 넣으면 재시도마다 다시 재지도 않는다.
"""

from __future__ import annotations

#: 라틴 문자 기준 문자당 토큰 비율. 영어 산문이 대략 이 근처다.
ASCII_CHARS_PER_TOKEN = 4.0

#: 한글·한자·가나는 BPE 에서 **문자 하나가 토큰 하나 이상**이 되는 일이 흔하다.
#: 하나의 비율(3.0)로 뭉뚱그리면 한국어 프롬프트의 입력 토큰을 서너 배 과소 추정하고,
#: 그 순간 "상한 예약" 이 상한이 아니게 된다 — 예산이 넘은 뒤에야 드러난다.
WIDE_CHARS_PER_TOKEN = 1.0

#: 이 코드포인트 이상을 넓은 문자로 본다. CJK 부수(U+2E80)부터 시작해 한중일 문자와
#: 가나·한글·이모지를 모두 덮는다. 정확한 토크나이저를 흉내 내지 않는다 —
#: **예약은 상한이므로 넉넉한 쪽으로 틀리는 것이 맞다.**
WIDE_CODEPOINT_START = 0x2E80


def estimate_input_tokens(text: str) -> int:
    """입력 토큰 상한 추정. 프로바이더마다 토크나이저가 달라 정확할 수 없다.

    정확할 수 없으므로 **어느 쪽으로 틀릴지를 고른다.** 과소 추정은 예산을 넘긴 뒤에
    드러나고, 과대 추정은 예약이 조금 더 잡혔다가 정산에서 풀린다. 후자를 고른다.
    """
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) >= WIDE_CODEPOINT_START)
    narrow = len(text) - wide
    return int(narrow / ASCII_CHARS_PER_TOKEN + wide / WIDE_CHARS_PER_TOKEN)


def estimate_outbound_tokens(
    prompt_masked: str | None,
    system_masked: str | None,
    prompt_external: str | None,
    system_external: str | None,
) -> int:
    """이 잡이 노드로 보낼 수 있는 최대 입력 토큰. 비용 예약의 입력 근거다.

    경계마다 마스킹 결과가 다르고(외부용이 더 많이 가려진다) 배치 전에는 어느 쪽으로
    갈지 모른다. **예약은 상한이므로 큰 쪽을 쓴다** — 남는 예약은 정산에서 풀린다.

    길이가 아니라 **토큰 수로 비교한다.** 외부용이 더 짧은데 한글 비중이 높아서
    토큰은 더 많은 경우가 실제로 있다 — 마스킹이 ASCII 자리표시자로 바꾸기 때문이다.
    """
    internal = (prompt_masked or "") + (system_masked or "")
    external = (prompt_external or prompt_masked or "") + (
        system_external or system_masked or ""
    )
    return max(estimate_input_tokens(internal), estimate_input_tokens(external))
