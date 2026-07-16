# Weekly Product Digest MCP

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-FastMCP-7C3AED)](https://modelcontextprotocol.io/)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![Sources](https://img.shields.io/badge/sources-6-orange)

주간 스타트업·제품·인디메이커 뉴스를 여러 공개 소스에서 모아 **리포트 형식**으로 뽑아주는 MCP 서버입니다. (FastMCP / Python)

**TechCrunch · BetaNews · Product Hunt · Indie Hackers · Hacker News · Reddit** 6개 소스를 7일 윈도로 병렬 수집 →
**제품 출시·투자 우선 Key Picks** + 소스별 리포트. 한 소스가 죽어도 나머지로 완성됩니다.

```text
🎯 Key Picks — 제품 출시 · 투자 우선 (키워드 선별)
- DeepSeek reportedly in talks to raise $1.5B, then IPO — TechCrunch · 투자 · 제품 출시
- The founder of Hinge raised $18M to build a new AI dating service — TechCrunch · 투자
⭐ Highlights (HN points)
📌 TechCrunch / BetaNews / Product Hunt / Indie Hackers / Hacker News / Reddit …
```

## 수집 소스

| Key | 소스 | 방식 | 점수 |
|-----|------|------|------|
| `techcrunch` | TechCrunch | RSS | – |
| `betanews` | BetaNews | RSS | – |
| `producthunt` | Product Hunt | RSS | – |
| `indiehackers` | Indie Hackers | **3단 폴백**: 비공식 피드 → SSR HTML 파싱 → (선택) Algolia | – |
| `hackernews` | Hacker News (Show HN) | 공개 Algolia API | ⭐ points |
| `reddit` | Reddit (r/SaaS, r/indiehackers) | 서브레딧 RSS (`/top/.rss`) | – |

> **Reddit 메모**: Reddit은 미인증 JSON 엔드포인트를 403으로 차단하므로 서브레딧 **Atom 피드**(`/r/<sub>/top/.rss?t=week`)를 사용합니다. RSS엔 추천수가 없어 Reddit 항목은 점수 하이라이트엔 안 뜨고 자체 섹션에만 표시됩니다. 서브레딧 전부 실패 시 해당 섹션은 "Unavailable"로 정직하게 표기됩니다.

- 모든 소스는 **병렬 수집**되고, 한 소스가 실패해도 리포트는 나머지로 완성됩니다(섹션에 실패 사유 표기).
- HN Show HN / Reddit은 인디해커 성격을 보강하는 백업 겸 확장 소스입니다.
- 전 구간 UTF-8 처리 (한글 Windows의 cp949 디코딩 문제 회피).

### Indie Hackers 이중화 (3단 폴백)

공식 API가 없어 단일 소스 의존을 피하도록 계층화했습니다. 각 계층은 앞 계층이 **빈 결과일 때만** 다음으로 넘어갑니다.

1. **비공식 RSS** — `feed.indiehackers.world` (+ 주간 top 폴백). 발행일 포함.
2. **SSR HTML 파싱** — `indiehackers.com` 홈/`/tech`/`/starting-up`이 서버렌더링이라 글 링크가 HTML에 그대로 있음. **키·인덱스 불필요**, 로테이트에 안 깨짐. 발행일은 없어 최신 글 위주로 수집.
3. **Algolia (선택)** — 아래 환경변수가 모두 설정된 경우에만 동작.

> **Algolia 관련 실측 메모**: indiehackers.com의 Algolia app(`applicationId`, `searchOnlyApiKey`)은 페이지에 공개돼 있으나 config상 `places.js`(주소 자동완성)에 연결돼 있고, **글 검색용 인덱스명은 페이지에 노출되지 않습니다**(콘텐츠 백엔드는 Firebase). 그래서 Algolia 계층은 리버스 엔지니어링한 키를 하드코딩하지 않고, 운영자가 **확인된 인덱스명**을 직접 넣어야 켜지는 옵션으로 뒀습니다. 클라이언트 키/인덱스는 예고 없이 로테이트되므로 취약한 경로로 간주하세요.

Algolia 계층을 켜려면 (선택):

```powershell
$env:IH_ALGOLIA_APP_ID = "<application id>"
$env:IH_ALGOLIA_API_KEY = "<search-only api key>"
$env:IH_ALGOLIA_INDEX   = "<확인된 콘텐츠 인덱스명>"
```

세 값이 모두 있으면 1·2단이 빈 결과일 때 Algolia로 폴백합니다. 하나라도 비면 계층이 조용히 건너뜁니다.

## 리포트 구조 (표준 흐름)

리포트는 위에서부터 다음 순서로 구성됩니다:

1. **🎯 Key Picks — 제품 출시 · 투자 우선** — 전체 수집분을 가로질러 **제품 출시**와 **투자** 신호로 채점해 가장 중요한 항목을 헤드라인으로 선별. 각 항목에 `투자`/`제품 출시` 태그 표시.
2. **⭐ Highlights** — 추천수(HN points 등) 기반 상위 항목.
3. **📌 소스별 섹션** — 소스마다 랭킹된 항목 목록.

### Key Picks 선별 방식 — LLM 또는 키워드 휴리스틱

`key_pick_mode`로 선택합니다:

| 모드 | 동작 |
|------|------|
| `heuristic` (기본) | 키워드 채점만 (API 호출 없음) |
| `auto` | **API 키가 있으면 LLM 선별**, 없으면 키워드 휴리스틱으로 자동 폴백 |
| `llm` | Claude 기반 선별 강제 (호출 실패 시에만 휴리스틱으로 폴백) |

> 기본은 `heuristic`입니다. LLM 선별을 쓰려면 요청에 `key_pick_mode="auto"`(또는 `"llm"`)를 주거나 `KEY_PICK_MODE=auto` 환경변수를 설정하세요.

리포트 헤드라인에 어느 방식이 쓰였는지 표기됩니다 — `(LLM 선별)` 또는 `(키워드 선별)`.

**① LLM 선별 (권장)** — Claude가 전체 항목을 읽고 **제품 출시·투자** 우선순위로 직접 골라 순위를 매기고, 각 항목에 한글 사유(`reason`)를 답니다. 키워드가 못 잡는 맥락(“$X 조달”이지만 표현이 특이한 경우 등)까지 판단합니다.

환경변수:
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."      # 또는 `ant auth login` 프로필
$env:KEY_PICK_MODEL    = "claude-opus-4-8" # 기본값. 비용을 낮추려면 claude-haiku-4-5 등
$env:KEY_PICK_MODE     = "auto"            # 기본 heuristic. LLM 쓰려면 auto/llm (요청 파라미터로도 지정 가능)
```
> 인증은 `ANTHROPIC_API_KEY` 또는 `ANTHROPIC_AUTH_TOKEN`, 아니면 `ant auth login` 프로필을 자동으로 사용합니다. 모델 기본값은 Claude Opus 4.8입니다.

**② 키워드 휴리스틱 (폴백)** — 결정론적 채점, API 불필요:

- **투자(funding, 가중치 3)** — `raise/raised $X`, `Series A~E`, `valuation`, `valued at`, `IPO`, `acquisition`, `venture capital`, `backed by`, 그리고 **백만/억 단위 금액**(`$18M`, `$2B` 등). `raise`류는 오탐 방지를 위해 금액·투자문맥이 함께 있을 때만 인정(“가격을 올렸다(raised prices)”는 제외).
- **제품 출시(launch, 가중치 2)** — `launch/introduces/unveils/releases/announces`, `now available`, `rolls out`, `beta`, `Show HN` 등. Product Hunt·Hacker News 항목은 본질적으로 출시라 기본 가점.

> ⚠️ 휴리스틱은 완벽하지 않습니다(예: MRR/가격 글이 드물게 투자로 오태깅). 정밀 선별이 필요하면 `auto`/`llm` 모드로 API 키를 설정해 LLM 선별을 쓰세요.

## 툴

| 툴 | 설명 |
|----|------|
| `digest_weekly_report` | 전체(또는 선택) 소스를 7일 윈도로 모아 리포트 생성. `sources`, `week_offset`, `per_source_limit`, `key_picks_count`, `key_pick_mode`, `highlights_count`, `response_format` |
| `digest_fetch_source` | 단일 소스만 조회(디버깅용) |
| `digest_list_sources` | 사용 가능한 소스 목록 |

- `key_picks_count`: 헤드라인 Key Picks 개수 (기본 8, `0`이면 숨김)
- `key_pick_mode`: `auto`(기본) | `llm` | `heuristic` — 위 "Key Picks 선별 방식" 참고
- `highlights_count`: 추천수 기반 하이라이트 개수 (기본 5, `0`이면 숨김)
- `response_format`: `markdown`(사람이 읽는 리포트) 또는 `json`(구조화 데이터, `key_picks` 배열에 `importance`·`tags` 포함)
- `week_offset`: `0`=최근 7일, `1`=그 전 주 …

## 설치

**요구사항**: Python 3.10+

```bash
git clone https://github.com/salwks/CreateTrend.git
cd CreateTrend

# 가상환경 생성 + 활성화
python -m venv .venv
# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 의존성 설치
pip install -r requirements.txt
# 또는 패키지로 설치(콘솔 스크립트 생성):
pip install -e .
```

동작 확인:

```bash
python -c "import server; print('ok')"
```

## Claude Desktop / Claude Code 등록

MCP 설정 파일(`claude_desktop_config.json` 등)의 `mcpServers`에 추가합니다. 아래 경로 두 곳(`<REPO>`)을 **clone한 절대 경로**로 바꿔주세요.

**Windows**
```json
{
  "mcpServers": {
    "weekly-product-digest": {
      "command": "<REPO>\\.venv\\Scripts\\python.exe",
      "args": ["<REPO>\\server.py"]
    }
  }
}
```

**macOS / Linux**
```json
{
  "mcpServers": {
    "weekly-product-digest": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["<REPO>/server.py"]
    }
  }
}
```

> venv를 안 쓰면 `command`를 시스템 `python`(또는 `python3`) 경로로 지정하고 전역에 의존성을 설치하세요.
> LLM 선별을 쓰려면 위 블록에 `"env": {"ANTHROPIC_API_KEY": "sk-ant-...", "KEY_PICK_MODE": "auto"}`를 추가하면 됩니다.

Claude Code CLI로 등록(경로는 실제 clone 위치로):

```bash
# Windows
claude mcp add weekly-product-digest -- "<REPO>\.venv\Scripts\python.exe" "<REPO>\server.py"
# macOS / Linux
claude mcp add weekly-product-digest -- "<REPO>/.venv/bin/python" "<REPO>/server.py"
```

## 사용 예 (등록 후 대화에서)

- "이번 주 제품 뉴스 리포트 뽑아줘" → `digest_weekly_report` 기본값
- "지난주 TechCrunch랑 Product Hunt만" → `sources=["techcrunch","producthunt"], week_offset=1`
- "슬라이드에 쓰게 JSON으로" → `response_format="json"`

## 커스터마이즈

- 소스 추가/변경: `server.py`의 `SOURCES` 딕셔너리 수정.
- Reddit 서브레딧 변경: `reddit` 소스의 `extra["subreddits"]`.
- Show HN 최소 추천수: `hackernews` 소스의 `extra["min_points"]`.

## 리포트 출력

생성된 주간 리포트는 `reports/` 폴더에 마크다운으로 쌓입니다(저장소에는 포함되지 않음 — `.gitignore` 처리). 한글 번역+요약본을 원하면 리포트를 뽑은 뒤 요청하세요.

## License

[MIT](LICENSE) © weekly-product-digest-mcp contributors
