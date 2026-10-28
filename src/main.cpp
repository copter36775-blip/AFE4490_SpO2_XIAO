/*
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
#include <bluefruit.h>
#include <Adafruit_LittleFS.h>
#include <InternalFileSystem.h>
#include "AFE4490.h"

// =====================================================================
// PIN MAP
// =====================================================================
/* Rev 2 wiring. The SPI pins moved and RESET/PWDN are no longer brought
 * out to the MCU, so every reset now goes through CONTROL0's SW_RST bit.
 * 0xFF tells the driver those pins do not exist rather than having it
 * drive D5 - which is the button - as an output.
 *
 *   H1 pin 7  CS0   -> D0        H1 pin 10 MISO -> D3
 *   H1 pin 8  MOSI  -> D1        H1 pin 11 DRDY -> D4
 *   H1 pin 9  SCK   -> D2        SW1 to GND     -> D5
 */
static const uint8_t PIN_AFE_CS    = D0;
static const uint8_t PIN_AFE_MOSI  = D1;
static const uint8_t PIN_AFE_SCK   = D2;
static const uint8_t PIN_AFE_MISO  = D3;
static const uint8_t PIN_AFE_RDY   = D4;
static const uint8_t PIN_SW        = D5;   // momentary to GND, internal pull-up
static const uint8_t PIN_AFE_RESET = 0xFF; // not wired on rev 2
static const uint8_t PIN_AFE_PWDN  = 0xFF; // not wired on rev 2

SPIClass SPI_AFE(NRF_SPIM1, PIN_AFE_MISO, PIN_AFE_SCK, PIN_AFE_MOSI);
AFE4490  afe(SPI_AFE, PIN_AFE_CS, PIN_AFE_RESET, PIN_AFE_PWDN, PIN_AFE_RDY,
             PIN_AFE_SCK, PIN_AFE_MOSI, /*onSpim3=*/false);

static const char FIRMWARE_VERSION[] = "V2.1.0";
static const char LINK_PROTOCOL[]    = "A1";

// =====================================================================
// ACQUISITION RING
// ---------------------------------------------------------------------
// The ISR only bumps a counter - it never touches SPI, so a slow USB
// host can never stall the interrupt or corrupt a register access.
// =====================================================================
/* Sized from a measurement, not a guess. tools/chrome_ble_probe.py shows
 * Chrome's Bluetooth path going completely silent for about 2.2 s at a time,
 * several times a minute - and it does exactly the same with the animation
 * loop disabled, so it is not the page competing for the main thread. A
 * Python client on the same adapter, minutes apart, never stalls at all.
 *
 * Whatever Chrome is doing, the device cannot argue with it; it can only
 * hold on to the samples until the link comes back. 256 slots was 2.05 s at
 * 125 Hz, which is why the losses were small but never zero, and 1.02 s at
 * 250 Hz, which is why they were bad. 2048 slots cost 48 kB of a 256 kB part
 * and cover 4 s even at the full 500 Hz. */
static const uint16_t RING_SIZE = 2048;
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

// =====================================================================
// LINK MODE
// ---------------------------------------------------------------------
// Three states, cycled by a double press on SW1:
//
//   AUTO   follow the cable. USB mounted -> red. Nothing -> white.
//   BLE    radio on. Advertising -> blue blink. Connected -> blue solid.
//   IDLE   the operator asked for quiet. White, streaming stopped, even
//          with a USB cable in.
//
//   AUTO (white, nothing plugged) --2x--> BLE
//   AUTO (red, USB plugged)       --2x--> IDLE
//   BLE  (blinking or solid)      --2x--> IDLE
//   IDLE                          --2x--> BLE
//
// which is exactly the four transitions asked for. IDLE is a latch, so it
// needs a way out that is not a guess: plugging or unplugging USB clears
// it, and so does a long press.
// =====================================================================
enum LinkMode : uint8_t { MODE_AUTO = 0, MODE_BLE = 1, MODE_IDLE = 2 };
static LinkMode linkMode = MODE_AUTO;

static const char BLE_DEVICE_NAME[] = "AFE4490-XIAO";

/* Nordic UART Service, declared here rather than using BLEUart, because
 * BLEUart::write() goes through Bluefruit's notify(), which waits on a
 * semaphore for up to BLE_GENERIC_TIMEOUT. Waiting is fatal in a loop that
 * also has to drain a 500 Hz ADC: the stall shows up as `drop` climbing
 * into the thousands. Owning the characteristic means owning its handle,
 * which means sd_ble_gatts_hvx() can be called directly - it returns
 * NRF_ERROR_RESOURCES immediately instead of blocking, so the same packet
 * can simply be retried after the next connection event.
 *
 * UUID bytes are little-endian, which is why they read backwards. */
static const uint8_t NUS_SVC_UUID[16] =
  {0x9E,0xCA,0xDC,0x24,0x0E,0xE5,0xA9,0xE0,0x93,0xF3,0xA3,0xB5,0x01,0x00,0x40,0x6E};
static const uint8_t NUS_TX_UUID[16] =   // notify, device -> host
  {0x9E,0xCA,0xDC,0x24,0x0E,0xE5,0xA9,0xE0,0x93,0xF3,0xA3,0xB5,0x03,0x00,0x40,0x6E};
static const uint8_t NUS_RX_UUID[16] =   // write,  host -> device
  {0x9E,0xCA,0xDC,0x24,0x0E,0xE5,0xA9,0xE0,0x93,0xF3,0xA3,0xB5,0x02,0x00,0x40,0x6E};

static BLEService        nusService(NUS_SVC_UUID);
static BLECharacteristic nusTx(NUS_TX_UUID);
static BLECharacteristic nusRx(NUS_RX_UUID);

static bool     bleConnected  = false;
static uint16_t bleConnHandle = BLE_CONN_HANDLE_INVALID;
static uint32_t bleDropCount  = 0;

/* Payload capacity of one notification, or 0 when there is no link. */
static uint16_t bleMtu() {
  if (!bleConnected) return 0;
  BLEConnection *c = Bluefruit.Connection(bleConnHandle);
  if (!c) return 0;
  const uint16_t mtu = c->getMtu();
  return mtu > 23 ? (uint16_t)(min((uint16_t)247, mtu) - 3) : 20;
}

/* What the central chose, for the record. Nothing here changes behaviour -
 * it is the difference between guessing why the stream fits and knowing.
 * getDataLength() below 251 means no data-length extension, which is what
 * caps a notification at 20 useful bytes per link-layer packet. */
static uint16_t bleDataLength() {
  if (!bleConnected) return 0;
  BLEConnection *c = Bluefruit.Connection(bleConnHandle);
  return c ? c->getDataLength() : 0;
}

static uint16_t bleIntervalUnits() {
  if (!bleConnected) return 0;
  BLEConnection *c = Bluefruit.Connection(bleConnHandle);
  return c ? c->getConnectionInterval() : 0;
}

// ---- transmit queue --------------------------------------------------
static const uint8_t  BLE_TXQ_DEPTH = 16;
static const uint16_t BLE_TXQ_SLOT  = 244;
static uint8_t  bleTxQ[BLE_TXQ_DEPTH][BLE_TXQ_SLOT];
static uint16_t bleTxLen[BLE_TXQ_DEPTH];
static uint8_t  bleTxHead = 0, bleTxTail = 0;
static uint32_t bleBackpressure = 0, bleNotifyPackets = 0, bleQueueDrops = 0;
static uint32_t bleNotifyBytes = 0;
static uint32_t bleNextTry = 0;

static inline uint8_t bleTxDepth() {
  return (uint8_t)((bleTxHead + BLE_TXQ_DEPTH - bleTxTail) % BLE_TXQ_DEPTH);
}

/* Queue or drop - never wait. `isData` is the waveform; text is capped at
 * half the queue so a burst of console output cannot crowd the stream out. */
static bool bleQueue(const uint8_t *p, uint16_t n, bool isData) {
  if (n == 0 || n > BLE_TXQ_SLOT) return false;
  const uint8_t next = (uint8_t)((bleTxHead + 1) % BLE_TXQ_DEPTH);
  if (next == bleTxTail) { bleQueueDrops++; return false; }
  if (!isData && bleTxDepth() > BLE_TXQ_DEPTH / 2) { bleQueueDrops++; return false; }
  memcpy(bleTxQ[bleTxHead], p, n);
  bleTxLen[bleTxHead] = n;
  bleTxHead = next;
  return true;
}

