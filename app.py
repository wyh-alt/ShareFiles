# -*- coding: utf-8 -*-
"""ShareFiles —— 把文件拖进来，就能发给别人的局域网分享工具。

界面分四块，从上到下正好是使用顺序：
    1. 分享内容  —— 拖入文件 / 文件夹
    2. 服务      —— 选地址、端口，启动共享
    3. 下载链接  —— 复制发给对方
    4. 下载状态  —— 看谁正在下、下到哪儿了，好决定什么时候能安全关掉
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import urllib.parse

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFontMetrics, QIcon, QPainter, QPen,
                         QPixmap)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog,
                             QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                             QMessageBox, QSizePolicy, QSystemTrayIcon,
                             QVBoxLayout, QWidget)

from qfluentwidgets import (BodyLabel, CaptionLabel, CardWidget, CheckBox,
                            FluentIcon as FIF, IconWidget, InfoBar,
                            InfoBarPosition, LineEdit, ListWidget, MessageBox,
                            MessageBoxBase, PasswordLineEdit, ProgressBar,
                            PushButton, ScrollArea, SpinBox, StrongBodyLabel,
                            SubtitleLabel, TransparentToolButton,
                            isDarkTheme, qconfig, setTheme, Theme)

import config as appconfig
from fileserver import (SecurityPolicy, ShareRegistry, TransferTracker,
                        create_server, get_lan_ips, human_size, human_speed,
                        parse_block_entry)

APP_NAME = "ShareFiles"
DEFAULT_PORT = 8000

# 单实例用的管道名。同一台电脑上只允许跑一个，第二个会被劝退
SINGLE_INSTANCE_KEY = "ShareFiles.SingleInstance"

# 下载详情里最多列出多少条历史记录（追踪器本身留得更多，只是不全画出来）
HISTORY_SHOWN = 60

# 卡片的内边距，标题要和卡片里的文字左对齐，得往里让出这么多
CARD_PAD = 20

# 分享内容 / 下载详情各自一屏最多显示几行，超出后列表内部滚动
LIST_MAX_ROWS = 8

# qfluentwidgets 的滚动条是浮在内容上的，滚动时给行让出这么宽，
# 否则条子会压住行尾的复制 / 移除按钮
SCROLLBAR_GUTTER = 12

# 状态指示灯的颜色
C_ON = "#0f7b0f"        # 绿：共享中
C_LIVE = "#0078d4"      # 蓝：有下载在跑
C_OFF = "#8a8a8a"       # 灰：没共享

# 下载详情里每个状态一个颜色点：绿=已完成，黄=下载中，红=中断/异常。
# 深浅主题各一套 —— 真黄在浅色底上几乎看不见，深色底上又得换亮的。
STATE_COLORS = {
    "done": ("#0f7b0f", "#6ccb5f"),
    "active": ("#d99413", "#fce100"),
    "cancelled": ("#c42b1c", "#ff99a4"),
}
C_STATE_FALLBACK = ("#8a8a8a", "#b0b0b0")


def state_color(state: str) -> str:
    light, dark = STATE_COLORS.get(state, C_STATE_FALLBACK)
    return dark if isDarkTheme() else light


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------

_current_toast = None


def toast(level: str, title: str, content: str = "", duration: int = 2400,
          parent=None, **_ignored):
    """弹一条通知。

    同一时刻只留一条：新来的先把旧的就地收掉，而不是在窗口右上角堆一摞
    —— 堆起来会盖住界面，而且用户一次也读不了那么多条。
    """
    global _current_toast
    if _current_toast is not None:
        try:
            # 先 hide 再 close：close 带淡出动画，等它放完新的就叠上去了
            _current_toast.hide()
            _current_toast.close()
        except RuntimeError:
            pass                      # 底层对象已经被销毁（超时自动关掉了）
        _current_toast = None

    maker = {"success": InfoBar.success, "info": InfoBar.info,
             "warning": InfoBar.warning, "error": InfoBar.error}[level]
    _current_toast = maker(title, content, duration=duration,
                           position=InfoBarPosition.TOP_RIGHT, parent=parent)
    return _current_toast


def resource_path(name: str) -> str:
    """兼容 PyInstaller 单文件模式的资源定位。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def dot(color: str, text: str) -> str:
    return f'<span style="color:{color};font-size:15px">●</span>&nbsp;{text}'


# --------------------------------------------------------------------------
# 拖放区
# --------------------------------------------------------------------------

