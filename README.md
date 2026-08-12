# NU40DK 음악 반응형 LED

유튜브 링크를 주면 음원을 분석해서 NU40DK의 LED 4개를 파동에 맞춰 구동한다.

```
YouTube ──yt-dlp──▶ m4a ──afconvert──▶ WAV ──numpy STFT──▶ 4밴드
                                                             │
   USB 시리얼 ◀── 7바이트 프레임/60fps ◀── 대비 강화 체인 ◀──┘
      │
   NU40DK ──▶ 감마 LUT ──▶ 12비트 PWM ──▶ LED1~4
```

## 구성

| 파일 | 역할 |
|---|---|
| `nu40dk_music_led.ino` | 펌웨어. 프레임 수신 → 감마 보정 → PWM |
| `host/music_led.py` | CLI. 다운로드·분석·재생·시리얼 전송 |
| `host/server.py` | 웹 UI용 로컬 서버 |
| `host/notify_watch.py` | 맥 알림센터 감시. 지정한 알림에 LED 반응 |
| `host/web/index.html` | 웹 UI (글래스모피즘) |

## 사용법

```bash
cd ~/Documents/Arduino/nu40dk_music_led/host

# 보드 없이 터미널에서 확인
./.venv/bin/python music_led.py "<유튜브 링크>" --preview

# 분석 결과만 (튜닝용, 재생 안 함)
./.venv/bin/python music_led.py "<유튜브 링크>" --analyze-only

# 보드로 재생
./.venv/bin/python music_led.py "<유튜브 링크>"

# 웹 UI
./.venv/bin/python server.py     # http://127.0.0.1:8765
```

펌웨어 업로드:
```bash
CLI="/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"
"$CLI" compile --fqbn nucode:nrf52:nu40dk ~/Documents/Arduino/nu40dk_music_led
"$CLI" upload  --fqbn nucode:nrf52:nu40dk -p /dev/cu.usbmodemXXXX ~/Documents/Arduino/nu40dk_music_led
```

## LED 대비를 어떻게 확보했나

"파동에 따라 빛나는 정도의 차이가 크지 않다"는 건 이 프로젝트의 기본 실패 모드다.
원인이 세 가지라서 한 군데만 고치면 해결되지 않는다.

**원인 1 — 현대 음악은 이미 압축되어 있다.**
상용 마스터링은 크레스트 팩터가 6~10dB 수준이라 진폭이 거의 안 움직인다.
원시 진폭을 그대로 PWM에 넣으면 LED가 중간 밝기에 붙어 있는다.

**원인 2 — 밴드별 절대 에너지 차이가 20~30dB다.**
베이스는 항상 세고 고역은 항상 약하다. 절대값을 쓰면 LED1은 늘 포화, LED4는 늘 꺼짐.

**원인 3 — 눈의 밝기 감각은 로그 스케일이다.**
PWM 듀티 50%는 눈에 약 73% 밝기로 보인다. 선형 매핑을 쓰면 위쪽 절반이 전부 비슷해 보인다.

대응은 6단계 체인이다. 각각이 다른 원인을 담당한다.

| 단계 | 위치 | 담당 |
|---|---|---|
| dB 변환 | `band_energy` | 원인 1 |
| 밴드별 롤링 정규화(AGC) | `enhance` (1) | 원인 2 ← **효과가 가장 큼** |
| 노이즈 게이트 | `enhance` (2) | 완전한 검정 확보 |
| 대비 확장 커브 | `enhance` (3) | 원인 1 |
| 온셋 펀치 | `enhance` (4) | 타격감 |
| 어택/디케이 엔벨로프 | `enhance` (5) | 눈의 잔상 시간 확보 |
| 감마 2.6 LUT + 12비트 PWM | 펌웨어 | 원인 3 |

