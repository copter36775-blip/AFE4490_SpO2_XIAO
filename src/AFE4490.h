/*
 * =====================================================================
 *  AFE4490.h - driver for the TI AFE4490 pulse-oximetry analog front end
 *  Target: Seeed XIAO nRF52840 Sense (Adafruit nRF52 BSP)
 * =====================================================================
 *
 *  Design notes
 *  ------------
 *  * The AFE has no "burst read" mode. Every register access is its own
 *    32-clock frame, so a full result set (6 registers) costs 6 frames.
 *    At 4 MHz SCLK that is ~60 us including CS overhead - comfortable
 *    even at the 1 kHz maximum PRF this driver allows.
 *
 *  * SPI_READ (CONTROL0 bit 0) is a mode bit, not a per-transfer flag.
 *    Toggling it around every access would double the traffic, so the
 *    driver keeps a shadow of the mode and only writes CONTROL0 when the
 *    direction actually changes. During streaming it simply stays 1.
 *
 *  * All timing registers are derived from PRF + duty cycle rather than
 *    being hard-coded, because the four phases must stay inside their
 *    own quarter of the pulse-repetition period or the ADC converts the
 *    wrong sample. See computeTiming() in the .cpp for the arithmetic
 *    and how it reproduces the datasheet's Table 2 example exactly.
 */
#pragma once

#include <Arduino.h>
#include <SPI.h>
#include "AFE4490_Regs.h"

// ---------------------------------------------------------------------
// One acquisition frame: the six result registers of a single PRP.
// Codes are signed 22-bit; ambient-subtracted values come from the chip.
// ---------------------------------------------------------------------
struct AfeSample {
  int32_t led2;        // 0x2A RED
  int32_t aled2;       // 0x2B ambient during the RED phase
  int32_t led1;        // 0x2C IR
  int32_t aled1;       // 0x2D ambient during the IR phase
  int32_t led2_amb;    // 0x2E RED - ambient
  int32_t led1_amb;    // 0x2F IR  - ambient
};

// ---------------------------------------------------------------------
// Everything the timing generator needs. Anything not listed here is a
// plain register field and is set through writeReg() from the console.
// ---------------------------------------------------------------------
struct AfeTiming {
  uint16_t prf;        // pulse repetition frequency, Hz (62..maxPrf())
  uint8_t  dutyPct;    // LED on-time as a % of the PRP (1..25)
  uint16_t settle;     // sample delay after LED turn-on, 4 MHz counts
  uint8_t  adcRst;     // ADC reset pulse width, 4 MHz counts (2..16)
  uint8_t  numav;      // CONTROL1 NUMAV field (averages - 1)
  bool     numavAuto;  // recompute numav from the PRP on every apply()
};

class AFE4490 {
public:
  AFE4490(SPIClass &spi, uint8_t csPin, uint8_t resetPin,
          uint8_t pwdnPin, uint8_t rdyPin,
          uint8_t sckPin = 0xFF, uint8_t mosiPin = 0xFF,
          bool onSpim3 = false);

  // One measured step of an SPI speed sweep.
  struct SpiSweepStep { uint32_t hz; uint16_t errors; };

  // ---- lifecycle -----------------------------------------------------
  bool begin(uint32_t spiHz = 4000000UL);
  void hardwareReset();                 // RESET pin pulse + settling wait
  void softwareReset();                 // CONTROL0 SW_RST
  void powerDown(bool afe, bool rx, bool tx);
  /* The nRF52 SPIM only has discrete clocks and the BSP rounds a request
   * UP to the next one, so the number you asked for is rarely the number
   * on the wire. setSpiSpeed() quantizes first and spiSpeed() always
   * reports what SCLK will actually be. */
  void setSpiSpeed(uint32_t hz);
  uint32_t spiSpeed() const { return _spiHz; }
  static uint32_t quantizeSpiHz(uint32_t hz, bool onSpim3);
  uint32_t maxSpiHz() const { return _spim3 ? 32000000UL : 8000000UL; }

  /* The BSP forces high drive (H0H1) on SCK and MOSI for "highspeed
   * SPIM". On jumper wiring that overshoots and cross-couples into MISO.
   * Standard drive (S0S1) is usually the single biggest improvement.
   * Must be called after begin(), which is where the BSP sets H0H1. */
  void setDriveStrength(bool high);
  bool driveStrength() const { return _highDrive; }

  /* Walk every SPIM clock step and count register read-back errors at
   * each one. Returns how many steps were filled in. */
  uint8_t spiSweep(SpiSweepStep *out, uint8_t maxSteps, uint16_t trials);

