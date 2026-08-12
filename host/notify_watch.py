"""
맥 알림센터 DB를 폴링해서 지정한 알림을 감지한다.

앱이 macOS 알림을 띄우면 시스템이 알림센터 SQLite DB에 레코드를 남긴다.
그 레코드의 바이너리 plist에서 제목·본문을 꺼내 감시 규칙과 대조한다.

앱에 접근하는 게 아니라 알림센터를 읽는 것이라 앱 종류를 가리지 않는다.
카톡이든 버블이든 알림만 뜨면 규칙 한 줄로 추가된다.

한계: 해당 앱이 실행 중이 아니거나 알림이 꺼져 있으면 레코드 자체가 생기지
않으므로 감지할 수 없다. 자세한 내용은 README 참고.
"""

import plistlib
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Sequence

# 알림센터 DB. 전체 디스크 접근 권한이 있어야 읽을 수 있다.
NOTIF_DB = (
    Path.home()
    / "Library/Group Containers/group.com.apple.usernoted/db2/db"
)

# Apple 절대시간(2001-01-01 UTC 기준)을 유닉스 시각으로 옮기는 오프셋
APPLE_EPOCH_OFFSET = 978307200.0

POLL_INTERVAL = 1.5   # 초. 알림이 알림센터에 머무는 동안 놓치지 않을 만큼 잦게.
ERROR_BACKOFF = 10.0  # DB 읽기가 실패했을 때 다음 시도까지


class Rule:
    """
    감시 규칙 하나.

    app_like : `app.identifier`에 대고 쓸 SQL LIKE 패턴 (예: "%kakao%").
               번들 ID를 정확히 몰라도 되게 LIKE로 받는다.
    names    : 발신자 이름 목록. 비워 두면 그 앱의 모든 알림에 반응한다.
    label    : 로그와 웹 UI에 쓸 이름.
    """

    def __init__(self, app_like: str, names: Sequence[str] = (), label: str = ""):
        self.app_like = app_like
        self.names = [n.strip() for n in names if n and n.strip()]
        self.label = label or app_like

    def matches(self, title: str, subtitle: str) -> bool:
        """
        발신자가 이 규칙에 걸리는지 본다.

        부분 일치로 판정한다. 카톡 표시 이름은 '홍길동', '길동🌸'처럼 꾸며진
        경우가 많아서 완전 일치로 걸면 놓친다.
        names가 비어 있으면 앱만 맞으면 통과다.
        """
        if not self.names:
            return True
        haystack = f"{title}\n{subtitle}"
        return any(n in haystack for n in self.names)

    def describe(self) -> str:
        who = ", ".join(self.names) if self.names else "전체"
        return f"{self.label}({who})"


def _connect() -> sqlite3.Connection:
    """
    알림 DB를 읽기 전용으로 연다.

    `immutable=1`이 아니라 `mode=ro`를 쓴다. immutable은 파일이 변하지 않는다고
    SQLite에 알리는 것이라 WAL에 새로 쌓인 알림이 보이지 않는다. 폴링에는 치명적.
    """
    return sqlite3.connect(f"file:{NOTIF_DB}?mode=ro", uri=True, timeout=2.0)


def _fields_of(record_data: bytes) -> tuple[str, str, str]:
    """
    레코드의 바이너리 plist에서 (제목, 부제, 본문)을 꺼낸다.

    1:1 대화는 titl이 발신자 이름이다. 그룹 대화는 앱 버전에 따라 titl이 방
    이름이고 subt가 발신자일 수 있어서 둘 다 돌려준다.
    """
    plist: dict[str, Any] = plistlib.loads(record_data)
    req = plist.get("req", {}) or {}
    return (
        (req.get("titl") or "").strip(),
        (req.get("subt") or "").strip(),
        (req.get("body") or "").strip(),
    )


def latest_rec_id() -> int:
    """현재 DB의 최대 rec_id. 시작 시점의 기준선으로 쓴다."""
    conn = _connect()
    try:
        row = conn.execute("select max(rec_id) from record").fetchone()
        return row[0] or 0
    finally:
        conn.close()


def list_apps() -> list[tuple[str, int]]:
    """
    알림을 띄운 적이 있는 앱 목록. (identifier, 현재 레코드 수)

    새 앱의 번들 ID를 알아낼 때 쓴다. 버블 같은 앱을 깔고 알림을 하나 받은 뒤
    이걸 돌리면 규칙에 넣을 패턴이 바로 나온다.
    """
    conn = _connect()
    try:
        return conn.execute(
            """
            select a.identifier, count(r.rec_id)
            from app a left join record r on r.app_id = a.app_id
            group by a.identifier order by 2 desc, 1
            """
        ).fetchall()
    finally:
        conn.close()


