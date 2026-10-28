/*
 * =====================================================================
 *  AFE4490_Regs.h - complete register map for the TI AFE4490
 *  Source: SBAS602H (Dec 2012, revised Oct 2014), section 8.6.
 * =====================================================================
 *
 *  Every register is 24 bits wide on the wire: one SPI transaction is
 *  an 8-bit address followed by 24 bits of data (32 clocks total).
 *  Timing registers only use bits D[15:0]; D[23:16] must be written 0.
 *
 *  Nothing here touches hardware - this file is pure description.
 */
#pragma once
#include <stdint.h>

// =====================================================================
// REGISTER ADDRESSES
// =====================================================================
enum : uint8_t {
  // ---- control -------------------------------------------------------
  AFE_CONTROL0        = 0x00,   // write-only

  // ---- Rx sample / LED-on timing (counts of the 4 MHz internal clock) -
  AFE_LED2STC         = 0x01,   // sample LED2 (RED) start
  AFE_LED2ENDC        = 0x02,   // sample LED2 end
  AFE_LED2LEDSTC      = 0x03,   // LED2 drive on
  AFE_LED2LEDENDC     = 0x04,   // LED2 drive off
  AFE_ALED2STC        = 0x05,   // sample ambient (LED2 phase) start
  AFE_ALED2ENDC       = 0x06,
  AFE_LED1STC         = 0x07,   // sample LED1 (IR) start
  AFE_LED1ENDC        = 0x08,
  AFE_LED1LEDSTC      = 0x09,   // LED1 drive on
  AFE_LED1LEDENDC     = 0x0A,
  AFE_ALED1STC        = 0x0B,   // sample ambient (LED1 phase) start
  AFE_ALED1ENDC       = 0x0C,

  // ---- ADC conversion windows ----------------------------------------
  AFE_LED2CONVST      = 0x0D,
  AFE_LED2CONVEND     = 0x0E,
  AFE_ALED2CONVST     = 0x0F,
  AFE_ALED2CONVEND    = 0x10,
  AFE_LED1CONVST      = 0x11,
  AFE_LED1CONVEND     = 0x12,
  AFE_ALED1CONVST     = 0x13,
  AFE_ALED1CONVEND    = 0x14,

  // ---- ADC reset pulses (four per pulse-repetition period) ------------
  AFE_ADCRSTSTCT0     = 0x15,
  AFE_ADCRSTENDCT0    = 0x16,
  AFE_ADCRSTSTCT1     = 0x17,
  AFE_ADCRSTENDCT1    = 0x18,
  AFE_ADCRSTSTCT2     = 0x19,
  AFE_ADCRSTENDCT2    = 0x1A,
  AFE_ADCRSTSTCT3     = 0x1B,
  AFE_ADCRSTENDCT3    = 0x1C,

  AFE_PRPCOUNT        = 0x1D,   // pulse repetition period - 1

  // ---- analog / power configuration ----------------------------------
  AFE_CONTROL1        = 0x1E,
  AFE_SPARE1          = 0x1F,
  AFE_TIAGAIN         = 0x20,   // LED1 side when separate-gain is enabled
  AFE_TIA_AMB_GAIN    = 0x21,   // LED2 side + ambient DAC + filter corner
  AFE_LEDCNTRL        = 0x22,
  AFE_CONTROL2        = 0x23,
  AFE_SPARE2          = 0x24,
  AFE_SPARE3          = 0x25,
  AFE_SPARE4          = 0x26,
  AFE_RESERVED1       = 0x27,   // factory use, read-only, varies per part
  AFE_RESERVED2       = 0x28,
  AFE_ALARM           = 0x29,

  // ---- results (read-only) -------------------------------------------
  AFE_LED2VAL         = 0x2A,
  AFE_ALED2VAL        = 0x2B,
  AFE_LED1VAL         = 0x2C,
  AFE_ALED1VAL        = 0x2D,
  AFE_LED2_ALED2VAL   = 0x2E,   // LED2 - ambient, subtracted on-chip
  AFE_LED1_ALED1VAL   = 0x2F,   // LED1 - ambient, subtracted on-chip
  AFE_DIAG            = 0x30,   // diagnostic flags

