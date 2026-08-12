#!/usr/bin/env python3
"""
music_led.py — 유튜브 링크의 음원을 4밴드로 분석해서 NU40DK의 LED 4개를 구동한다.

파이프라인:
  YouTube --yt-dlp--> m4a --afconvert--> wav --numpy STFT--> 4밴드 에너지
    -> dB 변환 -> 밴드별 롤링 정규화(AGC) -> 노이즈 게이트 -> 대비 확장
    -> 어택/디케이 엔벨로프 -> 온셋 펀치 -> 8비트 레벨 -> 시리얼 스트리밍

"육안으로 확실한 차이"를 만드는 건 감마 하나가 아니라 이 체인 전체다.
자세한 이유는 README.md 참고.

사용:
  ./music_led.py "https://youtu.be/..."            # 보드로 재생
  ./music_led.py "https://youtu.be/..." --preview  # 보드 없이 터미널 미리보기
"""

from __future__ import annotations  # Python 3.9에서 str|None 표기 허용

import argparse
import math
import os
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

CACHE = Path(__file__).resolve().parent / ".cache"
FPS = 60.0
FFT_SIZE = 2048
SPECTRUM_BINS = 128  # 웹 UI 파형용. LED 4채널과는 별개.
                     # 레퍼런스처럼 촘촘한 막대를 그리려면 32로는 너무 성기다.

# (이름, 저역 Hz, 고역 Hz) — LED1..LED4 순서
BANDS = [
    ("BASS", 20, 160),
    ("LOW ", 160, 800),
    ("MID ", 800, 4000),
    ("HIGH", 4000, 16000),
]

SYNC1, SYNC2 = 0xAA, 0x55


# --------------------------------------------------------------------------
# 1. 음원 확보: 유튜브 다운로드 -> WAV 디코딩
# --------------------------------------------------------------------------

def fetch_audio(url: str, venv_bin: Path,
                known_id: str | None = None,
                known_title: str | None = None) -> tuple[Path, str]:
    """유튜브에서 오디오를 받아 캐시에 저장하고 (파일경로, 제목)을 반환."""
    CACHE.mkdir(exist_ok=True)
    # 단독 실행 바이너리를 우선 쓴다. 시스템 파이썬이 3.9라 pip으로 깔리는
    # yt-dlp는 구버전에 묶이고, 구버전은 유튜브 봇 차단에 403으로 막힌다.
    standalone = venv_bin.parent.parent / "bin" / "yt-dlp"
    ytdlp = standalone if standalone.exists() else venv_bin / "yt-dlp"
    if not ytdlp.exists():
        sys.exit(f"yt-dlp를 찾을 수 없음: {ytdlp}")

    # 제목/ID를 먼저 조회해서 캐시 히트를 판단한다
    # id와 제목을 이미 알고 있으면 조회 호출을 통째로 건너뛴다.
    # yt-dlp 단독 바이너리는 실행할 때마다 자체 파이썬을 푸느라 몇 초씩 걸려서,
    # 호출 한 번을 줄이는 것만으로도 체감이 크다. (서버는 목록에서 이미 안다)
    if known_id:
        vid, title = known_id, (known_title or known_id)
    else:
        meta = subprocess.run(
            [str(ytdlp), "--no-warnings", "--print", "%(id)s\t%(title)s", url],
            capture_output=True, text=True,
        )
        if meta.returncode != 0:
            sys.exit(f"유튜브 정보 조회 실패:\n{meta.stderr.strip()}")
        vid, title = meta.stdout.strip().split("\t", 1)

    # 이미 받아둔 게 있으면 재사용
    for existing in CACHE.glob(f"{vid}.*"):
        if existing.suffix != ".wav":
            return existing, title

    print(f"  다운로드 중: {title}")
    out = subprocess.run(
        # 반드시 AAC(m4a)를 받아야 한다. macOS CoreAudio(afconvert)는 WebM/Opus를
        # 못 읽어서, 기본 bestaudio가 webm을 고르면 디코딩 단계에서 실패한다.
        # 유튜브 m4a는 DASH 조각 파일이라 조각을 동시에 받으면 훨씬 빠르다
        [str(ytdlp), "--no-warnings", "--concurrent-fragments", "8",
         "-f", "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/best[ext=mp4]/best",
         "-o", str(CACHE / f"{vid}.%(ext)s"), url],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"다운로드 실패:\n{out.stderr.strip()}")

    files = [f for f in CACHE.glob(f"{vid}.*") if f.suffix != ".wav"]
    if not files:
        sys.exit("다운로드된 오디오 파일을 찾을 수 없음")
    return files[0], title