def fetch_new(
    last_rec_id: int, rules: Sequence[Rule]
) -> tuple[int, list[dict[str, Any]]]:
    """
    last_rec_id 이후에 도착한 알림 중 규칙에 걸리는 것을 돌려준다.

    rec_id는 INTEGER PRIMARY KEY라 단조 증가하므로 이걸 워터마크로 쓴다.
    delivered_date를 쓰면 시계 변경에 취약하고 레코드 삭제에도 안전하지 않다.

    반환: (갱신된 워터마크, 걸린 알림 목록)
    """
    if not rules:
        return last_rec_id, []

    # 규칙의 앱 패턴을 OR로 묶어 한 번에 조회한다
    where = " or ".join("a.identifier like ?" for _ in rules)
    params = [r.app_like for r in rules] + [last_rec_id]

    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            select r.rec_id, r.data, r.delivered_date, a.identifier
            from record r
            join app a on r.app_id = a.app_id
            where ({where}) and r.rec_id > ?
            order by r.rec_id
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    watermark = last_rec_id
    hits: list[dict[str, Any]] = []

    for rec_id, data, delivered, identifier in rows:
        watermark = max(watermark, rec_id)
        if not data:
            continue
        try:
            title, subtitle, body = _fields_of(data)
        except Exception:
            continue   # plist가 깨졌거나 형식이 바뀐 레코드는 건너뛴다

        # 이 레코드를 실제로 담당하는 규칙을 찾는다. 앱 패턴을 OR로 조회했으니
        # 여기서 어느 규칙 소관인지 다시 확인해야 한다
        for rule in rules:
            like = rule.app_like.replace("%", "")
            if like and like.lower() not in identifier.lower():
                continue
            if rule.matches(title, subtitle):
                hits.append(
                    {
                        "rec_id": rec_id,
                        "app": identifier,
                        "rule": rule.label,
                        "sender": title,
                        "subtitle": subtitle,
                        "body": body,
                        "at": (delivered or 0.0) + APPLE_EPOCH_OFFSET,
                    }
                )
                break

    return watermark, hits


def watch_loop(
    rules: Sequence[Rule],
    on_match: Callable[[dict[str, Any]], None],
    interval: float = POLL_INTERVAL,
) -> None:
    """
    무한 폴링 루프. 데몬 스레드로 돌린다.

    시작 시점의 최대 rec_id를 기준선으로 잡아서, 이미 쌓여 있던 알림에는
    반응하지 않는다. 서버를 켤 때마다 LED가 터지면 곤란하다.
    """
    if not rules:
        return

    try:
        last_rec_id = latest_rec_id()
    except Exception as exc:
        print(f"[알림] 알림 DB를 열 수 없습니다: {exc}")
        print("[알림] 시스템 설정 > 개인정보 보호 > 전체 디스크 접근 권한을 확인하세요.")
        return

    print(f"[알림] 감시 시작: {', '.join(r.describe() for r in rules)}")

    while True:
        try:
            last_rec_id, hits = fetch_new(last_rec_id, rules)
            for hit in hits:
                on_match(hit)
            time.sleep(interval)
        except Exception as exc:
            # DB가 잠깐 잠겼거나 교체되는 중일 수 있다. 스레드는 살려둔다.
            print(f"[알림] 폴링 실패, {ERROR_BACKOFF:.0f}초 후 재시도: {exc}")
            time.sleep(ERROR_BACKOFF)


if __name__ == "__main__":
    # 단독 실행. LED는 건드리지 않고 감지되는지만 터미널에서 확인한다.
    #   python notify_watch.py --apps          알림 띄운 앱 목록 (번들 ID 조회용)
    #   python notify_watch.py 길동            카톡에서 그 이름 감시
    #   python notify_watch.py --app %bubble%  그 앱의 모든 알림 감시
    import sys

    argv = sys.argv[1:]

    if "--apps" in argv:
        for identifier, count in list_apps():
            print(f"{count:5d}  {identifier}")
        sys.exit(0)

    if "--app" in argv:
        i = argv.index("--app")
        watch_rules = [Rule(argv[i + 1], [], argv[i + 1])]
    else:
        watch_rules = [Rule("%kakao%", argv or ["길동"], "카톡")]

    watch_loop(
        watch_rules,
        lambda hit: print(
            f"[알림] {time.strftime('%H:%M:%S', time.localtime(hit['at']))} "
            f"[{hit['rule']}] {hit['sender']}: {hit['body']}"
        ),
    )