class DropZone(QFrame):
    """一块虚线框，把文件或文件夹拖进来即可加入分享。"""

    filesDropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(124)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._active = False
        self._compact = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        lay.addStretch(1)

        self.iconWidget = IconWidget(FIF.DOWNLOAD, self)
        self.iconWidget.setFixedSize(42, 42)
        lay.addWidget(self.iconWidget, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title = StrongBodyLabel("拖放文件或文件夹到此处", self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)

        self.hint = CaptionLabel("或使用下方按钮选择", self)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.hint)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        self.fileBtn = PushButton(FIF.DOCUMENT, "选择文件", self)
        self.dirBtn = PushButton(FIF.FOLDER, "选择文件夹", self)
        btns.addWidget(self.fileBtn)
        btns.addWidget(self.dirBtn)
        btns.addStretch(1)
        lay.addLayout(btns)
        lay.addStretch(1)

        self.fileBtn.clicked.connect(self._pick_files)
        self.dirBtn.clicked.connect(self._pick_dir)
        qconfig.themeChanged.connect(self._restyle)
        self._restyle()

    # -- 外观 ---------------------------------------------------------------

    def _restyle(self):
        dark = isDarkTheme()
        if self._active:
            border, bg = "#0078d4", "rgba(0,120,212,0.13)"
            fg, fg_dim = "#4cc2ff" if dark else "#0067c0", "#4cc2ff" if dark else "#2b7cc4"
        else:
            border = "rgba(255,255,255,0.34)" if dark else "rgba(0,0,0,0.26)"
            bg = "rgba(255,255,255,0.03)" if dark else "rgba(0,0,0,0.016)"
            fg = "#f0f0f0" if dark else "#1b1b1b"
            fg_dim = "#b0b0b0" if dark else "#5c5c5c"
        self.setStyleSheet(
            f"DropZone{{border:2px dashed {border};border-radius:8px;"
            f"background:{bg};}}"
        )
        # 颜色必须显式给出：清空 stylesheet 会让 qfluentwidgets 的标签
        # 掉回它自己的默认色，在浅色底上会变成看不见的白字
        self.title.setStyleSheet(f"color:{fg};")
        self.hint.setStyleSheet(f"color:{fg_dim};")

    def _set_active(self, active: bool):
        if active != self._active:
            self._active = active
            self._restyle()

    def set_compact(self, compact: bool):
        """列表里已经有东西之后就把这块收窄，把纵向空间让给下载明细。"""
        if compact == self._compact:
            return
        self._compact = compact
        self.iconWidget.setVisible(not compact)
        self.hint.setVisible(not compact)
        self.title.setText("拖放文件或文件夹以添加" if compact else "拖放文件或文件夹到此处")
        self.setMinimumHeight(84 if compact else 124)
        self.updateGeometry()

    # -- 拖放 ---------------------------------------------------------------

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()
            self._set_active(True)
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._set_active(False)
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._set_active(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.filesDropped.emit(paths)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._pick_files()
        super().mouseReleaseEvent(e)

    # -- 选择按钮 -----------------------------------------------------------

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要分享的文件")
        if paths:
            self.filesDropped.emit(paths)

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择要分享的文件夹")
        if path:
            self.filesDropped.emit([path])


# --------------------------------------------------------------------------
# 列表行
# --------------------------------------------------------------------------

class ElidedLabel(BodyLabel):
    """放不下就从中间省略，完整文本进悬停提示。

    关键是 sizeHint 用 Ignored 策略：不然长文件名的 sizeHint 会撑得很宽，
    把同一行里的复制、移除按钮顶出窗口外面去。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)          # BodyLabel 的构造只收 parent
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        if text:
            self.setText(text)

    def setText(self, text: str):                # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()

    def _apply(self):
        avail = self.width() - 2
        if avail <= 0 or not self._full:
            return
        fm = QFontMetrics(self.font())
        super().setText(
            fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle, avail))


class ShareRow(QWidget):
    """分享列表里的一行：文件名 + 次数余量 + 大小 + 复制链接 + 移除。"""

    removeRequested = pyqtSignal(str)

    def __init__(self, item: dict, url: str = "", quota: str = "", parent=None):
        super().__init__(parent)
        self.vpath = item["vpath"]
        self.item_name = item["name"]
        self.url = url

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 5, 0, 5)
        lay.setSpacing(10)

        icon = QLabel("📁" if item["is_dir"] else "📄", self)
        icon.setFixedWidth(20)
        lay.addWidget(icon)

        name = ElidedLabel(item["name"], self)
        name.setToolTip(f'{item["name"]}\n{item["realpath"]}')
        lay.addWidget(name, 1)

        self.quotaLabel = CaptionLabel(quota, self)
        self.quotaLabel.setFixedWidth(64)
        self.quotaLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.quotaLabel.setVisible(bool(quota))
        if quota:
            self.quotaLabel.setToolTip("该分享项的剩余下载次数")
        lay.addWidget(self.quotaLabel)

        size = CaptionLabel(
            f'{item["count"]} 项' if item["is_dir"] else human_size(item["size"]), self
        )
        size.setFixedWidth(80)
        size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(size)

        copyBtn = TransparentToolButton(FIF.COPY, self)
        copyBtn.setFixedSize(26, 26)
        copyBtn.setToolTip("复制这一项的下载链接")
        copyBtn.clicked.connect(self._copy_link)
        lay.addWidget(copyBtn)

        btn = TransparentToolButton(FIF.CLOSE, self)
        btn.setFixedSize(26, 26)
        btn.setToolTip("从分享列表移除（不影响进行中的下载）")
        btn.clicked.connect(lambda: self.removeRequested.emit(self.vpath))
        lay.addWidget(btn)

    def _copy_link(self):
        if not self.url:
            return
        QApplication.clipboard().setText(self.url)
        # 提示里不铺开那条长链接，报一下复制的是哪一项就够了
        toast("success", f'已复制 "{self.item_name}" 链接', "", duration=2200,
                        position=InfoBarPosition.TOP_RIGHT, parent=self.window())


class TransferRow(QWidget):
    """下载详情里的一行。同一行会随任务状态变化：进行中 / 已完成 / 已中断。"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # 列宽按 550px 的默认窗口算过：可用宽度只有约 438px，所以省掉了独立的
        # 「大小」列 —— 那一列的信息在非进行中时挪到最后一列显示，进行中则放进
        # 悬停提示。完整信息都在 tooltip 里，不会丢。
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(8)

        self.iconLabel = QLabel("", self)
        self.iconLabel.setFixedWidth(16)
        self.iconLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.iconLabel)

        self.ipLabel = CaptionLabel("", self)
        self.ipLabel.setFixedWidth(92)
        self.ipLabel.setToolTip("下载者的 IP 地址")
        lay.addWidget(self.ipLabel)

        self.nameLabel = ElidedLabel("", self)
        lay.addWidget(self.nameLabel, 1)

        self.bar = ProgressBar(self)
        self.bar.setFixedWidth(72)
        lay.addWidget(self.bar)

        self.stateLabel = CaptionLabel("", self)
        self.stateLabel.setFixedWidth(44)
        self.stateLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.stateLabel)

        # 进行中显示速度，结束后改显示大小
        self.tailLabel = CaptionLabel("", self)
        self.tailLabel.setFixedWidth(80)
        self.tailLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.tailLabel)

    @staticmethod
    def _clock(ts: float) -> str:
        try:
            return time.strftime("%H:%M:%S", time.localtime(ts))
        except (ValueError, OSError):
            return ""

    @staticmethod
    def _size_pair(sent: int, total: int) -> str:
        """紧凑的 '2.5MB/14.0MB'，好塞进窄列。"""
        return f'{human_size(sent).replace(" ", "")}/{human_size(total).replace(" ", "")}'

    def update_data(self, d: dict):
        state = d["state"]
        pct = int(round(d["progress"] * 100))
        when = self._clock(d["updated"])

        self.ipLabel.setText(d["ip"])
        self.nameLabel.setText(d["name"])
        self.bar.setValue(pct)

        # 三种状态统一是圆点，靠颜色区分：绿=已完成，黄=下载中，红=中断
        self.iconLabel.setText("●")
        self.iconLabel.setStyleSheet(f"color:{state_color(state)};")

        if state == "active":
            self.stateLabel.setText(f"{pct}%")
            self.tailLabel.setText(human_speed(d["speed"]))
            state_txt = f"进行中 {pct}%"
        elif state == "done":
            self.stateLabel.setText("已完成")
            self.tailLabel.setText(human_size(d["total"]).replace(" ", ""))
            state_txt = f"已完成 {when}"
        else:
            self.stateLabel.setText("已中断")
            self.tailLabel.setText(self._size_pair(d["sent"], d["total"]))
            state_txt = f"已中断 {when}"

        # 窄栏放不下的（时间、连接数）都退到这里
        conns = f'（{d["conns"]} 条连接）' if d["conns"] > 1 else ""
        self.setToolTip(
            f'{d["ip"]}　{d["name"]}\n{d["url_path"]}\n'
            f'{human_size(d["sent"])} / {human_size(d["total"])}{conns}\n{state_txt}'
        )