def decode_wav(src: Path) -> tuple[np.ndarray, int]:
    """macOS 내장 afconvert로 모노 16bit WAV 디코딩. ffmpeg 불필요."""
    wav_path = src.with_suffix(".wav")
    if not wav_path.exists():
        r = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@44100", "-c", "1",
             str(src), str(wav_path)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            sys.exit(f"afconvert 디코딩 실패:\n{r.stderr.strip()}")

    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return samples, sr


# --------------------------------------------------------------------------
# 2. 스펙트럼 분석 -> 4밴드 원시 에너지
# --------------------------------------------------------------------------

def stft(samples: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """프레임별 크기 스펙트럼과 주파수 축을 반환. 무거우니 한 번만 돌린다."""
    hop = int(round(sr / FPS))
    n_frames = max(1, (len(samples) - FFT_SIZE) // hop)
    window = np.hanning(FFT_SIZE).astype(np.float32)

    # 프레임을 겹쳐서 한 번에 잘라낸다 (메모리 뷰라 복사 없음)
    idx = np.arange(FFT_SIZE)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = samples[idx] * window

    spec = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / sr)
    return spec, freqs


def band_energy(samples: np.ndarray, sr: int,
                precomputed: tuple | None = None) -> tuple[np.ndarray, np.ndarray]:
    """STFT를 돌려 프레임별 4밴드 파워(dB)와 스펙트럴 플럭스를 반환.

    precomputed로 (spec, freqs)를 넘기면 STFT를 재계산하지 않는다.
    """
    spec, freqs = precomputed if precomputed is not None else stft(samples, sr)
    n_frames = spec.shape[0]

    power = np.empty((n_frames, len(BANDS)), dtype=np.float32)
    for i, (_, lo, hi) in enumerate(BANDS):
        mask = (freqs >= lo) & (freqs < hi)
        # 파워 합이 아니라 평균을 쓴다. 밴드 폭이 8배씩 차이나서
        # 합을 쓰면 넓은 고역 밴드가 폭 때문에 유리해진다.
        power[:, i] = (spec[:, mask] ** 2).mean(axis=1)

    db = 10.0 * np.log10(power + 1e-12)

    # 스펙트럴 플럭스: 직전 프레임 대비 에너지 증가분 = 타격감(온셋)
    diff = np.diff(spec, axis=0, prepend=spec[:1])
    flux = np.maximum(diff, 0).sum(axis=1)
    flux = flux / (flux.max() + 1e-9)

    return db, flux.astype(np.float32)


def spectrum_levels(spec: np.ndarray, freqs: np.ndarray,
                    n_bins: int = SPECTRUM_BINS) -> np.ndarray:
    """로그 간격 n_bins 밴드 스펙트럼을 0~255로. 웹 UI 파형 표시 전용.

    LED 4채널은 물리 출력이라 대비를 극단적으로 밀어야 하지만, 화면 파형은
    형태가 읽히는 게 목적이라 정규화를 훨씬 단순하게 간다.
    """
    edges = np.geomspace(30.0, 16000.0, n_bins + 1)
    n_frames = spec.shape[0]
    power = np.zeros((n_frames, n_bins), dtype=np.float32)

    for i in range(n_bins):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if not mask.any():
            # 저역 구간은 FFT 분해능보다 좁을 수 있다. 가장 가까운 빈 하나를 쓴다.
            mask = np.zeros(freqs.shape, dtype=bool)
            mask[np.argmin(np.abs(freqs - edges[i]))] = True
        power[:, i] = (spec[:, mask] ** 2).mean(axis=1)

    db = 10.0 * np.log10(power + 1e-12)

    # 빈별 전역 정규화: 곡 전체 기준 하위 15% ~ 상위 97%를 0..1로 편다
    lo = np.percentile(db, 15.0, axis=0)
    hi = np.percentile(db, 97.0, axis=0)
    y = np.clip((db - lo) / np.maximum(hi - lo, 6.0), 0.0, 1.0)
    y = np.power(y, 1.3)

    # 짧은 잔광으로 프레임 간 떨림을 없앤다
    decay = math.exp(-1.0 / (FPS * 0.10))
    for t in range(1, n_frames):
        np.maximum(y[t], y[t - 1] * decay, out=y[t])

    return np.clip(y * 255.0, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# 3. 대비 강화 체인 — 여기가 이 프로그램의 핵심
# --------------------------------------------------------------------------

def rolling_percentile(x: np.ndarray, win: int, q: float, step: int = 8) -> np.ndarray:
    """롤링 백분위수. step 간격으로만 계산하고 사이를 선형보간해 속도를 확보."""
    n = len(x)
    pad = win // 2
    padded = np.pad(x, pad, mode="edge")
    picks = np.arange(0, n, step)
    view = np.lib.stride_tricks.sliding_window_view(padded, win)[picks]
    vals = np.percentile(view, q, axis=1)
    return np.interp(np.arange(n), picks, vals).astype(np.float32)


def enhance(db: np.ndarray, flux: np.ndarray, args) -> np.ndarray:
    """dB 밴드 에너지를 0..1 LED 밝기로. 대비를 최대한 벌린다."""
    n_frames, n_bands = db.shape
    win = max(8, int(args.agc_window * FPS))
    out = np.zeros_like(db)

    for b in range(n_bands):
        x = db[:, b]

        # (1) 밴드별 롤링 정규화(AGC).
        #     베이스는 고역보다 절대 에너지가 20~30dB 높다. 절대값을 쓰면
        #     베이스 LED는 항상 포화되고 고역 LED는 영영 안 켜진다.
        #     각 밴드가 자기 최근 구간의 하위/상위값을 0과 1로 쓰게 만든다.
        lo = rolling_percentile(x, win, args.floor_pct)
        hi = rolling_percentile(x, win, args.ceil_pct)
        span = np.maximum(hi - lo, args.min_span)  # 조용한 구간 증폭 폭주 방지
        y = np.clip((x - lo) / span, 0.0, 1.0)

        # (2) 노이즈 게이트. 완전한 검정이 있어야 대비가 눈에 읽힌다.
        #     게이트가 없으면 LED가 늘 어중간하게 켜져 있어 밋밋해 보인다.
        y = np.where(y < args.gate, 0.0, (y - args.gate) / (1.0 - args.gate))

        # (3) 대비 확장 커브. 중간값을 위아래로 밀어낸다.
        y = np.power(y, args.expand)

        out[:, b] = y

    # (4) 온셋 펀치: 타격 순간 전 밴드를 순간적으로 끌어올린다.
    if args.punch > 0:
        out = np.clip(out + args.punch * flux[:, None] * (1.0 - out), 0.0, 1.0)

    # (5) 어택/디케이 엔벨로프. 상승은 즉시, 하강은 천천히.
    #     눈이 인식하려면 피크가 최소 수십 ms 유지돼야 하고,
    #     이 잔광이 "파도치는" 느낌을 만든다.
    decay = math.exp(-1.0 / (FPS * args.decay))
    for t in range(1, n_frames):
        prev = out[t - 1] * decay
        out[t] = np.maximum(out[t], prev)

    return out


def to_levels(env: np.ndarray, mode: str) -> np.ndarray:
    """0..1 엔벨로프를 0..255 정수 레벨로. 감마는 펌웨어의 LUT가 담당."""
    if mode == "wave":
        # 저역 에너지를 LED1->LED4로 흘려보내 파도처럼 전파시킨다
        shifted = np.zeros_like(env)
        for i in range(env.shape[1]):
            lag = i * 3  # 프레임 단위 지연 = LED당 50ms
            src = env[:, 0]
            shifted[:, i] = np.concatenate([np.zeros(lag, np.float32), src])[:len(src)]
        env = np.maximum(env * 0.35, shifted)
    return np.clip(env * 255.0, 0, 255).astype(np.uint8)


def analyze_track(samples: np.ndarray, sr: int, args,
                  mode: str = "bands") -> tuple[np.ndarray, np.ndarray]:
    """LED 4채널 레벨과 화면용 32밴드 스펙트럼을 한 번에. STFT는 한 번만 돈다."""
    spec, freqs = stft(samples, sr)
    db, flux = band_energy(samples, sr, precomputed=(spec, freqs))
    levels = to_levels(enhance(db, flux, args), mode)
    spectrum = spectrum_levels(spec, freqs)
    return levels, spectrum


# --------------------------------------------------------------------------
# 4. 출력: 시리얼 스트리밍 / 터미널 미리보기
# --------------------------------------------------------------------------

def find_port() -> str | None:
    import glob
    for pat in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


def frame_bytes(level: np.ndarray) -> bytes:
    b = [int(v) for v in level]
    return bytes([SYNC1, SYNC2, *b, sum(b) & 0xFF])


BLOCKS = " ▁▂▃▄▅▆▇█"


def render_row(level: np.ndarray, t: float) -> str:
    cells = []
    for (name, _, _), v in zip(BANDS, level):
        bar = "█" * int(v / 255 * 24)
        cells.append(f"{name} {BLOCKS[int(v / 255 * 8)]} {bar:<24}")
    return f"\r{t:6.1f}s  " + " ".join(cells)


def play(levels: np.ndarray, audio: Path, args):
    ser = None
    if not args.preview:
        import serial
        port = args.port or find_port()
        if not port:
            sys.exit("보드를 찾을 수 없음. USB로 연결했는지 확인하거나 "
                     "--preview 로 터미널 미리보기를 쓰세요.")
        ser = serial.Serial(port, 115200, timeout=0)
        print(f"  시리얼 연결: {port}")
        time.sleep(0.3)  # CDC 열림 안정화

    proc = None
    if not args.no_audio:
        proc = subprocess.Popen(["afplay", str(audio)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    t0 = time.monotonic() + args.latency
    try:
        for i, level in enumerate(levels):
            target = t0 + i / FPS
            delay = target - time.monotonic()
            if delay < -0.1:
                continue      # 밀렸으면 프레임을 버려 오디오와 동기 유지
            if delay > 0:
                time.sleep(delay)

            if ser:
                ser.write(frame_bytes(level))
            if args.preview or args.verbose:
                sys.stdout.write(render_row(level, i / FPS))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n중단됨")
    finally:
        if ser:
            ser.write(frame_bytes(np.zeros(4, np.uint8)))  # 소등
            ser.close()
        if proc:
            proc.terminate()
        print()


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="유튜브 음원에 맞춰 NU40DK LED 4개를 구동")
    p.add_argument("url", help="유튜브 링크 또는 로컬 오디오 파일 경로")
    p.add_argument("--preview", action="store_true", help="보드 없이 터미널로 미리보기")
    p.add_argument("--analyze-only", action="store_true",
                   help="재생 없이 분석 통계만 출력 (튜닝용)")
    p.add_argument("--port", help="시리얼 포트 (기본: 자동 탐지)")
    p.add_argument("--no-audio", action="store_true", help="소리 재생 없이 LED만")
    p.add_argument("--verbose", action="store_true", help="보드 구동 중에도 막대 표시")
    p.add_argument("--mode", choices=["bands", "wave"], default="bands",
                   help="bands=밴드별 직결, wave=저역이 LED를 타고 흐름")
    p.add_argument("--latency", type=float, default=0.25,
                   help="afplay 시작 지연 보정 초 (LED가 빠르면 늘리세요)")

    g = p.add_argument_group("대비 튜닝")
    g.add_argument("--gate", type=float, default=0.12,
                   help="이 값 미만은 완전 소등 (0~1, 높일수록 대비 강함)")
    g.add_argument("--expand", type=float, default=1.5,
                   help="대비 확장 지수 (>1 일수록 강함)")
    g.add_argument("--decay", type=float, default=0.18, help="잔광 시상수(초)")
    g.add_argument("--punch", type=float, default=0.35, help="타격 강조량 (0~1)")
    g.add_argument("--agc-window", type=float, default=3.0, help="AGC 창 길이(초)")
    g.add_argument("--floor-pct", type=float, default=10.0, help="0으로 볼 백분위")
    g.add_argument("--ceil-pct", type=float, default=95.0, help="1로 볼 백분위")
    g.add_argument("--min-span", type=float, default=6.0,
                   help="정규화 최소 dB 폭 (무음 구간 노이즈 증폭 방지)")

    args = p.parse_args()
    venv_bin = Path(__file__).resolve().parent / ".venv" / "bin"

    local = Path(args.url)
    if local.exists():
        audio, title = local, local.name
    else:
        audio, title = fetch_audio(args.url, venv_bin)

    print(f"  분석 중: {title}")
    samples, sr = decode_wav(audio)
    db, flux = band_energy(samples, sr)
    env = enhance(db, flux, args)
    levels = to_levels(env, args.mode)

    dur = len(levels) / FPS
    used = (levels > 8).mean() * 100
    print(f"  {len(levels)}프레임 / {dur:.0f}초 / 점등률 {used:.0f}%")
    for i, (name, _, _) in enumerate(BANDS):
        col = levels[:, i]
        print(f"    LED{i+1} {name}  평균 {col.mean():5.1f}  "
              f"피크 {col.max():3d}  소등 {(col < 8).mean()*100:4.1f}%")

    if args.analyze_only:
        # 시간축을 40칸으로 압축해 곡 전체의 밝기 분포를 한눈에 본다
        step = max(1, len(levels) // 40)
        print("\n  곡 전체 윤곽 (좌:시작 우:끝)")
        for i, (name, _, _) in enumerate(BANDS):
            row = "".join(BLOCKS[min(8, int(v / 255 * 8))]
                          for v in levels[::step, i][:40])
            print(f"    LED{i+1} {name} |{row}|")
        return

    play(levels, audio, args)


if __name__ == "__main__":
    main()
