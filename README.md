# LCR Sweep: frequency-dependent measurements with the Hioki 3532-50

A desktop application for Windows that controls a **Hioki 3532-50 LCR HiTESTER** over **GPIB**
(Keithley **KUSB-488A** USB-GPIB adapter). It sweeps the test frequency (42 Hz to 5 MHz) and records the
impedance at every point. It plots the results live and saves them as CSV or Excel.

![screenshot](docs/screenshot.png)

## What it measures

At every frequency the meter returns **|Z|** and **θ**. The program calculates all other parameters from
these two values, with the same formulas the meter uses internally:

| Series circuit | Parallel circuit | Other |
|---|---|---|
| Rs, X, Ls, Cs | Rp, Lp, Cp, G, B | \|Y\|, D (tan δ), Q |

The app also has:

* Log, linear or custom frequency lists, with SI input (`100`, `1k`, `2.5M`).
* Level type V, CV or CC; speed FAST, NORMAL, SLOW or SLOW2; meter averaging; and a settling delay after each frequency change.
* An option to take several readings per point, which saves the mean and standard deviation of |Z| and θ.
* Live plots: any two parameters against frequency, each with its own log scale, plus a **Nyquist plot** (Z′ vs −Z″) and a data table.
* **Crash-safe autosave**: each point is written to the CSV as soon as it is measured.
* Overlays of several runs or old CSV files for comparing samples.
* Export of all runs to Excel, and of the plots as PNG.
* Sample metadata (name, operator, temperature, notes) and all instrument settings, stored in the CSV header.
* **Fixture calibration (open / short)** with one button: the app guides you through measuring the empty and the
  shorted fixture, then corrects every reading. The raw readings are kept in the CSV (`Z_raw_ohm`, `theta_raw_deg`).
* File names with date, time, sample, operator and test number, e.g. `20261123_181057_C100nF_JDoe_T3.csv`.
* A **GPIB console** for typing any command by hand.
* A **simulator** for trying the app without the meter.
* A built-in **user manual** (tab *Help / Manual*, or press **F1**), from beginner to advanced: what impedance and
  every parameter mean, equations and circuit diagrams, the 4-terminal connection, calibration, settings,
  recipes for common parts, reading the plots, troubleshooting and dielectric analysis. It can also be opened in a
  web browser for printing ([`lcr_app/help/manual.html`](lcr_app/help/manual.html)).

## Download and install (Windows 10/11, no Python needed)

