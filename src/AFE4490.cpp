#include "AFE4490.h"
#include <nrf_gpio.h>

extern const uint32_t g_ADigitalPinMap[];

// =====================================================================
// CONSTRUCTION / LOW-LEVEL SPI
// =====================================================================
AFE4490::AFE4490(SPIClass &spi, uint8_t csPin, uint8_t resetPin,
                 uint8_t pwdnPin, uint8_t rdyPin,
                 uint8_t sckPin, uint8_t mosiPin, bool onSpim3)
  : _spi(spi), _cs(csPin), _reset(resetPin), _pwdn(pwdnPin), _rdy(rdyPin),
    _sck(sckPin), _mosi(mosiPin), _spim3(onSpim3), _highDrive(false),
    _spiHz(4000000UL), _spiReadMode(false), _inTxn(false) {
  _timing = { 500, 25, 50, 4, 8, true };
  for (uint16_t i = 0; i <= AFE_REG_MAX; i++) _shadow[i] = 0;
}

void AFE4490::beginTxn() {
  if (_inTxn) return;
  _spi.beginTransaction(SPISettings(_spiHz, MSBFIRST, SPI_MODE0));
  _inTxn = true;
}

void AFE4490::endTxn() {
  if (!_inTxn) return;
  _spi.endTransaction();
  _inTxn = false;
}

/* One complete AFE frame: 8 address bits then 24 data bits, CS low for
 * the whole 32 clocks. On a write the AFE ignores SPISOMI; on a read it
 * drives the register contents there while we shift out dummy data. */
void AFE4490::transfer32(uint8_t addr, uint32_t data, uint32_t *rx) {
  uint8_t tx[4], rb[4];
  tx[0] = addr;
  tx[1] = (uint8_t)(data >> 16);
  tx[2] = (uint8_t)(data >> 8);
  tx[3] = (uint8_t)(data);

  beginTxn();
  digitalWrite(_cs, LOW);
  _spi.transfer(tx, rb, 4);
  digitalWrite(_cs, HIGH);
  endTxn();

  if (rx) *rx = ((uint32_t)rb[1] << 16) | ((uint32_t)rb[2] << 8) | rb[3];
}

/* SPI_READ is a persistent mode bit, so only touch CONTROL0 when the
 * direction actually changes. Streaming leaves it at 1 permanently. */
void AFE4490::setSpiReadMode(bool enable) {
  if (_spiReadMode == enable) return;
  _spiReadMode = enable;
  uint32_t c0 = _shadow[AFE_CONTROL0] & ~AFE_C0_SPI_READ;
  if (enable) c0 |= AFE_C0_SPI_READ;
  _shadow[AFE_CONTROL0] = c0;
  transfer32(AFE_CONTROL0, c0, nullptr);
}

void AFE4490::writeReg(uint8_t addr, uint32_t value) {
  value &= 0x00FFFFFFUL;
  if (addr == AFE_CONTROL0) {
    // CONTROL0 is write-only and self-clearing for SW_RST/DIAG_EN, so it
    // is never shadowed beyond the sticky SPI_READ/TIM_CNT_RST bits.
    _spiReadMode = (value & AFE_C0_SPI_READ) != 0;
    _shadow[AFE_CONTROL0] = value & (AFE_C0_SPI_READ | AFE_C0_TIM_CNT_RST);
    transfer32(AFE_CONTROL0, value, nullptr);
    return;
  }
  setSpiReadMode(false);
  transfer32(addr, value, nullptr);
  if (addr <= AFE_REG_MAX) _shadow[addr] = value;
}

uint32_t AFE4490::readReg(uint8_t addr) {
  setSpiReadMode(true);
  uint32_t v = 0;
  transfer32(addr, 0, &v);
  return v & 0x00FFFFFFUL;
}

void AFE4490::refreshShadow() {
  setSpiReadMode(true);
  for (uint8_t a = 0x01; a <= AFE_REG_MAX; a++) {
    uint32_t v = 0;
    transfer32(a, 0, &v);
    _shadow[a] = v & 0x00FFFFFFUL;
  }
}

/* Mirrors SPIClass::setClockDivider() in the Adafruit BSP, which does
 *   div = 64 MHz / requested
 * and then walks a ladder of `div >=` tests. The effect is that SCLK is
 * rounded UP to the next available step, and that SPIM0/1/2 saturate at
 * 8 MHz because only SPIM3 has the 16 and 32 MHz dividers.
 *
 * Asking for 6 MHz on SPIM1 therefore gives 8 MHz, and asking for 200 kHz
 * gives 250 kHz. Quantizing here keeps the driver's idea of the clock and
 * the silicon's in agreement, so a reported speed is never a fiction. */
