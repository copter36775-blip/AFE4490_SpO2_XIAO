/*
 * =====================================================================
 *  REV 1 FIRMWARE - kept buildable, not dead code.
 *
 *  This is the USB-only firmware for the first wiring revision:
 *    D0 ADC_RDY   D1 MISO   D2 SCK   D3 MOSI   D4 CS   D5 RESET   D6 PWDN
 *
 *  Build it with:  pio run -e v1_usb -t upload
 *  Rev 2 (src/main.cpp) is the default environment.
 * =====================================================================
 * =====================================================================
 *  AFE4490_XIAO - pulse-oximeter front end, register-level control
 *  Target : Seeed XIAO nRF52840 Sense (Adafruit nRF52 BSP)
 *  Version: V1.0.0
 *  Pairs with: web/afe4490_studio.html   (link protocol "A1")
 * =====================================================================
 *
 *  WIRING (matches the project schematic, H3 on the AFE4490 breakout)
 *  ------------------------------------------------------------------
 *      H3.1  VCC       -> 3V3
 *      H3.12 GND       -> GND
 *      H3.11 DDRY      -> D0   ADC_RDY, one 4 MHz clock wide, rising edge
 *      H3.10 MISO      -> D1
 *      H3.9  SCK       -> D2
 *      H3.8  MOSI      -> D3
 *      H3.7  CS0       -> D4
 *      H3.6  START     -> D5   AFE RESET, active low
 *      H3.5  PDWN      -> D6   AFE_PDN,   active low
 *      H3.4/3/2 DIAG_END, LED_ALM, PD_ALM are left unconnected; the same
 *      information is available over SPI in the DIAG register (0x30).
 *
 *  Those SPI pins are NOT the XIAO's default SPI bus (D8/D9/D10), so the
 *  driver runs on its own SPIM instance. SPIM3 already backs the global
 *  `SPI` object and SPIM2 backs `SPI1` (the on-board IMU), which leaves
 *  SPIM1 - the same choice HECG01 made for its ADS1292R.
 *  SPIM1 shares a peripheral ID with TWIM1, so Wire1 must stay unused.
 *
 *  LINK PROTOCOL "A1"
 *  ------------------------------------------------------------------
 *  One USB CDC pipe carries two interleaved things:
 *
 *   * ASCII lines, '\n' terminated, for commands and replies. Pure
 *     7-bit, so a byte >= 0x80 can never appear inside one.
 *   * Binary sample frames, which always start 0xAA 0x55:
 *
 *       AA 55 | TYPE(1) | LEN(2 LE) | PAYLOAD(LEN) | CRC16(2 LE)
 *
 *     CRC16-CCITT (poly 0x1021, init 0xFFFF) over TYPE..PAYLOAD.
 *     TYPE 0x01 payload = seq(u32) count(u8) pad(u8) rate(u16)
 *                         then count x 6 x int32 LE, in register order
 *                         LED2, ALED2, LED1, ALED1, LED2-ALED2, LED1-ALED1.
 *
 *  Because 0xAA never occurs in the ASCII stream a receiver can switch
 *  between the two with a two-byte look-ahead and no escaping.
 *  `FMT:TEXT` swaps the binary frames for CSV so a plain serial monitor
 *  can be used while bringing the hardware up.
 */

#include <Arduino.h>
#include <SPI.h>
#include <Adafruit_TinyUSB.h>
#include "AFE4490.h"

// =====================================================================
// PIN MAP
// =====================================================================
static const uint8_t PIN_AFE_RDY   = D0;
static const uint8_t PIN_AFE_MISO  = D1;
static const uint8_t PIN_AFE_SCK   = D2;
static const uint8_t PIN_AFE_MOSI  = D3;
static const uint8_t PIN_AFE_CS    = D4;
static const uint8_t PIN_AFE_RESET = D5;   // breakout silkscreen "START"
static const uint8_t PIN_AFE_PWDN  = D6;

SPIClass SPI_AFE(NRF_SPIM1, PIN_AFE_MISO, PIN_AFE_SCK, PIN_AFE_MOSI);
AFE4490  afe(SPI_AFE, PIN_AFE_CS, PIN_AFE_RESET, PIN_AFE_PWDN, PIN_AFE_RDY,
             PIN_AFE_SCK, PIN_AFE_MOSI, /*onSpim3=*/false);

static const char FIRMWARE_VERSION[] = "V1.0.0";
static const char LINK_PROTOCOL[]    = "A1";

// =====================================================================
// ACQUISITION RING
// ---------------------------------------------------------------------
// The ISR only bumps a counter - it never touches SPI, so a slow USB
// host can never stall the interrupt or corrupt a register access.
// =====================================================================
static const uint16_t RING_SIZE = 64;
static AfeSample      ring[RING_SIZE];
static volatile uint16_t ringHead = 0;      // written by loop()
static uint16_t          ringTail = 0;      // read by the sender
static volatile uint32_t rdyPending = 0;
static volatile uint32_t rdyTotal   = 0;

static uint32_t seqCounter   = 0;
static uint32_t dropCount    = 0;   // ring overruns
static uint32_t txDropCount  = 0;   // frames abandoned because USB was full
static uint32_t missedRdy    = 0;   // ADC_RDY seen while still busy

static bool     streaming    = false;
static uint8_t  outputFormat = 0;   // 0 = binary, 1 = CSV text
static uint16_t decimation   = 1;
static uint16_t decCounter   = 0;
static uint32_t batchOpenedUs = 0;

