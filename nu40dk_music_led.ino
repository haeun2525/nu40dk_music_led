#include <Adafruit_TinyUSB.h>

// LED 핀 배열
const uint8_t LEDS[4] = {PIN_LED1, PIN_LED2, PIN_LED3, PIN_LED4};

// 감마 LUT (256 항목, 12비트 출력)
static uint16_t gammaLut[256];

// 상태 변수
uint8_t current[4] = {0, 0, 0, 0};     // 현재 밝기 레벨
uint32_t lastFrameMs = 0;              // 마지막 프레임 수신 시간
uint32_t lastFadeMs = 0;               // 마지막 페이드 업데이트

// 바이트 상태머신
uint8_t frameBuffer[4];
uint8_t frameIndex = 0;

// 보드 버튼 4개. 눌리면 호스트로 "B1"~"B4" 한 줄을 올려보낸다.
// 무엇을 할지는 호스트가 정한다 (다음곡/음량 등은 브라우저 쪽 기능이라
// 펌웨어는 "몇 번이 눌렸다"만 알리는 게 맞다).
const uint8_t BUTTONS[4] = {PIN_BUTTON1, PIN_BUTTON2, PIN_BUTTON3, PIN_BUTTON4};
uint8_t btnState[4] = {HIGH, HIGH, HIGH, HIGH};
uint32_t btnChangedMs[4] = {0, 0, 0, 0};
const uint32_t BTN_DEBOUNCE_MS = 40;

void setup() {
  // LED 핀 설정 (OUTPUT, active-HIGH)
  for (int i = 0; i < 4; i++) {
    pinMode(LEDS[i], OUTPUT);
    digitalWrite(LEDS[i], LOW);
  }

  // 버튼 핀 설정 (active-LOW, 내부 풀업)
  for (int i = 0; i < 4; i++) {
    pinMode(BUTTONS[i], INPUT_PULLUP);
  }

  // PWM 해상도 12비트 (0-4095)
  analogWriteResolution(12);

  // 감마 LUT 초기화 (감마 2.6)
  for (int i = 0; i < 256; i++) {
    gammaLut[i] = (uint16_t)(pow((float)i / 255.0f, 2.6f) * 4095.0f + 0.5f);
  }

  // Serial 초기화 (115200 baud)
  Serial.begin(115200);

  // Serial 준비 대기 (최대 2초)
  uint32_t startMs = millis();
  while (!Serial && (millis() - startMs) < 2000) {
    delay(10);
  }

  lastFrameMs = millis();
  lastFadeMs = lastFrameMs;
}

void loop() {
  uint32_t now = millis();

  // 버튼 스캔 (눌리는 순간에만 한 줄 전송, 40ms 디바운스)
  for (int i = 0; i < 4; i++) {
    uint8_t s = digitalRead(BUTTONS[i]);
    if (s != btnState[i] && (now - btnChangedMs[i]) > BTN_DEBOUNCE_MS) {
      btnState[i] = s;
      btnChangedMs[i] = now;
      if (s == LOW) {          // 눌림 (풀업이라 LOW가 눌린 상태)
        Serial.print('B');
        Serial.println(i + 1);
      }
    }
  }

  // 프로토콜 파싱: 바이트 단위 상태머신
  // 프레임: 0xAA 0x55 [B1 B2 B3 B4] [checksum]
  while (Serial.available()) {
    uint8_t b = Serial.read();

    if (frameIndex == 0) {
      // 첫 번째 동기 바이트 0xAA 대기
      if (b == 0xAA) frameIndex = 1;
    } else if (frameIndex == 1) {
      // 두 번째 동기 바이트 0x55 확인
      if (b == 0x55) frameIndex = 2;
      else if (b == 0xAA) frameIndex = 1;  // 0xAA 연속 시 동기 유지
      else frameIndex = 0;
    } else if (frameIndex < 6) {
      // 밝기 데이터 4바이트 (인덱스 2-5)
      frameBuffer[frameIndex - 2] = b;
      frameIndex++;
    } else {
      // 체크섬 바이트 (인덱스 6)
      uint8_t expected = (frameBuffer[0] + frameBuffer[1] + frameBuffer[2] + frameBuffer[3]) & 0xFF;
      if (b == expected) {
        // 유효한 프레임: 모든 채널 감마 LUT을 통해 갱신
        for (int i = 0; i < 4; i++) {
          current[i] = frameBuffer[i];
          analogWrite(LEDS[i], gammaLut[current[i]]);
        }
        lastFrameMs = now;
      }
      frameIndex = 0;
    }
  }

  // 타임아웃 처리
  uint32_t elapsed = now - lastFrameMs;

  if (elapsed > 4000) {
    // 4초 이상: 호흡 애니메이션 (4초 주기)
    // 각 LED는 1/4 주기씩 위상 오프셋
    float t = (now % 4000) / 1000.0f;  // 0-4 seconds
    for (int ch = 0; ch < 4; ch++) {
      float phase = t + (ch * 0.25f);
      float breath = (sinf(phase * 2.0f * 3.14159f) + 1.0f) / 2.0f;  // 0-1 범위
      uint8_t level = (uint8_t)(breath * 128.0f);
      analogWrite(LEDS[ch], gammaLut[level]);
    }
  } else if (elapsed > 400) {
    // 400ms 이상: 페이드 아웃 (10ms마다 4씩 감소)
    if ((now - lastFadeMs) >= 10) {
      for (int i = 0; i < 4; i++) {
        if (current[i] > 4) {
          current[i] -= 4;
        } else {
          current[i] = 0;
        }
        analogWrite(LEDS[i], gammaLut[current[i]]);
      }
      lastFadeMs = now;
    }
  }
}