uint32_t AFE4490::quantizeSpiHz(uint32_t hz, bool onSpim3) {
  if (hz < 1) hz = 1;
  const uint32_t div = 64000000UL / hz;
  if (div >= 512) return 125000UL;
  if (div >= 256) return 250000UL;
  if (div >= 128) return 500000UL;
  if (div >= 64)  return 1000000UL;
  if (div >= 32)  return 2000000UL;
  if (div >= 16)  return 4000000UL;
  if (div >= 8)   return 8000000UL;
  if (onSpim3)    return (div >= 4) ? 16000000UL : 32000000UL;
  return 8000000UL;
}

void AFE4490::setSpiSpeed(uint32_t hz) {
  // The AFE itself allows tSCLK down to 62.5 ns, i.e. 16 MHz, so on this
  // wiring the SPIM ceiling is what binds, not the chip.
  _spiHz = quantizeSpiHz(hz, _spim3);
}

void AFE4490::setDriveStrength(bool high) {
  _highDrive = high;
  if (_sck == 0xFF || _mosi == 0xFF) return;
  const nrf_gpio_pin_drive_t drive = high ? NRF_GPIO_PIN_H0H1 : NRF_GPIO_PIN_S0S1;
  nrf_gpio_cfg(g_ADigitalPinMap[_sck],
               NRF_GPIO_PIN_DIR_OUTPUT, NRF_GPIO_PIN_INPUT_CONNECT,
               NRF_GPIO_PIN_NOPULL, drive, NRF_GPIO_PIN_NOSENSE);
  nrf_gpio_cfg(g_ADigitalPinMap[_mosi],
               NRF_GPIO_PIN_DIR_OUTPUT, NRF_GPIO_PIN_INPUT_DISCONNECT,
               NRF_GPIO_PIN_NOPULL, drive, NRF_GPIO_PIN_NOSENSE);
}

/* Measure, rather than assume, what this wiring will carry. Runs the
 * walking-pattern self-test at every SPIM step and reports the error
 * count for each. The caller decides what to do with the answer. */
uint8_t AFE4490::spiSweep(SpiSweepStep *out, uint8_t maxSteps, uint16_t trials) {
  static const uint32_t LADDER[] = {
    125000UL, 250000UL, 500000UL, 1000000UL, 2000000UL, 4000000UL,
    8000000UL, 16000000UL, 32000000UL
  };
  const uint32_t saved = _spiHz;
  const uint32_t ceiling = maxSpiHz();
  uint8_t n = 0;
  for (uint8_t i = 0; i < (sizeof(LADDER)/sizeof(LADDER[0])) && n < maxSteps; i++) {
    if (LADDER[i] > ceiling) break;
    _spiHz = LADDER[i];
    out[n].hz = LADDER[i];
    out[n].errors = selfTest(trials);
    n++;
  }
  _spiHz = saved;
  return n;
}

// =====================================================================
// LIFECYCLE
// =====================================================================
bool AFE4490::begin(uint32_t spiHz) {
  setSpiSpeed(spiHz);

  /* 0xFF means "not wired". Rev 2 of the board brings out neither RESET
   * nor PWDN, and D5 - which used to be RESET - is now the button, so
   * driving it as an output would fight the pull-up. */
  pinMode(_cs, OUTPUT); digitalWrite(_cs, HIGH);
  if (_reset != 0xFF) { pinMode(_reset, OUTPUT); digitalWrite(_reset, HIGH); }
  if (_pwdn  != 0xFF) { pinMode(_pwdn,  OUTPUT); digitalWrite(_pwdn,  HIGH); }  // active low
  pinMode(_rdy, INPUT);

  _spi.begin();
  // begin() is where the BSP stamps H0H1 onto SCK/MOSI, so the drive
  // strength has to be re-applied afterwards, not before.
  setDriveStrength(_highDrive);
  delay(10);
  hardwareReset();

  // Prove the link before anyone trusts a measurement.
  return selfTest(16) == 0;
}

/* Datasheet section 7.7: RESET must be low for at least 0.5 ms (t3) and
 * SPI must stay quiet for 1 us afterwards (t4). The generous delays here
 * also cover t2 when begin() runs straight after power-up. */
void AFE4490::hardwareReset() {
  // No RESET line on this board: fall back to CONTROL0's SW_RST, which
  // reaches the same state, just over SPI instead of over a wire.
  if (_reset == 0xFF) { softwareReset(); return; }
  if (_pwdn != 0xFF) { digitalWrite(_pwdn, HIGH); delay(20); }
  digitalWrite(_reset, LOW);
  delay(5);
  digitalWrite(_reset, HIGH);
  delay(50);
  _spiReadMode = false;
  for (uint16_t i = 0; i <= AFE_REG_MAX; i++) _shadow[i] = 0;
}