enum BleTxResult : uint8_t { BLE_TX_OK, BLE_TX_BUSY, BLE_TX_NOT_READY };

static BleTxResult tryGattNotify(const uint8_t *data, uint16_t length) {
  if (!bleConnected) return BLE_TX_NOT_READY;
  ble_gatts_char_handles_t h = nusTx.handles();
  uint16_t len = length;
  ble_gatts_hvx_params_t p = {};
  p.handle = h.value_handle;
  p.type   = BLE_GATT_HVX_NOTIFICATION;
  p.offset = 0;
  p.p_len  = &len;
  p.p_data = const_cast<uint8_t *>(data);

  const uint32_t st = sd_ble_gatts_hvx(bleConnHandle, &p);
  if (st == NRF_SUCCESS) {
    /* Counted here rather than at the call sites: this is the only function
     * that puts bytes on the radio, so it is the only honest place to
     * measure airtime from. */
    bleNotifyPackets++; bleNotifyBytes += len;
    return BLE_TX_OK;
  }
  if (st == NRF_ERROR_RESOURCES || st == NRF_ERROR_BUSY) return BLE_TX_BUSY;
  return BLE_TX_NOT_READY;
}

/* Fill every SoftDevice slot that is free, then stop. Called from loop(),
 * never from the acquisition path. */
static void serviceBleTx() {
  if (!bleConnected) return;
  const uint32_t now = millis();
  if ((int32_t)(now - bleNextTry) < 0) return;

  const uint16_t cap = bleMtu();
  uint8_t budget = 8;
  while (bleTxTail != bleTxHead && budget--) {
    /* A packet larger than the negotiated MTU will never be accepted. Left
     * at the head of the queue it would jam the stream silently for the rest
     * of the session, so drop it and say so in the counters instead. */
    if (cap && bleTxLen[bleTxTail] > cap) {
      bleTxTail = (uint8_t)((bleTxTail + 1) % BLE_TXQ_DEPTH);
      bleQueueDrops++; txDropCount++;
      continue;
    }
    const BleTxResult r = tryGattNotify(bleTxQ[bleTxTail], bleTxLen[bleTxTail]);
    if (r != BLE_TX_OK) {
      bleNextTry = now + (r == BLE_TX_BUSY ? 5 : 10);
      bleBackpressure++;
      break;
    }
    bleTxTail = (uint8_t)((bleTxTail + 1) % BLE_TXQ_DEPTH);
  }
}

// ---- receive ---------------------------------------------------------
/* A WM: line for the whole register map is ~400 bytes arriving in 20-byte
 * writes; 256 bytes dropped the tail of it whenever loop() was busy. */
static uint8_t  bleRxBuf[1024];
static volatile uint16_t bleRxHead = 0, bleRxTail = 0;

static void nusRxCallback(uint16_t conn, BLECharacteristic *chr,
                          uint8_t *data, uint16_t len) {
  (void)conn; (void)chr;
  for (uint16_t i = 0; i < len; i++) {
    const uint16_t nx = (uint16_t)((bleRxHead + 1) % sizeof(bleRxBuf));
    if (nx == bleRxTail) break;              // console flooding us: drop
    bleRxBuf[bleRxHead] = data[i];
    bleRxHead = nx;
  }
}

static int bleRxRead() {
  if (bleRxTail == bleRxHead) return -1;
  const int c = bleRxBuf[bleRxTail];
  bleRxTail = (uint16_t)((bleRxTail + 1) % sizeof(bleRxBuf));
  return c;
}

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

static const uint32_t BLE_BLINK_MS = 250;   // advertising blink half-period