  // ---- raw register access -------------------------------------------
  void     writeReg(uint8_t addr, uint32_t value);
  uint32_t readReg(uint8_t addr);
  void     setSpiReadMode(bool enable);
  bool     spiReadMode() const { return _spiReadMode; }

  /* Shadow copy of every writable register. Kept in sync by writeReg()
   * so the console can render the map without 49 SPI reads. */
  uint32_t shadow(uint8_t addr) const {
    return (addr <= AFE_REG_MAX) ? _shadow[addr] : 0;
  }
  void refreshShadow();                 // re-read the whole map from silicon

  // ---- configuration --------------------------------------------------
  /* PRPCOUNT bottoms out at 800, which nominally puts the pulse rate at
   * 5000 Hz - but two windows have to fit inside a quarter of the period
   * and neither of them does at that rate. A conversion needs 200 counts
   * (50 us) after the ADC reset pulse, and the Rx sample window needs 200
   * of its own. Both hold to about 4900 Hz and fail at 5000, silently:
   * the ADC simply does not finish, and the result register still reads
   * out something. The ceiling is computed rather than tabulated because
   * it moves with the ADC reset width. */
  static uint16_t maxPrf(uint8_t adcRst = 4);

  /* What applyTiming() actually managed to fit, in 4 MHz counts. Below
   * 200 either of these means the datasheet's minimum was not met. */
  uint16_t sampleWindow() const { return _sampleWin; }
  uint16_t convertWindow() const { return _convertWin; }

  void applyDefaults();                 // the SpO2 preset described below
  void applyTiming(const AfeTiming &t); // writes 0x01..0x1D (+ NUMAV)

  /* Rebuild timing() and the window sizes from the register shadow. A raw
   * register write goes around applyTiming(), and without this the pulse
   * rate the frame header, STATUS and the pulse detector quote would keep
   * describing timing that is no longer on the chip. `numavWritten` says
   * CONTROL1 was written by hand, which ends automatic averaging. */
  void syncTimingFromShadow(bool numavWritten = false);
  const AfeTiming &timing() const { return _timing; }
  AfeTiming &timing() { return _timing; }

  void setLedCurrents(uint8_t led1, uint8_t led2);
  void setLedRange(uint8_t range);      // LEDCNTRL LED_RANGE[1:0]
  void setTxRef(uint8_t code);          // CONTROL2 TX_REF[1:0]
  void setTia(uint8_t rf, uint8_t cf, bool separate,
              uint8_t rf1 = 0xFF, uint8_t cf1 = 0xFF);
  void setStage2(bool enable, uint8_t gain, bool bothChannels = true);
  void setAmbientDac(uint8_t code);     // TIA_AMB_GAIN AMBDAC[3:0], 0..10 uA
  void setFilterCorner(bool khz);       // false = 500 Hz, true = 1000 Hz
  void setTimerEnable(bool on);
  void setNumAv(uint8_t numav);

  // ---- acquisition -----------------------------------------------------
  void readSample(AfeSample &out);      // reads 0x2A..0x2F
  uint32_t runDiagnostics(uint32_t timeoutMs = 60);

  /* Automatic gain control. Sweeps TIA gain and LED current until the DC
   * level of both channels sits near `targetPct` of full scale, which is
   * the loop sketched in datasheet figure 135. Returns true on success. */
  bool autoGain(uint8_t targetPct = 50, uint16_t maxIterations = 12);

  // ---- link integrity --------------------------------------------------
  /* Writes a walking pattern to a scratch timing register and reads it
   * back. Returns the number of mismatching trials out of `trials`. */
  uint16_t selfTest(uint16_t trials = 64);

  // ---- helpers used by the console ------------------------------------
  uint16_t ledFullScaleMa() const;
  float    ledCurrentMa(uint8_t code) const;
  uint32_t sampleRate() const { return _timing.prf; }

private:
  void     transfer32(uint8_t addr, uint32_t data, uint32_t *rx);
  void     beginTxn();
  void     endTxn();
  uint32_t computeNumAv(uint32_t prpCounts) const;

  SPIClass &_spi;
  uint8_t   _cs, _reset, _pwdn, _rdy, _sck, _mosi;
  bool      _spim3;
  bool      _highDrive;
  uint32_t  _spiHz;
  bool      _spiReadMode;
  bool      _inTxn;
  AfeTiming _timing;
  uint16_t  _sampleWin = 0, _convertWin = 0;
  uint32_t  _shadow[AFE_REG_MAX + 1];
};