void AFE4490::softwareReset() {
  writeReg(AFE_CONTROL0, AFE_C0_SW_RST);
  delay(50);
  _spiReadMode = false;
  for (uint16_t i = 0; i <= AFE_REG_MAX; i++) _shadow[i] = 0;
}

void AFE4490::powerDown(bool afe, bool rx, bool tx) {
  uint32_t c2 = _shadow[AFE_CONTROL2];
  c2 &= ~(AFE_C2_PDNAFE | AFE_C2_PDNRX | AFE_C2_PDNTX);
  if (afe) c2 |= AFE_C2_PDNAFE;
  if (rx)  c2 |= AFE_C2_PDNRX;
  if (tx)  c2 |= AFE_C2_PDNTX;
  writeReg(AFE_CONTROL2, c2);
}

// =====================================================================
// TIMING GENERATOR
// ---------------------------------------------------------------------
// The pulse-repetition period is divided into four equal quarters. Each
// quarter samples one phase and simultaneously converts the phase that
// was sampled one quarter earlier, in this fixed order:
//
//   quarter 0 : sample ambient(LED2)  | convert LED2
//   quarter 1 : sample LED1  + LED1 on| convert ambient(LED2)
//   quarter 2 : sample ambient(LED1)  | convert LED1
//   quarter 3 : sample LED2  + LED2 on| convert ambient(LED1)
//
// Inside a quarter that starts at count P:
//   LED on      : [P,            P + Ton - 1]
//   Rx sample   : [P + settle,   P + Ton - 2]   (settle skips LED/cable rise)
//   ADC reset   : [P,            P + adcRst - 1]
//   ADC convert : [P + adcRst,   P + Q     - 1]
//
// With PRF = 500 Hz and 25 % duty this reproduces datasheet Table 2
// exactly (PRPCOUNT 7999, LED2STC 6050, ALED2ENDC 1998, and so on).
// =====================================================================
uint32_t AFE4490::computeNumAv(uint32_t prpCounts) const {
  // Each ADC conversion takes 50 us = 200 counts of the 4 MHz clock and
  // must finish inside one quarter of the PRP (datasheet equation 5).
  uint32_t perQuarter = (prpCounts / 4) / 200;
  if (perQuarter < 2) return 0;
  uint32_t n = perQuarter - 2;           // NUMAV = (averages - 1)
  if (n > 15) n = 15;                    // averaging saturates at 16
  return n;
}

/* A quarter of the period has to hold both of the datasheet's windows:
 * [settle, Ton-2] for sampling, at least 200 counts, with settle >= 1; and
 * [adcRst, Q-1] for the conversion, also 200. Whichever needs more sets
 * the floor on the quarter, and the pulse rate follows from it. */
uint16_t AFE4490::maxPrf(uint8_t adcRst) {
  if (adcRst < 2)  adcRst = 2;
  if (adcRst > 16) adcRst = 16;
  const uint32_t needSample  = 1u + 200u + 2u;          // settle + window + tail
  const uint32_t needConvert = (uint32_t)adcRst + 200u;
  uint32_t q = needSample > needConvert ? needSample : needConvert;
  uint32_t T = q * 4;
  if (T < 800) T = 800;                                 // PRPCOUNT floor
  return (uint16_t)(4000000UL / T);
}