AGC는 각 밴드가 **자기 최근 3초 구간의 하위 10%~상위 95%** 를 0과 1로 쓰게 만든다.
그래서 조용한 인트로에서도, 꽉 찬 후렴에서도 LED가 항상 전 범위를 쓴다.

감마와 12비트는 짝이다. 8비트에서 감마 2.6을 적용하면 어두운 쪽 계단이 심하게 튄다.
12비트(4096단계)로 올려야 어두운 구간이 매끄럽다. `analogWriteResolution(12)`.

## 더 강하게 / 더 약하게

여전히 밋밋하면 이 순서로 조절한다.

```bash
# 1순위: 게이트를 올린다. 어두운 구간이 확실히 꺼진다
--gate 0.20

# 2순위: 확장 지수를 올린다. 중간값이 위아래로 벌어진다
--expand 2.0

# 3순위: 잔광을 줄인다. 뭉개짐이 사라지고 또렷해진다
--decay 0.12

# 타격감을 더 원하면
--punch 0.6
```

너무 깜빡여서 산만하면 반대로 `--gate 0.05 --expand 1.2 --decay 0.25`.

곡 전체가 잔잔해서 AGC가 노이즈를 증폭하는 것 같으면 `--min-span` 을 올린다(기본 6dB).

## 알아둘 것

- **오디오는 반드시 m4a(AAC)로 받는다.** macOS의 `afconvert`는 WebM/Opus를 못 읽는다.
  기본 `bestaudio`는 webm을 고르기 때문에 포맷 셀렉터에서 m4a를 강제한다. ffmpeg 불필요.
- **yt-dlp는 `host/bin/`의 단독 실행 바이너리를 쓴다.** 시스템 파이썬이 3.9라
  pip으로는 구버전만 깔리고, 구버전은 유튜브 봇 차단에 403으로 막힌다.
  차단이 재발하면 이 바이너리만 다시 받으면 된다:
  ```bash
  curl -L -o host/bin/yt-dlp \
    https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos
  chmod +x host/bin/yt-dlp
  ```
- **이 보드에서 `Serial`을 쓰려면 `#include <Adafruit_TinyUSB.h>` 가 필요하다.**
  없으면 `undefined reference to 'Serial'` 링크 에러가 난다.
- **브라우저는 유튜브 iframe의 소리를 분석할 수 없다.** 크로스 오리진이라 Web Audio
  접근이 막힌다. 그래서 웹 UI는 단독으로 못 돌고 `server.py`가 분석을 맡는다.
- 웹 UI에서 소리와 LED가 어긋나면 `--latency`(CLI) 또는 서버의 sync 보정을 조절한다.
  `afplay` 시작 지연 때문에 기본값 0.25초를 잡아 두었다.

## 카톡 알림에 LED 반응시키기

지정한 사람에게 메시지가 오면 LED가 6초간 알림 패턴(전체 3회 점멸 → 좌에서 우로 훑기)을
낸다. 음악 재생 중이어도 알림이 우선한다. 알리는 게 목적이라 그 몇 초는 독점해야 한다.

`server.py`를 켜면 감시 스레드가 같이 뜬다. 대상은 `server.py` 상단에서 바꾼다.

```python
WATCH_RULES = [
    notify_watch.Rule("%kakao%", ["길동"], "카톡"),
]
```

이름은 **부분 일치**다. `'길동'`은 `'홍길동'`, `'길동🌸'`에도 걸린다. 카톡 표시 이름은
꾸며진 경우가 많아서 완전 일치로 걸면 놓친다. 이름 목록을 비우면 그 앱의 모든 알림에 반응한다.

### 앱은 가리지 않는다

카톡 앱에 접근하는 게 아니라 **맥 알림센터 DB를 읽는다.** 그래서 macOS 알림을 띄우는
앱이면 종류를 안 가린다. 버블 같은 다른 앱을 추가하려면 번들 ID 패턴으로 한 줄 더 넣으면 된다.