# --------------------------------------------------------------------------
# 高级设置
# --------------------------------------------------------------------------

class SettingsDialog(MessageBoxBase):
    """网络地址 + 下载安全。服务是常开的，所以这里改完就重启生效，不放开关。"""

    def __init__(self, page: "SharePage"):
        super().__init__(page.window())
        self.page = page

        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel("高级设置", self))

        # ---------- 网络 ----------
        self.viewLayout.addWidget(StrongBodyLabel("网络", self))

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(BodyLabel("局域网地址", self))

        # 地址是自动检测出来的，不给选：多网卡时系统已经挑了默认联网的那一块，
        # 摆个下拉框反而让人以为得自己挑一个。换网络后启动时会自动跟着变。
        self.ipLabel = StrongBodyLabel(page.cfg_ip or "未检测到", self)
        self.ipLabel.setMinimumWidth(120)
        detected = get_lan_ips()
        tip = "自动使用本机当前联网的网卡地址，访问者通过它连接。"
        if len(detected) > 1:
            tip += f"\n检测到的全部地址：{'、'.join(detected)}"
        self.ipLabel.setToolTip(tip)
        row.addWidget(self.ipLabel)

        row.addSpacing(14)
        row.addWidget(BodyLabel("端口", self))
        self.portSpin = SpinBox(self)
        self.portSpin.setRange(1024, 65535)
        self.portSpin.setValue(page.cfg_port)
        # 两个微调按钮是并排的，占掉约 60px，宽度给窄了会把数字挤没
        self.portSpin.setFixedWidth(152)
        row.addWidget(self.portSpin)
        row.addStretch(1)
        self.viewLayout.addLayout(row)

        self.previewCheck = CheckBox("允许浏览器直接预览图片 / 视频", self)
        self.previewCheck.setChecked(bool(page.server and page.server.inline_preview))
        self.previewCheck.setToolTip(
            "未勾选时所有文件均以附件形式下载；\n"
            "勾选后图片、PDF、视频将在浏览器中直接打开。")
        self.viewLayout.addWidget(self.previewCheck)

        # ---------- 下载安全 ----------
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(StrongBodyLabel("下载安全", self))

        self.pwdCheck = CheckBox("需要密码才能下载", self)
        self.viewLayout.addWidget(self.pwdCheck)

        pwdRow = QHBoxLayout()
        pwdRow.addSpacing(26)
        self.pwdEdit = PasswordLineEdit(self)
        self.pwdEdit.setPlaceholderText("访问密码")
        self.pwdEdit.setText(page.policy.password)
        self.pwdEdit.setToolTip("访问时浏览器将要求身份验证：用户名可任意填写，仅校验密码")
        pwdRow.addWidget(self.pwdEdit)

        pwdNote = CaptionLabel("用户名可任意填写或留空，仅校验密码", self)
        pwdNote.setStyleSheet(f"color:{C_OFF};")
        pwdRow.addWidget(pwdNote)
        pwdRow.addStretch(1)
        self.viewLayout.addLayout(pwdRow)

        self.limitCheck = CheckBox("限制下载次数", self)
        self.viewLayout.addWidget(self.limitCheck)

        limitRow = QHBoxLayout()
        limitRow.addSpacing(26)
        limitRow.addWidget(BodyLabel("每个文件最多下载", self))
        self.limitSpin = SpinBox(self)
        self.limitSpin.setRange(1, 9999)
        self.limitSpin.setValue(page.policy.limit or 3)
        self.limitSpin.setFixedWidth(120)
        limitRow.addWidget(self.limitSpin)
        limitRow.addWidget(BodyLabel("次", self))
        limitRow.addStretch(1)
        self.viewLayout.addLayout(limitRow)

        hint = CaptionLabel(
            "明文校验，不防抓包；次数按完整下载计，断线重试与分段请求不重复扣。", self)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_OFF};")
        self.viewLayout.addWidget(hint)

        self.pwdCheck.setChecked(bool(page.policy.password))
        self.limitCheck.setChecked(bool(page.policy.limit))
        self.pwdCheck.stateChanged.connect(self._sync_enabled)
        self.limitCheck.stateChanged.connect(self._sync_enabled)
        self._sync_enabled()

        # ---------- IP 黑名单 ----------
        self.viewLayout.addSpacing(8)
        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("IP 黑名单", self))
        head.addStretch(1)
        self.blockCount = CaptionLabel("", self)
        head.addWidget(self.blockCount)
        self.viewLayout.addLayout(head)

        hint = CaptionLabel("支持单个 IP 或网段，如 192.168.1.0/24；双击条目可改。", self)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{C_OFF};")
        self.viewLayout.addWidget(hint)

        self.blockList = ListWidget(self)
        self.blockList.setFixedHeight(66)
        # 不加个框的话，名单看着就是几行普通文字，完全不像可点的列表
        self.blockList.setStyleSheet(
            "ListWidget{border:1px solid rgba(128,128,128,0.3);border-radius:4px;}")
        self.blockList.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        for entry in page.policy.blocked:
            self.blockList.addItem(entry)
        self.viewLayout.addWidget(self.blockList)

        brow = QHBoxLayout()
        brow.setSpacing(8)
        self.blockEdit = LineEdit(self)
        self.blockEdit.setPlaceholderText("例如 192.168.1.100")
        self.blockEdit.returnPressed.connect(self._add_block)
        brow.addWidget(self.blockEdit, 1)

        addBtn = PushButton("添加", self)
        addBtn.clicked.connect(self._add_block)
        brow.addWidget(addBtn)

        delBtn = PushButton("移除", self)
        delBtn.setToolTip("移除选中的条目（可多选）")
        delBtn.clicked.connect(self._remove_block)
        brow.addWidget(delBtn)
        self.viewLayout.addLayout(brow)

        self._sync_block_count()

        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(480)

    def _sync_enabled(self):
        self.pwdEdit.setEnabled(self.pwdCheck.isChecked())
        self.limitSpin.setEnabled(self.limitCheck.isChecked())

    # -- 黑名单增删 ---------------------------------------------------------

    def _blocked_entries(self) -> list:
        return [self.blockList.item(i).text() for i in range(self.blockList.count())]

    def _sync_block_count(self):
        n = self.blockList.count()
        self.blockCount.setText(f"{n} 条" if n else "未启用")

    def _add_block(self):
        text = self.blockEdit.text().strip()
        norm = parse_block_entry(text)
        if norm is None:
            InfoBar.warning("格式不正确", f"{text or '空内容'} 不是有效的 IP 或网段",
                            duration=2600, position=InfoBarPosition.TOP,
                            parent=self)
            return
        if norm in self._blocked_entries():
            InfoBar.info("条目已存在", norm, duration=2000,
                         position=InfoBarPosition.TOP, parent=self)
        else:
            self.blockList.addItem(norm)
        self.blockEdit.clear()
        self._sync_block_count()

    def _remove_block(self):
        selected = self.blockList.selectedItems()
        if not selected:
            InfoBar.warning("未选中条目", "请先在列表中选中要移除的条目", duration=2200,
                            position=InfoBarPosition.TOP, parent=self)
            return
        for item in selected:
            self.blockList.takeItem(self.blockList.row(item))
        self._sync_block_count()

    def values(self) -> dict:
        return {
            "ip": self.page.cfg_ip,      # 地址自动检测，界面上不给改
            "port": self.portSpin.value(),
            "preview": self.previewCheck.isChecked(),
            "password": self.pwdEdit.text().strip() if self.pwdCheck.isChecked() else "",
            "limit": self.limitSpin.value() if self.limitCheck.isChecked() else 0,
            # 双击改过的条目在这里统一过一遍校验，不合法的会被 set_blocked 丢掉
            "blocked": [t.strip() for t in self._blocked_entries() if t.strip()],
        }