void AFE4490::applyTiming(const AfeTiming &tin) {
  AfeTiming t = tin;
  if (t.adcRst < 2)  t.adcRst = 2;
  if (t.adcRst > 16) t.adcRst = 16;
  // adcRst first: it is what the ceiling depends on.
  const uint16_t prfCeil = maxPrf(t.adcRst);
  if (t.prf < 62)       t.prf = 62;
  if (t.prf > prfCeil)  t.prf = prfCeil;
  if (t.dutyPct < 1)  t.dutyPct = 1;
  if (t.dutyPct > 25) t.dutyPct = 25;

  uint32_t T = 4000000UL / t.prf;        // total counts in one PRP
  if (T < 800)   T = 800;                // PRPCOUNT valid range is 800..64000
  if (T > 64000) T = 64000;
  const uint32_t Q = T / 4;

  /* Stop the sequencer before rewriting any of this. It reads the phase
   * boundaries continuously, so thirty register writes with TIMEREN set
   * leave it running on a mixture of old and new values for the length of
   * the burst - sampling and converting at instants that belong to neither
   * setting. Those results are not marked in any way and are shipped as
   * ordinary samples, which is what a rate change looked like on screen. */
  const bool wasRunning = (_shadow[AFE_CONTROL1] & AFE_C1_TIMEREN) != 0;
  if (wasRunning) setTimerEnable(false);

  uint32_t Ton = (T * t.dutyPct) / 100;
  if (Ton > Q) Ton = Q;
  // The Rx window is [settle, Ton-2] inside the quarter and the datasheet
  // requires at least 50 us (200 counts) of sampling.
  if (t.settle > Q / 2) t.settle = (uint16_t)(Q / 2);
  if (Ton < (uint32_t)t.settle + 202) {
    Ton = (uint32_t)t.settle + 202;
    if (Ton > Q) { Ton = Q; t.settle = (uint16_t)(Q > 202 ? Q - 202 : 1); }
  }

  const uint16_t sampleEnd = (uint16_t)(Ton - 2);
  const uint16_t ledEnd    = (uint16_t)(Ton - 1);

  // -- sample windows + LED drive, one quarter each -------------------
  writeReg(AFE_ALED2STC,     0 * Q + t.settle);
  writeReg(AFE_ALED2ENDC,    0 * Q + sampleEnd);

  writeReg(AFE_LED1LEDSTC,   1 * Q);
  writeReg(AFE_LED1LEDENDC,  1 * Q + ledEnd);
  writeReg(AFE_LED1STC,      1 * Q + t.settle);
  writeReg(AFE_LED1ENDC,     1 * Q + sampleEnd);

  writeReg(AFE_ALED1STC,     2 * Q + t.settle);
  writeReg(AFE_ALED1ENDC,    2 * Q + sampleEnd);

  writeReg(AFE_LED2LEDSTC,   3 * Q);
  writeReg(AFE_LED2LEDENDC,  3 * Q + ledEnd);
  writeReg(AFE_LED2STC,      3 * Q + t.settle);
  writeReg(AFE_LED2ENDC,     3 * Q + sampleEnd);

  // -- ADC reset pulse then conversion, in each quarter ----------------
  static const uint8_t rstStart[4] = { AFE_ADCRSTSTCT0, AFE_ADCRSTSTCT1,
                                       AFE_ADCRSTSTCT2, AFE_ADCRSTSTCT3 };
  static const uint8_t rstEnd[4]   = { AFE_ADCRSTENDCT0, AFE_ADCRSTENDCT1,
                                       AFE_ADCRSTENDCT2, AFE_ADCRSTENDCT3 };
  static const uint8_t cvStart[4]  = { AFE_LED2CONVST, AFE_ALED2CONVST,
                                       AFE_LED1CONVST, AFE_ALED1CONVST };
  static const uint8_t cvEnd[4]    = { AFE_LED2CONVEND, AFE_ALED2CONVEND,
                                       AFE_LED1CONVEND, AFE_ALED1CONVEND };
  for (uint8_t k = 0; k < 4; k++) {
    const uint32_t P = (uint32_t)k * Q;
    writeReg(rstStart[k], P);
    writeReg(rstEnd[k],   P + t.adcRst - 1);
    writeReg(cvStart[k],  P + t.adcRst);      // one clock after reset ends
    writeReg(cvEnd[k],    P + Q - 1);
  }

  writeReg(AFE_PRPCOUNT, T - 1);

  /* Start the period counter from zero so the first PRP after the change is
   * a whole one rather than whatever was left of the old one. TIM_CNT_RST is
   * a level bit, so it is set and cleared; SPI_READ is preserved because
   * CONTROL0 is write-only and the driver's shadow of that mode is the only
   * copy there is. */
  const uint32_t rd = _spiReadMode ? AFE_C0_SPI_READ : 0;
  writeReg(AFE_CONTROL0, rd | AFE_C0_TIM_CNT_RST);
  writeReg(AFE_CONTROL0, rd);

  // Recorded so the console can see what was actually achieved rather than
  // what was asked for - the clamps above are silent otherwise.
  _sampleWin  = (uint16_t)(sampleEnd >= t.settle ? sampleEnd - t.settle + 1 : 0);
  _convertWin = (uint16_t)(Q > t.adcRst ? Q - t.adcRst : 0);

  if (t.numavAuto) t.numav = (uint8_t)computeNumAv(T);
  uint32_t c1 = _shadow[AFE_CONTROL1] & ~AFE_C1_NUMAV_MSK;
  c1 |= ((uint32_t)t.numav << AFE_C1_NUMAV_SH) & AFE_C1_NUMAV_MSK;
  if (wasRunning || t.numavAuto) c1 |= AFE_C1_TIMEREN;
  writeReg(AFE_CONTROL1, c1);

  /* The sampling instants just moved, which is a change to the signal chain,
   * and each of the four switched-cap filters needs more than 3 ms of
   * sampling to settle after one. applyDefaults() has always waited here;
   * a rate change is the same event and was not waiting at all. */
  if (wasRunning) delay(60);

  _timing = t;
}