static void onAdcReady() { rdyPending++; rdyTotal++; }

static volatile uint32_t probeEdges = 0;
static void onProbeEdge() { probeEdges++; }

// =====================================================================
// PULSE DETECTOR
// ---------------------------------------------------------------------
// Runs on the IR ambient-subtracted channel. Two things to know:
//
//  * The AFE's LED-minus-ambient registers come out inverted compared
//    with the waveforms in most publications (datasheet notes on
//    figures 128 and 129), and physically a systolic pressure wave means
//    more absorption and so LESS photocurrent. Both point the same way,
//    so systole is a DOWNWARD excursion and the detector negates first.
//
//  * The threshold tracks an envelope rather than being fixed, because
//    the perfusion index of a finger varies by two orders of magnitude
//    between subjects. A refractory window caps the rate at 200 bpm and
//    stops the dicrotic notch from being counted as a second beat.
// =====================================================================
struct PulseDetector {
  float    dc = 0, env = 0;
  bool     primed = false, armed = false;
  uint32_t lastBeatMs = 0;
  uint16_t bpm = 0;

  void reset() { primed = armed = false; env = 0; bpm = 0; lastBeatMs = 0; }

  bool update(int32_t x, uint32_t nowMs, uint16_t fs) {
    if (fs < 20) fs = 20;
    if (!primed) { dc = (float)x; primed = true; return false; }

    dc += ((float)x - dc) * (1.0f / (0.8f * fs));   // ~0.8 s baseline
    const float sys = dc - (float)x;                // positive at systole

    // Fast attack, slow decay: follows a fading signal without letting a
    // single motion spike raise the bar for the next few seconds.
    if (sys > env) env += (sys - env) * 0.20f;
    else           env += (sys - env) * (1.0f / (2.5f * fs));

    if (env < 150.0f) { bpm = 0; return false; }    // noise floor only

    const uint32_t dt = nowMs - lastBeatMs;
    if (!armed && sys < 0.30f * env) armed = true;
    if (armed && sys > 0.62f * env && dt > 300) {
      armed = false;
      if (lastBeatMs && dt < 3000) {
        const uint16_t inst = (uint16_t)(60000UL / dt);
        bpm = bpm ? (uint16_t)((bpm * 3 + inst) / 4) : inst;
      }
      lastBeatMs = nowMs;
      return true;
    }
    if (lastBeatMs && dt > 3000) bpm = 0;           // pulse lost
    return false;
  }
};
static PulseDetector pulse;

// =====================================================================
// STATUS LED
// ---------------------------------------------------------------------
// The XIAO's RGB LEDs are common-anode: driving a pin LOW lights it.
// The Seeed variant defines LED_STATE_ON as 1, which does not match the
// board, so the polarity is stated explicitly here rather than inherited.
//
//   nothing attached  -> white    (all three on)
//   USB host present  -> red
//   BLE connected     -> blue     (reserved; no BLE stack linked yet)
//   each detected beat-> a short green flash that replaces the base
//                        colour, so it reads as a clean blink
// =====================================================================
static const uint32_t BEAT_FLASH_MS = 45;
static uint32_t beatFlashUntil = 0;
static bool     bleConnected   = false;   // future BLE transport

/* serviceLeds() runs on every loop() iteration - tens of thousands of
 * times a second - so it only touches the GPIO when the colour actually
 * changes. Writing three pins unconditionally would burn real time in
 * the same loop that has to drain the ADC. */
static uint8_t ledLast = 0xFF;
static inline void ledSet(uint8_t rgb) {
  if (rgb == ledLast) return;
  ledLast = rgb;
  digitalWrite(LED_RED,   (rgb & 4) ? LOW : HIGH);
  digitalWrite(LED_GREEN, (rgb & 2) ? LOW : HIGH);
  digitalWrite(LED_BLUE,  (rgb & 1) ? LOW : HIGH);
}
static inline void ledWrite(bool r, bool g, bool b) {
  ledSet((uint8_t)((r ? 4 : 0) | (g ? 2 : 0) | (b ? 1 : 0)));
}

static void serviceLeds(uint32_t nowMs) {
  if (nowMs < beatFlashUntil)   { ledSet(0b010); return; }   // green flash
  if (bleConnected)             { ledSet(0b001); return; }   // blue
  if (TinyUSBDevice.mounted())  { ledSet(0b100); return; }   // red
  ledSet(0b111);                                             // white
}

// =====================================================================
// CRC16-CCITT
// =====================================================================
static uint16_t crc16(const uint8_t *data, size_t len, uint16_t crc = 0xFFFF) {
  while (len--) {
    crc ^= (uint16_t)(*data++) << 8;
    for (uint8_t i = 0; i < 8; i++)
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
  }
  return crc;
}

// =====================================================================
// USB OUTPUT
// =====================================================================
static inline bool usbReady() {
  return TinyUSBDevice.mounted() && Serial.dtr();
}

static void sendLine(const char *s) {
  if (!usbReady()) return;
  Serial.println(s);
}

static void sendLineF(const char *fmt, ...) {
  if (!usbReady()) return;
  char buf[220];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  Serial.println(buf);
}

/* 8 samples = 5 + 8 + 192 + 2 = 207 B, comfortably inside the 256-byte
 * CFG_TUD_CDC_TX_BUFSIZE. At 500 Hz that is ~62 frames/s instead of 500,
 * and 16 ms of buffering, which no one can see on a plethysmograph. */
