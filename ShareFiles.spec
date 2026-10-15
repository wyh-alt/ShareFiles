# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

不用命令行直接打包，是因为 PyQt6 的官方 hook 会调 collect_extra_binaries()
把整个 Qt6/bin 无差别收进来 —— 光 Qt6WebEngineCore.dll 就 195MB，而本程序
只用到 QtCore / QtGui / QtWidgets / QtNetwork 四个模块。这里在分析完成后
按文件名把用不上的 DLL 剔掉，能省下七成体积。

用 OFFLINE 模式（pyinstaller ShareFiles.spec）打包。
"""

import os

# 一个都用不到的 Qt 模块 / 第三方库，按前缀匹配剔掉。
# 注意别误伤 Qt6Network —— 单实例检测用的 QLocalServer 在它里面。
SKIP_PREFIXES = (
    "Qt6WebEngine", "Qt6WebChannel", "Qt6WebSockets", "Qt6WebKit",
    "Qt6Quick", "Qt6Qml", "Qt6Pdf",
    "Qt6Multimedia", "Qt6SpatialAudio", "Qt6TextToSpeech",
    "Qt63D", "Qt6Charts", "Qt6DataVisualization", "Qt6Graphs",
    "Qt6Sql", "Qt6Test", "Qt6Designer", "Qt6UiTools", "Qt6Help",
    "Qt6Bluetooth", "Qt6Nfc", "Qt6Positioning", "Qt6Location",
    "Qt6Sensors", "Qt6SerialPort", "Qt6RemoteObjects", "Qt6Scxml",
    "Qt6StateMachine", "Qt6OpenGL", "Qt6DBus",
    "Qt6Concurrent", "Qt6NetworkAuth", "Qt6VirtualKeyboard",
    # 注意：Qt6Svg 和 Qt6Xml 看着像用不上，其实 qfluentwidgets 的图标模块
    # 要它俩（common/icon.py 里 import QtXml），排掉会直接起不来。
    "Qt6EglFSDeviceIntegration", "Qt6EglFsKmsSupport",
    # 软件 OpenGL 回退和音视频解码器，跟着上面那些一起进来的
    "opengl32sw", "d3dcompiler", "avcodec", "avformat", "avutil",
    "swresample", "swscale",
)

# 用不到的 Python 包，从分析阶段就排除。
#
# 这里最要紧的是 qframelesswindow.webengine —— 它的 __init__ 无条件
# import 了 PyQt6.QtWebEngineWidgets，只要这个子模块被收进来，就会顺着
# 触发 WebEngine 的 hook，把 195MB 的 Qt6WebEngineCore.dll 一起拖进来。
# 光排除 PyQt6.QtWebEngineCore 没用：依赖链上还有人 import 它。
EXCLUDED_MODULES = [
    "tkinter", "unittest", "pydoc", "doctest", "pdb", "test",
    "qframelesswindow.webengine", "qfluentwidgets.webengine",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineQuick", "PyQt6.QtWebChannel", "PyQt6.QtWebSockets",
    "PyQt6.QtQuick", "PyQt6.QtQml", "PyQt6.QtMultimedia",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtPdf",

    # 这台机器上装了一大堆科学计算 / 深度学习库，PyInstaller 顺着某条 import
    # 链把它们整个收了进来。光一个 torch_cpu.dll 就 293MB，llvmlite 84MB。
    # 本程序只用标准库 + PyQt6 + qfluentwidgets，下面这些确定用不到。
    #
    # 只列"确定无关"的：早先凭感觉还排过 win32com、Qt6Xml 之类，结果
    # qframelesswindow 要 win32com、qfluentwidgets 要 QtXml，一启动就崩。
    "torch", "torchvision", "torchaudio", "torchtext", "pytorch_lightning",
    "numpy", "scipy", "pandas", "matplotlib", "sklearn", "scikit_learn",
    "skimage", "sympy", "networkx", "statsmodels", "seaborn",
    "onnx", "onnxruntime", "llvmlite", "numba", "cupy",
    "tensorflow", "keras", "jax", "transformers", "datasets", "safetensors",
    "cv2", "imageio", "tifffile", "h5py", "pyarrow",
    "IPython", "jupyter", "notebook", "nbformat", "nbconvert", "ipykernel",
]


def _wanted(entry) -> bool:
    """entry 是 (目标路径, 源路径, 类型) 三元组，按文件名判断。"""
    name = os.path.basename(entry[0])
    return not any(name.startswith(p) for p in SKIP_PREFIXES)


# qfluentwidgets 带 qss / 图片等资源，这些必须收全
from PyInstaller.utils.hooks import collect_all  # noqa: E402

datas = [("icon.ico", ".")]
binaries = []
hiddenimports = []
for _pkg in ("qfluentwidgets", "qframelesswindow"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    # collect_all 会把所有子模块都列成 hiddenimport，webengine 就是从这里
    # 溜进来的 —— excludes 管不住 hiddenimport，得在这儿单独滤掉
    hiddenimports += [h for h in _h if ".webengine" not in h.lower()]


a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDED_MODULES,
    noarchive=False,
)

# 关键的一步：把 hook 塞进来的无用 DLL 剔掉
_before = len(a.binaries)
a.binaries = TOC([e for e in a.binaries if _wanted(e)])
print(f"[spec] 剔除了 {_before - len(a.binaries)} 个无用二进制，"
      f"保留 {len(a.binaries)} 个")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ShareFiles",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon="icon.ico",
)