/* Inclusive width of a [start, end] edge pair, or 0 when the pair is
 * reversed - a reversed pair is a fault the console reports, not a size. */
static inline uint32_t edgeSpan(uint32_t st, uint32_t en) {
  st &= 0xFFFF; en &= 0xFFFF;
  return en >= st ? en - st + 1 : 0;
}

void AFE4490::syncTimingFromShadow(bool numavWritten) {
  const uint32_t T = (_shadow[AFE_PRPCOUNT] & 0xFFFFUL) + 1UL;
  // Below 62 counts the rate no longer fits the 16-bit fields that quote it,
  // and nothing converts there anyway, so the last credible value stands.
  if (T >= 62) {
    uint32_t prf = (4000000UL + T / 2) / T;
    _timing.prf = (uint16_t)(prf > 65535UL ? 65535UL : prf);
  }

  const uint32_t ledOn = edgeSpan(_shadow[AFE_LED2LEDSTC], _shadow[AFE_LED2LEDENDC]);
  if (ledOn && T) {
    uint32_t duty = (ledOn * 100UL + T / 2) / T;
    _timing.dutyPct = (uint8_t)(duty < 1 ? 1 : (duty > 25 ? 25 : duty));
  }
  const uint32_t ledSt = _shadow[AFE_LED2LEDSTC] & 0xFFFF;
  const uint32_t smpSt = _shadow[AFE_LED2STC] & 0xFFFF;
  _timing.settle = (uint16_t)(smpSt >= ledSt ? smpSt - ledSt : 0);

  const uint32_t rst = edgeSpan(_shadow[AFE_ADCRSTSTCT0], _shadow[AFE_ADCRSTENDCT0]);
  if (rst) _timing.adcRst = (uint8_t)(rst < 2 ? 2 : (rst > 16 ? 16 : rst));

  _timing.numav = (uint8_t)(_shadow[AFE_CONTROL1] & AFE_C1_NUMAV_MSK);
  if (numavWritten) _timing.numavAuto = false;

  // The narrowest of the four, because that is the one that fails first.
  static const uint8_t smp[4][2] = {
    { AFE_LED2STC, AFE_LED2ENDC }, { AFE_ALED2STC, AFE_ALED2ENDC },
    { AFE_LED1STC, AFE_LED1ENDC }, { AFE_ALED1STC, AFE_ALED1ENDC } };
  static const uint8_t cnv[4][2] = {
    { AFE_LED2CONVST, AFE_LED2CONVEND }, { AFE_ALED2CONVST, AFE_ALED2CONVEND },
    { AFE_LED1CONVST, AFE_LED1CONVEND }, { AFE_ALED1CONVST, AFE_ALED1CONVEND } };
  uint32_t sMin = 0xFFFF, cMin = 0xFFFF;
  for (uint8_t k = 0; k < 4; k++) {
    const uint32_t s = edgeSpan(_shadow[smp[k][0]], _shadow[smp[k][1]]);
    const uint32_t c = edgeSpan(_shadow[cnv[k][0]], _shadow[cnv[k][1]]);
    if (s < sMin) sMin = s;
    if (c < cMin) cMin = c;
  }
  _sampleWin  = (uint16_t)sMin;
  _convertWin = (uint16_t)cMin;
}

// =====================================================================
// CONFIGURATION HELPERS
// =====================================================================
void AFE4490::setLedCurrents(uint8_t led1, uint8_t led2) {
  uint32_t v = _shadow[AFE_LEDCNTRL] & ~(AFE_LC_LED1_MSK | AFE_LC_LED2_MSK);
  v |= ((uint32_t)led1 << AFE_LC_LED1_SH) & AFE_LC_LED1_MSK;
  v |= ((uint32_t)led2 << AFE_LC_LED2_SH) & AFE_LC_LED2_MSK;
  writeReg(AFE_LEDCNTRL, v);
}

void AFE4490::setLedRange(uint8_t range) {
  uint32_t v = _shadow[AFE_LEDCNTRL] & ~AFE_LC_RANGE_MSK;
  v |= ((uint32_t)(range & 3) << AFE_LC_RANGE_SH);
  writeReg(AFE_LEDCNTRL, v);
}

void AFE4490::setTxRef(uint8_t code) {
  uint32_t v = _shadow[AFE_CONTROL2] & ~AFE_C2_TXREF_MSK;
  v |= ((uint32_t)(code & 3) << AFE_C2_TXREF_SH);
  writeReg(AFE_CONTROL2, v);
  /* TX_REF drives a 2.2 uF cap; the datasheet allows ~1 s for it to
   * settle before the transmitter dynamic range is fully specified. */
  delay(20);
}