1. Download **`LCRSweep-<version>-windows.zip`** from the
   [**Releases page**](https://github.com/arshadthottappali/LCR-meter-/releases/latest).
2. Unzip it anywhere, for example to `C:\LCRSweep`. Keep the folder together.
3. Double-click **`LCRSweep.exe`**. If Windows SmartScreen warns about an unknown publisher, click
   *More info → Run anyway* (the program is not code-signed).

To try it without a meter, choose the interface **Simulator (no hardware)** and click **Connect**.

### The one other thing you need: your GPIB adapter's driver

Windows needs the adapter manufacturer's driver to talk to any USB-GPIB adapter. Install it once; the app
cannot include it. The app tells you if it is missing: **Tools → Check drivers / connection…** (it also opens
by itself when *Scan* finds nothing or *Connect* fails) shows what is installed, whether the adapter is plugged
in, and download links.

| Your adapter | Install | Interface to choose in the app |
|---|---|---|
| Keithley **KUSB-488 / 488A / 488B** | KUSB-488A driver (tek.com → Support → Software, search "KUSB-488") | **GPIB DLL**, or **VISA** if a VISA library is installed |
| NI **GPIB-USB-HS**, PCI/PCIe-GPIB | [NI-488.2](https://www.ni.com/en/support/downloads/drivers/download.ni-488-2.html) | **GPIB DLL** or **VISA** |
| Keysight / Agilent **82357B** | [Keysight IO Libraries Suite](https://www.keysight.com/find/iosuite) | **VISA** |
| Prologix GPIB-USB / Ethernet | not supported yet | – |

* **GPIB DLL** talks to the adapter driver directly (`gpib488.dll` / `ni4882.dll`); no VISA needed.
* **VISA** needs a VISA library in addition (NI-VISA, Keysight VISA or Keithley KI-VISA). Resource name `GPIB0::<address>::INSTR`.

### Hardware setup

1. The 3532-50 needs the **9518-01 GP-IB interface board** fitted in the rear option slot.
2. On the meter, open the interface settings screen and set a **GPIB address**, for example `1`.
3. Connect the GPIB cable to the adapter and plug the adapter into a USB port. In the adapter's configuration
   utility it should appear as **board 0** (GPIB0).

### Running from source instead (developers)

1. Install **Python 3.10 or newer** from python.org and tick "Add Python to PATH".
2. Double-click **`setup_windows.bat`** once. It installs the libraries.
3. Double-click **`run_lcr.bat`** to start the program.

**`build_exe.bat`** builds the stand-alone `dist\LCRSweep\LCRSweep.exe` that is published on the Releases page.

## First measurement

The built-in manual (**F1**) explains every step in detail.

1. **Connection**: choose the interface for your adapter and click **Scan**. The app finds the meter's address. Then click **Connect**, and the Instrument field should show `HIOKI,3532,50,…`.
2. **Tools → Verify command profile on meter**: sends every command once and reads the meter's error register. Do this once with a new meter (see "GPIB commands" below).
3. Set the start and stop frequency, number of points, level and speed (sections 2 and 3).
4. **Calibrate the fixture** (section 4, *Calibrate fixture (open + short)…*), **without the sample**:
   * step 1, OPEN: fixture empty, clips not touching;
   * step 2, SHORT: fixture terminals connected directly together.

   (Calibrate with the same frequency range and the same voltage that you will use for the measurement. The app
   uses the settings of sections 2 and 3 and warns you if you change them afterwards.)
   The calibration is saved in `LCR_data\calibration\` and loaded again the next time the app starts.
   Use either this or the meter's own front-panel open/short compensation, not both.
5. Put the sample in the fixture, enter sample name, operator and test number, and click **Start sweep**.
   The test number counts up by 1 after every completed sweep.
6. The data is saved automatically to `C:\Users\<you>\LCR_data\YYYYMMDD_HHMMSS_<sample>_<operator>_T<test no>.csv`.
   You can change the folder.

## How fast is it?

PyQt is not the limiting factor. The time per point is set by the meter:

| Speed | Measuring time per reading (datasheet) |
|---|---|
| FAST | 5 ms |
| NORMAL | 21 ms |
| SLOW / SLOW2 | 72 ms / 140 ms |

A GPIB command and response take about 1–2 ms. The real cost is the **settling delay** after each frequency
change and the meter's **auto-ranging**. Measured on a real 3532-50: 0.25–0.6 s per point in NORMAL mode with
auto-ranging, so a 51-point sweep takes about 15–30 s.

The measurement runs in a background thread, so the window stays responsive. The 3532-50 has no internal
memory for storing a frequency sweep: its panel memory saves settings only. Measuring live from the PC is
therefore both the fastest and the only option, and every point reaches the disk at once.

For precise results (dielectrics, low D values), use **SLOW2**, averaging 4–16, and a settling delay of 100–200 ms.

## GPIB commands (command profile)

All commands sent to the meter are in
[`lcr_app/instrument/commands_3532_50.json`](lcr_app/instrument/commands_3532_50.json).

| Purpose | Command |
|---|---|
| Identify | `*IDN?` |
| Frequency | `:FREQuency 1.00000E+03` |
| Level type / value | `:LEVel V` + `:LEVel:VOLTage 1` (also `CV`/`:LEVel:CVOLTage`, `CC`/`:LEVel:CCURrent`) |
| Speed, averaging, range | `:SPEEd NORMal`, `:AVERaging 4`, `:RANGe:AUTO ON` |
| Response = \|Z\|, θ | `:HEADer OFF`, `:MEASure:ITEM 5,0` |
| Trigger one reading | `:TRIGger EXTernal`, `*TRG`, `:MEASure?` |

The full command reference for the 3532-50 is in the separate **9518-01 GP-IB Interface** manual, and these
command strings have not yet been checked against it. If **Tools → Verify command profile** reports a
`FAIL`, correct that line in the JSON file (**Tools → Open command profile**), then click
**Tools → Reload command profile**. You don't need to change any code.

## File format

```
# sample: C100nF
# level_mode: V
# speed: NORMal
...
index,freq_set_Hz,freq_Hz,Z_ohm,theta_deg,Rs_ohm,X_ohm,Ls_H,Cs_F,Rp_ohm,Lp_H,Cp_F,Y_S,G_S,B_S,D,Q,Z_std,theta_std,Z_raw_ohm,theta_raw_deg,timestamp
0,100,100,15920.2,-89.98,5.56,-15920.2,...
```

Lines starting with `#` hold metadata, including `test_number` and `compensation` (which calibration file was
applied, or `none`). `Z_ohm`/`theta_deg` and everything derived from them are corrected; `Z_raw_ohm`/`theta_raw_deg`
are the meter's readings before correction. Excel, Origin, MATLAB and Python all read the file directly, for example:

```python
import pandas as pd
df = pd.read_csv("20261123_164308_C100nF.csv", comment="#")
```

## Project layout

```
lcr_app/
  instrument/  transport.py (VISA + GPIB DLL), diagnostics.py (driver check), hioki3532.py (driver), simulator.py, commands_3532_50.json
  core/        params.py (Z, θ → all parameters), sweep.py (sweep engine), storage.py (CSV/Excel),
               compensation.py (open/short correction)
  gui/         main_window.py, worker.py (background thread)
tests/         pytest tests (run: python -m pytest tests)
```

To develop or run from source on any OS: `pip install -r requirements.txt` then `python -m lcr_app`.

`packaging/make_release.py` (called by `build_exe.bat`) builds the release zip: PyInstaller build, removal of
GPL-only Qt modules, third-party notices and licence texts, SHA-256 checksum.

## Licences and trademarks

**LCR Sweep: Copyright (c) 2026 arshadthottappali. All rights reserved.** You may download and use the
released program free of charge; copying, modifying or redistributing the code or the program needs written
permission. See [`LICENSE`](LICENSE) for the exact terms.

The Windows release bundles Qt / PySide6 under the **LGPL-3.0** and other open-source components under
permissive licences (MIT, BSD, PSF); see [`packaging/THIRD_PARTY_NOTICES.md`](packaging/THIRD_PARTY_NOTICES.md)
and the texts in [`packaging/licenses`](packaging/licenses). The release contains no GPL-only Qt modules.

Hioki and LCR HiTESTER are trademarks of HIOKI E.E. CORPORATION; Keithley of Tektronix, Inc.; NI-488.2 and
NI-VISA of National Instruments; Keysight of Keysight Technologies. They are used only to name compatible
hardware and software. This project is not affiliated with or endorsed by any of these companies.