static const uint8_t  SAMPLES_PER_FRAME = 8;
static const uint32_t BATCH_MAX_AGE_US  = 25000;   // ship early rather than stall

static void sendSampleFrame(const AfeSample *s, uint8_t count) {
  if (!usbReady() || count == 0) return;

  uint8_t body[8 + SAMPLES_PER_FRAME * 24];
  uint16_t n = 0;
  const uint32_t seq = seqCounter;
  body[n++] = (uint8_t)(seq);
  body[n++] = (uint8_t)(seq >> 8);
  body[n++] = (uint8_t)(seq >> 16);
  body[n++] = (uint8_t)(seq >> 24);
  body[n++] = count;
  body[n++] = 0;
  const uint16_t rate = afe.timing().prf / (decimation ? decimation : 1);
  body[n++] = (uint8_t)(rate);
  body[n++] = (uint8_t)(rate >> 8);

  for (uint8_t k = 0; k < count; k++) {
    const int32_t v[6] = { s[k].led2, s[k].aled2, s[k].led1,
                           s[k].aled1, s[k].led2_amb, s[k].led1_amb };
    for (uint8_t j = 0; j < 6; j++) {
      body[n++] = (uint8_t)(v[j]);
      body[n++] = (uint8_t)(v[j] >> 8);
      body[n++] = (uint8_t)(v[j] >> 16);
      body[n++] = (uint8_t)(v[j] >> 24);
    }
  }

  uint8_t head[5] = { 0xAA, 0x55, 0x01, (uint8_t)(n), (uint8_t)(n >> 8) };
  uint16_t crc = crc16(&head[2], 3);
  crc = crc16(body, n, crc);

  const uint16_t total = 5 + n + 2;
  // Never block the acquisition path waiting on a stalled host.
  if ((uint16_t)Serial.availableForWrite() < total) { txDropCount++; return; }

  Serial.write(head, 5);
  Serial.write(body, n);
  uint8_t tail[2] = { (uint8_t)(crc), (uint8_t)(crc >> 8) };
  Serial.write(tail, 2);
  seqCounter += count;
}

static void sendSampleText(const AfeSample &s) {
  if (!usbReady()) return;
  if (Serial.availableForWrite() < 80) { txDropCount++; return; }
  char buf[96];
  snprintf(buf, sizeof(buf), "D,%lu,%ld,%ld,%ld,%ld,%ld,%ld",
           (unsigned long)seqCounter,
           (long)s.led2, (long)s.aled2, (long)s.led1,
           (long)s.aled1, (long)s.led2_amb, (long)s.led1_amb);
  Serial.println(buf);
  seqCounter++;
}

// =====================================================================
// TELEMETRY
// =====================================================================
static float readBatteryVolts() {
  // XIAO nRF52840: P0.14 low enables the VBAT divider (1 M / 510 k).
  pinMode(VBAT_ENABLE, OUTPUT);
  digitalWrite(VBAT_ENABLE, LOW);
  analogReference(AR_INTERNAL_3_0);
  analogReadResolution(12);
  delay(2);
  const uint32_t raw = analogRead(PIN_VBAT);
  digitalWrite(VBAT_ENABLE, HIGH);
  return ((float)raw * 3.0f / 4096.0f) * 1510.0f / 510.0f;
}

static void sendStatus() {
  const AfeTiming &t = afe.timing();
  sendLineF("ST:fw=%s,proto=%s,stream=%u,fmt=%s,prf=%u,duty=%u,numav=%u,"
            "dec=%u,seq=%lu,rdy=%lu,drop=%lu,txdrop=%lu,miss=%lu,"
            "spihz=%lu,spidrive=%s,led1ma=%.2f,led2ma=%.2f,fs=%u,bpm=%u,"
            "vbat=%.2f,up=%lu",
            FIRMWARE_VERSION, LINK_PROTOCOL, streaming ? 1u : 0u,
            outputFormat ? "TEXT" : "BIN",
            t.prf, t.dutyPct, t.numav, decimation,
            (unsigned long)seqCounter, (unsigned long)rdyTotal,
            (unsigned long)dropCount, (unsigned long)txDropCount,
            (unsigned long)missedRdy, (unsigned long)afe.spiSpeed(),
            afe.driveStrength() ? "H0H1" : "S0S1",
            afe.ledCurrentMa((afe.shadow(AFE_LEDCNTRL) & AFE_LC_LED1_MSK) >> AFE_LC_LED1_SH),
            afe.ledCurrentMa((afe.shadow(AFE_LEDCNTRL) & AFE_LC_LED2_MSK) >> AFE_LC_LED2_SH),
            afe.ledFullScaleMa(), pulse.bpm, readBatteryVolts(),
            (unsigned long)(millis() / 1000UL));
}

static void sendCaps() {
  sendLineF("CAPS:PROTO=%s,FW=%s,CHIP=AFE4490,MCU=XIAO_NRF52840,"
            "CH=6,BITS=22,FSV=1.2,PRFMIN=62,PRFMAX=1000,DUTYMAX=25,"
            "REGS=0x30,AGC=1,DIAG=1,SELFTEST=1,PINTEST=1,SPITEST=1,"
            "SPIMAX=%lu,SPISTEPS=125000|250000|500000|1000000|2000000|4000000|8000000,"
            "FMT=BIN|TEXT,BLE=0,LED=RGB,PULSE=1,BATCH=8",
            LINK_PROTOCOL, FIRMWARE_VERSION, (unsigned long)afe.maxSpiHz());
}