  AFE_REG_MAX         = 0x30
};

// =====================================================================
// BIT DEFINITIONS
// =====================================================================

// ---- CONTROL0 (0x00), write only -------------------------------------
#define AFE_C0_SW_RST        (1UL << 3)
#define AFE_C0_DIAG_EN       (1UL << 2)
#define AFE_C0_TIM_CNT_RST   (1UL << 1)
#define AFE_C0_SPI_READ      (1UL << 0)

// ---- CONTROL1 (0x1E) -------------------------------------------------
#define AFE_C1_CLKALMPIN_SH  9
#define AFE_C1_CLKALMPIN_MSK (0x7UL << 9)
#define AFE_C1_TIMEREN       (1UL << 8)
#define AFE_C1_NUMAV_SH      0
#define AFE_C1_NUMAV_MSK     (0xFFUL << 0)

// ---- TIAGAIN (0x20) --------------------------------------------------
#define AFE_TG_ENSEPGAIN     (1UL << 15)
#define AFE_TG_STAGE2EN1     (1UL << 14)
#define AFE_TG_STG2GAIN1_SH  8
#define AFE_TG_STG2GAIN1_MSK (0x7UL << 8)
#define AFE_TG_CF_LED1_SH    3
#define AFE_TG_CF_LED1_MSK   (0x1FUL << 3)
#define AFE_TG_RF_LED1_SH    0
#define AFE_TG_RF_LED1_MSK   (0x7UL << 0)

// ---- TIA_AMB_GAIN (0x21) ---------------------------------------------
#define AFE_AG_AMBDAC_SH     16
#define AFE_AG_AMBDAC_MSK    (0xFUL << 16)
#define AFE_AG_FLTRCNRSEL    (1UL << 15)      // 0 = 500 Hz, 1 = 1000 Hz
#define AFE_AG_STAGE2EN2     (1UL << 14)
#define AFE_AG_STG2GAIN2_SH  8
#define AFE_AG_STG2GAIN2_MSK (0x7UL << 8)
#define AFE_AG_CF_LED2_SH    3
#define AFE_AG_CF_LED2_MSK   (0x1FUL << 3)
#define AFE_AG_RF_LED2_SH    0
#define AFE_AG_RF_LED2_MSK   (0x7UL << 0)

// ---- LEDCNTRL (0x22) -------------------------------------------------
#define AFE_LC_RANGE_SH      16
#define AFE_LC_RANGE_MSK     (0x3UL << 16)
#define AFE_LC_LED1_SH       8
#define AFE_LC_LED1_MSK      (0xFFUL << 8)
#define AFE_LC_LED2_SH       0
#define AFE_LC_LED2_MSK      (0xFFUL << 0)

// ---- CONTROL2 (0x23) -------------------------------------------------
#define AFE_C2_TXREF_SH      17
#define AFE_C2_TXREF_MSK     (0x3UL << 17)    // 00=0.75V 01=0.50V 10=1.00V
#define AFE_C2_RSTCLK_PDALM  (1UL << 16)
#define AFE_C2_EN_ADC_BYP    (1UL << 15)
#define AFE_C2_TXBRGMOD      (1UL << 11)      // 0 = H-bridge, 1 = push-pull
#define AFE_C2_DIGOUT_TRI    (1UL << 10)
#define AFE_C2_XTALDIS       (1UL << 9)
#define AFE_C2_EN_SLOW_DIAG  (1UL << 8)
#define AFE_C2_PDNTX         (1UL << 2)
#define AFE_C2_PDNRX         (1UL << 1)
#define AFE_C2_PDNAFE        (1UL << 0)

// ---- ALARM (0x29) ----------------------------------------------------
#define AFE_AL_ALMPINCLKEN   (1UL << 7)