static void serviceLeds(uint32_t nowMs) {
  const bool usbLink = (linkMode == MODE_AUTO) && TinyUSBDevice.mounted();
  const bool live    = bleConnected || usbLink;

  // The beat flash only makes sense on a live link, and only there does it
  // outrank the base colour.
  if (live && nowMs < beatFlashUntil) { ledSet(0b010); return; }

  if (bleConnected)         { ledSet(0b001); return; }              // blue solid
  if (linkMode == MODE_BLE) {                                       // blue blink
    ledSet(((nowMs / BLE_BLINK_MS) & 1) ? 0b001 : 0b000);
    return;
  }
  if (usbLink)              { ledSet(0b100); return; }              // red
  ledSet(0b111);                                                    // white idle
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
// OUTPUT - USB CDC or BLE, whichever is up
// ---------------------------------------------------------------------
// The wire format does not change between the two. Protocol A1 frames are
// 207 bytes, which was chosen in rev 1 precisely so that one frame fits in
// one BLE notification once the ATT MTU is negotiated up - so the same
// console parser works over either transport without knowing which it is.
// =====================================================================
static inline bool usbReady() {
  return TinyUSBDevice.mounted() && Serial.dtr();
}
static inline bool bleReady() {
  return bleConnected && nusTx.notifyEnabled(bleConnHandle);
}
/* BLE wins when it is up: it is the transport the operator explicitly
 * asked for by pressing the button, and on the bench a USB cable is
 * often plugged in for power alone. */
static inline bool linkReady() { return bleReady() || usbReady(); }

/* A status line can outgrow one notification, and truncating it drops
 * whichever counters happen to sort last - which is exactly how bledle and
 * bleci vanished the first time, silently, leaving a line that still looked
 * well formed. The console reassembles on the newline, so a long line can
 * simply span packets.
 *
 * Capacity is checked for the whole line before any of it is queued: half a
 * line on the wire would join itself to the next one and corrupt that too,
 * which is worse than losing this one. */
/* Discard everything in flight. Used after any change that invalidates
 * samples already converted - a new pulse rate, a new gain, a reset. */
static void flushAcquisition() {
  rdyPending = 0;
  ringTail = ringHead;
  decCounter = 0;
  batchOpenedUs = 0;
}

static void sendLine(const char *s) {
  if (bleReady()) {
    const uint16_t CH = BLE_TXQ_SLOT - 2;          // room for CR LF on the last
    const uint16_t n  = (uint16_t)strlen(s);
    uint8_t need = (uint8_t)((n + CH - 1) / CH);
    if (need == 0) need = 1;                       // an empty line is still a line
    if (bleTxDepth() + need > BLE_TXQ_DEPTH / 2) { bleQueueDrops++; return; }

    uint16_t off = 0;
    do {
      uint8_t buf[BLE_TXQ_SLOT];
      uint16_t take = n - off;
      if (take > CH) take = CH;
      memcpy(buf, s + off, take);
      off += take;
      uint16_t len = take;
      if (off >= n) { buf[len++] = '\r'; buf[len++] = '\n'; }
      bleQueue(buf, len, false);
    } while (off < n);
    return;
  }
  if (usbReady()) Serial.println(s);
}

static void sendLineF(const char *fmt, ...) {
  if (!linkReady()) return;
  /* Sized for the longest line the firmware can produce with every counter
   * at full width, not for what one notification carries - sendLine() splits
   * across packets now, so this is the only place a line can still be cut. */
  char buf[400];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  sendLine(buf);
}

/* USB and BLE want opposite things from a frame, so they get different ones.
 *
 * USB has bandwidth to spare and cares about latency: 8 samples x 24 B =
 * 207 B, inside the 256-byte CFG_TUD_CDC_TX_BUFSIZE, shipped every 25 ms at
 * the latest.
 *
 * BLE is the other way round. Windows grants a peripheral roughly four
 * link-layer packets per connection event, and Chrome does not negotiate a
 * longer data length, so a notification is carried 20 useful bytes at a
 * time and the honest ceiling is about 4 x 20 B x 33 events/s = 2.6 kB/s.
 * Six 32-bit channels at 125 Hz is 3.4 kB/s. Over the ceiling by about a
 * seventh - which is exactly the fraction `drop` was counting.
 *
 * The compact frame gets under it twice over: 12 B per sample instead of 24,
 * and 16 samples per packet instead of 8 so the packet rate falls with the
 * byte rate. 125 Hz then costs 1.6 kB/s in 8 packets/s, which is the same
 * envelope HECG01 has been running in for months. */
static const uint8_t  FRAME_TYPE_FULL    = 0x01;   // 6 channels, 32-bit
static const uint8_t  FRAME_TYPE_COMPACT = 0x02;   // 4 channels, 24-bit

static const uint8_t  USB_SAMPLES_PER_FRAME = 8;
static const uint8_t  BLE_SAMPLES_PER_FRAME = 16;
static const uint8_t  SAMPLES_PER_FRAME     = 16;   // sizes the batch buffer
static const uint32_t USB_BATCH_MAX_AGE_US  = 25000;
/* Long enough that a full 16-sample frame - 128 ms at 125 Hz - always wins
 * the race. The 25 ms limit shared with USB was shipping four-sample frames
 * and quadrupling the packet rate for no extra data. */
static const uint32_t BLE_BATCH_MAX_AGE_US  = 200000;

static const uint16_t FRAME_BODY_MAX =
    8 + (USB_SAMPLES_PER_FRAME * 24 > BLE_SAMPLES_PER_FRAME * 12
         ? USB_SAMPLES_PER_FRAME * 24 : BLE_SAMPLES_PER_FRAME * 12);

/* Whether the transport could take a frame right now. Asked before the batch
 * is assembled, so a stalled radio costs a comparison per loop rather than a
 * 200-byte copy. */
static bool linkCanAcceptFrame(uint16_t total) {
  if (bleReady()) return bleTxDepth() < (uint8_t)(BLE_TXQ_DEPTH - 1);
  if (usbReady()) return (uint16_t)Serial.availableForWrite() >= total;
  return false;
}

/* Returns false when the bytes did not go anywhere, so the caller can leave
 * the samples in the ring and try again on a later pass instead of punching
 * a hole in the trace. */
/* Which frame to send.
 *
 * The compact frame is lossless - the converter is 22-bit and the two
 * ambient-subtracted channels are exactly the difference of the raw pair,
 * which the console recomputes - so the only reason USB ever used the full
 * one is history. It matters at the top of the rate range: 24 bytes a
 * sample is 118 kB/s at 4901 Hz, and the CDC pipe on this board was
 * measured to carry about 105. Twelve bytes a sample is 59.
 *
 * Below 2000 Hz the full frame costs nothing and the rev 1 consoles, which
 * only know type 0x01, keep working - so AUTO leaves them alone. The
 * override is deliberately session-only: a stored COMPACT would make an
 * older console show nothing with no clue why. */
enum FrameMode : uint8_t { FRAME_AUTO = 0, FRAME_FULL = 1, FRAME_COMPACT = 2 };
static FrameMode frameMode = FRAME_AUTO;
static const uint16_t COMPACT_ABOVE_HZ = 2000;

static bool useCompactFrame() {
  if (frameMode == FRAME_FULL)    return false;
  if (frameMode == FRAME_COMPACT) return true;
  if (bleReady()) return true;                    // the radio pays per byte
  return afe.timing().prf > COMPACT_ABOVE_HZ;
}

static bool sendSampleFrame(const AfeSample *s, uint8_t count) {
  if (!linkReady() || count == 0) return false;
  const bool compact = useCompactFrame();

  static uint8_t body[FRAME_BODY_MAX];
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

  if (compact) {
    /* LED2-ALED2 and LED1-ALED1 are exactly the difference of the raw pair -
     * 1.1811 V - 0.4597 V = 0.7214 V on the bench - so spending airtime on
     * them is spending it on a subtraction the console can do itself. What
     * is left is four conversions, and the converter is 22-bit, so three
     * bytes each is lossless rather than a compromise. */
    for (uint8_t k = 0; k < count; k++) {
      const int32_t v[4] = { s[k].led2, s[k].aled2, s[k].led1, s[k].aled1 };
      for (uint8_t j = 0; j < 4; j++) {
        body[n++] = (uint8_t)(v[j]);
        body[n++] = (uint8_t)(v[j] >> 8);
        body[n++] = (uint8_t)(v[j] >> 16);
      }
    }
  } else {
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
  }

  const uint8_t type = compact ? FRAME_TYPE_COMPACT : FRAME_TYPE_FULL;
  uint8_t head[5] = { 0xAA, 0x55, type, (uint8_t)(n), (uint8_t)(n >> 8) };
  uint16_t crc = crc16(&head[2], 3);
  crc = crc16(body, n, crc);

  const uint16_t total = (uint16_t)(5 + n + 2);
  if (!linkCanAcceptFrame(total)) return false;

  /* One contiguous buffer, one write. Over USB that is what it always was;
   * over BLE it matters, because a frame split across two write() calls
   * can be interleaved with a line of console text and desync the parser.
   * Whole frame or nothing. */
  static uint8_t pkt[5 + FRAME_BODY_MAX + 2];
  memcpy(pkt, head, 5);
  memcpy(pkt + 5, body, n);
  pkt[5 + n]     = (uint8_t)(crc);
  pkt[5 + n + 1] = (uint8_t)(crc >> 8);

  // Never block the acquisition path waiting on a stalled host.
  if (bleReady()) {
    if (!bleQueue(pkt, total, true)) { bleDropCount++; return false; }
  } else if (usbReady()) {
    Serial.write(pkt, total);
  } else {
    return false;
  }
  seqCounter += count;
  return true;
}

static void sendSampleText(const AfeSample &s) {
  if (!linkReady()) return;
  if (usbReady() && !bleReady() && Serial.availableForWrite() < 80) {
    txDropCount++; return;
  }
  char buf[96];
  snprintf(buf, sizeof(buf), "D,%lu,%ld,%ld,%ld,%ld,%ld,%ld",
           (unsigned long)seqCounter,
           (long)s.led2, (long)s.aled2, (long)s.led1,
           (long)s.aled1, (long)s.led2_amb, (long)s.led1_amb);
  sendLine(buf);
  seqCounter++;
}

// =====================================================================
// TELEMETRY
// =====================================================================
// =====================================================================
// BATTERY AND CHARGER  (BQ25100)
// ---------------------------------------------------------------------
// VBAT reaches P0.31 through a 1 M / 510 k divider that P0.14 enables by
// going low. Two things about that divider were being ignored, and
// together they reported a 3.7 V cell as 4.48 V - a number no lithium
// polymer cell can produce, which is what gave it away.
//
//  * The source impedance the SAADC sees is 1 M || 510 k = 338 kohm. The
//    core's default acquisition window is 3 us, which Nordic rates for a
//    10 kohm source. The sampling capacitor never finished charging, so
//    each result was dragged toward the charge already on it. 40 us is
//    the longest the SAADC offers and is rated to 800 kohm.
//
//  * With the divider disabled P0.31 floats up toward VBAT through the
//    1 M leg, so the first conversion after enabling it is still on the
//    way down. It gets thrown away.
//
// Absolute accuracy still rests on the band-gap reference and on two
// resistors with their own tolerance, so BATCAL: exists to trim it
// against a multimeter rather than pretending the arithmetic is exact.
//
// ~CHG (P0.17) is the charger's open-drain status line: pulled low while
// charging, released when the cell is full - or when there is no input
// at all, which is why VBUS has to be read as well to tell "full" from
// "unplugged".
// =====================================================================
/* The variant stops naming pins at D10, but its g_ADigitalPinMap keeps
 * going: index 22 is P0.13 and index 23 is P0.17, both on the BQ25100. */
static const uint8_t PIN_CHG   = 23;           // P0.17, LOW while charging
static const uint8_t PIN_HICHG = 22;           // P0.13, LOW selects 100 mA
static const float   VBAT_DIV  = 1510.0f / 510.0f;

static float    vbatTrim   = 1.0f;             // set by BATCAL:
static uint16_t vbatCounts = 0;                // last raw reading, for BAT?

enum ChargeState : uint8_t { CHG_OFF = 0, CHG_ACTIVE = 1, CHG_DONE = 2 };

static bool vbusPresent() {
  return (NRF_POWER->USBREGSTATUS & POWER_USBREGSTATUS_VBUSDETECT_Msk) != 0;
}

static ChargeState chargeState() {
  if (!vbusPresent()) return CHG_OFF;
  return digitalRead(PIN_CHG) == LOW ? CHG_ACTIVE : CHG_DONE;
}

static const char *chargeName(ChargeState c) {
  return c == CHG_ACTIVE ? "CHARGING" : (c == CHG_DONE ? "FULL" : "BATTERY");
}

static float readBatteryVolts() {
  static bool calibrated = false;

  pinMode(VBAT_ENABLE, OUTPUT);
  digitalWrite(VBAT_ENABLE, LOW);              // enable the divider
  analogReference(AR_INTERNAL_3_0);            // band gap, gain 1/5 -> 3.0 V FS
  analogReadResolution(12);
  analogSampleTime(40);                        // see the note above
  analogOversampling(8);                       // burst mode: one call, 8 averaged
  if (!calibrated) { analogCalibrateOffset(); calibrated = true; }
  delay(3);

  (void)analogRead(PIN_VBAT);                  // still settling: discard
  uint32_t acc = 0;
  for (uint8_t i = 0; i < 4; i++) acc += analogRead(PIN_VBAT);
  digitalWrite(VBAT_ENABLE, HIGH);

  vbatCounts = (uint16_t)(acc / 4);
  return (float)vbatCounts * (3.0f / 4096.0f) * VBAT_DIV * vbatTrim;
}

/* A single cell spends most of its life between 3.7 and 4.0 V, so a straight
 * line from 3.0 to 4.2 sits near 50 % for most of the discharge and then
 * falls off a cliff. This is the usual resting-voltage curve - coarse, and
 * optimistic under load, which is why everything that shows it calls it an
 * estimate. */
static uint8_t batteryPercent(float v) {
  static const float pts[][2] = {
    {3.27f,0}, {3.61f,5}, {3.69f,10}, {3.71f,20}, {3.73f,30}, {3.75f,40},
    {3.77f,50}, {3.79f,60}, {3.82f,70}, {3.87f,80}, {3.92f,90},
    {4.00f,95}, {4.20f,100},
  };
  const uint8_t n = sizeof(pts) / sizeof(pts[0]);
  if (v <= pts[0][0]) return 0;
  if (v >= pts[n-1][0]) return 100;
  for (uint8_t i = 1; i < n; i++) {
    if (v <= pts[i][0]) {
      const float t = (v - pts[i-1][0]) / (pts[i][0] - pts[i-1][0]);
      return (uint8_t)(pts[i-1][1] + t * (pts[i][1] - pts[i-1][1]) + 0.5f);
    }
  }
  return 100;
}

/* analogRead() here reconfigures the SAADC and needs a 2 ms settle - a 2 ms
 * hole in a loop that is also draining a 500 Hz converter, once a second,
 * for a number that moves over hours. Read it every ten seconds instead and
 * let the status line quote the last one. */
static float    vbatCached  = 0.0f;
static uint32_t vbatStampMs = 0;

static float batteryVolts() {
  const uint32_t now = millis();
  if (vbatCached > 0.0f && (now - vbatStampMs) < 10000UL) return vbatCached;
  vbatStampMs = now;
  vbatCached  = readBatteryVolts();
  return vbatCached;
}

static void sendStatus() {
  const AfeTiming &t = afe.timing();
  sendLineF("ST:fw=%s,proto=%s,stream=%u,fmt=%s,prf=%u,duty=%u,numav=%u,"
            "dec=%u,prpct=%lu,seq=%lu,rdy=%lu,drop=%lu,txdrop=%lu,miss=%lu,"
            "spihz=%lu,spidrive=%s,led1ma=%.2f,led2ma=%.2f,fs=%u,bpm=%u,"
            /* The radio detail used to live here too and pushed the line past
             * what one notification carries, so everything after mode= was
             * silently cut - including the counters that say why the radio is
             * slow. It is all in BLESTAT?, which is the right place for it. */
            "link=%s,mode=%s,vbat=%.2f,bpct=%u,chg=%u,vbus=%u,up=%lu",
            FIRMWARE_VERSION, LINK_PROTOCOL, streaming ? 1u : 0u,
            outputFormat ? "TEXT" : "BIN",
            t.prf, t.dutyPct, t.numav, decimation,
            (unsigned long)(afe.shadow(AFE_PRPCOUNT) & 0xFFFFUL),
            (unsigned long)seqCounter, (unsigned long)rdyTotal,
            (unsigned long)dropCount, (unsigned long)txDropCount,
            (unsigned long)missedRdy, (unsigned long)afe.spiSpeed(),
            afe.driveStrength() ? "H0H1" : "S0S1",
            afe.ledCurrentMa((afe.shadow(AFE_LEDCNTRL) & AFE_LC_LED1_MSK) >> AFE_LC_LED1_SH),
            afe.ledCurrentMa((afe.shadow(AFE_LEDCNTRL) & AFE_LC_LED2_MSK) >> AFE_LC_LED2_SH),
            afe.ledFullScaleMa(), pulse.bpm,
            bleReady() ? "BLE" : (usbReady() ? "USB" : "NONE"),
            linkMode == MODE_BLE ? "BLE" : (linkMode == MODE_IDLE ? "IDLE" : "AUTO"),
            batteryVolts(), batteryPercent(batteryVolts()),
            (unsigned)chargeState(), vbusPresent() ? 1u : 0u,
            (unsigned long)(millis() / 1000UL));
}

static void sendCaps() {
  sendLineF("CAPS:PROTO=%s,FW=%s,CHIP=AFE4490,MCU=XIAO_NRF52840,"
            "CH=6,BITS=22,FSV=1.2,PRFMIN=62,PRFMAX=%u,DUTYMAX=25,"
            "REGS=0x30,AGC=1,DIAG=1,SELFTEST=1,PINTEST=1,SPITEST=1,"
            "SPIMAX=%lu,SPISTEPS=125000|250000|500000|1000000|2000000|4000000|8000000,"
            "FMT=BIN|TEXT,BLE=1,BLENAME=%s,SW=1,LED=RGB,PULSE=1,"
            "BATCH=8,BLEBATCH=16,FRAMES=1|2,BAT=1,CHG=1,PROFILE=1,RATES=1,"
            "WM=48,REGSQ=1,SEQMARK=1",
            LINK_PROTOCOL, FIRMWARE_VERSION, AFE4490::maxPrf(afe.timing().adcRst),
            (unsigned long)afe.maxSpiHz(), BLE_DEVICE_NAME);
}

// =====================================================================
// SETTINGS PROFILE
// ---------------------------------------------------------------------
// The AFE4490 has no non-volatile memory of any kind. All 49 registers
// are volatile: remove power and every one of them comes back at its
// datasheet reset value. There is nothing on the chip to save a setting
// into, so persistence has to live on the MCU - a profile in the nRF52's
// internal flash that boot replays into the AFE once it is alive.
//
// What gets stored is chosen, not dumped wholesale:
//
//   0x01..0x26  timing, gains, LED drive, CONTROL1/2, spares - saved
//   0x29        ALARM - saved
//   0x00        CONTROL0 is write-only and self-clearing. Replaying a
//               captured value would replay SW_RST at every boot.
//   0x27,0x28   factory trim. Read-only, and different on every part.
//   0x2A..0x30  results and diagnostics, not settings.
//
// Registers are the ground truth and are replayed verbatim, because
// recomputing the timing from PRF and duty would quantise a second time
// and could land a count away from what was actually measured with.
// AfeTiming rides along so the console's PRF and duty boxes agree with
// the silicon, but it is never re-applied.
// =====================================================================
using namespace Adafruit_LittleFS_Namespace;

static const char     PROFILE_PATH[]  = "/afe4490.cfg";
static const uint32_t PROFILE_MAGIC   = 0x34454641UL;   // "AFE4"
static const uint16_t PROFILE_VERSION = 1;

static const uint8_t PROFILE_REGS[] = {
  0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0A,0x0B,0x0C,0x0D,0x0E,
  0x0F,0x10,0x11,0x12,0x13,0x14,0x15,0x16,0x17,0x18,0x19,0x1A,0x1B,0x1C,
  0x1D,0x1E,0x1F,0x20,0x21,0x22,0x23,0x24,0x25,0x26,0x29,
};
static const uint8_t PROFILE_REG_COUNT =
    (uint8_t)(sizeof(PROFILE_REGS) / sizeof(PROFILE_REGS[0]));

struct Profile {
  uint32_t  magic;
  uint16_t  version;
  uint16_t  size;
  uint32_t  spiHz;
  uint8_t   drive;            // 0 = S0S1, 1 = H0H1
  uint8_t   outputFormat;     // 0 = binary, 1 = CSV
  uint16_t  decimation;
  AfeTiming timing;
  uint32_t  regs[PROFILE_REG_COUNT];
  uint16_t  crc;              // over every byte before it
  uint16_t  pad;
};

static bool profileLoaded = false;      // a stored profile was applied at boot
static bool fsReady       = false;

static uint16_t profileCrc(const Profile &p) {
  return crc16((const uint8_t *)&p, offsetof(Profile, crc));
}

/* memset first: the struct has padding, and padding that is not written
 * is padding the CRC cannot agree about between two builds. */
static void profileCapture(Profile &p) {
  memset(&p, 0, sizeof(p));
  p.magic        = PROFILE_MAGIC;
  p.version      = PROFILE_VERSION;
  p.size         = (uint16_t)sizeof(p);
  p.spiHz        = afe.spiSpeed();
  p.drive        = afe.driveStrength() ? 1 : 0;
  p.outputFormat = outputFormat;
  p.decimation   = decimation;
  p.timing       = afe.timing();
  for (uint8_t i = 0; i < PROFILE_REG_COUNT; i++)
    p.regs[i] = afe.shadow(PROFILE_REGS[i]);
  p.crc = profileCrc(p);
}

static bool profileRead(Profile &p) {
  if (!fsReady) return false;
  File f(InternalFS);
  if (!f.open(PROFILE_PATH, FILE_O_READ)) return false;
  const int n = f.read((uint8_t *)&p, sizeof(p));
  f.close();
  return n == (int)sizeof(p) && p.magic == PROFILE_MAGIC
      && p.version == PROFILE_VERSION && p.size == sizeof(p)
      && p.crc == profileCrc(p);
}

static bool profileWrite() {
  if (!fsReady) return false;
  Profile p;
  profileCapture(p);
  InternalFS.remove(PROFILE_PATH);        // LittleFS opens for append otherwise
  File f(InternalFS);
  if (!f.open(PROFILE_PATH, FILE_O_WRITE)) return false;
  const size_t n = f.write((const uint8_t *)&p, sizeof(p));
  f.close();
  return n == sizeof(p);
}

static void profileApply(const Profile &p) {
  afe.setSpiSpeed(p.spiHz);
  afe.setDriveStrength(p.drive != 0);
  outputFormat = p.outputFormat ? 1 : 0;
  decimation   = p.decimation ? p.decimation : 1;
  for (uint8_t i = 0; i < PROFILE_REG_COUNT; i++)
    afe.writeReg(PROFILE_REGS[i], p.regs[i]);
  afe.timing() = p.timing;
  delay(60);        // four switched-cap filters, >3 ms each after a change
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
  if (linkMode == MODE_IDLE) linkMode = MODE_AUTO;
  if (streaming) return;
  ringHead = ringTail = 0;
  rdyPending = 0;
  batchOpenedUs = 0;
  pulse.reset();
  seqCounter = 0;
  dropCount = txDropCount = missedRdy = 0;
  // The radio counters are lifetime totals otherwise, which makes BLESTAT
  // unreadable the moment a session lasts longer than one experiment.
  bleBackpressure = bleNotifyPackets = bleQueueDrops = bleNotifyBytes = 0;
  bleDropCount = 0;
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

// =====================================================================
// BLE
// ---------------------------------------------------------------------
// A Nordic UART Service carrying exactly the same byte stream as the USB
// CDC link. The SoftDevice's radio work does not disturb acquisition: the
// AFE generates its own pulse timing and the MCU only reads the result
// registers when ADC_RDY fires, so radio jitter costs buffer depth, not
// sample timing. The ring absorbs it; `drop` in STATUS is the number to
// watch if it ever does not.
// =====================================================================
static void bleOnConnect(uint16_t handle) {
  bleConnHandle = handle;
  bleConnected  = true;
  bleTxHead = bleTxTail = 0;
  bleRxHead = bleRxTail = 0;
  bleNextTry = 0;
  BLEConnection *c = Bluefruit.Connection(handle);
  if (c) c->setTxPower(4);
  /* Do NOT request a PHY change, a data-length update, an MTU exchange or a
   * connection-parameter update here. Chrome on Windows is the central and
   * runs those procedures itself; a competing procedure from this side is
   * the most plausible cause of the link dropping with HCI 0x28. Whatever
   * the central settles on is what we live with - bleMtu() reports it. */
}

static void bleOnDisconnect(uint16_t handle, uint8_t reason) {
  (void)handle; (void)reason;
  bleConnected  = false;
  bleConnHandle = BLE_CONN_HANDLE_INVALID;
  bleTxHead = bleTxTail = 0;
  bleRxHead = bleRxTail = 0;
  streaming     = false;                     // do not stream into nothing
  // Advertising restarts on its own (restartOnDisconnect), so the LED
  // falls back from solid blue to a blink and the link can be picked up
  // again without touching the button.
}

static void bleSetup() {
  // Both of these have to happen before begin(): they size the SoftDevice's
  // buffers. BANDWIDTH_MAX asks for a 247-byte ATT MTU, which is what lets
  // a 207-byte frame travel as a single notification.
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);

  Bluefruit.begin(1, 0);                     // one peripheral, no central
  Bluefruit.setTxPower(4);
  Bluefruit.setName(BLE_DEVICE_NAME);
  /* Preferred connection parameters, published in the GAP PPCP characteristic
   * before anything connects. This is not the live renegotiation the comment
   * in bleOnConnect() warns against - it is the peripheral stating what it
   * wants up front, which is exactly what HECG01 does with the same window.
   * The BSP default asks for 20-30 ms; a steady stream is better served by a
   * central that grants 30 ms and then leaves it alone. */
  /* Windows grants the slowest interval in the range, so the range is the
   * request. 24-48 got 60 ms, and at 60 ms this link delivers only about
   * ten notifications a second - barely above the 7.8/s a 125 Hz stream
   * needs, which leaves nothing to catch up with after a stall. Ask for
   * 15-30 ms: the events are what the throughput is made of. */
  Bluefruit.Periph.setConnInterval(12, 24);            // 15 - 30 ms
  Bluefruit.Periph.setConnSupervisionTimeoutMS(6000);
  Bluefruit.Periph.setConnectCallback(bleOnConnect);
  Bluefruit.Periph.setDisconnectCallback(bleOnDisconnect);
  nusService.begin();

  nusTx.setProperties(CHR_PROPS_NOTIFY);
  nusTx.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  nusTx.setMaxLen(BLE_TXQ_SLOT);
  nusTx.begin();

  nusRx.setProperties(CHR_PROPS_WRITE | CHR_PROPS_WRITE_WO_RESP);
  nusRx.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  nusRx.setMaxLen(BLE_TXQ_SLOT);
  nusRx.setWriteCallback(nusRxCallback);
  nusRx.begin();

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(nusService);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);   // 20 ms fast, 152 ms slow
  Bluefruit.Advertising.setFastTimeout(30);
}