static void dumpRegisters() {
  afe.refreshShadow();
  char buf[32];
  for (uint8_t a = 0x00; a <= AFE_REG_MAX; a++) {
    snprintf(buf, sizeof(buf), "REG:%02X=%06lX", a, (unsigned long)afe.shadow(a));
    sendLine(buf);
    // CONTROL0 is write-only: report the driver's shadow, flagged as such.
  }
  sendLine("REG:END");
}

static void reportDiag(uint32_t flags) {
  sendLineF("DIAG:%06lX", (unsigned long)flags);
  if (flags == 0) { sendLine("DIAG:OK"); return; }
  struct { uint32_t bit; const char *name; } tbl[] = {
    { AFE_DG_PD_ALM,    "PD_ALM"    }, { AFE_DG_LED_ALM,  "LED_ALM"   },
    { AFE_DG_LED1OPEN,  "LED1_OPEN" }, { AFE_DG_LED2OPEN, "LED2_OPEN" },
    { AFE_DG_LEDSC,     "LED_SHORT" }, { AFE_DG_OUTPSHGND,"TXP_SH_GND"},
    { AFE_DG_OUTNSHGND, "TXN_SH_GND"}, { AFE_DG_PDOC,     "PD_OPEN"   },
    { AFE_DG_PDSC,      "PD_SHORT"  }, { AFE_DG_INNSCGND, "INN_SH_GND"},
    { AFE_DG_INPSCGND,  "INP_SH_GND"}, { AFE_DG_INNSCLED, "INN_SH_LED"},
    { AFE_DG_INPSCLED,  "INP_SH_LED"}
  };
  for (auto &e : tbl) if (flags & e.bit) sendLineF("DIAG:FAULT:%s", e.name);
}

// =====================================================================
// BRING-UP PROBE
// ---------------------------------------------------------------------
// When every register reads back 000000 the interesting question is
// whether anything is out there at all. Two facts settle it without a
// scope:
//
//  * Is a pin driven? Pull it up, read; pull it down, read. A pin that
//    follows the pull has nothing on the other end. This works on
//    SPISOMI in particular because the AFE4490 does NOT tri-state its
//    digital outputs when deselected (datasheet 8.5.2.5) - so a healthy,
//    powered AFE holds MISO at a fixed level even with CS high.
//
//  * Is the AFE running? ADC_RDY pulses once per pulse-repetition period
//    whenever TIMEREN is set and the crystal is oscillating. Counting
//    edges for a quarter second proves the analog core has a clock,
//    entirely independently of the SPI bus.
// =====================================================================
static void startStream();      // defined below, used by the probe
static void stopStream();

static const char *pinDriveState(uint8_t pin) {
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(400);
  const int hi = digitalRead(pin);
  pinMode(pin, INPUT_PULLDOWN);
  delayMicroseconds(400);
  const int lo = digitalRead(pin);
  pinMode(pin, INPUT);
  if (hi == HIGH && lo == LOW)  return "float";   // followed the pull
  if (hi == HIGH && lo == HIGH) return "driven-H";
  if (hi == LOW  && lo == LOW)  return "driven-L";
  return "odd";                                   // inverted: wired oddly
}

static void runPinTest() {
  const bool wasStreaming = streaming;
  if (wasStreaming) stopStream();

  // Hand the pins back to the GPIO block for the duration of the test.
  SPI_AFE.end();
  delay(2);

  const char *miso = pinDriveState(PIN_AFE_MISO);
  const char *rdy  = pinDriveState(PIN_AFE_RDY);

  SPI_AFE.begin();
  pinMode(PIN_AFE_CS, OUTPUT);    digitalWrite(PIN_AFE_CS, HIGH);
  pinMode(PIN_AFE_RESET, OUTPUT); digitalWrite(PIN_AFE_RESET, HIGH);
  pinMode(PIN_AFE_PWDN, OUTPUT);  digitalWrite(PIN_AFE_PWDN, HIGH);
  pinMode(PIN_AFE_RDY, INPUT);
  delay(2);

  probeEdges = 0;
  attachInterrupt(digitalPinToInterrupt(PIN_AFE_RDY), onProbeEdge, RISING);
  delay(250);
  detachInterrupt(digitalPinToInterrupt(PIN_AFE_RDY));
  const uint32_t edges = probeEdges;

  // Does anything at all come back on the bus? Walk one register.
  afe.writeReg(AFE_LED2STC, 0x00A5A5);
  const uint32_t rb1 = afe.readReg(AFE_LED2STC);
  afe.writeReg(AFE_LED2STC, 0x005A5A);
  const uint32_t rb2 = afe.readReg(AFE_LED2STC);

  sendLineF("PIN:MISO=%s", miso);
  sendLineF("PIN:ADC_RDY=%s,edges=%lu/250ms", rdy, (unsigned long)edges);
  sendLineF("PIN:LOOPBACK=%06lX,%06lX", (unsigned long)rb1, (unsigned long)rb2);

  const bool misoDead = (strcmp(miso, "float") == 0);
  const bool spiDead  = (rb1 != 0x00A5A5 || rb2 != 0x005A5A);

  if (misoDead && edges == 0)
    sendLine("PIN:VERDICT=NO_AFE - MISO floats and ADC_RDY is silent. "
             "Check 3V3 and GND on H3, and that the breakout is plugged in.");
  else if (misoDead)
    sendLine("PIN:VERDICT=MISO_OPEN - ADC_RDY is alive, so the AFE has power "
             "and a clock, but D1 is not connected to H3.10 (MISO).");
  else if (spiDead && edges == 0)
    sendLine("PIN:VERDICT=NO_CLOCK - the bus responds but ADC_RDY never "
             "pulses. Suspect the 8 MHz crystal, or TIMEREN left clear.");
  else if (spiDead)
    sendLine("PIN:VERDICT=SPI_CORRUPT - readback does not match. Check "
             "D2=SCK / D3=MOSI / D4=CS and try a slower SPI clock.");
  else
    sendLine("PIN:VERDICT=OK - register readback and ADC_RDY both healthy.");

  // Put the scratch register back the way applyTiming() left it.
  afe.applyTiming(afe.timing());
  if (wasStreaming) startStream();
}

