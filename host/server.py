#!/usr/bin/env python3
"""
server.py — 유튜브 링크로 NU40DK LED를 구동하는 웹 서버

웹 UI에서 유튜브 링크를 붙여넣고, YouTube IFrame API로 브라우저에서 재생하면
별도로 음원을 다운로드해 분석해서 4채널 LED 프레임을 USB 직렬로 스트리밍한다.
브라우저 재생 위치와 직렬 스트리밍을 /api/sync로 동기화한다.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import threading
import time
import types
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import numpy as np

# 맥 알림센터를 감시해서 지정한 알림에 LED를 반응시킨다
import notify_watch

# music_led 모듈에서 분석 함수와 상수를 가져온다
from music_led import (
    BANDS,
    CACHE,
    FPS,
    SPECTRUM_BINS,
    SYNC1,
    SYNC2,
    analyze_track,
    band_energy,
    decode_wav,
    enhance,
    fetch_audio,
    find_port,
    frame_bytes,
    to_levels,
)

# --------------------------------------------------------------------------
# 설정
# --------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 8765
WEB_DIR = Path(__file__).resolve().parent / "web"
VENV_BIN = Path(__file__).resolve().parent / ".venv" / "bin"
YTDLP = Path(__file__).resolve().parent / "bin" / "yt-dlp"
# 제목을 메모리에만 두면 서버를 껐다 켤 때마다 잃는다. 그러면 곡 이름 자리에
# 영상 id가 뜬다. 오디오는 .cache에 남아 재분석도 안 하니 스스로 회복되지도
# 않는다. 파일로 남겨서 재시작을 넘긴다.
TITLES_FILE = Path(__file__).resolve().parent / ".cache" / "titles.json"
PLAYLIST_LIMIT = 50   # 자동 믹스/라디오는 사실상 무한이라 앞에서 끊는다

# 알림 감시 규칙: 여기 걸리는 알림이 오면 LED로 알린다.
# 앱 무관 구조다. 다른 앱을 추가하려면 번들 ID 패턴으로 한 줄 더 넣으면 된다.
# 번들 ID는 `python notify_watch.py --apps`로 확인한다.
#   예) notify_watch.Rule("%bubble%", [], "버블")  ← 그 앱의 모든 알림
WATCH_RULES = [
    notify_watch.Rule("%kakao%", ["길동"], "카톡"),
]
ALERT_SECONDS = 6.0   # 알림 패턴을 유지하는 시간

# --------------------------------------------------------------------------
# 모듈 상태: 직렬 스트리밍 스레드가 접근
# --------------------------------------------------------------------------

state_lock = threading.Lock()

# 앞으로 재생할 비디오 ID, 시각 기준점, 벽시계, 재생 중 여부
sync_id: str | None = None
sync_time: float = 0.0
sync_walltime: float = 0.0
sync_playing: bool = False

# 캐시된 레벨 배열: {video_id -> ndarray shape (n_frames, 4), uint8}
def ensure_playable(src: Path) -> Path:
    """브라우저 <audio>가 재생할 수 있는 파일을 만들어 그 경로를 돌려준다.

    유튜브가 주는 m4a(format 140)는 sidx/moof/mdat로 조각화된 DASH 파일이라
    moov에 샘플 테이블이 없다. MSE 없이는 <audio>가 재생하지 못하고 readyState가
    0에서 멈춘다. afconvert로 일반 MP4 컨테이너에 다시 담아 해결한다.
    분석용 WAV와 달리 스테레오를 유지해야 하므로 채널 수는 건드리지 않는다.
    """
    out = src.with_name(src.stem + "_play.m4a")
    if out.exists():
        return out

    r = subprocess.run(
        ["afconvert", "-f", "m4af", "-d", "aac", str(src), str(out)],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and out.exists():
        return out

    # 변환이 실패하면 분석 단계에서 만들어 둔 WAV로 물러선다(모노지만 확실히 재생됨)
    wav = src.with_suffix(".wav")
    return wav if wav.exists() else src


levels_cache: dict[str, np.ndarray] = {}
spectrum_cache: dict[str, np.ndarray] = {}   # 화면 파형용 32밴드
playlist_titles: dict[str, str] = {}         # 목록에서 얻은 제목 (조회 호출 절약)
prefetching: set[str] = set()                # 미리 받는 중인 영상 id
levels_meta: dict[str, dict[str, Any]] = {}  # {video_id -> {"title": str, "duration": float}}
titles_lock = threading.Lock()


def load_titles() -> None:
    """디스크에 남겨둔 제목을 메모리로 올린다. 서버 시작 때 한 번."""
    try:
        data = json.loads(TITLES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            playlist_titles.update({k: v for k, v in data.items()
                                    if isinstance(v, str) and v and v != k})
    except Exception:
        pass   # 파일이 없거나 깨졌으면 빈 상태로 시작한다


def remember_title(vid: str, title: str) -> None:
    """제목을 알게 되면 메모리와 디스크에 같이 남긴다."""
    if not vid or not title or title == vid:
        return
    with titles_lock:
        if playlist_titles.get(vid) == title:
            return
        playlist_titles[vid] = title
        try:
            TITLES_FILE.parent.mkdir(exist_ok=True)
            TITLES_FILE.write_text(
                json.dumps(playlist_titles, ensure_ascii=False, indent=0),
                encoding="utf-8")
        except Exception:
            pass   # 저장 실패로 재생까지 막지는 않는다


_title_lookups: set[str] = set()


def _kick_title_lookup(vid: str) -> None:
    """제목 조회를 백그라운드로 돌린다. 곡당 한 번만."""
    with titles_lock:
        if vid in _title_lookups:
            return
        _title_lookups.add(vid)

    def _run() -> None:
        title = resolve_title(vid)
        if title and levels_meta.get(vid):
            levels_meta[vid]["title"] = title

    threading.Thread(target=_run, daemon=True).start()


def resolve_title(vid: str) -> str:
    """제목을 모르는 곡의 이름을 yt-dlp로 물어본다. 실패하면 빈 문자열."""
    if not vid:
        return ""
    known = playlist_titles.get(vid)
    if known:
        return known
    try:
        r = subprocess.run(
            [str(YTDLP), "--no-warnings", "--no-playlist", "--skip-download",
             "--print", "%(title)s", f"https://www.youtube.com/watch?v={vid}"],
            capture_output=True, text=True, timeout=45,
        )
        title = r.stdout.strip().split("\n")[0] if r.returncode == 0 else ""
    except Exception:
        title = ""
    remember_title(vid, title)
    return title


# 현재 표시할 LED 레벨과 시리얼 연결 상태
current_levels: np.ndarray = np.zeros(4, dtype=np.uint8)
current_spectrum: np.ndarray = np.zeros(SPECTRUM_BINS, dtype=np.uint8)
ledtest_until: float = 0.0   # 이 시각까지는 LED 테스트 패턴을 내보낸다
alert_until: float = 0.0     # 이 시각까지는 카톡 알림 패턴을 내보낸다
pending_alert: dict[str, Any] | None = None   # 웹 UI가 한 번 가져가면 비운다

# 보드 버튼 -> 브라우저로 넘길 이벤트 큐. /api/levels 응답에 실어 보내고 비운다.
pending_events: list[str] = []
serial_rx_buf: bytes = b""
b2_pending_at: float = 0.0        # 2번 버튼 첫 눌림 시각 (더블클릭 판정용)
B2_DOUBLE_WINDOW = 0.40           # 이 안에 다시 눌리면 '이전 곡'


def _handle_button(num: int) -> None:
    """보드 버튼 눌림을 브라우저용 이벤트로 바꾼다. state_lock 안에서 호출."""
    global b2_pending_at
    if num == 1:
        pending_events.append("toggle")
    elif num == 2:
        # 한 번은 다음 곡, 두 번 연속은 이전 곡.
        # 첫 눌림은 바로 내보내지 않고 두 번째를 기다린다.
        now = time.monotonic()
        if b2_pending_at and (now - b2_pending_at) <= B2_DOUBLE_WINDOW:
            b2_pending_at = 0.0
            pending_events.append("prev")
        else:
            b2_pending_at = now
    elif num == 3:
        pending_events.append("vol_up")
    elif num == 4:
        pending_events.append("vol_down")
serial_connected: bool = False


def trigger_alert(hit: dict[str, Any]) -> None:
    """
    카톡 알림 감지 → LED 알림 패턴 시작. 감시 스레드에서 호출한다.

    연달아 오면 매번 타이머를 새로 늘린다. 대화가 이어지는 동안 계속 빛난다.
    """
    global alert_until, pending_alert
    with state_lock:
        alert_until = time.monotonic() + ALERT_SECONDS
        pending_alert = {
            "rule": hit.get("rule", ""),
            "sender": hit.get("sender", ""),
            "body": hit.get("body", ""),
            "at": hit.get("at", 0.0),
        }
    print(f"[알림] {hit.get('rule', '')} {hit.get('sender', '')} — {hit.get('body', '')}")


# 각 비디오 ID별 준비 중 잠금: 동시 다운로드 방지
prepare_locks: dict[str, threading.Lock] = {}


# --------------------------------------------------------------------------
# 유틸리티
# --------------------------------------------------------------------------

def get_prepare_lock(vid: str) -> threading.Lock:
    """비디오별 준비 잠금을 가져온다. 없으면 새로 만든다."""
    global prepare_locks
    if vid not in prepare_locks:
        prepare_locks[vid] = threading.Lock()
    return prepare_locks[vid]


def resolve_playlist(url: str) -> list[dict[str, str]]:
    """
    yt-dlp로 플레이리스트/비디오 URL을 분석해 트랙 목록을 반환한다.
    각 트랙은 {"id": "VIDEOID", "title": "...", "thumb": "https://..."} 형태.
    """
    try:
        result = subprocess.run(
            # --playlist-end 없이 두면 유튜브 자동 믹스/라디오(list=RD...)에서
            # 끝없이 열거하다가 타임아웃 난다. 앞부분만 가져온다.
            [str(YTDLP), "--no-warnings", "--flat-playlist",
             "--playlist-end", str(PLAYLIST_LIMIT),
             "--print", "%(id)s\t%(title)s", url],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp 실패: {result.stderr.strip()}")

        tracks = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            vid, title = parts
            thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            remember_title(vid, title)
            tracks.append({"id": vid, "title": title, "thumb": thumb})
        if tracks:
            return tracks
        raise RuntimeError("목록이 비어 있음")
    except Exception as e:
        # 목록 열거가 실패해도 URL에 v=<id>가 있으면 그 곡 하나만이라도 살린다.
        # 믹스/라디오 링크에서 흔한 상황이고, 사용자가 원한 건 대개 그 곡이다.
        single = _single_video_from_url(url)
        if single:
            return single
        raise RuntimeError(f"플레이리스트 분석 실패: {str(e)}")


def _single_video_from_url(url: str) -> list[dict[str, Any]] | None:
    """URL에서 영상 id 하나만 뽑아 단일 트랙 목록으로 만든다."""
    try:
        q = urllib.parse.urlparse(url)
        vid = urllib.parse.parse_qs(q.query).get("v", [None])[0]
        if not vid and q.netloc.endswith("youtu.be"):
            vid = q.path.lstrip("/") or None
        if not vid:
            return None

        r = subprocess.run(
            [str(YTDLP), "--no-warnings", "--no-playlist",
             "--print", "%(title)s", f"https://www.youtube.com/watch?v={vid}"],
            capture_output=True, text=True, timeout=45,
        )
        title = r.stdout.strip().split("\n")[0] if r.returncode == 0 else vid
        remember_title(vid, title)        # 이 경로로 들어와도 제목을 기억해둔다
        return [{
            "id": vid,
            "title": title or vid,
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        }]
    except Exception:
        return None


def analyze_video(vid: str) -> dict[str, Any]:
    """
    비디오를 다운로드/분석해서 레벨 배열을 캐시하고 메타데이터를 반환한다.
    각 비디오별 잠금으로 동시 요청을 직렬화한다.
    """
    lock = get_prepare_lock(vid)
    with lock:
        # 이미 캐시되어 있으면 빠르게 반환
        if vid in levels_cache:
            meta = levels_meta[vid]
            return {
                "ready": True,
                "duration": meta["duration"],
                "title": meta["title"],
            }

        # 다운로드 및 분석
        try:
            url = f"https://www.youtube.com/watch?v={vid}"
            # 목록에서 이미 제목을 알고 있으면 yt-dlp 조회 호출을 건너뛴다
            hint = playlist_titles.get(vid)
            audio_path, title = fetch_audio(url, VENV_BIN,
                                            known_id=vid, known_title=hint)

            samples, sr = decode_wav(audio_path)

            # 기본 enhance 파라미터로 사용하는 args 객체
            args = types.SimpleNamespace(
                gate=0.12, expand=1.5, decay=0.18, punch=0.35,
                agc_window=3.0, floor_pct=10.0, ceil_pct=95.0, min_span=6.0,
            )
            # LED 4채널과 화면 파형용 32밴드를 한 번의 STFT로 같이 얻는다
            levels, spectrum = analyze_track(samples, sr, args, "bands")

            # 캐시에 저장
            duration = len(levels) / FPS
            levels_cache[vid] = levels
            spectrum_cache[vid] = spectrum
            levels_meta[vid] = {
                "title": title,
                "duration": duration,
                # 브라우저가 재생할 수 있는 형태로 변환해서 넘긴다
                "path": str(ensure_playable(audio_path)),
            }
            # 제목을 못 구해 id가 들어간 경우. 아는 제목이 있으면 그걸 쓰고,
            # 그것도 없으면 유튜브에 직접 물어본다. 여기서 포기하면 곡 이름
            # 자리에 영상 id가 그대로 박힌 채 캐시에 굳는다.
            if title == vid:
                better = resolve_title(vid)
                if better:
                    title = better
                    levels_meta[vid]["title"] = better
            else:
                remember_title(vid, title)

            return {
                "ready": True,
                "duration": duration,
                "title": title,
            }
        except Exception as e:
            raise RuntimeError(f"분석 실패: {str(e)}")


# --------------------------------------------------------------------------
# 직렬 스트리밍 스레드
# --------------------------------------------------------------------------

def alert_frame(remaining: float) -> np.ndarray:
    """
    카톡 알림 패턴. 1초를 주기로 '4개 동시 3번 깜빡임 → 좌에서 우로 훑기'를 반복한다.

    음악 패턴과 확실히 구분되게 만든 것이다. 음악은 밴드별로 밝기가 제각각
    흔들리는 데 반해, 이건 전부 같이 켜졌다 꺼지므로 곁눈으로도 알아본다.
    """
    level = np.zeros(4, dtype=np.uint8)
    phase = remaining % 1.0

    if phase > 0.4:
        # 앞 0.6초: 0.1초 간격으로 전체 점멸 (켬-끔 3회)
        if int((phase - 0.4) / 0.1) % 2 == 0:
            level[:] = 255
    else:
        # 뒤 0.4초: 한 개씩 훑는다. phase가 정확히 0.4일 때 음수 인덱스로
        # 감기지 않도록 막는다
        level[3 - min(3, int(phase / 0.1))] = 255

    return level


def serial_loop():
    """
    60 fps 루프: 동기화된 재생 위치에서 LED 프레임을 직렬로 스트리밍한다.
    보드를 찾지 못하면 5초마다 재시도한다.
    """
    global serial_connected, current_levels, current_spectrum
    global sync_id, sync_time, sync_walltime, sync_playing, ledtest_until
    global serial_rx_buf, b2_pending_at, alert_until

    import serial

    ser = None
    last_retry = time.monotonic()
    frame_interval = 1.0 / FPS

    while True:
        try:
            frame_start = time.monotonic()

            # LED 테스트: 재생과 무관하게 4개를 차례로 훑는다
            if frame_start < ledtest_until:
                step = int((ledtest_until - frame_start) * 4) % 4
                level = np.zeros(4, dtype=np.uint8)
                level[3 - step] = 255
                with state_lock:
                    current_levels = level.copy()
                if ser:
                    try:
                        ser.write(frame_bytes(level))
                    except Exception:
                        ser = None
                        serial_connected = False
                time.sleep(frame_interval)
                continue

            # 카톡 알림: 음악 재생 중이어도 알림이 이긴다. 알리는 게 목적이라
            # 이 몇 초는 LED를 독점해야 눈에 띈다
            if frame_start < alert_until:
                level = alert_frame(alert_until - frame_start)
                with state_lock:
                    current_levels = level.copy()
                if ser:
                    try:
                        ser.write(frame_bytes(level))
                    except Exception:
                        ser = None
                        serial_connected = False
                time.sleep(frame_interval)
                continue

            # 포트 재시도는 논블로킹으로 한다. 보드가 없어도 프레임 계산은
            # 계속 돌아야 한다. 여기서 sleep/continue로 빠지면 /api/levels가
            # 5초에 한 번만 갱신돼서 웹 UI의 화면 시각화가 끊긴다.
            if ser is None and frame_start - last_retry >= 5.0:
                last_retry = frame_start
                port = find_port()
                if port:
                    try:
                        # write_timeout이 없으면 보드가 USB를 안 비울 때
                        # ser.write가 영원히 막힌다. 60fps 루프가 통째로 멎어
                        # LED도 파동도 같이 죽는다. 짧게 끊고 포트를 다시 잡는다.
                        ser = serial.Serial(port, 115200, timeout=0,
                                            write_timeout=0.2)
                        serial_connected = True
                    except Exception:
                        ser = None
                        serial_connected = False

            # 현재 재생 위치 계산
            with state_lock:
                if sync_playing and sync_id and sync_id in levels_cache:
                    elapsed = time.monotonic() - sync_walltime
                    current_time = sync_time + elapsed
                    frame_index = int(current_time * FPS)
                    levels_array = levels_cache[sync_id]

                    # 프레임 범위를 벗어나면 영점
                    if 0 <= frame_index < len(levels_array):
                        level = levels_array[frame_index]
                        spec_array = spectrum_cache.get(sync_id)
                        if spec_array is not None and frame_index < len(spec_array):
                            spec_frame = spec_array[frame_index]
                        else:
                            spec_frame = np.zeros(SPECTRUM_BINS, dtype=np.uint8)
                    else:
                        level = np.zeros(4, dtype=np.uint8)
                        spec_frame = np.zeros(SPECTRUM_BINS, dtype=np.uint8)
                else:
                    level = np.zeros(4, dtype=np.uint8)
                    spec_frame = np.zeros(SPECTRUM_BINS, dtype=np.uint8)

                current_levels = level.copy()
                current_spectrum = spec_frame.copy()

            # 보드가 올려보낸 버튼 이벤트 수신 ("B1"~"B4" 한 줄씩)
            if ser:
                try:
                    waiting = ser.in_waiting
                    if waiting:
                        serial_rx_buf += ser.read(waiting)
                    while b"\n" in serial_rx_buf:
                        line, _, serial_rx_buf = serial_rx_buf.partition(b"\n")
                        line = line.strip()
                        if len(line) == 2 and line[0:1] == b"B" and line[1:2].isdigit():
                            with state_lock:
                                _handle_button(int(line[1:2]))
                except Exception:
                    pass   # 수신 실패로 LED 출력까지 죽이지는 않는다

            # 2번 버튼 첫 눌림 후 더블클릭 시간이 지나면 '다음 곡'으로 확정
            with state_lock:
                if b2_pending_at and (frame_start - b2_pending_at) > B2_DOUBLE_WINDOW:
                    b2_pending_at = 0.0
                    pending_events.append("next")

            # 직렬 포트가 열려 있으면 전송
            if ser:
                try:
                    ser.write(frame_bytes(current_levels))
                except Exception:
                    # 직렬 포트 끊김(또는 쓰기 타임아웃): 다음 재시도 대기로.
                    # 밀린 출력 버퍼를 비우지 않으면 close에서 또 막힌다.
                    try:
                        ser.reset_output_buffer()
                    except Exception:
                        pass
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                    serial_connected = False

            # 프레임 레이트 유지
            elapsed = time.monotonic() - frame_start
            sleep_time = max(0, frame_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        except Exception:
            # 예상 밖의 에러도 처리: 스레드를 살려둔다
            if ser:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                serial_connected = False
            time.sleep(0.1)


# --------------------------------------------------------------------------
# HTTP 요청 핸들러
# --------------------------------------------------------------------------

# 제목에 붙는 홍보 문구. 팝업은 좁아서 이게 남으면 곡 이름이 잘린다.
_NOISE = re.compile(
    r"\s*[\(\[【][^)\]】]*"
    r"(?:official|mv|m/v|video|audio|lyrics?|visuali[sz]er|hd|4k|remaster(?:ed)?"
    r"|가사|뮤직비디오|공식)"
    r"[^)\]】]*[\)\]】]",
    re.IGNORECASE,
)
# 괄호 없이 뒤에 붙는 꼬리표. 끝에 있을 때만 떼야 곡 이름을 다치지 않는다.
_TAIL = re.compile(
    r"\s*[-–—|]?\s*(?:m/?v|official\s+(?:music\s+)?video|official\s+audio|lyric\s+video)\s*$",
    re.IGNORECASE,
)
_SEPARATORS = (" - ", " – ", " — ", " ‐ ", " ~ ")


def split_artist_title(raw: str) -> tuple[str, str]:
    """유튜브 제목을 (아티스트, 곡 이름)으로 나눈다.

    유튜브는 아티스트를 따로 주지 않는다. 업로더 이름은 레이블 채널인 경우가
    많아서 오히려 더 틀린다. 그래서 제목의 관용 표기 'Artist - Title'을 쓴다.
    나뉘지 않으면 아티스트를 비우고 제목만 보여준다. 잘못 쪼개서 곡 이름이
    사라지는 것보다 아티스트가 없는 편이 낫다.
    """
    title = _TAIL.sub("", _NOISE.sub("", raw or "")).strip(" -–—·|")
    if not title:
        return "", raw or ""

    for sep in _SEPARATORS:
        if sep in title:
            left, right = title.split(sep, 1)
            left, right = left.strip(), right.strip()
            # 왼쪽이 지나치게 길면 아티스트가 아니라 문장이 쪼개진 것이다
            if left and right and len(left) <= 40:
                return left, right
            break

    return "", title


class MusicLEDHandler(BaseHTTPRequestHandler):
    """웹 UI와 API 엔드포인트를 처리한다."""

    def log_message(self, format, *args):
        """로그 메시지를 억제한다 (30 req/s 스팸 방지)."""
        pass

    def do_GET(self):
        """GET 요청을 처리한다."""
        global current_levels, serial_connected

        if self.path == "/":
            # 홈페이지: web/index.html
            html_path = WEB_DIR / "index.html"
            if html_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_path.read_bytes())
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"index.html not found")

        elif self.path == "/popup":
            # 데스크톱 팝업 위젯: web/popup.html
            html_path = WEB_DIR / "popup.html"
            if html_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_path.read_bytes())
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"popup.html not found")

        elif self.path == "/api/now":
            # 지금 재생 중인 곡. 팝업 위젯이 폴링한다.
            with state_lock:
                vid = sync_id
                playing = sync_playing
                base = sync_time
                walltime = sync_walltime
                levels_list = [int(v) for v in current_levels]
                spectrum_list = [int(v) for v in current_spectrum]

            # 브라우저는 재생 중 1초마다 위치를 보낸다. 그게 끊겼다는 것은 탭이
            # 닫혔거나 멈췄다는 뜻이다. 그대로 두면 있지도 않은 재생이 영원히
            # 앞으로 흐른다. 유령 트랙을 띄우느니 재생 아님으로 본다.
            age = time.monotonic() - walltime
            if playing and age > 3.0:
                playing = False

            # 마지막 보고 이후 흐른 시간만큼 채워 넣는다
            position = base + age if playing else base

            meta = levels_meta.get(vid or "", {})
            # 제목을 모른 채 분석되면 levels_meta에 영상 id가 제목으로 남는다.
            # 나중에 목록에서 진짜 제목을 알게 되므로 그쪽을 우선한다.
            raw_title = meta.get("title") or ""
            if not raw_title or raw_title == vid:
                raw_title = playlist_titles.get(vid or "") or raw_title
            if vid and raw_title == vid:
                # 아직도 id뿐이다. 팝업 폴링을 붙잡아둘 수는 없으니 조회는
                # 뒤로 넘기고, 이번 응답에는 id 대신 빈 제목을 보낸다.
                # 팝업이 "트랙 준비 중"을 띄우고, 다음 폴링에서 진짜 이름이 온다.
                _kick_title_lookup(vid)
                raw_title = ""
            duration = float(meta.get("duration") or 0.0)
            if duration:
                position = max(0.0, min(position, duration))

            artist, title = split_artist_title(raw_title)
            response = {
                "id": vid,
                "title": title,
                "artist": artist,
                "playing": bool(playing and vid),
                "position": position,
                "duration": duration,
                "levels": levels_list,
                "spectrum": spectrum_list if playing else [],
                "serial": serial_connected,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        elif self.path.startswith("/api/thumb/"):
            # 썸네일 프록시
            vid = self.path[len("/api/thumb/"):]
            # hqdefault는 4:3 캔버스에 검은 레터박스 띠가 구워져 있어서
            # 앨범 커버로 쓰면 위아래 검은 띠가 그대로 보인다. 띠가 없는
            # 16:9 원본을 먼저 시도하고 없을 때만 hqdefault로 내려간다.
            data = None
            for name in ("maxresdefault", "hq720", "sddefault", "hqdefault"):
                try:
                    url = f"https://i.ytimg.com/vi/{vid}/{name}.jpg"
                    with urllib.request.urlopen(url, timeout=5) as response:
                        data = response.read()
                    break
                except Exception:
                    continue

            if data:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

        elif self.path.startswith("/api/audio/"):
            # 받아둔 오디오 파일을 그대로 스트리밍한다. 브라우저 <audio>가
            # 이걸 재생하므로 유튜브 iframe이 필요 없다.
            # Range 응답이 없으면 시크(스크럽)가 동작하지 않으니 반드시 지원한다.
            vid = self.path[len("/api/audio/"):]
            meta = levels_meta.get(vid)
            path = Path(meta["path"]) if meta and "path" in meta else None
            if path is None or not path.exists():
                # 서버 재시작으로 메타가 비었으면 캐시에서 직접 찾는다.
                # 원본 m4a는 조각화되어 재생이 안 되므로 변환본을 우선한다.
                for cand in (CACHE / f"{vid}_play.m4a", CACHE / f"{vid}.wav"):
                    if cand.exists():
                        path = cand
                        break

            if path is None or not path.exists():
                self.send_response(404)
                self.end_headers()
                return

            size = path.stat().st_size
            ctype = {".m4a": "audio/mp4", ".mp4": "audio/mp4",
                     ".wav": "audio/wav"}.get(path.suffix, "audio/mpeg")
            rng = self.headers.get("Range")

            start, end = 0, size - 1
            partial = False
            if rng and rng.startswith("bytes="):
                spec = rng[len("bytes="):].split(",")[0].strip()
                lo, _, hi = spec.partition("-")
                try:
                    if lo:
                        start = int(lo)
                        if hi:
                            end = min(int(hi), size - 1)
                    elif hi:                       # bytes=-N (마지막 N바이트)
                        start = max(0, size - int(hi))
                    partial = True
                except ValueError:
                    partial = False
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return

            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()

            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass   # 브라우저가 시크하면서 연결을 끊는 건 정상이다

        elif self.path == "/api/levels":
            # 현재 LED 레벨 (즉시 반환, 블로킹 없음)
            global pending_alert
            with state_lock:
                levels_list = [int(v) for v in current_levels]
                spectrum_list = [int(v) for v in current_spectrum]
                events = pending_events[:]      # 보드 버튼 이벤트를 넘기고 비운다
                pending_events.clear()
                alert, pending_alert = pending_alert, None
            response = {
                "levels": levels_list,
                "spectrum": spectrum_list,
                "serial": serial_connected,
                "events": events,
                "alert": alert,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        else:
            # 정적 파일: web/ 디렉토리
            file_path = WEB_DIR / self.path.lstrip("/")
            if file_path.exists() and file_path.is_file():
                # 보안: web 디렉토리 안에만 접근 가능
                try:
                    file_path.resolve().relative_to(WEB_DIR.resolve())
                except ValueError:
                    self.send_response(403)
                    self.end_headers()
                    return

                self.send_response(200)
                # 간단한 MIME 타입
                if file_path.suffix == ".js":
                    mime = "application/javascript"
                elif file_path.suffix == ".css":
                    mime = "text/css"
                elif file_path.suffix == ".json":
                    mime = "application/json"
                else:
                    mime = "application/octet-stream"
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
            else:
                self.send_response(404)
                self.end_headers()

    def do_POST(self):
        """POST 요청을 처리한다."""
        global sync_id, sync_time, sync_walltime, sync_playing

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        if self.path == "/api/playlist":
            # 플레이리스트 분석
            try:
                url = data.get("url", "")
                if not url:
                    raise ValueError("url이 필요합니다")
                tracks = resolve_playlist(url)
                response = {"tracks": tracks}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        elif self.path == "/api/prepare":
            # 비디오 분석 준비 (느린 작업)
            try:
                vid = data.get("id", "")
                if not vid:
                    raise ValueError("id가 필요합니다")
                result = analyze_video(vid)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        elif self.path == "/api/prefetch":
            # 현재 곡이 재생되는 동안 다음 곡을 미리 받아 분석해 둔다.
            # 준비 시간의 96%가 다운로드라 이걸 미리 해두면 체감이 즉시가 된다.
            vid = data.get("id", "")
            if vid and vid not in levels_cache and vid not in prefetching:
                prefetching.add(vid)

                def _run(v=vid):
                    try:
                        analyze_video(v)
                    except Exception:
                        pass
                    finally:
                        prefetching.discard(v)

                threading.Thread(target=_run, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif self.path == "/api/ledtest":
            # 재생과 무관하게 LED 4개를 3초간 훑어서 배선을 확인한다
            global ledtest_until
            with state_lock:
                ledtest_until = time.monotonic() + 3.0
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "seconds": 3}).encode())

        elif self.path == "/api/alert":
            # 카톡이 오지 않아도 알림 패턴을 확인할 수 있게 하는 수동 트리거
            trigger_alert(
                {
                    "rule": "테스트",
                    "sender": data.get("sender", "테스트"),
                    "body": data.get("body", "알림 패턴 테스트"),
                    "at": time.time(),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"ok": True, "seconds": ALERT_SECONDS}).encode()
            )

        elif self.path == "/api/sync":
            # 재생 동기화 (즉시 반환)
            try:
                vid = data.get("id", "")
                playing = data.get("playing", False)
                time_sec = data.get("time", 0.0)

                with state_lock:
                    sync_id = vid
                    sync_time = time_sec
                    sync_walltime = time.monotonic()
                    sync_playing = playing

                response = {"ok": True}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())

        else:
            self.send_response(404)
            self.end_headers()


# --------------------------------------------------------------------------
# 메인
# --------------------------------------------------------------------------

def main():
    """서버를 시작한다."""
    global serial_connected

    # 지난 실행에서 알아낸 곡 제목을 되살린다
    load_titles()

    # 직렬 스트리밍 스레드 시작 (데몬 모드)
    serial_thread = threading.Thread(target=serial_loop, daemon=True)
    serial_thread.start()

    # 알림 감시 스레드. 권한이 없으면 스스로 안내를 찍고 조용히 끝난다
    if WATCH_RULES:
        threading.Thread(
            target=notify_watch.watch_loop,
            args=(WATCH_RULES, trigger_alert),
            daemon=True,
        ).start()

    # 초기 포트 탐지 대기 (최대 2초)
    for _ in range(20):
        time.sleep(0.1)
        if serial_connected:
            break

    # 서버 시작
    server = ThreadingHTTPServer((HOST, PORT), MusicLEDHandler)

    # 시작 메시지
    port_status = "NU40DK 보드를 탐지했습니다" if serial_connected else "NU40DK 보드를 탐지하지 못했습니다"
    print(f"서버 시작: http://{HOST}:{PORT}")
    print(f"상태: {port_status}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # 정리: LED를 영점하고 종료
        global current_levels
        with state_lock:
            current_levels = np.zeros(4, dtype=np.uint8)
        print("\n서버 종료")
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