static void bleStart() {
  Bluefruit.Advertising.restartOnDisconnect(true);
  if (!Bluefruit.Advertising.isRunning() && !bleConnected)
    Bluefruit.Advertising.start(0);           // 0 = advertise until told not to
}

static void bleStop() {
  /* The restart-on-disconnect hook has to go first. Otherwise dropping the
   * central here immediately re-arms advertising and the double press that
   * was meant to reach idle white lands back on blue blink. */
  Bluefruit.Advertising.restartOnDisconnect(false);
  Bluefruit.Advertising.stop();
  if (bleConnected) Bluefruit.disconnect(bleConnHandle);
  bleConnected  = false;
  bleConnHandle = BLE_CONN_HANDLE_INVALID;
  bleTxHead = bleTxTail = 0;
}

// =====================================================================
// BUTTON on D5
// ---------------------------------------------------------------------
// Momentary to ground with the internal pull-up, so idle reads HIGH. The
// gesture the operator asked for is a double press; a long press is added
// as the way out of the IDLE latch that does not depend on remembering
// which state you are in.
// =====================================================================
static const uint32_t SW_DEBOUNCE_MS = 25;
static const uint32_t SW_GAP_MS      = 450;   // window for the second press
static const uint32_t SW_LONG_MS     = 1000;