/* Find out what this wiring will actually carry.
 *
 * Worth doing even though the AFE4490 has no equivalent of the ADS1292R's
 * t(SDECODE) inter-byte gap - the ceiling here is pure signal integrity,
 * and on jumper wires that is a property of the build, not of the parts.
 * The sweep leaves SCLK one step below the fastest error-free speed:
 * running at the edge of what works today is how a rig starts failing
 * when someone moves a wire. */
static void runSpiSweep() {
  const bool wasStreaming = streaming;
  if (wasStreaming) stopStream();

  AFE4490::SpiSweepStep steps[9];
  const uint32_t restore = afe.spiSpeed();
  const uint8_t n = afe.spiSweep(steps, 9, 32);

  uint32_t best = 0;
  for (uint8_t i = 0; i < n; i++) {
    sendLineF("SPITEST:%lu=%u/32,%s", (unsigned long)steps[i].hz,
              steps[i].errors, steps[i].errors ? "FAIL" : "PASS");
    if (steps[i].errors == 0) best = steps[i].hz;
  }

  uint32_t safe = best;
  for (uint8_t i = 0; i < n; i++)
    if (steps[i].hz < best && steps[i].errors == 0) safe = steps[i].hz;

  if (best) afe.setSpiSpeed(safe);
  else      afe.setSpiSpeed(restore);

  sendLineF("SPITEST:END:BEST=%lu,SAFE=%lu,ACTIVE=%lu,DRIVE=%s,MAX=%lu",
            (unsigned long)best, (unsigned long)safe,
            (unsigned long)afe.spiSpeed(),
            afe.driveStrength() ? "H0H1" : "S0S1",
            (unsigned long)afe.maxSpiHz());
  if (!best)
    sendLine("SPITEST:HINT:no speed passed - this is a wiring or power "
             "fault, not a clock problem. Run PINTEST.");

  afe.applyTiming(afe.timing());     // selfTest scribbles on LED2STC
  if (wasStreaming) startStream();
}

// =====================================================================
// STREAM CONTROL
// =====================================================================
static void startStream() {
  if (streaming) return;
  ringHead = ringTail = 0;
  rdyPending = 0;
  batchOpenedUs = 0;
  pulse.reset();
  seqCounter = 0;
  dropCount = txDropCount = missedRdy = 0;
  decCounter = 0;
  afe.setSpiReadMode(true);            // stays on for the whole stream
  attachInterrupt(digitalPinToInterrupt(PIN_AFE_RDY), onAdcReady, RISING);
  streaming = true;
  sendLine("ACK:START");
}

static void stopStream() {
  if (!streaming) { sendLine("ACK:STOP"); return; }
  detachInterrupt(digitalPinToInterrupt(PIN_AFE_RDY));
  streaming = false;
  sendLine("ACK:STOP");
}

// =====================================================================
// COMMAND DISPATCHER
// ---------------------------------------------------------------------
// A strict whitelist: anything unrecognised is rejected rather than
// being partially matched, so a typo can never rewrite a register.
// =====================================================================
static bool argU32(const char *s, uint32_t *out, int base = 10) {
  if (!s || !*s) return false;
  char *end = nullptr;
  const unsigned long v = strtoul(s, &end, base);
  if (end == s) return false;
  *out = (uint32_t)v;
  return true;
}

