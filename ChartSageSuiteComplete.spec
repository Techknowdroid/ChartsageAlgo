# -*- mode: python ; coding: utf-8 -*-

import os


bundle_name = os.environ.get("CHARTSAGE_BUNDLE_NAME", "ChartSageSuiteComplete")
project_dir = os.path.abspath(SPECPATH)


scanner = Analysis(
    ["ChartSageAlgoOPENcode.py"],
    pathex=[project_dir],
    binaries=[],
    datas=[("seed_data", "seed_data")],
    hiddenimports=[
        "nse_calendar",
        "tickertape_intraday",
        "startup_market_maintenance",
        "ChartSageBhavcopy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
scanner_pyz = PYZ(scanner.pure)
scanner_exe = EXE(
    scanner_pyz,
    scanner.scripts,
    [],
    exclude_binaries=True,
    name="ChartSageAlgo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)


final_scanner = Analysis(
    ["FINALCHARTinkV1.py"],
    pathex=[project_dir],
    binaries=[],
    datas=[],
    hiddenimports=[
        "nse_calendar",
        "tickertape_intraday",
        "startup_market_maintenance",
        "ChartSageBhavcopy",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
final_scanner_pyz = PYZ(final_scanner.pure)
final_scanner_exe = EXE(
    final_scanner_pyz,
    final_scanner.scripts,
    [],
    exclude_binaries=True,
    name="FINALCHARTinkV1",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)


feeder = Analysis(
    ["ChartSageBhavcopy.py"],
    pathex=[project_dir],
    binaries=[],
    datas=[],
    hiddenimports=["pypdf"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
feeder_pyz = PYZ(feeder.pure)
feeder_exe = EXE(
    feeder_pyz,
    feeder.scripts,
    [],
    exclude_binaries=True,
    name="ChartSageBhavcopy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)


coll = COLLECT(
    scanner_exe,
    final_scanner_exe,
    feeder_exe,
    scanner.binaries,
    scanner.datas,
    final_scanner.binaries,
    final_scanner.datas,
    feeder.binaries,
    feeder.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=bundle_name,
)