void AFE4490::setTia(uint8_t rf, uint8_t cf, bool separate,
                     uint8_t rf1, uint8_t cf1) {
  if (rf1 == 0xFF) rf1 = rf;
  if (cf1 == 0xFF) cf1 = cf;

  uint32_t ag = _shadow[AFE_TIA_AMB_GAIN] & ~(AFE_AG_RF_LED2_MSK | AFE_AG_CF_LED2_MSK);
  ag |= ((uint32_t)(rf & 7) << AFE_AG_RF_LED2_SH);
  ag |= ((uint32_t)(cf & 0x1F) << AFE_AG_CF_LED2_SH);
  writeReg(AFE_TIA_AMB_GAIN, ag);

  uint32_t tg = _shadow[AFE_TIAGAIN] & ~(AFE_TG_RF_LED1_MSK | AFE_TG_CF_LED1_MSK | AFE_TG_ENSEPGAIN);
  tg |= ((uint32_t)(rf1 & 7) << AFE_TG_RF_LED1_SH);
  tg |= ((uint32_t)(cf1 & 0x1F) << AFE_TG_CF_LED1_SH);
  if (separate) tg |= AFE_TG_ENSEPGAIN;
  writeReg(AFE_TIAGAIN, tg);
}

void AFE4490::setStage2(bool enable, uint8_t gain, bool bothChannels) {
  uint32_t ag = _shadow[AFE_TIA_AMB_GAIN] & ~(AFE_AG_STAGE2EN2 | AFE_AG_STG2GAIN2_MSK);
  if (enable) ag |= AFE_AG_STAGE2EN2;
  ag |= ((uint32_t)(gain & 7) << AFE_AG_STG2GAIN2_SH);
  writeReg(AFE_TIA_AMB_GAIN, ag);

  if (bothChannels) {
    uint32_t tg = _shadow[AFE_TIAGAIN] & ~(AFE_TG_STAGE2EN1 | AFE_TG_STG2GAIN1_MSK);
    if (enable) tg |= AFE_TG_STAGE2EN1;
    tg |= ((uint32_t)(gain & 7) << AFE_TG_STG2GAIN1_SH);
    writeReg(AFE_TIAGAIN, tg);
  }
}

void AFE4490::setAmbientDac(uint8_t code) {
  if (code > 10) code = 10;              // 1011..1111 are "do not use"
  uint32_t ag = _shadow[AFE_TIA_AMB_GAIN] & ~AFE_AG_AMBDAC_MSK;
  ag |= ((uint32_t)code << AFE_AG_AMBDAC_SH);
  writeReg(AFE_TIA_AMB_GAIN, ag);
}

void AFE4490::setFilterCorner(bool khz) {
  uint32_t ag = _shadow[AFE_TIA_AMB_GAIN] & ~AFE_AG_FLTRCNRSEL;
  if (khz) ag |= AFE_AG_FLTRCNRSEL;
  writeReg(AFE_TIA_AMB_GAIN, ag);
}

void AFE4490::setTimerEnable(bool on) {
  uint32_t c1 = _shadow[AFE_CONTROL1] & ~AFE_C1_TIMEREN;
  if (on) c1 |= AFE_C1_TIMEREN;
  writeReg(AFE_CONTROL1, c1);
}

void AFE4490::setNumAv(uint8_t numav) {
  _timing.numav = numav;
  _timing.numavAuto = false;
  uint32_t c1 = _shadow[AFE_CONTROL1] & ~AFE_C1_NUMAV_MSK;
  c1 |= ((uint32_t)numav << AFE_C1_NUMAV_SH) & AFE_C1_NUMAV_MSK;
  writeReg(AFE_CONTROL1, c1);
}

/* The shipping default: a plain transmissive-probe SpO2 setup.
 *
 * TX_REF is 0.5 V because the breakout is fed from the XIAO's 3V3 rail.
 * With a 3.0-3.6 V LED_DRV_SUP the datasheet explicitly asks for the
 * 0.5 V reference (Table 6 footnote) - leaving it at the 0.75 V reset
 * value costs headroom the red LED does not have.
 *
 * RF = 500 kOhm with a small LED current mirrors TI's own EVM default.
 * If the probe saturates or reads flat, run AGC and it will find the
 * right RF/LED pair for the sensor actually attached. */
