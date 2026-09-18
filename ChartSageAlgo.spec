# -*- mode: python ; coding: utf-8 -*-


scanner = Analysis(
    ['ChartSageAlgoOPENcode.py'],
    pathex=['C:\\OPENCODE'],
    binaries=[],
    datas=[('seed_data', 'seed_data')],
    hiddenimports=[
        'nse_calendar',
        'tickertape_intraday',
        'startup_market_maintenance',
        'ChartSageBhavcopy',
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
    name='ChartSageAlgo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

feeder = Analysis(
    ['ChartSageBhavcopy.py'],
    pathex=['C:\\OPENCODE'],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name='ChartSageBhavcopy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    scanner_exe,
    feeder_exe,
    scanner.binaries,
    scanner.datas,
    feeder.binaries,
    feeder.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ChartSageSuiteStrategyGuideWindow',
)