static void handleCommand(char *line) {
  // Trim whitespace and upper-case the verb portion in place.
  while (*line == ' ' || *line == '\t') line++;
  size_t len = strlen(line);
  while (len && (line[len - 1] == ' ' || line[len - 1] == '\r' ||
                 line[len - 1] == '\t')) line[--len] = 0;
  if (len == 0) return;
  for (char *p = line; *p && *p != ','; p++) *p = (char)toupper((unsigned char)*p);

  // ---- identity / status ---------------------------------------------
  if (!strcmp(line, "PING"))    { sendLineF("AFE:PONG:%s", FIRMWARE_VERSION); return; }
  if (!strcmp(line, "CAPS?"))   { sendCaps(); return; }
  if (!strcmp(line, "STATUS?")) { sendStatus(); return; }
  if (!strcmp(line, "REG?"))    { dumpRegisters(); return; }

  // ---- streaming ------------------------------------------------------
  if (!strcmp(line, "START")) { startStream(); return; }
  if (!strcmp(line, "STOP"))  { stopStream();  return; }
  if (!strncmp(line, "FMT:", 4)) {
    if (!strcmp(line + 4, "BIN"))       outputFormat = 0;
    else if (!strcmp(line + 4, "TEXT")) outputFormat = 1;
    else { sendLine("ERR:BAD_FMT"); return; }
    sendLineF("ACK:FMT:%s", outputFormat ? "TEXT" : "BIN");
    return;
  }
  if (!strncmp(line, "DEC:", 4)) {
    uint32_t d;
    if (!argU32(line + 4, &d) || d < 1 || d > 64) { sendLine("ERR:BAD_DEC"); return; }
    decimation = (uint16_t)d; decCounter = 0;
    sendLineF("ACK:DEC:%u", decimation);
    return;
  }

  // ---- raw register access --------------------------------------------
  if (!strncmp(line, "R:", 2)) {
    uint32_t a;
    if (!argU32(line + 2, &a, 16) || a > AFE_REG_MAX) { sendLine("ERR:BAD_ADDR"); return; }
    sendLineF("REG:%02lX=%06lX", (unsigned long)a, (unsigned long)afe.readReg((uint8_t)a));
    return;
  }
  if (!strncmp(line, "W:", 2)) {
    char *comma = strchr(line + 2, ',');
    if (!comma) { sendLine("ERR:BAD_W_FORMAT"); return; }
    *comma = 0;
    uint32_t a, v;
    if (!argU32(line + 2, &a, 16) || !argU32(comma + 1, &v, 16)) {
      sendLine("ERR:BAD_W_FORMAT"); return;
    }
    // Say which half was wrong: on a register console that is the
    // difference between a typo and a misread datasheet.
    if (a > AFE_REG_MAX) { sendLine("ERR:BAD_ADDR"); return; }
    if (v > 0x00FFFFFFUL) { sendLine("ERR:BAD_VALUE"); return; }
    afe.writeReg((uint8_t)a, v);
    if (a == AFE_CONTROL0) { sendLineF("ACK:W:%02lX=%06lX", (unsigned long)a, (unsigned long)(v & 0xFFFFFF)); return; }
    const uint32_t back = afe.readReg((uint8_t)a);
    sendLineF("ACK:W:%02lX=%06lX", (unsigned long)a, (unsigned long)back);
    return;
  }

  // ---- timing ----------------------------------------------------------
  if (!strncmp(line, "PRF:", 4)) {
    // PRF:<hz>[,<duty%>[,<settle>[,<adcrst>]]]
    AfeTiming t = afe.timing();
    char *tok = strtok(line + 4, ",");
    uint32_t v;
    if (!tok || !argU32(tok, &v)) { sendLine("ERR:BAD_PRF"); return; }
    t.prf = (uint16_t)v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) t.dutyPct = (uint8_t)v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) t.settle  = (uint16_t)v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) t.adcRst  = (uint8_t)v;
    afe.applyTiming(t);
    const AfeTiming &n = afe.timing();
    sendLineF("ACK:PRF:%u,%u,%u,%u,numav=%u", n.prf, n.dutyPct, n.settle, n.adcRst, n.numav);
    return;
  }
  if (!strncmp(line, "NUMAV:", 6)) {
    if (!strcmp(line + 6, "AUTO")) {
      AfeTiming t = afe.timing(); t.numavAuto = true;
      afe.applyTiming(t);
      sendLineF("ACK:NUMAV:AUTO=%u", afe.timing().numav);
      return;
    }
    uint32_t v;
    if (!argU32(line + 6, &v) || v > 255) { sendLine("ERR:BAD_NUMAV"); return; }
    afe.setNumAv((uint8_t)v);
    sendLineF("ACK:NUMAV:%lu", (unsigned long)v);
    return;
  }
  if (!strncmp(line, "TIMER:", 6)) {
    afe.setTimerEnable(line[6] != '0');
    sendLineF("ACK:TIMER:%c", line[6]);
    return;
  }

  // ---- transmitter ------------------------------------------------------
  if (!strncmp(line, "LED:", 4)) {
    char *comma = strchr(line + 4, ',');
    if (!comma) { sendLine("ERR:BAD_LED"); return; }
    *comma = 0;
    uint32_t a, b;
    if (!argU32(line + 4, &a) || !argU32(comma + 1, &b) || a > 255 || b > 255) {
      sendLine("ERR:BAD_LED"); return;
    }
    afe.setLedCurrents((uint8_t)a, (uint8_t)b);
    sendLineF("ACK:LED:%lu,%lu,%.2fmA,%.2fmA", (unsigned long)a, (unsigned long)b,
              afe.ledCurrentMa((uint8_t)a), afe.ledCurrentMa((uint8_t)b));
    return;
  }
  if (!strncmp(line, "LEDRANGE:", 9)) {
    uint32_t v;
    if (!argU32(line + 9, &v) || v > 3) { sendLine("ERR:BAD_RANGE"); return; }
    afe.setLedRange((uint8_t)v);
    sendLineF("ACK:LEDRANGE:%lu,fs=%umA", (unsigned long)v, afe.ledFullScaleMa());
    return;
  }
  if (!strncmp(line, "TXREF:", 6)) {
    uint32_t v;
    if (!argU32(line + 6, &v) || v > 3) { sendLine("ERR:BAD_TXREF"); return; }
    afe.setTxRef((uint8_t)v);
    sendLineF("ACK:TXREF:%lu,fs=%umA", (unsigned long)v, afe.ledFullScaleMa());
    return;
  }

  // ---- receiver ---------------------------------------------------------
  if (!strncmp(line, "TIA:", 4)) {
    // TIA:<rf>,<cf>[,<separate>,<rf1>,<cf1>]
    uint32_t rf = 0, cf = 0, sep = 0, rf1 = 0xFF, cf1 = 0xFF, v;
    char *tok = strtok(line + 4, ",");
    if (!tok || !argU32(tok, &rf)) { sendLine("ERR:BAD_TIA"); return; }
    tok = strtok(nullptr, ",");
    if (!tok || !argU32(tok, &cf)) { sendLine("ERR:BAD_TIA"); return; }
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) sep = v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) rf1 = v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) cf1 = v;
    if (rf > 7 || cf > 31) { sendLine("ERR:BAD_TIA"); return; }
    afe.setTia((uint8_t)rf, (uint8_t)cf, sep != 0, (uint8_t)rf1, (uint8_t)cf1);
    sendLineF("ACK:TIA:rf=%luohm,cf=%upF,sep=%lu",
              (unsigned long)AFE_RF_OHMS[rf & 7], afeCfPicofarads((uint8_t)cf),
              (unsigned long)sep);
    return;
  }
  if (!strncmp(line, "STAGE2:", 7)) {
    char *comma = strchr(line + 7, ',');
    uint32_t en = 0, g = 0;
    if (!argU32(line + 7, &en)) { sendLine("ERR:BAD_STAGE2"); return; }
    if (comma) argU32(comma + 1, &g);
    if (g > 4) { sendLine("ERR:BAD_STAGE2"); return; }
    afe.setStage2(en != 0, (uint8_t)g, true);
    sendLineF("ACK:STAGE2:%lu,%lu,%u.%udB", (unsigned long)en, (unsigned long)g,
              AFE_STG2_DB10[g] / 10, AFE_STG2_DB10[g] % 10);
    return;
  }
  if (!strncmp(line, "AMBDAC:", 7)) {
    uint32_t v;
    if (!argU32(line + 7, &v) || v > 10) { sendLine("ERR:BAD_AMBDAC"); return; }
    afe.setAmbientDac((uint8_t)v);
    sendLineF("ACK:AMBDAC:%luuA", (unsigned long)v);
    return;
  }
  if (!strncmp(line, "FILTER:", 7)) {
    afe.setFilterCorner(line[7] != '0');
    sendLineF("ACK:FILTER:%sHz", line[7] != '0' ? "1000" : "500");
    return;
  }

  // ---- power / reset -----------------------------------------------------
  if (!strncmp(line, "PDN:", 4)) {
    uint32_t a = 0, r = 0, t = 0, v;
    char *tok = strtok(line + 4, ",");
    if (tok && argU32(tok, &v)) a = v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) r = v;
    if ((tok = strtok(nullptr, ",")) && argU32(tok, &v)) t = v;
    afe.powerDown(a != 0, r != 0, t != 0);
    sendLineF("ACK:PDN:%lu,%lu,%lu", (unsigned long)a, (unsigned long)r, (unsigned long)t);
    return;
  }
  if (!strcmp(line, "DEFAULTS")) {
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    afe.applyDefaults();
    sendLine("ACK:DEFAULTS");
    if (wasStreaming) startStream();
    return;
  }
  if (!strcmp(line, "RESET") || !strcmp(line, "SWRESET")) {
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    if (line[0] == 'S') afe.softwareReset(); else afe.hardwareReset();
    afe.applyDefaults();
    sendLineF("ACK:%s", line);
    if (wasStreaming) startStream();
    return;
  }

  // ---- diagnostics / tuning ------------------------------------------------
  if (!strcmp(line, "DIAG")) {
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    reportDiag(afe.runDiagnostics());
    if (wasStreaming) startStream();
    return;
  }
  if (!strncmp(line, "AGC", 3)) {
    uint32_t target = 50;
    if (line[3] == ':') argU32(line + 4, &target);
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    const bool ok = afe.autoGain((uint8_t)target);
    const uint32_t ag = afe.shadow(AFE_TIA_AMB_GAIN);
    const uint32_t lc = afe.shadow(AFE_LEDCNTRL);
    sendLineF("ACK:AGC:%s,rf=%luohm,led1=%lu,led2=%lu,led1ma=%.2f,led2ma=%.2f",
              ok ? "OK" : "LIMIT",
              (unsigned long)AFE_RF_OHMS[ag & AFE_AG_RF_LED2_MSK],
              (unsigned long)((lc & AFE_LC_LED1_MSK) >> AFE_LC_LED1_SH),
              (unsigned long)((lc & AFE_LC_LED2_MSK) >> AFE_LC_LED2_SH),
              afe.ledCurrentMa((lc & AFE_LC_LED1_MSK) >> AFE_LC_LED1_SH),
              afe.ledCurrentMa((lc & AFE_LC_LED2_MSK) >> AFE_LC_LED2_SH));
    if (wasStreaming) startStream();
    return;
  }
  if (!strcmp(line, "PINTEST")) { runPinTest(); return; }
  if (!strncmp(line, "SELFTEST", 8)) {
    uint32_t trials = 64;
    if (line[8] == ':') argU32(line + 9, &trials);
    if (trials < 1 || trials > 4096) trials = 64;
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    const uint16_t errs = afe.selfTest((uint16_t)trials);
    sendLineF("ACK:SELFTEST:%u/%lu,%s", errs, (unsigned long)trials,
              errs ? "FAIL" : "PASS");
    if (wasStreaming) startStream();
    return;
  }
  if (!strcmp(line, "SPI:TEST")) { runSpiSweep(); return; }
  if (!strncmp(line, "SPI:SET:", 8)) {
    uint32_t hz;
    if (!argU32(line + 8, &hz)) { sendLine("ERR:BAD_SPI"); return; }
    afe.setSpiSpeed(hz);
    // Report the quantized clock, never the request: the SPIM only has
    // discrete steps and the BSP rounds up to the next one.
    sendLineF("ACK:SPI:%lu,req=%lu", (unsigned long)afe.spiSpeed(),
              (unsigned long)hz);
    return;
  }
  if (!strncmp(line, "SPI:DRIVE:", 10)) {
    afe.setDriveStrength(line[10] != '0');
    sendLineF("ACK:SPI:DRIVE=%s", afe.driveStrength() ? "H0H1" : "S0S1");
    return;
  }

  sendLine("ERR:UNKNOWN_COMMAND");
}