void AFE4490::applyDefaults() {
  writeReg(AFE_CONTROL0, 0);                       // clear SW_RST/DIAG_EN
  writeReg(AFE_CONTROL2, (1UL << AFE_C2_TXREF_SH));// TX_REF = 0.5 V, H-bridge

  _timing = { 500, 25, 50, 4, 8, true };
  applyTiming(_timing);

  setTia(0 /*500k*/, 0 /*5 pF*/, false);
  setStage2(false, 0, true);
  setAmbientDac(0);
  setFilterCorner(false);                          // 500 Hz corner
  setLedRange(1);                                  // 50 mA FS at TX_REF 0.5 V
  setLedCurrents(0x28, 0x28);                      // ~7.8 mA on both LEDs
  writeReg(AFE_ALARM, 0);
  setTimerEnable(true);

  // t5: each switched-cap filter needs >3 ms of sampling to settle after
  // any signal-chain change, and there are four of them.
  delay(60);
}

// =====================================================================
// ACQUISITION
// =====================================================================
void AFE4490::readSample(AfeSample &out) {
  setSpiReadMode(true);
  uint32_t v;
  transfer32(AFE_LED2VAL,       0, &v); out.led2     = afeSignExtend22(v);
  transfer32(AFE_ALED2VAL,      0, &v); out.aled2    = afeSignExtend22(v);
  transfer32(AFE_LED1VAL,       0, &v); out.led1     = afeSignExtend22(v);
  transfer32(AFE_ALED1VAL,      0, &v); out.aled1    = afeSignExtend22(v);
  transfer32(AFE_LED2_ALED2VAL, 0, &v); out.led2_amb = afeSignExtend22(v);
  transfer32(AFE_LED1_ALED1VAL, 0, &v); out.led1_amb = afeSignExtend22(v);
}

uint32_t AFE4490::runDiagnostics(uint32_t timeoutMs) {
  // DIAG_EN self-clears when the sequence finishes: 8 ms fast, 16 ms slow.
  const bool slow = (_shadow[AFE_CONTROL2] & AFE_C2_EN_SLOW_DIAG) != 0;
  const uint32_t need = slow ? 20 : 12;
  if (timeoutMs < need) timeoutMs = need;

  writeReg(AFE_CONTROL0, AFE_C0_DIAG_EN);
  delay(timeoutMs);
  uint32_t flags = readReg(AFE_DIAG);

  // Diagnostics toggles the input switches, so the filters need to settle
  // again before the ADC data means anything (28 ms at the 500 Hz corner).
  delay(30);
  return flags;
}