```bash
# 알림을 띄운 적 있는 앱 목록 (번들 ID 확인용)
./.venv/bin/python notify_watch.py --apps

# LED 없이 감지되는지만 터미널에서 확인
./.venv/bin/python notify_watch.py 길동
./.venv/bin/python notify_watch.py --app %bubble%
```

새 앱을 붙이는 순서: 앱을 깔고 → 알림을 하나 받고 → `--apps`로 번들 ID를 확인하고 →
`WATCH_RULES`에 추가한다.

### 안 되는 경우 (중요)

알림센터에 레코드가 남아야 감지된다. 그래서 다음은 **원리적으로 불가능하다.**

- **앱이 꺼져 있으면 안 된다.** 카톡 PC는 앱이 떠 있을 때만 메시지를 받는다. 맥이 아예
  수신하지 않은 메시지는 어떤 방법으로도 감지할 수 없다. 알림 DB 방식의 한계가 아니라
  이 구성 자체의 한계다.
- **알림이 꺼져 있으면 안 된다.** 시스템 알림 설정, 앱 내 알림 설정, 채팅방별 음소거
  셋 중 하나라도 꺼져 있으면 레코드가 생기지 않는다.
- **그룹채팅은 발신자를 못 가린다.** 실측 결과 카톡은 `titl`에 **방 이름**을 넣고 `subt`는
  비워 두며, 본문에도 발신자 접두어가 없다. 즉 그룹방에서 특정 인물만 골라내는 건 불가능하다.
  1:1 대화이거나, 방 이름에 그 이름이 들어 있어야 걸린다.

### 알아둘 것

- **전체 디스크 접근 권한이 필요하다.** 없으면 `authorization denied`가 난다.
  시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근에서 터미널(또는 실행 주체)을 켠다.
  권한이 없으면 감시 스레드가 안내를 찍고 조용히 종료한다. 서버 나머지는 정상 동작한다.
- **DB를 열 때 `immutable=1`을 쓰면 안 된다.** 파일이 변하지 않는다고 SQLite에 알리는
  것이라 WAL에 새로 쌓인 알림이 안 보인다. 폴링에는 치명적이라 `mode=ro`를 쓴다.
  일회성 조회에는 `immutable=1`이 편하지만 그 습관을 여기 들고 오면 조용히 실패한다.
- **이 DB는 알림센터에 현재 남아 있는 알림만 보관한다.** 사용자가 알림을 지우면 레코드도
  사라진다. 실제로 이 프로젝트 작업 중에 31건이 0건으로 비워졌다가 다시 33건으로 찼다.
  과거 기록 조회용으로 쓸 수 없다.
- **중복 발화는 `rec_id` 워터마크로 막는다.** `delivered_date`는 시계 변경에 취약하고
  레코드 삭제에도 안전하지 않다. `rec_id`는 INTEGER PRIMARY KEY라 단조 증가한다.
- 서버를 켤 때의 최대 `rec_id`를 기준선으로 잡는다. 그래서 이미 쌓여 있던 알림에는
  반응하지 않는다. 서버 켤 때마다 LED가 터지면 곤란하다.
- 패턴만 확인하려면 카톡 없이도 쏠 수 있다: `curl -X POST http://127.0.0.1:8765/api/alert`

## 프로토콜

60fps, 7바이트 고정 프레임.

```
0xAA  0x55  B1  B2  B3  B4  CHK
             └── LED1~4 밝기 0~255 ──┘
CHK = (B1+B2+B3+B4) & 0xFF
```

밝기는 **감각 기준 8비트**로 보내고, 감마 → 12비트 변환은 펌웨어의 LUT가 한다.
덕분에 링크 대역폭은 420 B/s면 충분하고, 감마 곡선은 펌웨어에서만 바꾸면 된다.

프레임이 400ms간 끊기면 페이드 아웃, 4초간 끊기면 숨쉬기 애니메이션으로 넘어간다.