static bool     swStable   = true;    // HIGH = released
static bool     swLastRaw  = true;
static uint32_t swEdgeMs   = 0;
static uint32_t swPressMs  = 0;
static uint8_t  swClicks   = 0;
static uint32_t swFirstMs  = 0;
static bool     swLongDone = false;

static void onDoublePress();
static void onLongPress();

static void serviceButton(uint32_t nowMs) {
  const bool raw = (digitalRead(PIN_SW) != LOW);      // HIGH = released
  if (raw != swLastRaw) { swLastRaw = raw; swEdgeMs = nowMs; }

  if (raw != swStable && (uint32_t)(nowMs - swEdgeMs) >= SW_DEBOUNCE_MS) {
    swStable = raw;
    if (!swStable) {                                  // pressed
      swPressMs  = nowMs;
      swLongDone = false;
    } else {                                          // released
      if (!swLongDone) {
        if (swClicks == 0) { swClicks = 1; swFirstMs = nowMs; }
        else               { swClicks = 0; onDoublePress(); }
      }
      swLongDone = false;
    }
  }

  // A press held past the long threshold fires once and cancels the click.
  if (!swStable && !swLongDone && (uint32_t)(nowMs - swPressMs) >= SW_LONG_MS) {
    swLongDone = true;
    swClicks   = 0;
    onLongPress();
  }
  // A lone click that never got a partner is not a double press.
  if (swClicks == 1 && (uint32_t)(nowMs - swFirstMs) > SW_GAP_MS) swClicks = 0;
}