// =====================================================================
// AUTOMATIC GAIN CONTROL
// ---------------------------------------------------------------------
// Datasheet figure 135: park the DC level at roughly half of full scale
// so the pulsatile component has room in both directions. LED current is
// the fine knob (8 bits) and RF the coarse one, so current moves first
// and the transimpedance only changes when the current hits a rail.
//
// Both emitters have their own 8-bit current register, so both can reach
// the target and both should. This loop used to stop as soon as the
// STRONGER channel arrived, which left the weaker one at whatever the
// shared setting happened to give it: on the bench that was RED at 26 %FS
// against IR at 81 %, from identical 12.1 mA drives, because the red
// emitter and the photodiode's red responsivity are simply worse. That
// costs the red channel most of its signal-to-noise and buys nothing.
//
// It does not move SpO2. R is (AC/DC)red over (AC/DC)ir and both AC and DC
// scale with drive current, so the ratio is invariant to what this loop
// does - balancing the channels buys precision, never accuracy. Accuracy
// lives in the R-to-saturation curve, which is a calibration, not a gain.
// =====================================================================
bool AFE4490::autoGain(uint8_t targetPct, uint16_t maxIterations) {
  if (targetPct < 10) targetPct = 10;
  if (targetPct > 80) targetPct = 80;
  const int32_t target = (int32_t)((2097152.0f * targetPct) / 100.0f);

  uint8_t rf = (uint8_t)(_shadow[AFE_TIA_AMB_GAIN] & AFE_AG_RF_LED2_MSK);
  uint8_t gi = 0;                                   // index into AFE_RF_BY_GAIN
  for (uint8_t i = 0; i < 7; i++) if (AFE_RF_BY_GAIN[i] == rf) gi = i;

  int16_t led1 = (int16_t)((_shadow[AFE_LEDCNTRL] & AFE_LC_LED1_MSK) >> AFE_LC_LED1_SH);
  int16_t led2 = (int16_t)((_shadow[AFE_LEDCNTRL] & AFE_LC_LED2_MSK) >> AFE_LC_LED2_SH);
  if (led1 < 1) led1 = 1;
  if (led2 < 1) led2 = 1;

  for (uint16_t iter = 0; iter < maxIterations; iter++) {
    // Let the switched-cap filters settle, then average a few PRPs so a
    // single motion artefact cannot steer the loop.
    delay(40);
    int64_t s1 = 0, s2 = 0;
    const uint8_t N = 8;
    for (uint8_t k = 0; k < N; k++) {
      AfeSample s;
      // Wait for a fresh conversion; fall through on timeout rather than hang.
      uint32_t t0 = millis();
      while (digitalRead(_rdy) == HIGH && (millis() - t0) < 5) { }
      t0 = millis();
      while (digitalRead(_rdy) == LOW && (millis() - t0) < 20) { }
      readSample(s);
      s1 += abs(s.led1);
      s2 += abs(s.led2);
    }
    const int32_t m1 = (int32_t)(s1 / N);
    const int32_t m2 = (int32_t)(s2 / N);
    const int32_t peak = (m1 > m2) ? m1 : m2;

    // Both channels within +/-25 % of target, not just whichever is louder.
    const int32_t lo = (target * 3) / 4, hi = (target * 5) / 4;
    if (m1 > lo && m1 < hi && m2 > lo && m2 < hi) return true;

    if (peak < 64) {
      // Essentially nothing coming back - raise both LEDs hard, then RF.
      if (led1 < 255 || led2 < 255) {
        led1 = (int16_t)min(255, led1 * 4 + 8);
        led2 = (int16_t)min(255, led2 * 4 + 8);
      } else if (gi > 0) {
        gi--;
      } else return false;
    } else {
      // Scale each LED by how far its own channel is from target.
      const float k1 = (m1 > 0) ? (float)target / (float)m1 : 4.0f;
      const float k2 = (m2 > 0) ? (float)target / (float)m2 : 4.0f;
      int16_t n1 = (int16_t)(led1 * constrain(k1, 0.25f, 4.0f) + 0.5f);
      int16_t n2 = (int16_t)(led2 * constrain(k2, 0.25f, 4.0f) + 0.5f);

      /* A rail only matters if the channel that hit it is still short of
       * target. Otherwise a channel that is happily parked at 255 drags RF
       * around on behalf of one that does not need the help. */
      const bool railHigh = (n1 > 255 && m1 < lo) || (n2 > 255 && m2 < lo);
      const bool railLow  = (n1 < 6   && m1 > hi) || (n2 < 6   && m2 > hi);
      if (railHigh && gi > 0)      { gi--; n1 = min(n1, (int16_t)255); n2 = min(n2, (int16_t)255); }
      else if (railLow && gi < 6)  { gi++; n1 = max(n1, (int16_t)6);   n2 = max(n2, (int16_t)6);   }
      led1 = constrain(n1, (int16_t)1, (int16_t)255);
      led2 = constrain(n2, (int16_t)1, (int16_t)255);
    }

    setTia(AFE_RF_BY_GAIN[gi], (uint8_t)((_shadow[AFE_TIA_AMB_GAIN] & AFE_AG_CF_LED2_MSK) >> AFE_AG_CF_LED2_SH),
           (_shadow[AFE_TIAGAIN] & AFE_TG_ENSEPGAIN) != 0);
    setLedCurrents((uint8_t)led1, (uint8_t)led2);
  }
  return false;
}

// =====================================================================
// LINK INTEGRITY
// ---------------------------------------------------------------------
// LED2STC is a plain 16-bit R/W timing register, so it is safe to use as
// scratch: it is rewritten by the next applyTiming() anyway. A walking
// pattern catches stuck bits, a shifted clock and a dead MISO alike.
// =====================================================================
uint16_t AFE4490::selfTest(uint16_t trials) {
  const uint32_t saved = _shadow[AFE_LED2STC];
  uint16_t errors = 0;

  for (uint16_t i = 0; i < trials; i++) {
    uint32_t pattern;
    switch (i & 3) {
      case 0:  pattern = 0x0000AAAAUL; break;
      case 1:  pattern = 0x00005555UL; break;
      case 2:  pattern = (uint32_t)(1UL << (i % 16)); break;
      default: pattern = (uint32_t)(0xFFFFUL ^ (1UL << (i % 16))); break;
    }
    writeReg(AFE_LED2STC, pattern);
    if (readReg(AFE_LED2STC) != pattern) errors++;
  }

  writeReg(AFE_LED2STC, saved);
  return errors;
}

// =====================================================================
// CONSOLE HELPERS
// =====================================================================
uint16_t AFE4490::ledFullScaleMa() const {
  const uint8_t txref = (uint8_t)((_shadow[AFE_CONTROL2] & AFE_C2_TXREF_MSK) >> AFE_C2_TXREF_SH);
  const uint8_t range = (uint8_t)((_shadow[AFE_LEDCNTRL] & AFE_LC_RANGE_MSK) >> AFE_LC_RANGE_SH);
  return AFE_LED_FS_MA[txref & 3][range & 3];
}

float AFE4490::ledCurrentMa(uint8_t code) const {
  return ((float)code / 256.0f) * (float)ledFullScaleMa();
}