class CloseDialog(MessageBoxBase):
    """关窗口时问一句：收进托盘继续共享，还是退出。

    两个按钮都是"动作"，没有传统意义上的取消；直接把对话框关掉就等于什么都不做。
    所以这里记下用户点了哪个，而不是看 exec() 的返回值。
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        self.choice = None                 # "tray" / "quit" / None（关掉了对话框）
        self.remember = False

        self.viewLayout.setSpacing(10)
        self.viewLayout.addWidget(SubtitleLabel("关闭窗口", self))
        # 显式分成两行：句子长了 Qt 会把对话框撑宽去迁就，最后一个词还会被甩到下一行
        self.viewLayout.addWidget(BodyLabel(
            "收进托盘后共享继续运行、链接保持有效；\n"
            "退出则停止共享，链接随即失效。", self))

        self.rememberCheck = CheckBox("记住我的选择，以后不再询问", self)
        self.viewLayout.addWidget(self.rememberCheck)

        self.yesButton.setText("收进托盘")
        self.cancelButton.setText("退出程序")
        self.widget.setMinimumWidth(400)

        self.yesButton.clicked.connect(lambda: self._pick("tray"))
        self.cancelButton.clicked.connect(lambda: self._pick("quit"))

    def _pick(self, action: str):
        self.choice = action
        self.remember = self.rememberCheck.isChecked()


# --------------------------------------------------------------------------
# 主页面
# --------------------------------------------------------------------------

class SharePage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__(window)
        self.win = window
        self.setObjectName("sharePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.registry = ShareRegistry()
        self.tracker = TransferTracker()
        self.policy = SecurityPolicy()
        self.server = None

        # 高级设置存在 ~/.sharefiles/config.json，首次运行会把这个文件建出来
        saved = appconfig.load_or_create()
        # 存的地址可能来自另一个网络环境（换了路由器、换了网线），不在当前网卡里就重新挑
        ips = get_lan_ips() or ["127.0.0.1"]
        self.cfg_ip = saved["ip"] if saved["ip"] in ips else ips[0]
        self.cfg_port = saved["port"]
        self.cfg_preview = saved["preview"]
        self.cfg_close_action = saved["close_action"]
        self.policy.set_password(saved["password"])
        self.policy.set_limit(saved["limit"])
        self.policy.set_blocked(saved["blocked"])

        self._quota_snapshot = None
        self._share_count = 0
        self._known_tasks: set = set()
        self._share_rows: dict[str, QWidget] = {}
        self._transfer_rows: dict[tuple, TransferRow] = {}

        # --- 滚动容器 ---
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)          # 否则滚动区和状态栏之间会空出一条缝

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        # 不关掉边框的话，这里会画一条线，正好压在状态栏自己的 border-top 上面
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)

        body = QWidget()
        body.setObjectName("pageBody")
        # 页面的这块底必须自己画不透明背景：AcrylicWindow 把窗口背景交给系统合成，
        # 一旦云母效果不可用（远程桌面、部分显卡驱动），没被卡片盖住的空白就会
        # 露出底下的深色，在浅色主题下格外难看。
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(16, 16, 16, 18)      # 窄边距，550 宽的窗口下内容别缩水
        lay.setSpacing(14)
        scroll.setWidget(body)
        self.body = body

        lay.addLayout(self._build_header(body))
        lay.addWidget(self._build_share_card(body))
        lay.addWidget(self._build_status_card(body))
        lay.addStretch(1)

        # 状态栏钉在窗口底部、不跟着滚动 —— 「现在能不能安全关掉」这个判断
        # 不该需要用户先滚动才能看到
        outer.addWidget(self._build_status_bar())

        # 卡片都建好之后再统一同步，否则刷新会撞上还没创建的控件
        self._sync_share_ui()

        self._apply_page_bg()
        qconfig.themeChanged.connect(self._apply_page_bg)

        # --- 定时刷新下载状态 ---
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()

        # 打开就开始共享：不用先点一次开关，拖进文件对方马上就能下。
        # 放到事件循环里做，免得"端口全被占用"的提示框赶在窗口显示之前弹出来。
        QTimer.singleShot(60, self._start_server)

    # -- 卡片 1：分享内容 ---------------------------------------------------

    def _build_share_card(self, parent) -> CardWidget:
        card = CardWidget(parent)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(CARD_PAD, 16, CARD_PAD, 16)
        lay.setSpacing(12)

        title = StrongBodyLabel("分享内容", card)
        lay.addWidget(title)

        self.dropZone = DropZone(card)
        self.dropZone.filesDropped.connect(self._add_paths)
        lay.addWidget(self.dropZone)

        self.shareList = QVBoxLayout()
        self.shareList.setSpacing(0)
        self.shareScroll = self._wrap_scroll(self.shareList, card)
        lay.addWidget(self.shareScroll)

        self.shareEmpty = CaptionLabel("尚未添加任何内容。", card)
        lay.addWidget(self.shareEmpty)

        line = QFrame(card)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("background:rgba(128,128,128,0.25);border:none;")
        self.shareSep = line
        lay.addWidget(line)

        # 开了密码就在这里亮出来，好连同链接一起发给对方
        self.pwdHint = CaptionLabel("", card)
        self.pwdHint.setWordWrap(True)
        lay.addWidget(self.pwdHint)

        row = QHBoxLayout()
        row.addStretch(1)
        self.copyRootBtn = PushButton(FIF.LINK, "复制链接", card)
        self.copyRootBtn.setToolTip("复制共享地址，打开后可看到全部内容")
        self.copyRootBtn.clicked.connect(self._copy_root)
        row.addWidget(self.copyRootBtn)

        self.refreshBtn = PushButton(FIF.SYNC, "刷新链接", card)
        self.refreshBtn.setToolTip("换一串新的随机地址，旧链接立即失效")
        self.refreshBtn.clicked.connect(self._refresh_link)
        row.addWidget(self.refreshBtn)

        self.clearBtn = PushButton("清空列表", card)
        self.clearBtn.clicked.connect(self._clear_shares)
        row.addWidget(self.clearBtn)
        lay.addLayout(row)

        return card

    # -- 页头 ---------------------------------------------------------------

    def _build_header(self, parent) -> QVBoxLayout:
        block = QVBoxLayout()
        block.setSpacing(6)
        # 往里缩一个卡片内边距，好跟卡片里的「分享内容」「下载详情」左对齐 ——
        # 只对齐卡片边框的话，大字标题看着会比卡片内容往外冒一截
        block.setContentsMargins(CARD_PAD, 0, 0, 0)

        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(SubtitleLabel("文件分享", parent))
        col.addWidget(CaptionLabel(
            "拖入文件即可生成下载链接，供同一局域网内的用户获取。", parent))
        row.addLayout(col)
        row.addStretch(1)

        # 服务是打开就在跑的，所以这里不放开关，只留一个进设置的入口
        self.settingsBtn = PushButton(FIF.SETTING, "高级设置", parent)
        self.settingsBtn.setMinimumWidth(118)
        self.settingsBtn.clicked.connect(self._open_settings)
        row.addWidget(self.settingsBtn, 0, Qt.AlignmentFlag.AlignBottom)
        block.addLayout(row)

        return block

    def _open_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec():
            self._apply_settings(dlg.values())

    def _apply_settings(self, values: dict):
        """把设置落到服务上。地址/端口变了必须重开一次监听才生效。"""
        need_restart = (values["ip"] != self.cfg_ip
                        or values["port"] != self.cfg_port)
        # 先记下来：_stop_server() 会把 self.server 清空，之后就问不出"原来在不在跑"了
        was_running = self.server is not None

        if need_restart and was_running:
            # 换地址得重开监听，这就等于停一次服务 —— 有下载在跑时先问过用户
            if not self._stop_server():
                return                       # 用户选了继续共享，这次改动整个作废

        self.cfg_ip = values["ip"]
        self.cfg_port = values["port"]
        self.cfg_preview = values["preview"]
        self.policy.set_password(values["password"])
        self.policy.set_limit(values["limit"])

        # 黑名单是逐请求检查的，改完立即生效，不用重开服务
        wanted = values.get("blocked", [])
        kept = self.policy.set_blocked(wanted)
        if kept != len(wanted):
            toast("warning", "有黑名单条目格式不对",
                  f"{len(wanted) - kept} 条已跳过", parent=self.window())

        # 开关次数限制会改变列表要显示的列，而用量本身没变、快照比对发现不了，
        # 所以这里清掉快照逼一次重建
        self._quota_snapshot = None
        self._sync_share_ui()

        if was_running:
            self._start_server()
        else:
            self._sync_share_ui()
            self._refresh_status()

        self._save_config()

        toast("success",
            "设置已保存",
            "地址或端口已变更，共享服务已按新配置重启。" if need_restart else "已生效。",
            duration=2600, position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def set_close_action(self, action: str):
        """记住用户选的关闭行为（"tray" / "quit"）。"""
        self.cfg_close_action = action
        self._save_config()

    def _save_config(self):
        """把当前设置写回 ~/.sharefiles/config.json。"""
        ok = appconfig.save({
            "ip": self.cfg_ip,
            "port": self.cfg_port,
            "preview": self.cfg_preview,
            "password": self.policy.password,
            "limit": self.policy.limit,
            "blocked": self.policy.blocked,
            "close_action": self.cfg_close_action,
        })
        if not ok:
            toast("warning", "设置已生效，但没能存进配置文件",
                  appconfig.CONFIG_PATH, duration=3600, parent=self.window())

    # -- 卡片 2：下载状态 ---------------------------------------------------

    def _build_status_card(self, parent) -> CardWidget:
        card = CardWidget(parent)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(CARD_PAD, 16, CARD_PAD, 16)
        lay.setSpacing(12)

        # 这里不再重复统计已完成 / 已中断的数量 —— 底部状态栏已经有"已服务 N 次"
        head = QHBoxLayout()
        head.addWidget(StrongBodyLabel("下载详情", card))
        head.addStretch(1)

        self.clearLogBtn = PushButton(FIF.DELETE, "清空记录", card)
        self.clearLogBtn.setToolTip("移除已结束的记录，进行中的下载不受影响")
        self.clearLogBtn.clicked.connect(self._clear_log)
        head.addWidget(self.clearLogBtn)
        lay.addLayout(head)

        self.transferList = QVBoxLayout()
        self.transferList.setSpacing(0)
        self.transferScroll = self._wrap_scroll(self.transferList, card)
        lay.addWidget(self.transferScroll)

        self.transferEmpty = CaptionLabel("暂无下载记录。", card)
        lay.addWidget(self.transferEmpty)

        return card

    def _clear_log(self):
        gone = self.tracker.clear_finished()
        self._refresh_status()
        if gone:
            toast("success", "已清空下载记录", f"移除了 {gone} 条",
                            duration=2200, position=InfoBarPosition.TOP_RIGHT,
                            parent=self.window())

    # -- 底部状态栏 ---------------------------------------------------------

    def _build_status_bar(self) -> QFrame:
        """钉在窗口底部的状态条：一眼看到「共享中吗、有没有人在下」。"""
        bar = QFrame(self)
        bar.setObjectName("statusBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bar.setFixedHeight(40)
        self.statusBar = bar

        lay = QHBoxLayout(bar)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(20)

        self.shareStatus = BodyLabel("", bar)
        self.queueStatus = BodyLabel("", bar)
        lay.addWidget(self.shareStatus)
        lay.addWidget(self.queueStatus)
        lay.addStretch(1)

        self.doneLabel = CaptionLabel("", bar)
        lay.addWidget(self.doneLabel)
        return bar

    def _apply_page_bg(self):
        """页面底色跟随主题 —— 不依赖系统云母合成，深浅色都稳定。"""
        dark = isDarkTheme()
        bg = "#202020" if dark else "#f3f3f3"
        line = "rgba(255,255,255,0.09)" if dark else "rgba(0,0,0,0.08)"
        self.setStyleSheet(f"#sharePage{{background:{bg};}}")
        self.body.setStyleSheet(f"#pageBody{{background:{bg};}}")
        self.statusBar.setStyleSheet(
            f"#statusBar{{background:{bg};border-top:1px solid {line};}}"
        )

    # ---------------------------------------------------------------------
    # 分享项增删
    # ---------------------------------------------------------------------

    def _add_paths(self, paths: list):
        added, skipped, failed = 0, 0, []
        for p in paths:
            try:
                _, is_new = self.registry.add(p)
                added += int(is_new)
                skipped += int(not is_new)
            except (FileNotFoundError, OSError):
                failed.append(os.path.basename(p))

        self._sync_share_ui()

        if failed:
            box = MessageBox(
                "部分内容未能加入",
                "以下路径不存在或无法读取：\n\n" + "\n".join(failed), self.window())
            box.yesButton.setText("知道了")
            box.cancelButton.hide()
            box.exec()
        elif added:
            msg = f"已加入 {added} 项"
            if skipped:
                msg += f"，{skipped} 项已在列表中"
            toast("success", "添加成功", msg, duration=2400,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())
        elif skipped:
            toast("info", "已在列表中", f"这 {skipped} 项已在列表中。",
                         duration=2400, position=InfoBarPosition.TOP_RIGHT,
                         parent=self.window())

    def _remove_share(self, vpath: str):
        # 只是从分享列表摘掉，已经在传的下载不受影响（handler 早把文件打开
        # 握在手里了），新的请求才会 404
        self.registry.remove(vpath)
        self._sync_share_ui()

    def _clear_shares(self):
        if not len(self.registry):
            return
        box = MessageBox("清空分享列表",
                         "确定要移除全部内容吗？移除后这些链接将立即失效。\n"
                         "（进行中的下载不受影响）",
                         self.window())
        box.yesButton.setText("清空")
        box.cancelButton.setText("取消")
        if not box.exec():
            return
        self.registry.clear()
        self._sync_share_ui()

    # ---------------------------------------------------------------------
    # 界面同步
    # ---------------------------------------------------------------------

    def _wrap_scroll(self, layout: QVBoxLayout, parent) -> ScrollArea:
        """把一列行包进滚动区，超过 LIST_MAX_ROWS 行就在列表内部滚动。"""
        layout.setContentsMargins(0, 0, 0, 0)
        holder = QWidget(parent)
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        holder.setStyleSheet("background:transparent;")
        holder.setLayout(layout)

        area = ScrollArea(parent)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.enableTransparentBackground()
        area.setWidget(holder)
        area.setVisible(False)
        area.content_layout = layout          # 后面调边距要用
        return area

    def _fit_scroll(self, area: ScrollArea, rows: list):
        """把滚动区高度收到最多 LIST_MAX_ROWS 行。"""
        if not rows:
            area.setVisible(False)
            return
        area.setVisible(True)

        # 要滚动了才让出滚动条那一溜，不然没滚动条时白白空一条边
        gutter = SCROLLBAR_GUTTER if len(rows) > LIST_MAX_ROWS else 0
        area.content_layout.setContentsMargins(0, 0, gutter, 0)

        shown = rows[:LIST_MAX_ROWS]
        h = sum(max(1, r.sizeHint().height()) for r in shown)
        area.setFixedHeight(h)

    @staticmethod
    def _scroll_to_bottom(area: ScrollArea):
        """把列表滚到最下面。

        布局是异步的：改完高度之后，滚动条的范围要等 Qt 处理完布局事件才更新。
        光用 singleShot(0) 会赶在范围算出来之前跑掉，那一刻 maximum() 还是 0，
        等于没滚——表现就是"新增了文件但没定位过去"。所以挂一次 rangeChanged
        兜底，滚到位就摘掉，免得之后再把用户手动滚动拽回来。
        """
        bar = area.verticalScrollBar()

        def detach():
            try:
                bar.rangeChanged.disconnect(on_range)
            except TypeError:
                pass

        def on_range(_lo, hi):
            # 范围终于算出来了，这才是真正滚到位的时机
            bar.setValue(hi)
            detach()

        def go():
            # 范围可能已经是对的，先直接试一次；不对的话 on_range 会补上。
            # 注意这里不能 detach —— 否则连接刚到就被摘掉，永远等不到范围更新
            bar.setValue(bar.maximum())

        bar.rangeChanged.connect(on_range)
        QTimer.singleShot(0, go)
        QTimer.singleShot(300, detach)      # 范围一直没变过也得把钩子摘干净

    def _clear_layout(self, layout):
        """清空一个布局。先 setParent(None) 立刻摘掉，否则 deleteLater 要等到
        下一轮事件循环，中间那一帧新旧控件会叠在一起。"""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _sync_share_ui(self):
        self._clear_layout(self.shareList)
        self._share_rows.clear()

        items = self.registry.snapshot()
        limit = self.policy.limit
        for it in items:
            quota = ""
            if limit:
                left = self.policy.remaining(it["vpath"])
                quota = f"剩 {left} 次"
            row = ShareRow(it, self._url_for(it), quota, self)
            row.removeRequested.connect(self._remove_share)
            self.shareList.addWidget(row)
            self._share_rows[it["vpath"]] = row

        has = bool(items)
        self._fit_scroll(self.shareScroll, self._layout_rows(self.shareList))
        self.shareEmpty.setVisible(not has)
        self.shareSep.setVisible(has)
        self.clearBtn.setVisible(has)
        self.copyRootBtn.setVisible(has)
        self.refreshBtn.setVisible(has)
        self.dropZone.set_compact(has)
        self._sync_pwd_hint()

        # 新加的排在最后，滚过去让用户直接看到
        if len(items) > self._share_count:
            self._scroll_to_bottom(self.shareScroll)
        self._share_count = len(items)

    @staticmethod
    def _layout_rows(layout: QVBoxLayout) -> list:
        return [layout.itemAt(i).widget() for i in range(layout.count())
                if layout.itemAt(i).widget() is not None]

    # ---------------------------------------------------------------------
    # 链接
    # ---------------------------------------------------------------------

    def _base_url(self) -> str:
        ip = self.cfg_ip or (get_lan_ips() or ["127.0.0.1"])[0]
        port = self.server.port if self.server else self.cfg_port
        return f"http://{ip}:{port}"

    def _prefix(self) -> str:
        """分享地址里的随机前缀，只有带上它才访问得到。"""
        token = self.server.token if self.server else ""
        return f"/{token}" if token else ""

    def _url_for(self, item: dict) -> str:
        url = self._base_url() + self._prefix() + urllib.parse.quote(item["vpath"])
        if item["is_dir"]:
            url += "/"
        return url

    def _sync_pwd_hint(self):
        pwd = self.policy.password
        if pwd:
            self.pwdHint.setText(
                f'🔒 已启用访问密码：{pwd}　'
                f'（访问时需身份验证，用户名可任意填写，仅校验密码）'
            )
        self.pwdHint.setVisible(bool(pwd))

    def _copy_root(self):
        url = f"{self._base_url()}{self._prefix()}/"
        QApplication.clipboard().setText(url)
        toast("success", "已复制链接", url, duration=2400,
                        position=InfoBarPosition.TOP_RIGHT, parent=self.window())

    def _refresh_link(self):
        """换一串新的随机前缀，旧链接当场作废。"""
        if self.server is None:
            toast("warning", "共享尚未启动", "稍后再试。", duration=2400,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())
            return

        snap = self.tracker.snapshot()
        if snap["task_count"]:
            box = MessageBox(
                "仍有下载进行中",
                f"当前有 {snap['task_count']} 个下载任务正在进行。\n\n"
                "已开始的下载不会中断；但接收方若中途断线，"
                "旧链接已失效将无法续传。\n\n确定要更换链接吗？",
                self.window())
            box.yesButton.setText("换新链接")
            box.cancelButton.setText("取消")
            if not box.exec():
                return

        self.server.refresh_token()
        self._sync_share_ui()
        self._refresh_status()
        toast("success", "已生成新链接", "旧链接已立即失效",
                        duration=2600, position=InfoBarPosition.TOP_RIGHT,
                        parent=self.window())

    # ---------------------------------------------------------------------
    # 服务开关
    # ---------------------------------------------------------------------

    def _start_server(self, notify: bool = False):
        """服务是常开的：程序一启动就调它，之后只有改地址/端口才会重开。"""
        if self.server is not None:
            return

        try:
            server, actual = create_server(self.registry, self.tracker,
                                           self.cfg_port, policy=self.policy)
        except OSError as exc:
            box = MessageBox("无法启动共享服务",
                             f"{exc}\n\n请在「高级设置」中更换端口后重试。", self.window())
            box.yesButton.setText("知道了")
            box.cancelButton.hide()
            box.exec()
            return

        server.inline_preview = self.cfg_preview
        server.start()
        self.server = server

        if actual != self.cfg_port:
            wanted = self.cfg_port          # 先留住原值，下面一赋值两个数字就一样了
            self.cfg_port = actual
            toast("warning", "端口被占用",
                            f"{wanted} 已被其他程序占用，已改用 {actual}。",
                            duration=4200, position=InfoBarPosition.TOP_RIGHT,
                            parent=self.window())
        elif notify:
            toast("success", "共享中", f"{self._base_url()}/", duration=2400,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.window())

        self._sync_share_ui()
        self._refresh_status()

    def _stop_server(self) -> bool:
        """停止服务。有下载在跑时先问一句 —— 这正是「下载状态」存在的意义。"""
        if self.server is None:
            return True

        snap = self.tracker.snapshot()
        if snap["task_count"]:
            names = "、".join(it["name"] for it in snap["active"][:3])
            more = "…" if snap["task_count"] > 3 else ""
            box = MessageBox(
                "仍有下载进行中",
                f"当前有 {snap['task_count']} 个下载正在进行（{names}{more}）。\n\n"
                "停止共享将中断这些下载，接收方需重新下载。\n"
                "建议等待下载完成后再操作。",
                self.window())
            box.yesButton.setText("仍要停止")
            box.cancelButton.setText("继续共享")
            if not box.exec():
                return False

        self.server.stop()
        self.server = None
        self._sync_share_ui()
        self._refresh_status()
        return True

    # ---------------------------------------------------------------------
    # 状态刷新
    # ---------------------------------------------------------------------

    def _refresh_status(self):
        snap = self.tracker.snapshot()
        running = self.server is not None

        # 共享状态
        if running:
            # 地址不放状态栏了：550 宽的窗口装不下，会被截成半截 URL 反而更难认，
            # 需要地址时用「复制链接」
            self.shareStatus.setText(dot(C_ON, "当前处于共享中"))
            self.shareStatus.setToolTip(f"{self._base_url()}/")
        else:
            self.shareStatus.setText(dot(C_OFF, "当前未共享"))
            self.shareStatus.setToolTip("")

        # 托盘图标右下角的状态点跟着一起变
        self.win.set_tray_busy(snap["task_count"] > 0)

        # 下载队列
        n, conns = snap["task_count"], snap["conn_count"]
        if n:
            text = f"当前 {n} 个下载队列"
            if conns > n:
                text += f"（{conns} 条连接）"
            self.queueStatus.setText(dot(C_LIVE, text))
        else:
            self.queueStatus.setText(dot(C_OFF, "当前没有下载"))

        # 累计
        if snap["completed"] or snap["bytes_served"]:
            self.doneLabel.setText(
                f'已服务 {snap["completed"]} 次 · {human_size(snap["bytes_served"])}')
        else:
            self.doneLabel.setText("")

        # 明细行按创建时间排，最新的在最下面。这样任务从「进行中」变成
        # 「已完成」时位置不动，不会在眼前跳来跳去。
        items = snap["active"] + snap["recent"][:HISTORY_SHOWN]
        items.sort(key=lambda x: x.get("created", 0))

        seen = set()
        for it in items:
            key = (it["ip"], it["url_path"])
            if key in seen:
                continue
            seen.add(key)
            row = self._transfer_rows.get(key)
            if row is None:
                row = TransferRow(self)
                self._transfer_rows[key] = row
                self.transferList.addWidget(row)
            row.update_data(it)

        for key in [k for k in self._transfer_rows if k not in seen]:
            w = self._transfer_rows.pop(key)
            w.setParent(None)
            w.deleteLater()

        self._fit_scroll(self.transferScroll, self._layout_rows(self.transferList))
        self.transferEmpty.setVisible(not seen)

        # 只有冒出新的下载任务才自动滚到底，不然用户正翻着记录会被一直拽走
        if seen - self._known_tasks:
            self._scroll_to_bottom(self.transferScroll)
        self._known_tasks = seen

        self.clearLogBtn.setEnabled(bool(snap["recent"]))

        # 次数余量变了才重建分享列表，不然每 400ms 重建一次太浪费
        if self.policy.limit:
            usage = tuple(sorted(self.policy.snapshot()["used"].items()))
            if usage != self._quota_snapshot:
                self._quota_snapshot = usage
                self._sync_share_ui()

    # ---------------------------------------------------------------------

    def shutdown(self):
        self._timer.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    """用系统原生标题栏。

    qfluentwidgets 的无边框窗口在这台机器上画不出云母效果（见 README），
    自绘标题栏的按钮图标会糊在背景里；原生标题栏的最小化/最大化/关闭按钮
    由系统绘制，深色浅色都跟着系统走，反而更稳。
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(500, 520)

        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.page = SharePage(self)
        self.setCentralWidget(self.page)

        # 按屏幕大小定初始尺寸并居中，笔记本小屏上也不会顶到任务栏
        avail = QApplication.primaryScreen().availableGeometry()
        w = min(600, int(avail.width() * 0.9))
        h = min(940, int(avail.height() * 0.88))
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2,
                  avail.y() + max(0, (avail.height() - h) // 2))

        self._tray = self._build_tray(icon_path)

        # 守着单实例的管道：第二个实例启动时会来敲门，我们负责把窗口亮出来
        self._ipc = QLocalServer(self)
        self._ipc.removeServer(SINGLE_INSTANCE_KEY)   # 清掉上次没退干净留下的
        self._ipc.listen(SINGLE_INSTANCE_KEY)
        self._ipc.newConnection.connect(self._on_second_launch)

    def _on_second_launch(self):
        """又有一个人双击了图标。把窗口叫到前面来，然后收掉这条连接。"""
        while self._ipc.hasPendingConnections():
            conn = self._ipc.nextPendingConnection()
            if conn is not None:
                conn.readAll()
                conn.disconnected.connect(conn.deleteLater)
        self._restore_from_tray()

    def _build_tray(self, icon_path: str):
        """托盘图标。关窗口时可以把程序收进来继续共享。

        系统没有托盘（少数精简环境）就返回 None，关闭逻辑会据此直接走退出，
        不会给出一个做不到的"收进托盘"。
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None

        self._base_icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        # 两种状态各画一次存起来，别每次刷新都重画
        self._tray_icons = {False: self._tray_icon(False),
                            True: self._tray_icon(True)}
        self._tray_busy = None

        tray = QSystemTrayIcon(self)
        tray.setIcon(self._tray_icons[False])
        tray.setToolTip(f"{APP_NAME} · 正在共享")

        menu = QMenu()
        menu.addAction("显示主窗口", self._restore_from_tray)
        menu.addSeparator()
        menu.addAction("退出", self._quit_from_tray)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_clicked)
        tray.show()
        return tray

    def _tray_icon(self, busy: bool) -> QIcon:
        """图标右下角点一个小圆点：亮色 = 待命，绿色 = 有人正在下载。

        圆点带一圈半透明描边，压在图标上不会糊成一团；位置往里收了一点，
        免得落到图标圆角之外悬空。
        """
        size = 64
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(0, 0, self._base_icon.pixmap(size, size))

        d = size * 0.36
        m = size * 0.09
        # 描边要够重：浅色点亮时它紧挨着图标里那个白箭头，描边浅了就糊成一坨
        p.setPen(QPen(QColor(20, 20, 20, 140), max(1.5, size * 0.055)))
        p.setBrush(QColor("#6ccb5f") if busy else QColor("#ffffff"))
        p.drawEllipse(QRectF(size - d - m, size - d - m, d, d))
        p.end()
        return QIcon(pm)

    def set_tray_busy(self, busy: bool):
        """切换托盘图标的状态点。只在状态真的变了时才重设。"""
        if self._tray is None or busy == self._tray_busy:
            return
        self._tray_busy = busy
        self._tray.setIcon(self._tray_icons[busy])
        self._tray.setToolTip(
            f"{APP_NAME} · " + ("正在被下载" if busy else "正在共享"))

    # -- 托盘 ---------------------------------------------------------------

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_from_tray()

    def _quit_from_tray(self):
        if self._do_quit():
            QApplication.quit()

    # -- 关闭与退出 ---------------------------------------------------------

    def _resolve_close_action(self):
        """决定这次关窗口干什么。没记住过就先问一次。"""
        if self._tray is None:
            return "quit"                # 没托盘就没什么可选的
        saved = self.page.cfg_close_action
        if saved in ("tray", "quit"):
            return saved

        dlg = CloseDialog(self)
        dlg.exec()
        if dlg.choice is None:
            return None                  # 直接把对话框关掉了 = 什么都不做
        if dlg.remember:
            self.page.set_close_action(dlg.choice)
        return dlg.choice

    def _do_quit(self) -> bool:
        """退出流程。有下载在跑时先问过用户，返回是否真的退出。"""
        snap = self.page.tracker.snapshot()
        if snap["task_count"]:
            names = "、".join(it["name"] for it in snap["active"][:3])
            more = "…" if snap["task_count"] > 3 else ""
            box = MessageBox(
                "仍有下载进行中",
                f"当前有 {snap['task_count']} 个下载任务正在进行（{names}{more}）。\n\n"
                "退出将中断这些下载，接收方需重新下载。\n"
                "确定要退出吗？", self)
            box.yesButton.setText("仍然退出")
            box.cancelButton.setText("继续共享")
            if not box.exec():
                return False

        self.page.shutdown()
        if self._tray is not None:
            self._tray.hide()
        return True

    def closeEvent(self, e):
        action = self._resolve_close_action()

        if action == "tray":
            # 隐藏到托盘：服务照跑，对方还能接着下
            e.ignore()
            self.hide()
            if self._tray is not None:
                self._tray.showMessage(
                    APP_NAME, "仍在后台共享中，双击托盘图标可以回到窗口。",
                    QSystemTrayIcon.MessageIcon.Information, 3000)
            return

        if action == "quit" and self._do_quit():
            e.accept()
            return

        e.ignore()      # 用户取消了，或者退出被"还有人在下载"拦下了


def hand_off_to_running() -> bool:
    """已经有实例在跑就交给它，自己不再启动。

    光提示"已在运行"不够用：程序可能正收在托盘里，用户看不到窗口只会更困惑。
    所以顺带敲一下那个实例，让它把窗口亮出来。
    """
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_KEY)
    if not sock.waitForConnected(300):
        return False                      # 没人在跑，可以正常启动

    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()

    # 这里不能用 qfluentwidgets 的 MessageBox：它的遮罩层要靠 parent 的尺寸
    # 铺满窗口，而此刻主窗口还没建出来，parent 只能是 None，一构造就抛
    # AttributeError。原生 QMessageBox 不要求 parent。
    box = QMessageBox()
    box.setWindowTitle("程序已在运行")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText("ShareFiles 已在本机运行。")
    box.setInformativeText(
        "重复启动会占用同一端口，故不再开启新实例。\n已切换到正在运行的窗口。")
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
    return True


def main() -> int:
    # 让任务栏图标用本程序的图标，而不是 Python 的
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sharefiles.lan.fileshare.1")
    except (AttributeError, OSError):
        pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    if hand_off_to_running():
        return 0

    setTheme(Theme.AUTO)
    icon_path = resource_path("icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