// ---- DIAG (0x30), all flags are active high --------------------------
#define AFE_DG_PD_ALM        (1UL << 12)
#define AFE_DG_LED_ALM       (1UL << 11)
#define AFE_DG_LED1OPEN      (1UL << 10)
#define AFE_DG_LED2OPEN      (1UL << 9)
#define AFE_DG_LEDSC         (1UL << 8)
#define AFE_DG_OUTPSHGND     (1UL << 7)
#define AFE_DG_OUTNSHGND     (1UL << 6)
#define AFE_DG_PDOC          (1UL << 5)
#define AFE_DG_PDSC          (1UL << 4)
#define AFE_DG_INNSCGND      (1UL << 3)
#define AFE_DG_INPSCGND      (1UL << 2)
#define AFE_DG_INNSCLED      (1UL << 1)
#define AFE_DG_INPSCLED      (1UL << 0)

// =====================================================================
// SETTING TABLES (indexed by the raw register field value)
// =====================================================================

// RF_LEDx[2:0] -> transimpedance feedback resistance in ohms.
// Code 7 leaves the feedback network open (no valid gain).
static const uint32_t AFE_RF_OHMS[8] = {
  500000UL, 250000UL, 100000UL, 50000UL, 25000UL, 10000UL, 1000000UL, 0UL
};

// The same codes ordered by descending transimpedance. The AGC walks
// this list, so it must stay sorted: 1M, 500k, 250k, 100k, 50k, 25k, 10k.
static const uint8_t AFE_RF_BY_GAIN[7] = { 6, 0, 1, 2, 3, 4, 5 };

// CF_LEDx[4:0] is a bit field. A fixed 5 pF is always present and each
// set bit adds the amount below (datasheet figure 114).
static const uint16_t AFE_CF_ADD_PF[5] = { 5, 15, 25, 50, 150 };

// STG2GAINx[2:0] -> stage-2 gain, in tenths of a dB and as a x10 linear
// multiplier. Codes 5..7 are marked "do not use" and are clamped to 0 dB.
static const uint8_t AFE_STG2_DB10[8]  = { 0, 35, 60, 95, 120, 0, 0, 0 };
static const uint8_t AFE_STG2_LIN10[8] = { 10, 15, 20, 30, 40, 10, 10, 10 };

/* Full-scale LED current in mA, indexed [TX_REF][LED_RANGE] per Table 6.
 * TX_REF code 11 behaves like 00; LED_RANGE code 11 disables the Tx. */
static const uint16_t AFE_LED_FS_MA[4][4] = {
  { 150, 75,  150, 0 },   // TX_REF 00 = 0.75 V (reset default, 5 V Tx rail)
  { 100, 50,  100, 0 },   // TX_REF 01 = 0.50 V (use with a 3.0-3.6 V Tx rail)
  { 200, 100, 200, 0 },   // TX_REF 10 = 1.00 V (needs a 5 V Tx rail)
  { 150, 75,  150, 0 }    // TX_REF 11 = 0.75 V
};

// Total feedback capacitance in pF for a CF_LEDx[4:0] field value.
static inline uint16_t afeCfPicofarads(uint8_t cfField) {
  uint16_t pf = 5;                        // the fixed 5 pF is always in circuit
  for (uint8_t i = 0; i < 5; i++)
    if (cfField & (1u << i)) pf = (uint16_t)(pf + AFE_CF_ADD_PF[i]);
  return pf;
}

/* Sign-extend a raw 24-bit result register into a signed 22-bit ADC code.
 * D23/D22 are sign-extension bits and carry no information (section 8.4.1),
 * so shifting bit 21 up to bit 31 and back down does the whole job. */
static inline int32_t afeSignExtend22(uint32_t raw) {
  return ((int32_t)(raw << 10)) >> 10;
}

// ADC code -> volts. Full scale is +/-1.2 V across +/-2^21 codes.
static inline float afeCodeToVolts(int32_t code) {
  return (float)code * (1.2f / 2097152.0f);
}