// =====================================================================
// SETUP / LOOP
// =====================================================================
static char    cmdBuf[192];
static uint8_t cmdLen = 0;
static uint32_t lastStatus = 0;
static bool     lastUsbState = false;

void setup() {
  Serial.begin(115200);

  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);
  ledWrite(true, true, true);           // white until something attaches

  const bool linkOk = afe.begin(4000000UL);
  afe.applyDefaults();

  // Give the host a moment to enumerate before the banner goes out.
  const uint32_t t0 = millis();
  while (!usbReady() && (millis() - t0) < 2500) delay(10);

  sendLineF("EVT:BOOT:%s,proto=%s", FIRMWARE_VERSION, LINK_PROTOCOL);
  sendLineF("EVT:SPI:%s,sclk=%lu,drive=%s", linkOk ? "OK" : "FAIL",
            (unsigned long)afe.spiSpeed(), afe.driveStrength() ? "H0H1" : "S0S1");
  if (!linkOk)
    sendLine("EVT:HINT:run PINTEST to tell a missing board from a wiring "
             "fault, then SPI:TEST to find the speed this wiring will carry");
  sendCaps();
  sendLine("EVT:READY");
}

void loop() {
  // ---- 1. drain the ADC ------------------------------------------------
  // Reading six registers costs ~60 us; do it first so a burst of console
  // traffic can never push the acquisition past the next ADC_RDY.
  while (rdyPending) {
    rdyPending--;
    if (rdyPending > 2) missedRdy++;      // we are falling behind

    AfeSample s;
    afe.readSample(s);

    // Detect on every conversion, before decimation, so the pulse
    // indicator keeps working at any stream rate.
    if (pulse.update(s.led1_amb, millis(), afe.timing().prf))
      beatFlashUntil = millis() + BEAT_FLASH_MS;

    if (++decCounter >= decimation) {
      decCounter = 0;
      const uint16_t next = (uint16_t)((ringHead + 1) % RING_SIZE);
      if (next == ringTail) { dropCount++; ringTail = (uint16_t)((ringTail + 1) % RING_SIZE); }
      ring[ringHead] = s;
      ringHead = next;
    }
  }

  // ---- 2. ship whatever is buffered ------------------------------------
  if (streaming) {
    if (outputFormat == 0) {
      const uint16_t avail =
          (uint16_t)((ringHead + RING_SIZE - ringTail) % RING_SIZE);
      if (avail == 0) {
        batchOpenedUs = 0;
      } else {
        if (batchOpenedUs == 0) batchOpenedUs = micros();
        const bool full  = avail >= SAMPLES_PER_FRAME;
        const bool stale = (uint32_t)(micros() - batchOpenedUs) >= BATCH_MAX_AGE_US;
        if (full || stale) {
          AfeSample batch[SAMPLES_PER_FRAME];
          uint8_t n = 0;
          while (ringTail != ringHead && n < SAMPLES_PER_FRAME) {
            batch[n++] = ring[ringTail];
            ringTail = (uint16_t)((ringTail + 1) % RING_SIZE);
          }
          if (n) sendSampleFrame(batch, n);
          batchOpenedUs = 0;
        }
      }
    } else {
      uint8_t guard = 0;
      while (ringTail != ringHead && guard++ < SAMPLES_PER_FRAME) {
        sendSampleText(ring[ringTail]);
        ringTail = (uint16_t)((ringTail + 1) % RING_SIZE);
      }
    }
  } else {
    ringTail = ringHead;                  // discard while idle
  }

  // ---- 3. console ------------------------------------------------------
  while (Serial.available()) {
    const int c = Serial.read();
    if (c < 0) break;
    if (c == '\n' || c == '\r') {
      if (cmdLen) { cmdBuf[cmdLen] = 0; handleCommand(cmdBuf); cmdLen = 0; }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = (char)c;
    }
  }

  // ---- 4. heartbeat ----------------------------------------------------
  const uint32_t now = millis();
  serviceLeds(now);
  if (now - lastStatus >= 1000) {
    lastStatus = now;
    if (streaming) sendStatus();
  }

  // Re-announce after a reconnect so a console that attaches late still
  // learns what it is talking to without the user pressing anything.
  const bool usb = usbReady();
  if (usb && !lastUsbState) {
    delay(50);
    sendLineF("EVT:BOOT:%s,proto=%s", FIRMWARE_VERSION, LINK_PROTOCOL);
    sendCaps();
    sendLine("EVT:READY");
  }
  lastUsbState = usb;
}