static void onDoublePress() {
  switch (linkMode) {
    case MODE_BLE:                      // advertising or connected -> quiet
      bleStop();
      streaming = false;
      linkMode  = MODE_IDLE;
      break;
    case MODE_IDLE:                     // quiet -> radio on
      linkMode = MODE_BLE;
      bleStart();
      break;
    case MODE_AUTO:
    default:
      if (TinyUSBDevice.mounted()) {    // red -> quiet
        streaming = false;
        linkMode  = MODE_IDLE;
      } else {                          // white -> radio on
        linkMode = MODE_BLE;
        bleStart();
      }
      break;
  }
  sendLineF("EVT:MODE:%s",
            linkMode == MODE_BLE ? "BLE" : (linkMode == MODE_IDLE ? "IDLE" : "AUTO"));
}

static void onLongPress() {
  bleStop();
  linkMode = MODE_AUTO;
  sendLine("EVT:MODE:AUTO");
}

/* The same rule the driver applies internally, exposed so RATES? can report
 * the ceiling rather than making the host rediscover it: a conversion takes
 * 50 us = 200 counts, every average has to finish inside its own quarter of
 * the period, and the field saturates at 16 averages. */
static uint8_t maxAveragesFor(uint32_t prpCounts) {
  const uint32_t perQuarter = (prpCounts / 4) / 200;
  if (perQuarter < 2) return 0;
  const uint32_t n = perQuarter - 2;
  return (uint8_t)(n > 15 ? 15 : n);
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
    afe.syncTimingFromShadow(a == AFE_CONTROL1);
    if (a == AFE_CONTROL0) { sendLineF("ACK:W:%02lX=%06lX", (unsigned long)a, (unsigned long)(v & 0xFFFFFF)); return; }
    const uint32_t back = afe.readReg((uint8_t)a);
    sendLineF("ACK:W:%02lX=%06lX", (unsigned long)a, (unsigned long)back);
    return;
  }

  /* WM:aa=vvvvvv,aa=vvvvvv,...   several registers as one transaction.
   *
   * W: is one register per round trip and a timing change is thirty of them.
   * Sent one at a time, the sequencer spends the whole burst running on a mix
   * of old and new edges - the fault applyTiming() halts the timer to avoid -
   * so this does the same: if anything in 0x01..0x1D is in the batch, TIMEREN
   * is stopped, everything is written, the period counter restarts, CONTROL1
   * goes last, and whatever was converted in between is discarded.
   *
   * Gain and LED writes are NOT flushed. The analog chain's step response is
   * then on the trace, which is what someone tuning a register wants to see.
   *
   * seq= in the reply is the sequence number of the first sample shipped
   * after the write took effect, so the console can mark that exact sample
   * rather than the moment the acknowledgement happened to arrive.
   *
   * Everything is validated before anything is written: a batch with one bad
   * pair changes nothing. */
  if (!strncmp(line, "WM:", 3)) {
    static const uint8_t WM_MAX = 48;
    uint8_t  wa[WM_MAX];
    uint32_t wv[WM_MAX];
    uint8_t  n = 0;
    for (char *tok = strtok(line + 3, ","); tok; tok = strtok(nullptr, ",")) {
      char *eq = strchr(tok, '=');
      if (!eq) { sendLine("ERR:BAD_WM_FORMAT"); return; }
      *eq = 0;
      uint32_t a, v;
      if (!argU32(tok, &a, 16) || !argU32(eq + 1, &v, 16)) {
        sendLine("ERR:BAD_WM_FORMAT"); return;
      }
      if (a > AFE_ALARM || a == AFE_RESERVED1 || a == AFE_RESERVED2) {
        sendLineF("ERR:WM_READONLY:%02lX", (unsigned long)a); return;
      }
      if (v > 0x00FFFFFFUL) { sendLineF("ERR:BAD_VALUE:%02lX", (unsigned long)a); return; }
      if (n >= WM_MAX)      { sendLine("ERR:WM_TOO_MANY"); return; }
      wa[n] = (uint8_t)a; wv[n] = v; n++;
    }
    if (!n) { sendLine("ERR:BAD_WM_FORMAT"); return; }

    bool timing = false, wroteC1 = false, swrst = false;
    for (uint8_t i = 0; i < n; i++) {
      if (wa[i] >= AFE_LED2STC && wa[i] <= AFE_PRPCOUNT) timing = true;
      if (wa[i] == AFE_CONTROL1) wroteC1 = true;
      if (wa[i] == AFE_CONTROL0 && (wv[i] & AFE_C0_SW_RST)) swrst = true;
    }
    const uint32_t c2Before   = afe.shadow(AFE_CONTROL2);
    const bool     wasRunning = (afe.shadow(AFE_CONTROL1) & AFE_C1_TIMEREN) != 0;
    if (timing && wasRunning) afe.setTimerEnable(false);

    // Pass 1: everything except CONTROL1, in the order given.
    for (uint8_t i = 0; i < n; i++) {
      if (wa[i] == AFE_CONTROL1) continue;
      if (wa[i] == AFE_CONTROL0 && (wv[i] & AFE_C0_SW_RST)) afe.softwareReset();
      else afe.writeReg(wa[i], wv[i]);
    }
    if (timing && !swrst) {
      const uint32_t rd = afe.spiReadMode() ? AFE_C0_SPI_READ : 0;
      afe.writeReg(AFE_CONTROL0, rd | AFE_C0_TIM_CNT_RST);
      afe.writeReg(AFE_CONTROL0, rd);
    }
    // Pass 2: CONTROL1 last, so TIMEREN never runs half-written timing.
    for (uint8_t i = 0; i < n; i++)
      if (wa[i] == AFE_CONTROL1) afe.writeReg(AFE_CONTROL1, wv[i]);
    if (timing && wasRunning && !wroteC1 && !swrst) afe.setTimerEnable(true);

    afe.syncTimingFromShadow(wroteC1);

    const bool txref = ((afe.shadow(AFE_CONTROL2) ^ c2Before) & AFE_C2_TXREF_MSK) != 0;
    const bool running = (afe.shadow(AFE_CONTROL1) & AFE_C1_TIMEREN) != 0;
    if (timing && running) delay(60);    // four switched-cap filters settle
    if (txref) delay(20);                // 2.2 uF on TX_REF, see setTxRef()
    /* Any delay above leaves ADC_RDY counts behind that would be read back as
     * copies of one conversion, so a transaction that waited also flushes. */
    const bool flushed = timing || txref || swrst;

    uint32_t seq;
    if (flushed) {
      flushAcquisition();
      seq = seqCounter;
    } else {
      const uint32_t inRing = (uint32_t)((ringHead + RING_SIZE - ringTail) % RING_SIZE);
      const uint16_t dec    = decimation ? decimation : 1;
      seq = seqCounter + inRing + (uint32_t)((rdyPending + decCounter) / dec);
    }

    static char out[48 + WM_MAX * 10];
    int k = snprintf(out, sizeof(out), "ACK:WM:n=%u,seq=%lu,flush=%u,prf=%u",
                     n, (unsigned long)seq, flushed ? 1u : 0u, afe.timing().prf);
    for (uint8_t i = 0; i < n && k > 0 && k < (int)sizeof(out) - 12; i++) {
      const uint32_t back = (wa[i] == AFE_CONTROL0) ? (wv[i] & 0x00FFFFFFUL)
                                                     : afe.readReg(wa[i]);
      k += snprintf(out + k, sizeof(out) - k, ",%02X=%06lX", wa[i], (unsigned long)back);
    }
    // The read-back costs SPI time ADC_RDY does not wait for; more than one
    // pending conversion would only be read as a duplicate of the latest.
    if (rdyPending > 1) { missedRdy += rdyPending - 1; rdyPending = 1; }
    sendLine(out);
    return;
  }

  /* REGS?    every register from the driver's shadow, one line, no SPI.
   * REGS?HW  the same from silicon, without touching the shadow - so the
   *          console can diff the two and catch a register the bus corrupted.
   * CONTROL0 is write-only, so both report its shadow. */
  if (!strcmp(line, "REGS?") || !strcmp(line, "REGS?HW")) {
    const bool hw = (line[5] == 'H');
    static char out[16 + 7 * (AFE_REG_MAX + 1)];
    int k = snprintf(out, sizeof(out), "%s", hw ? "REGSHW:" : "REGS:");
    for (uint8_t a = 0; a <= AFE_REG_MAX && k > 0 && k < (int)sizeof(out) - 8; a++) {
      const uint32_t v = (hw && a != AFE_CONTROL0) ? afe.readReg(a) : afe.shadow(a);
      k += snprintf(out + k, sizeof(out) - k, a ? ",%06lX" : "%06lX", (unsigned long)v);
    }
    if (rdyPending > 1) { missedRdy += rdyPending - 1; rdyPending = 1; }
    sendLine(out);
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
    /* Whatever reached the ring during the retune belongs to the old timing
     * or to the settling transient, and downstream a transient is
     * indistinguishable from a signal. Drop it rather than ship it. */
    flushAcquisition();
    const AfeTiming &n = afe.timing();
    sendLineF("ACK:PRF:%u,%u,%u,%u,numav=%u,swin=%u,cwin=%u,max=%u,seq=%lu",
              n.prf, n.dutyPct, n.settle, n.adcRst, n.numav,
              afe.sampleWindow(), afe.convertWindow(), AFE4490::maxPrf(n.adcRst),
              (unsigned long)seqCounter);
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

  // ---- link ------------------------------------------------------------
  if (!strcmp(line, "BLE:ON"))  { linkMode = MODE_BLE;  bleStart(); sendLine("ACK:BLE:ON");  return; }
  if (!strcmp(line, "BLE:OFF")) { bleStop(); linkMode = MODE_AUTO;  sendLine("ACK:BLE:OFF"); return; }
  if (!strcmp(line, "IDLE"))    { bleStop(); streaming = false; linkMode = MODE_IDLE;
                                  sendLine("ACK:IDLE"); return; }
  if (!strcmp(line, "LINK?")) {
    sendLineF("LINK:mode=%s,active=%s,ble=%u,adv=%u,mtu=%u,name=%s,usb=%u",
              linkMode == MODE_BLE ? "BLE" : (linkMode == MODE_IDLE ? "IDLE" : "AUTO"),
              bleReady() ? "BLE" : (usbReady() ? "USB" : "NONE"),
              bleConnected ? 1u : 0u,
              Bluefruit.Advertising.isRunning() ? 1u : 0u,
              bleMtu(), BLE_DEVICE_NAME, TinyUSBDevice.mounted() ? 1u : 0u);
    return;
  }
  if (!strcmp(line, "SAVE")) {
    if (!fsReady) { sendLine("ERR:NO_FS"); return; }
    if (!profileWrite()) { sendLine("ERR:SAVE_FAILED"); return; }
    profileLoaded = true;
    sendLineF("ACK:SAVE:bytes=%u,regs=%u", (unsigned)sizeof(Profile),
              PROFILE_REG_COUNT);
    return;
  }
  if (!strcmp(line, "LOAD")) {
    Profile p;
    if (!profileRead(p)) { sendLine("ERR:NO_PROFILE"); return; }
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    profileApply(p);
    profileLoaded = true;
    sendLineF("ACK:LOAD:prf=%u,dec=%u", afe.timing().prf, decimation);
    if (wasStreaming) startStream();
    return;
  }
  if (!strcmp(line, "FACTORY")) {
    /* DEFAULTS resets the chip and leaves the stored profile alone, so a
     * power cycle brings the saved settings back. FACTORY is the one that
     * forgets - it has to erase the file as well or the next boot would
     * quietly undo it. */
    const bool wasStreaming = streaming;
    if (wasStreaming) stopStream();
    if (fsReady) InternalFS.remove(PROFILE_PATH);
    profileLoaded = false;
    decimation = 1;
    outputFormat = 0;
    afe.applyDefaults();
    sendLine("ACK:FACTORY");
    if (wasStreaming) startStream();
    return;
  }
  if (!strncmp(line, "FRAME:", 6)) {
    if      (!strcmp(line + 6, "AUTO"))    frameMode = FRAME_AUTO;
    else if (!strcmp(line + 6, "FULL"))    frameMode = FRAME_FULL;
    else if (!strcmp(line + 6, "COMPACT")) frameMode = FRAME_COMPACT;
    else { sendLine("ERR:BAD_FRAME"); return; }
    flushAcquisition();                 // the format changes mid-stream
    sendLineF("ACK:FRAME:%s,now=%s",
              frameMode == FRAME_AUTO ? "AUTO"
                : (frameMode == FRAME_FULL ? "FULL" : "COMPACT"),
              useCompactFrame() ? "COMPACT" : "FULL");
    return;
  }
  if (!strcmp(line, "REBOOT")) {
    /* The only honest way to check that a profile survives: come up the
     * way a power cycle comes up. The reply has to be flushed before the
     * core stops running, hence the delay. */
    sendLine("ACK:REBOOT");
    delay(120);
    NVIC_SystemReset();
    return;                                   // not reached
  }
  if (!strcmp(line, "PROFILE?")) {
    Profile p;
    const bool have = profileRead(p);
    sendLineF("PROFILE:fs=%u,stored=%u,applied=%u,bytes=%u,regs=%u,ver=%u"
              ",prf=%u,duty=%u,dec=%u,spihz=%lu,drive=%s",
              fsReady ? 1u : 0u, have ? 1u : 0u, profileLoaded ? 1u : 0u,
              (unsigned)sizeof(Profile), PROFILE_REG_COUNT, PROFILE_VERSION,
              have ? p.timing.prf : 0, have ? p.timing.dutyPct : 0,
              have ? p.decimation : 0,
              (unsigned long)(have ? p.spiHz : 0),
              have ? (p.drive ? "H0H1" : "S0S1") : "-");
    return;
  }
  if (!strcmp(line, "RATES?")) {
    /* PRPCOUNT is an integer, so the achievable rates are 4 MHz over an
     * integer - the PRF you ask for and the PRF you get are not the same
     * number. prpct is reported so the host can work out the exact one
     * instead of trusting the rounded request. */
    const AfeTiming &t = afe.timing();
    uint32_t T = (afe.shadow(AFE_PRPCOUNT) & 0xFFFFUL) + 1UL;
    if (T < 800) T = 4000000UL / (t.prf ? t.prf : 1);
    const uint16_t dec = decimation ? decimation : 1;
    sendLineF("RATES:clk=4000000,prpmin=800,prpmax=64000,prfmin=62,prfmax=%u,"
              "decmin=1,decmax=64,convus=50,prpct=%lu,prf=%u,numav=%u,"
              "numavmax=%u,swin=%u,cwin=%u,fsmhz=%lu",
              AFE4490::maxPrf(t.adcRst),
              (unsigned long)(T - 1), t.prf, t.numav, maxAveragesFor(T),
              afe.sampleWindow(), afe.convertWindow(),
              (unsigned long)((4000000000ULL / T) / dec));
    return;
  }
  if (!strcmp(line, "BAT?")) {
    const float v = readBatteryVolts();        // fresh, not the 10 s cache
    vbatCached = v; vbatStampMs = millis();
    const ChargeState c = chargeState();
    sendLineF("BAT:mv=%d,raw=%u,pct=%u,state=%s,chg=%u,vbus=%u,"
              "trim=%.4f,div=%.4f,tacq=40,ovs=8",
              (int)(v * 1000.0f + 0.5f), vbatCounts, batteryPercent(v),
              chargeName(c), (unsigned)c, vbusPresent() ? 1u : 0u,
              vbatTrim, VBAT_DIV);
    return;
  }
  if (!strncmp(line, "BATCAL:", 7)) {
    /* Trim against a meter rather than against the datasheet: the band-gap
     * reference and the two divider resistors all have tolerance, and no
     * amount of arithmetic here can find out what they actually are. */
    if (!strcmp(line + 7, "RESET")) {
      vbatTrim = 1.0f;
      sendLineF("ACK:BATCAL:trim=%.4f", vbatTrim);
      return;
    }
    uint32_t mv;
    if (!argU32(line + 7, &mv) || mv < 2500 || mv > 4500) {
      sendLine("ERR:BAD_BATCAL");            // millivolts, as a meter reads it
      return;
    }
    const float raw = (float)vbatCounts * (3.0f / 4096.0f) * VBAT_DIV;
    if (raw < 0.5f) { sendLine("ERR:NO_READING"); return; }
    vbatTrim = ((float)mv / 1000.0f) / raw;
    vbatCached = 0.0f;                        // force the cache to refill
    sendLineF("ACK:BATCAL:trim=%.4f", vbatTrim);
    return;
  }
  if (!strncmp(line, "BATCHG:", 7)) {
    // 200 mAh at 100 mA is 0.5 C, at 50 mA it is 0.25 C. Both are within
    // spec for a normal cell; the slower one runs cooler.
    if (!strcmp(line + 7, "100"))      { pinMode(PIN_HICHG, OUTPUT); digitalWrite(PIN_HICHG, LOW); }
    else if (!strcmp(line + 7, "50"))  { pinMode(PIN_HICHG, OUTPUT); digitalWrite(PIN_HICHG, HIGH); }
    else { sendLine("ERR:BAD_BATCHG"); return; }
    sendLineF("ACK:BATCHG:%s", line + 7);
    return;
  }
  if (!strcmp(line, "BLESTAT?")) {
    /* dle and ci are the two numbers that settle any argument about
     * throughput: without data-length extension a notification moves 20
     * bytes per connection event, and ci says how often those happen. */
    const uint32_t upS = millis() / 1000UL;
    sendLineF("BLESTAT:mtu=%u,dle=%u,ci=%u,queue=%u/%u,backpressure=%lu,"
              "packets=%lu,bytes=%lu,bps=%lu,qdrops=%lu,txdrop=%lu,drop=%lu",
              bleMtu(), bleDataLength(), bleIntervalUnits(),
              bleTxDepth(), BLE_TXQ_DEPTH,
              (unsigned long)bleBackpressure, (unsigned long)bleNotifyPackets,
              (unsigned long)bleNotifyBytes,
              (unsigned long)(upS ? bleNotifyBytes / upS : 0),
              (unsigned long)bleQueueDrops, (unsigned long)txDropCount,
              (unsigned long)dropCount);
    return;
  }

  sendLine("ERR:UNKNOWN_COMMAND");
}

// =====================================================================
// SETUP / LOOP
// =====================================================================
/* Long enough for WM: carrying every writable register in one line - the
 * only way a timing change reaches the chip as one transaction. */
static char     cmdBuf[640];
static uint16_t cmdLen = 0;
static uint32_t lastStatus = 0;
static bool     lastUsbState = false;
static bool     lastBleState = false;

void setup() {
  Serial.begin(115200);

  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);
  ledWrite(true, true, true);           // white until something attaches

  pinMode(PIN_SW, INPUT_PULLUP);
  /* ~CHG is open drain and shares the net with the orange charge LED, whose
   * series resistor already pulls it up. The internal pull-up costs nothing
   * and removes the question. HICHG is deliberately left alone: charge
   * current is a decision about the cell, not about the firmware. */
  pinMode(PIN_CHG, INPUT_PULLUP);
  swStable = swLastRaw = (digitalRead(PIN_SW) != LOW);

  // The radio is brought up but left silent. Nothing advertises until the
  // operator asks for it with a double press, so a board on the bench is
  // not discoverable by accident.
  bleSetup();

  const bool linkOk = afe.begin(4000000UL);
  afe.applyDefaults();

  /* Defaults first, always: a stored profile only covers the registers it
   * knows about, so anything added to the map in a later firmware still
   * starts from a sane value rather than from whatever survived. */
  fsReady = InternalFS.begin();
  {
    Profile p;
    if (fsReady && profileRead(p)) { profileApply(p); profileLoaded = true; }
  }

  // Give the host a moment to enumerate before the banner goes out.
  const uint32_t t0 = millis();
  while (!usbReady() && (millis() - t0) < 2500) delay(10);

  sendLineF("EVT:BOOT:%s,proto=%s", FIRMWARE_VERSION, LINK_PROTOCOL);
  sendLineF("EVT:SPI:%s,sclk=%lu,drive=%s", linkOk ? "OK" : "FAIL",
            (unsigned long)afe.spiSpeed(), afe.driveStrength() ? "H0H1" : "S0S1");
  sendLineF("EVT:PINS:cs=D0,mosi=D1,sck=D2,miso=D3,rdy=D4,sw=D5,rev=2");
  sendLineF("EVT:PROFILE:%s,fs=%u",
            profileLoaded ? "LOADED" : (fsReady ? "NONE" : "FS_FAIL"),
            fsReady ? 1u : 0u);
  sendLineF("EVT:BLE:READY,name=%s,adv=0", BLE_DEVICE_NAME);
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
      const bool     onBle    = bleReady();
      const uint8_t  perFrame = useCompactFrame() ? BLE_SAMPLES_PER_FRAME
                                                  : USB_SAMPLES_PER_FRAME;
      const uint32_t maxAge   = onBle ? BLE_BATCH_MAX_AGE_US : USB_BATCH_MAX_AGE_US;
      const uint16_t avail =
          (uint16_t)((ringHead + RING_SIZE - ringTail) % RING_SIZE);
      if (avail == 0) {
        batchOpenedUs = 0;
      } else {
        if (batchOpenedUs == 0) batchOpenedUs = micros();
        const bool full  = avail >= perFrame;
        const bool stale = (uint32_t)(micros() - batchOpenedUs) >= maxAge;
        if (full || stale) {
          /* Copy out without moving ringTail. If the transport will not take
           * the frame the samples stay where they are and go out on a later
           * pass, so a stalled central costs buffer depth instead of a hole
           * in the trace - which is what the old pop-then-send order paid
           * every time the radio hesitated. */
          AfeSample batch[SAMPLES_PER_FRAME];
          uint8_t n = 0;
          uint16_t t = ringTail;
          while (t != ringHead && n < perFrame) {
            batch[n++] = ring[t];
            t = (uint16_t)((t + 1) % RING_SIZE);
          }
          if (n && sendSampleFrame(batch, n)) {
            ringTail      = t;
            batchOpenedUs = 0;
          }
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

  // ---- 3. console - the same parser on either transport -----------------
  auto feedCommand = [](int c) {
    if (c < 0) return;
    if (c == '\n' || c == '\r') {
      if (cmdLen) { cmdBuf[cmdLen] = 0; handleCommand(cmdBuf); cmdLen = 0; }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = (char)c;
    }
  };
  while (Serial.available()) feedCommand(Serial.read());
  for (int c; (c = bleRxRead()) >= 0; ) feedCommand(c);

  // ---- 4. radio, then heartbeat ----------------------------------------
  serviceBleTx();
  const uint32_t now = millis();
  serviceButton(now);
  serviceLeds(now);
  if (now - lastStatus >= 1000) {
    lastStatus = now;
    if (streaming) sendStatus();
  }

  // Re-announce after a reconnect so a console that attaches late still
  // learns what it is talking to without the user pressing anything. The
  // same applies to a BLE central that has just subscribed.
  const bool usb = usbReady();
  const bool ble = bleReady();
  if ((usb && !lastUsbState) || (ble && !lastBleState)) {
    delay(50);
    sendLineF("EVT:BOOT:%s,proto=%s", FIRMWARE_VERSION, LINK_PROTOCOL);
    sendCaps();
    sendLine("EVT:READY");
  }
  // Plugging or unplugging the cable is a deliberate act, so it clears the
  // IDLE latch - otherwise the only way out would be to remember that a
  // double press cycles onward to BLE rather than back to red.
  if (usb != lastUsbState && linkMode == MODE_IDLE) linkMode = MODE_AUTO;
  lastUsbState = usb;
  lastBleState = ble;
}
