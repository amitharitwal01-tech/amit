"""
Manuscript Engine — a desktop front end for the paper-pipeline scripts.

Every button in this app is wired to the REAL, current command-line
interface of the script it calls (verified against the source, not
guessed). Reference, so a future script change can be checked against
this list:

    doi_resolver.py         no CLI args (config-driven)
    proxy_download.py       no CLI args (config-driven); one interactive
                             prompt at startup only, if auto-login fails
    download_papers.py      no CLI args (runs the two above in sequence)
    import_existing_pdfs.py --folder PATH --yes   (interactive if omitted)
    build_index.py          --rebuild | --search Q... | --find-figure Q...
                             | --match-figure IMG  [--top N] [+ filters]
    ask_library.py          [question...] [--file F] [--top N] [--no-ai]
                             [--pack] [--out FILE|DIR] [+ filters]
                             (default answers/)
    export_catalog.py       [--full] [--category C] [--journal J] [--since Y]
                             [--until Y] [--years SPEC] [--entries SPEC]
                             [--status S] [--out FILE|DIR]  (default catalogs/)
    export_for_claude.py    OUTLINE(.txt/.md/.docx) [--per-section N]
                             [--style FILE] [--out FILE|DIR] [+ filters]
                             (default research_packs/)

    "[+ filters]" = the shared library filters, accepted identically by
    build_index.py search modes, ask_library.py and export_for_claude.py:
    [--journal J] [--category C] [--years SPEC] [--since Y] [--until Y]
    [--entries SPEC]
    extract_cited_references.py   MANUSCRIPT [--out DIR]
    extract_cited_figures.py      MANUSCRIPT [--out DIR]
    export_citation_library.py    MANUSCRIPT [--out PREFIX]
                             (all three default to finalized/<manuscript name>/)

Every one of these is launched with its working directory set to the
scripts folder — required, since each script resolves its config file
and every relative path (downloads/, library_index/, ...) relative to
the directory it's run from.

Install:
    pip install PySide6 ruamel.yaml pandas openpyxl

Run:
    python manuscript_engine.py
"""
from __future__ import annotations

import csv
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QProcess, QSettings, Qt, QSize, Signal
from PySide6.QtGui import (
    QAction, QBrush, QColor, QKeySequence, QPixmap, QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QGraphicsDropShadowEffect, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QDoubleSpinBox, QSplitter,
    QStackedWidget, QStatusBar, QStyle, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextBrowser, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

try:
    import pandas as pd
except Exception:
    pd = None

try:
    from ruamel.yaml import YAML
    _yaml = YAML()
    _yaml.preserve_quotes = True
    _yaml.width = 4096  # avoid re-wrapping long comment/value lines on save
    HAS_RUAMEL = True
except Exception:
    HAS_RUAMEL = False

try:
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView
    HAS_QPDF = True
except Exception:
    QPdfDocument = None
    QPdfView = None
    HAS_QPDF = False

ORG = "PaperPipeline"
APP = "ManuscriptEngine"
CONFIG_FILENAME = "paper_pipeline_config.yaml"
CREDENTIALS_FILENAME = "login_credentials.txt"
GEMINI_KEY_FILENAME = "gemini_api_key.txt"
STYLE_RULES_FILENAME = "style_rules.txt"
SUPPORTED_VIEWER_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".xls", ".csv", ".txt", ".md"}

# Substrings in a script's own stdout that mean it's now blocked
# waiting for an Enter keypress on stdin — the panel's "Continue"
# button becomes active when one of these is seen.
STDIN_WAIT_MARKERS = (
    "press Enter to continue",
    "tap it now",
)

# "[12/345] ..." at the start of an output line is the pipeline scripts'
# progress marker (build_index.py per paper, doi_resolver.py per row,
# ask_library.py per sub-question, export_for_claude.py per section).
# The panel turns it into a real percentage bar instead of the endless
# "busy" animation, so it's obvious the run is alive and how far along.
PROGRESS_MARKER = re.compile(r"^\[(\d+)\s*/\s*(\d+)\b")


# ===========================================================================
# Theme — light and dark, a calmer/denser variant of common desktop-app design
# ===========================================================================

ACCENT = "#2C6E8E"
ACCENT_DARK = "#1F4E63"

LIGHT_QSS = """
* { font-family: "Segoe UI", "Inter", "Helvetica Neue", sans-serif; font-size: 10pt; }
QMainWindow, QWidget#Root { background: #F2F5F8; color: #1B2430; }
QFrame#Header {
    background: #FFFFFF;
    border-bottom: 1px solid #DCE3EA;
}
QLabel#AppTitle { font-size: 17pt; font-weight: 700; color: #14202B; }
QLabel#Subtitle { font-size: 9pt; color: #6B7A88; }
QLabel#SectionTitle { font-size: 12.5pt; font-weight: 700; color: #14202B; }
QLabel#HeroTitle { font-size: 17pt; font-weight: 700; color: #10202C; }
QLabel#HeroText, QLabel#Hint { color: #5E6E7D; }
QLabel#Mono { font-family: "Cascadia Mono", "Consolas", monospace; color: #5E6E7D; }
QLabel#Pill {
    background: #E7F0F5; color: #1E5670; border: 1px solid #CFE0E9;
    border-radius: 10px; padding: 3px 9px; font-weight: 600; font-size: 8.5pt;
}
QLabel#StatusOk { color: #1C7C4E; font-weight: 600; }
QLabel#StatusWarn { color: #A46A00; font-weight: 600; }
QLabel#StatusErr { color: #B23A3A; font-weight: 600; }
QFrame#Card, QGroupBox {
    background: #FFFFFF; border: 1px solid #DEE5EB; border-radius: 14px;
}
QGroupBox { margin-top: 14px; padding: 16px 14px 14px 14px; font-weight: 650; color: #14202B; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QTableWidget {
    background: #FFFFFF; border: 1px solid #CBD5DF; border-radius: 8px;
    padding: 7px; selection-background-color: #AED0E3;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid %(accent)s;
}
QPushButton, QToolButton {
    background: #EEF2F6; color: #1B2430; border: 1px solid #D2DAE2;
    border-radius: 8px; padding: 8px 14px; font-weight: 600;
}
QPushButton:hover, QToolButton:hover { background: #E1EAF0; }
QPushButton:disabled { color: #9AA7B2; background: #F2F5F8; }
QPushButton#Primary { background: %(accent)s; border: 1px solid %(accent)s; color: white; }
QPushButton#Primary:hover { background: %(accent_dark)s; }
QPushButton#Primary:disabled { background: #A9C2CD; border: 1px solid #A9C2CD; }
QPushButton#Danger { background: #FBEBEB; color: #7A2E2E; border: 1px solid #EBCACA; }
QPushButton#Ghost { background: transparent; border: 1px solid transparent; color: #4A5A68; }
QPushButton#Ghost:hover { background: #E9EEF3; }
QTabBar::tab { background: transparent; color: #55677A; padding: 8px 14px; margin-right: 2px; border-radius: 8px; }
QTabBar::tab:selected { background: #E7F0F5; color: #14202B; font-weight: 650; }
QListWidget#Sidebar { background: #FFFFFF; border: 1px solid #DEE5EB; border-radius: 14px; padding: 6px; }
QListWidget#Sidebar::item { padding: 10px 10px; border-radius: 9px; color: #4E5D6C; margin: 1px 0; }
QListWidget#Sidebar::item:hover { background: #EFF4F8; }
QListWidget#Sidebar::item:selected { background: #E1EDF3; color: #14202B; font-weight: 650; }
QProgressBar { border: 1px solid #CBD5DF; border-radius: 7px; background: #FFFFFF; text-align: center; height: 15px; }
QProgressBar::chunk { border-radius: 7px; background: %(accent)s; }
QStatusBar { background: #FFFFFF; border-top: 1px solid #DEE5EB; color: #5E6E7D; }
QScrollArea { border: none; }
QSplitter::handle { background: transparent; }
""" % {"accent": ACCENT, "accent_dark": ACCENT_DARK}

DARK_QSS = """
* { font-family: "Segoe UI", "Inter", "Helvetica Neue", sans-serif; font-size: 10pt; }
QMainWindow, QWidget#Root { background: #10161F; color: #E4EAF0; }
QFrame#Header { background: #131B26; border-bottom: 1px solid #223040; }
QLabel#AppTitle { font-size: 17pt; font-weight: 700; color: #F5F8FA; }
QLabel#Subtitle { font-size: 9pt; color: #93A3B4; }
QLabel#SectionTitle, QLabel#HeroTitle { font-weight: 700; color: #F5F8FA; }
QLabel#SectionTitle { font-size: 12.5pt; }
QLabel#HeroTitle { font-size: 17pt; }
QLabel#HeroText, QLabel#Hint { color: #93A3B4; }
QLabel#Mono { font-family: "Cascadia Mono", "Consolas", monospace; color: #93A3B4; }
QLabel#Pill { background: #1B2A38; color: #9FD3E8; border: 1px solid #2C4054; border-radius: 10px; padding: 3px 9px; font-weight: 600; font-size: 8.5pt; }
QLabel#StatusOk { color: #37B579; font-weight: 600; }
QLabel#StatusWarn { color: #D69A2D; font-weight: 600; }
QLabel#StatusErr { color: #E06060; font-weight: 600; }
QFrame#Card, QGroupBox, QListWidget#Sidebar { background: #151F2C; border: 1px solid #223040; border-radius: 14px; }
QGroupBox { margin-top: 14px; padding: 16px 14px 14px 14px; font-weight: 650; color: #F5F8FA; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QTableWidget {
    background: #0D1420; border: 1px solid #2A3A4C; border-radius: 8px; padding: 7px;
    color: #E4EAF0; selection-background-color: #2A5C74;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid #4CA6C4;
}
QPushButton, QToolButton { background: #1C2836; color: #E4EAF0; border: 1px solid #2E4257; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover, QToolButton:hover { background: #24374A; }
QPushButton:disabled { color: #5C6B7A; background: #151F2C; }
QPushButton#Primary { background: #2C7E9E; border: 1px solid #2C7E9E; color: white; }
QPushButton#Primary:hover { background: #3591B3; }
QPushButton#Primary:disabled { background: #24475A; border: 1px solid #24475A; }
QPushButton#Danger { background: #33201F; color: #E9AFAF; border: 1px solid #4A2C2C; }
QPushButton#Ghost { background: transparent; border: 1px solid transparent; color: #A9BACB; }
QPushButton#Ghost:hover { background: #1B2734; }
QTabBar::tab { background: transparent; color: #93A3B4; padding: 8px 14px; margin-right: 2px; border-radius: 8px; }
QTabBar::tab:selected { background: #1E2C3B; color: #F5F8FA; font-weight: 650; }
QListWidget#Sidebar::item { padding: 10px 10px; border-radius: 9px; color: #93A3B4; margin: 1px 0; }
QListWidget#Sidebar::item:hover { background: #1B2734; }
QListWidget#Sidebar::item:selected { background: #21374A; color: #F5F8FA; font-weight: 650; }
QProgressBar { border: 1px solid #2A3A4C; border-radius: 7px; background: #0D1420; text-align: center; height: 15px; }
QProgressBar::chunk { border-radius: 7px; background: #2C7E9E; }
QStatusBar { background: #151F2C; border-top: 1px solid #223040; color: #93A3B4; }
QScrollArea { border: none; }
QSplitter::handle { background: transparent; }
"""


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ===========================================================================
# Config file I/O — round-trip so comments in paper_pipeline_config.yaml survive
# ===========================================================================

def load_yaml_roundtrip(path: Path):
    if not HAS_RUAMEL:
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return _yaml.load(f)
    except Exception:
        return None


def save_yaml_roundtrip(path: Path, data) -> bool:
    if not HAS_RUAMEL or data is None:
        return False
    try:
        with path.open("w", encoding="utf-8") as f:
            _yaml.dump(data, f)
        return True
    except Exception:
        return False


def get_nested(data, dotted_key, default=""):
    node = data
    for part in dotted_key.split("."):
        if node is None or part not in node:
            return default
        node = node[part]
    return node if node is not None else default


def set_nested(data, dotted_key, value) -> None:
    parts = dotted_key.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or node[part] is None:
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


# ===========================================================================
# Small reusable widgets
# ===========================================================================

class Card(QFrame):
    def __init__(self, title: str = "", subtitle: str = "", hero: bool = False):
        super().__init__()
        self.setObjectName("Card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18 if hero else 12)
        shadow.setOffset(0, 5 if hero else 3)
        shadow.setColor(QColor(10, 18, 28, 26 if hero else 16))
        self.setGraphicsEffect(shadow)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(20, 18, 20, 18)
        self.layout_.setSpacing(11)
        if title:
            t = QLabel(title)
            t.setObjectName("HeroTitle" if hero else "SectionTitle")
            self.layout_.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("HeroText" if hero else "Hint")
            s.setWordWrap(True)
            self.layout_.addWidget(s)

    def layout(self):
        return self.layout_


class ScrollPage(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        content = QWidget()
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(22, 20, 26, 20)
        self._layout.setSpacing(14)
        self.setWidget(content)

    def layout(self):
        return self._layout


class PathPicker(QWidget):
    changed = Signal(str)

    def __init__(self, initial: str = "", is_dir: bool = True, filter_str: str = "All files (*.*)", placeholder: str = ""):
        super().__init__()
        self.is_dir = is_dir
        self.filter_str = filter_str
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.edit = QLineEdit(initial)
        self.edit.setPlaceholderText(placeholder or ("Choose a folder..." if is_dir else "Choose a file..."))
        self.edit.setClearButtonEnabled(True)
        self.browse_btn = QPushButton("Browse")
        self.browse_btn.clicked.connect(self.browse)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse_btn)
        self.edit.textChanged.connect(self.changed.emit)

    def browse(self):
        start = self.edit.text() or str(Path.home())
        if self.is_dir:
            path = QFileDialog.getExistingDirectory(self, "Select folder", start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Select file", start, self.filter_str)
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, value: str) -> None:
        self.edit.setText(value or "")


class OutputChooser(QWidget):
    """Optional 'where to save' row: a folder picker plus a filename
    field. Anything left blank falls back to the script's own organized
    default, so this never has to be filled in."""

    def __init__(self, name_placeholder: str):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.folder = PathPicker("", is_dir=True, placeholder="Folder (blank = default)")
        self.name = QLineEdit()
        self.name.setPlaceholderText(name_placeholder)
        self.name.setClearButtonEnabled(True)
        layout.addWidget(self.folder, 3)
        layout.addWidget(self.name, 2)

    def out_arg(self) -> list[str]:
        folder = self.folder.value()
        name = self.name.text().strip()
        if folder and name:
            return ["--out", str(Path(folder) / name)]
        if folder:
            # Folder only: the trailing separator tells the script to
            # keep its default filename inside this folder.
            return ["--out", folder.rstrip("/\\") + os.sep]
        if name:
            return ["--out", name]
        return []


class LogConsole(QPlainTextEdit):
    def __init__(self):
        super().__init__(readOnly=True)
        self.setMaximumBlockCount(20000)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

    def append_log(self, message: str, level: str = "INFO", source: str = "App") -> None:
        ts = time.strftime("%H:%M:%S")
        prefix = f"[{ts}] [{level.upper():7}] [{source}] "
        color = {
            "INFO": "#7A8898", "SUCCESS": "#1C7C4E", "WARNING": "#A46A00",
            "ERROR": "#B23A3A", "CMD": "#2C6E8E",
        }.get(level.upper(), "#7A8898")
        self.appendHtml(f'<span style="color:{color}; white-space:pre;">{escape_html(prefix + message)}</span>')
        self.moveCursor(QTextCursor.End)


class ProcessPanel(QWidget):
    """Runs one script via QProcess, streams its output, and — when the
    script prints one of STDIN_WAIT_MARKERS — offers a Continue button
    that sends Enter to it (needed for proxy_download.py's one
    remaining interactive login-fallback prompt)."""

    finished_ok = Signal()
    finished_error = Signal()

    def __init__(self, title: str, log_console: LogConsole, status_cb: Callable[[str], None]):
        super().__init__()
        self.title = title
        self.log_console = log_console
        self.status_cb = status_cb
        self.process: Optional[QProcess] = None
        self.start_time = 0.0
        self._pending_stdin_wait = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.setObjectName("Primary")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setEnabled(False)
        self.continue_btn = QPushButton("I've handled it — Continue")
        self.continue_btn.setVisible(False)
        self.state_label = QLabel("Idle")
        self.state_label.setObjectName("Hint")
        controls.addWidget(self.run_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.continue_btn)
        controls.addWidget(self.state_label, 1)
        layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(6000)
        self.log.setMinimumHeight(160)
        layout.addWidget(self.log)

        self.stop_btn.clicked.connect(self.stop)
        self.continue_btn.clicked.connect(self._send_continue)

    def start(self, python_exe: str, script_path: str, args: list[str], cwd: str) -> bool:
        if self.process is not None:
            QMessageBox.warning(self, "Busy", "This panel already has a run in progress.")
            return False
        if not Path(script_path).exists():
            self._append(f"Script not found: {script_path}", "ERROR")
            QMessageBox.warning(self, "Missing script", f"Could not find:\n{script_path}\n\nCheck the Scripts folder in Settings.")
            return False
        if not cwd or not Path(cwd).is_dir():
            self._append(f"Scripts folder is not valid: {cwd!r}", "ERROR")
            QMessageBox.warning(self, "Scripts folder missing", "Set a valid Scripts folder in Settings first.")
            return False

        self.log.clear()
        preview = " ".join([python_exe, os.path.basename(script_path)] + args)
        self._append("$ " + preview, "CMD")
        self.state_label.setText("Starting…")
        self.status_cb(f"Running: {self.title}")
        self.start_time = time.time()

        self.process = QProcess(self)
        self.process.setWorkingDirectory(cwd)
        self.process.setProgram(python_exe)
        # -u: unbuffered stdout/stderr. Without it, a script's prints sit in
        # a block buffer (stdout isn't a tty here) and don't reach this log
        # until the buffer fills or the process exits — which would also
        # hide proxy_download.py's login prompt right when it's needed.
        self.process.setArguments(["-u", script_path] + args)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(lambda e: self._append(f"Process error: {e}", "ERROR"))

        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # indeterminate until the script reports [i/N] progress
        self.progress.resetFormat()
        self.process.start()
        return True

    def _read_stdout(self):
        if self.process:
            self._handle_output(bytes(self.process.readAllStandardOutput()).decode(errors="replace"), "INFO")

    def _read_stderr(self):
        if self.process:
            self._handle_output(bytes(self.process.readAllStandardError()).decode(errors="replace"), "WARNING")

    def _handle_output(self, data: str, level: str):
        for line in data.splitlines():
            if not line.strip():
                continue
            self._append(line.rstrip(), level)
            if len(line) < 160:
                self.state_label.setText(line.strip())
            self._update_progress(line.strip())
            if any(marker in line for marker in STDIN_WAIT_MARKERS):
                self.continue_btn.setVisible(True)

    def _update_progress(self, line: str):
        match = PROGRESS_MARKER.match(line)
        if not match:
            return
        current, total = int(match.group(1)), int(match.group(2))
        if total <= 0 or current > total:
            return
        # Item i being *started* means i-1 of total are done; 100% is
        # reached only when the run actually finishes.
        self.progress.setRange(0, 100)
        self.progress.setValue((current - 1) * 100 // total)
        self.progress.setFormat(f"%p%  ({current} of {total})")

    def _send_continue(self):
        if self.process:
            self.process.write(b"\n")
            self._append("(sent Enter to the running process)", "CMD")
        self.continue_btn.setVisible(False)

    def _on_finished(self, exit_code: int, _status):
        elapsed = format_duration(time.time() - self.start_time)
        if exit_code == 0:
            self._append(f"Finished successfully in {elapsed}.", "SUCCESS")
            self.finished_ok.emit()
            self.status_cb("Ready")
        else:
            self._append(f"Exited with code {exit_code} after {elapsed}.", "ERROR")
            self.finished_error.emit()
            self.status_cb("Last run finished with errors")
        self.progress.setVisible(False)
        self.continue_btn.setVisible(False)
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.process = None

    def stop(self):
        if self.process:
            self._append("Stop requested.", "WARNING")
            self.process.kill()

    def _append(self, message: str, level: str = "INFO"):
        self.log.appendPlainText(message)
        self.log_console.append_log(message, level, self.title)


# ===========================================================================
# File viewer (PDF / image / Excel / CSV / text / Markdown)
# ===========================================================================

class DropArea(QFrame):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        label = QLabel("Drop a PDF, image, Excel, CSV, TXT, or Markdown file here")
        label.setAlignment(Qt.AlignCenter)
        label.setObjectName("Hint")
        layout.addWidget(label)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.files_dropped.emit(paths)


class ImageView(QWidget):
    def __init__(self, path: str):
        super().__init__()
        self.scale = 1.0
        self.pixmap = QPixmap(path)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        zo, zi, ft = QPushButton("-"), QPushButton("+"), QPushButton("Fit")
        for b in (zo, zi, ft):
            b.setFixedWidth(44)
        bar.addWidget(zo); bar.addWidget(zi); bar.addWidget(ft); bar.addStretch()
        layout.addLayout(bar)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.label)
        layout.addWidget(self.scroll, 1)
        zi.clicked.connect(lambda: self._zoom(1.2))
        zo.clicked.connect(lambda: self._zoom(1 / 1.2))
        ft.clicked.connect(self._fit)
        self._fit()

    def _zoom(self, factor):
        self.scale = max(0.1, min(8.0, self.scale * factor))
        self._render()

    def _fit(self):
        if self.pixmap.isNull():
            self.label.setText("Could not load image.")
            return
        area = self.scroll.viewport().size()
        self.scale = min(area.width() / max(self.pixmap.width(), 1), area.height() / max(self.pixmap.height(), 1), 1.0)
        self._render()

    def _render(self):
        if not self.pixmap.isNull():
            self.label.setPixmap(self.pixmap.scaled(self.pixmap.size() * self.scale, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class FileViewer(QWidget):
    recent_changed = Signal(list)

    def __init__(self, settings: QSettings, log_console: LogConsole):
        super().__init__()
        self.settings = settings
        self.log_console = log_console
        self.recent_files = self.settings.value("recent_files", [], type=list) or []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        self.open_btn = QPushButton("Open file")
        self.open_btn.setObjectName("Primary")
        self.open_btn.clicked.connect(self.open_dialog)
        toolbar.addWidget(self.open_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.tabs.removeTab)
        layout.addWidget(self.tabs, 1)

        self.drop_area = DropArea()
        self.drop_area.files_dropped.connect(self.open_files)
        layout.addWidget(self.drop_area)

    def open_dialog(self):
        filt = "Supported files (*.pdf *.png *.jpg *.jpeg *.xlsx *.xls *.csv *.txt *.md);;All files (*.*)"
        paths, _ = QFileDialog.getOpenFileNames(self, "Open files", str(Path.home()), filt)
        self.open_files(paths)

    def open_files(self, paths: list[str]):
        for p in paths:
            self.open_file(p)

    def open_file(self, path: str):
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "File not found", path)
            return
        suffix = p.suffix.lower()
        if suffix not in SUPPORTED_VIEWER_EXTENSIONS:
            QMessageBox.information(self, "Unsupported file", f"Not supported here: {suffix}")
            return
        try:
            widget = self._create_widget(p)
            self.tabs.addTab(widget, p.name)
            self.tabs.setCurrentWidget(widget)
            self._add_recent(str(p))
            self.log_console.append_log(f"Opened {p}", "SUCCESS", "Viewer")
        except Exception as exc:
            self.log_console.append_log(f"Could not open {p}: {exc}", "ERROR", "Viewer")
            QMessageBox.warning(self, "Open failed", f"{p}\n\n{exc}")

    def _create_widget(self, path: Path) -> QWidget:
        suffix = path.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg"}:
            return ImageView(str(path))
        if suffix == ".pdf":
            return self._pdf_widget(path)
        if suffix in {".xlsx", ".xls"}:
            return self._excel_widget(path)
        if suffix == ".csv":
            return self._csv_widget(path)
        if suffix in {".txt", ".md"}:
            return self._text_widget(path, markdown=(suffix == ".md"))
        raise ValueError("Unsupported format")

    def _pdf_widget(self, path: Path) -> QWidget:
        if not HAS_QPDF:
            return self._message(f"PDF preview needs the QtPdf module (part of PySide6). File: {path}")
        container = QWidget()
        layout = QVBoxLayout(container)
        doc = QPdfDocument(container)
        if doc.load(str(path)) != QPdfDocument.Status.Ready:
            raise ValueError("PDF could not be loaded")
        view = QPdfView(container)
        view.setDocument(doc)
        view.setPageMode(QPdfView.PageMode.MultiPage)
        view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        layout.addWidget(view)
        container._pdf_doc = doc
        return container

    def _excel_widget(self, path: Path) -> QWidget:
        if pd is None:
            return self._message("Install pandas + openpyxl to preview Excel files.")
        engine = "openpyxl" if path.suffix.lower() == ".xlsx" else "xlrd"
        df = pd.read_excel(path, engine=engine)
        return self._table(df.astype(str).values.tolist(), [str(c) for c in df.columns])

    def _csv_widget(self, path: Path) -> QWidget:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
            rows = list(csv.reader(f, dialect))
        headers = rows[0] if rows else []
        return self._table(rows[1:], headers)

    def _table(self, data, headers) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(max(len(headers), max((len(r) for r in data), default=0)))
        table.setRowCount(len(data))
        if headers:
            table.setHorizontalHeaderLabels(headers)
        for r, row in enumerate(data):
            for c, value in enumerate(row):
                table.setItem(r, c, QTableWidgetItem(str(value)))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSortingEnabled(True)
        return table

    def _text_widget(self, path: Path, markdown=False) -> QTextEdit:
        text = path.read_text(encoding="utf-8", errors="replace")
        editor = QTextEdit(readOnly=True)
        if markdown:
            editor.setMarkdown(text)
        else:
            editor.setPlainText(text)
        return editor

    def _message(self, text: str) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setAlignment(Qt.AlignCenter)
        lab.setObjectName("Hint")
        l.addWidget(lab)
        return w

    def _add_recent(self, path: str):
        path = str(Path(path))
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[:15]
        self.settings.setValue("recent_files", self.recent_files)
        self.recent_changed.emit(self.recent_files)

    def remove_recent(self, path: str):
        clean = str(Path(path))
        self.recent_files = [p for p in self.recent_files if p != clean]
        self.settings.setValue("recent_files", self.recent_files)
        self.recent_changed.emit(self.recent_files)

    def clear_recent(self):
        self.recent_files = []
        self.settings.setValue("recent_files", self.recent_files)
        self.recent_changed.emit(self.recent_files)


class RecentFilesPanel(QWidget):
    def __init__(self, open_cb, remove_cb, clear_cb):
        super().__init__()
        self.open_cb, self.remove_cb, self.clear_cb = open_cb, remove_cb, clear_cb
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.list_widget, 1)
        row = QHBoxLayout()
        open_btn, remove_btn, clear_btn = QPushButton("Open"), QPushButton("Remove"), QPushButton("Clear")
        open_btn.setObjectName("Primary")
        open_btn.clicked.connect(self._open_selected)
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn.clicked.connect(self.clear_cb)
        row.addWidget(open_btn); row.addWidget(remove_btn); row.addWidget(clear_btn)
        layout.addLayout(row)

    def set_files(self, files: list[str]):
        self.list_widget.clear()
        for path in files:
            item = QListWidgetItem(path if Path(path).exists() else f"{path}  [missing]")
            item.setData(Qt.UserRole, path)
            if not Path(path).exists():
                item.setForeground(QBrush(QColor("#A46A00")))
            self.list_widget.addItem(item)

    def _selected_path(self) -> str:
        item = self.list_widget.currentItem()
        return item.data(Qt.UserRole) if item else ""

    def _open_item(self, item):
        self.open_cb(item.data(Qt.UserRole))

    def _open_selected(self):
        path = self._selected_path()
        if path:
            self.open_cb(path)

    def _remove_selected(self):
        path = self._selected_path()
        if path:
            self.remove_cb(path)


# ===========================================================================
# Main window
# ===========================================================================

PAGES = ["Home", "Get Papers", "Search & Ask", "Export & Draft", "Finalize Manuscript", "Viewer", "Help", "Settings", "Logs"]
HELP_GUIDE_FILENAME = "HELP_GUIDE.md"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(ORG, APP)
        self.dark_mode = self.settings.value("dark_mode", False, type=bool)
        if not self.settings.value("scripts_dir", ""):
            # First run: default to the folder this app itself lives in —
            # that's where the user will normally have placed it.
            self.settings.setValue("scripts_dir", str(Path(__file__).resolve().parent))

        self.setWindowTitle("Manuscript Engine")
        self.resize(1300, 840)
        self.setMinimumSize(QSize(1040, 680))
        self._apply_theme()

        self.log_console = LogConsole()
        self.viewer = FileViewer(self.settings, self.log_console)
        self.home_recent = RecentFilesPanel(self.viewer.open_file, self.viewer.remove_recent, self.viewer.clear_recent)
        self.viewer_recent = RecentFilesPanel(self.viewer.open_file, self.viewer.remove_recent, self.viewer.clear_recent)
        self.viewer.recent_changed.connect(self._populate_recent)

        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(self._build_header())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.stack = QStackedWidget()
        splitter.addWidget(self._build_sidebar())

        self.stack.addWidget(self._build_home_page())
        self.stack.addWidget(self._build_get_papers_page())
        self.stack.addWidget(self._build_search_ask_page())
        self.stack.addWidget(self._build_export_page())
        self.stack.addWidget(self._build_finalize_page())
        self.stack.addWidget(self._build_viewer_page())
        self.stack.addWidget(self._build_help_page())
        self.stack.addWidget(self._build_settings_page())
        self.stack.addWidget(self._build_logs_page())
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 1100])
        root_layout.addWidget(splitter, 1)

        self._build_status_bar()
        self._build_shortcuts()
        self._populate_recent(self.viewer.recent_files)
        self._check_workspace(startup=True)
        self.log_console.append_log("Manuscript Engine ready.", "SUCCESS", "App")

    # ---------------- helpers ----------------

    def scripts_dir(self) -> str:
        return self.setting_value("scripts_dir")

    def script(self, name: str) -> str:
        return str(Path(self.scripts_dir()) / name)

    def config_path(self) -> Path:
        return Path(self.scripts_dir()) / CONFIG_FILENAME

    def setting_value(self, key: str) -> str:
        return str(self.settings.value(key, "") or "").strip()

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _apply_theme(self):
        QApplication.instance().setStyleSheet(DARK_QSS if self.dark_mode else LIGHT_QSS)

    def _build_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.viewer.open_dialog)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=lambda: self.sidebar.setCurrentRow(0))
        QShortcut(QKeySequence("Ctrl+L"), self, activated=lambda: self.sidebar.setCurrentRow(len(PAGES) - 1))
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self.toggle_dark_mode)

    def _std_icon(self, e):
        return self.style().standardIcon(e)

    def run_in_panel(self, panel: ProcessPanel, script_name: str, args: list[str]) -> None:
        panel.start(sys.executable, self.script(script_name), args, self.scripts_dir())

    def open_folder(self, path: str) -> None:
        if not path or not os.path.isdir(path):
            return
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            QProcess.startDetached("open", [path])
        else:
            QProcess.startDetached("xdg-open", [path])

    def toggle_dark_mode(self, value: Optional[bool] = None):
        self.dark_mode = (not self.dark_mode) if value is None else bool(value)
        self.settings.setValue("dark_mode", self.dark_mode)
        self._apply_theme()
        if hasattr(self, "theme_btn"):
            self.theme_btn.setText("Light mode" if self.dark_mode else "Dark mode")
        if hasattr(self, "dark_check"):
            self.dark_check.blockSignals(True)
            self.dark_check.setChecked(self.dark_mode)
            self.dark_check.blockSignals(False)

    def _check_workspace(self, startup=False) -> bool:
        sd = self.scripts_dir()
        ok = bool(sd) and Path(sd, "doi_resolver.py").exists()
        cfg_ok = self.config_path().exists()
        if hasattr(self, "workspace_status"):
            if ok and cfg_ok:
                self.workspace_status.setText("Scripts folder and config file found.")
                self.workspace_status.setObjectName("StatusOk")
            elif ok:
                self.workspace_status.setText(f"Scripts folder OK, but {CONFIG_FILENAME} was not found there.")
                self.workspace_status.setObjectName("StatusWarn")
            else:
                self.workspace_status.setText("doi_resolver.py was not found in the Scripts folder — set it in Settings.")
                self.workspace_status.setObjectName("StatusErr")
            self.workspace_status.style().unpolish(self.workspace_status)
            self.workspace_status.style().polish(self.workspace_status)
        if not ok and not startup:
            QMessageBox.warning(self, "Scripts folder", "That folder doesn't look like the pipeline folder (doi_resolver.py wasn't found there).")
        return ok

    # ---------------- header / sidebar / status bar ----------------

    def _build_header(self) -> QWidget:
        header = QFrame(objectName="Header")
        header.setFixedHeight(72)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(16)

        logo = QLabel("ME")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(42, 42)
        logo.setStyleSheet(f"border-radius: 12px; background: {ACCENT}; color: white; font-weight: 700; font-size: 12pt;")
        layout.addWidget(logo)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel("Manuscript Engine")
        title.setObjectName("AppTitle")
        subtitle = QLabel("Your paper pipeline, in one window")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box, 1)

        self.theme_btn = QPushButton("Light mode" if self.dark_mode else "Dark mode")
        self.theme_btn.setObjectName("Ghost")
        self.theme_btn.clicked.connect(self.toggle_dark_mode)
        layout.addWidget(self.theme_btn)
        return header

    def _build_sidebar(self) -> QWidget:
        container = QWidget()
        container.setMinimumWidth(190)
        container.setMaximumWidth(220)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(14, 14, 8, 14)
        self.sidebar = QListWidget(objectName="Sidebar")
        icons = [
            QStyle.SP_ComputerIcon, QStyle.SP_ArrowDown, QStyle.SP_FileDialogContentsView,
            QStyle.SP_FileIcon, QStyle.SP_DialogSaveButton, QStyle.SP_FileDialogDetailedView,
            QStyle.SP_DialogHelpButton, QStyle.SP_FileDialogInfoView, QStyle.SP_MessageBoxInformation,
        ]
        for label, icon in zip(PAGES, icons):
            self.sidebar.addItem(QListWidgetItem(self._std_icon(icon), label))
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        layout.addWidget(self.sidebar, 1)
        return container

    def _build_status_bar(self):
        status = QStatusBar()
        self.status_label = QLabel("Ready")
        status.addWidget(self.status_label, 1)
        self.setStatusBar(status)

    # ---------------- Home ----------------

    def _build_home_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        hero = Card(
            "Everything the pipeline can do, from one place",
            "Download and index papers, ask your library questions, export drafting material for Claude, "
            "and finalize a manuscript's references and figures — without touching a terminal.",
            hero=True,
        )
        row = QHBoxLayout()
        for text, page_index in (("Get papers", PAGES.index("Get Papers")), ("Search & Ask", PAGES.index("Search & Ask")),
                                  ("Open viewer", PAGES.index("Viewer")), ("Help", PAGES.index("Help"))):
            b = QPushButton(text)
            b.setObjectName("Primary" if text == "Get papers" else "")
            b.clicked.connect(lambda _=False, i=page_index: self.sidebar.setCurrentRow(i))
            row.addWidget(b)
        row.addStretch()
        hero.layout().addLayout(row)
        layout.addWidget(hero)

        self.workspace_status = QLabel("")
        ws_card = Card("Workspace", "The folder containing your pipeline scripts and paper_pipeline_config.yaml.")
        self.home_scripts_picker = PathPicker(self.scripts_dir())
        self.home_scripts_picker.changed.connect(self._on_scripts_dir_changed)
        ws_card.layout().addWidget(self.home_scripts_picker)
        ws_card.layout().addWidget(self.workspace_status)
        layout.addWidget(ws_card)

        tidy_card = Card("Tidy the workspace", "Moves generated files left in the workspace root by older runs "
                         "(catalog_*, research_pack_*, answer files) into their organized folders — "
                         "catalogs/, research_packs/, answers/. Nothing is deleted.")
        self.tidy_panel = ProcessPanel("Tidy Workspace", self.log_console, self.set_status)
        self.tidy_panel.run_btn.setText("Tidy now")
        self.tidy_panel.run_btn.clicked.connect(
            lambda: self.run_in_panel(self.tidy_panel, "tidy_workspace.py", ["--yes"]))
        tidy_card.layout().addWidget(self.tidy_panel)
        layout.addWidget(tidy_card)

        recent_card = Card("Recent files", "Files you've opened in the viewer.")
        recent_card.layout().addWidget(self.home_recent)
        layout.addWidget(recent_card)
        layout.addStretch(1)
        return page

    def _on_scripts_dir_changed(self, value: str):
        self.settings.setValue("scripts_dir", value)
        if hasattr(self, "settings_scripts_picker") and self.settings_scripts_picker.value() != value:
            self.settings_scripts_picker.set_value(value)
        if hasattr(self, "home_scripts_picker") and self.home_scripts_picker.value() != value:
            self.home_scripts_picker.set_value(value)
        self._check_workspace()

    # ---------------- Get Papers ----------------

    def _build_get_papers_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        stage1 = Card("Stage 1 — Resolve & download open-access papers", "Runs doi_resolver.py. Reads publication_data/*.xlsx, "
                       "fills in metadata, and downloads whatever is freely available. No login needed.")
        self.stage1_panel = ProcessPanel("Stage 1", self.log_console, self.set_status)
        stage1.layout().addWidget(self.stage1_panel)
        self.stage1_panel.run_btn.clicked.connect(lambda: self.run_in_panel(self.stage1_panel, "doi_resolver.py", []))
        layout.addWidget(stage1)

        stage2 = Card("Stage 2 — University proxy download", "Runs proxy_download.py. Opens a real browser window; "
                       "sign-in is automatic when possible. If it ever needs you to finish signing in by hand, "
                       "click Continue below once you have.")
        self.stage2_panel = ProcessPanel("Stage 2", self.log_console, self.set_status)
        stage2.layout().addWidget(self.stage2_panel)
        self.stage2_panel.run_btn.clicked.connect(lambda: self.run_in_panel(self.stage2_panel, "proxy_download.py", []))
        layout.addWidget(stage2)

        both = Card("Run both in sequence", "Runs download_papers.py — Stage 1 then Stage 2, one command.")
        self.both_panel = ProcessPanel("Stage 1 + 2", self.log_console, self.set_status)
        both.layout().addWidget(self.both_panel)
        self.both_panel.run_btn.setText("Run both")
        self.both_panel.run_btn.clicked.connect(lambda: self.run_in_panel(self.both_panel, "download_papers.py", []))
        layout.addWidget(both)

        importer = Card("Import PDFs you already have", "For papers downloaded outside this pipeline — copies them into "
                         "downloads/, renames them to match every other paper, and adds them to the tracking sheet. "
                         "Skips anything already in your library.")
        self.import_folder_picker = PathPicker("", is_dir=True, placeholder="Folder containing the PDFs to import")
        importer.layout().addWidget(self.import_folder_picker)
        self.import_panel = ProcessPanel("Import PDFs", self.log_console, self.set_status)
        self.import_panel.run_btn.setText("Import from this folder")
        self.import_panel.run_btn.clicked.connect(self.run_import_pdfs)
        importer.layout().addWidget(self.import_panel)
        layout.addWidget(importer)

        index_card = Card("Build / update the search index", "Runs build_index.py — extracts text, figures and tables "
                           "from every downloaded paper. Run this after any download batch.")
        self.rebuild_check = QCheckBox("Full rebuild (re-index everything, not just what's new)")
        index_card.layout().addWidget(self.rebuild_check)
        self.index_panel = ProcessPanel("Build Index", self.log_console, self.set_status)
        index_card.layout().addWidget(self.index_panel)
        self.index_panel.run_btn.clicked.connect(self.run_build_index)
        layout.addWidget(index_card)

        layout.addStretch(1)
        return page

    def run_import_pdfs(self):
        folder = self.import_folder_picker.value()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, "Choose a folder", "Pick the folder containing the PDFs to import first.")
            return
        count = len([f for f in os.listdir(folder) if f.lower().endswith(".pdf")])
        if count == 0:
            QMessageBox.information(self, "No PDFs found", f"No .pdf files found directly in:\n{folder}")
            return
        if QMessageBox.question(self, "Import PDFs", f"Import {count} PDF(s) from:\n{folder}\n\nProceed?") != QMessageBox.Yes:
            return
        self.run_in_panel(self.import_panel, "import_existing_pdfs.py", ["--folder", folder, "--yes"])

    def run_build_index(self):
        args = ["--rebuild"] if self.rebuild_check.isChecked() else []
        self.run_in_panel(self.index_panel, "build_index.py", args)

    # ---------------- Search & Ask ----------------

    def _build_search_ask_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        lookup = Card("Quick lookup (instant, no AI)", "Search passages, find figures by description, or find figures "
                      "that look like an image you have.")
        form = QFormLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("e.g. moisture stability of tin perovskites")
        search_btn = QPushButton("Search")
        search_btn.setObjectName("Primary")
        form.addRow("Search:", self._row(self.search_edit, search_btn))

        self.figure_edit = QLineEdit()
        self.figure_edit.setPlaceholderText("e.g. cross-section of device stack")
        figure_btn = QPushButton("Find figure")
        form.addRow("Find figure:", self._row(self.figure_edit, figure_btn))

        self.match_picker = PathPicker(is_dir=False, filter_str="Images (*.png *.jpg *.jpeg)")
        match_btn = QPushButton("Match figure")
        form.addRow("Match image:", self._row(self.match_picker, match_btn))

        self.lookup_top = QSpinBox(); self.lookup_top.setRange(1, 50); self.lookup_top.setValue(5)
        form.addRow("Results to show:", self.lookup_top)
        lookup_filter_widget, self.lookup_filters = self._make_filter_fields()
        form.addRow("Limit to papers:", lookup_filter_widget)
        lookup.layout().addLayout(form)

        self.lookup_panel = ProcessPanel("Quick Lookup", self.log_console, self.set_status)
        self.lookup_panel.run_btn.hide()
        lookup.layout().addWidget(self.lookup_panel)
        search_btn.clicked.connect(lambda: self._run_lookup(["--search", self.search_edit.text()]))
        figure_btn.clicked.connect(lambda: self._run_lookup(["--find-figure", self.figure_edit.text()]))
        match_btn.clicked.connect(lambda: self._run_lookup(["--match-figure", self.match_picker.value()]))
        layout.addWidget(lookup)

        ask = Card("Ask your library", "Runs ask_library.py. --pack is recommended: it finishes in seconds and produces "
                   "a file to upload to Claude for a properly written, cited answer — no local AI wait.")
        self.question_edit = QPlainTextEdit()
        self.question_edit.setMaximumHeight(90)
        self.question_edit.setPlaceholderText("Type your question, or a whole paragraph...")
        ask.layout().addWidget(self.question_edit)
        opt_row = QHBoxLayout()
        self.pack_check = QCheckBox("--pack (build a file for Claude — recommended)")
        self.pack_check.setChecked(True)
        self.noai_check = QCheckBox("--no-ai (verbatim passages only)")
        opt_row.addWidget(self.pack_check)
        opt_row.addWidget(self.noai_check)
        opt_row.addStretch()
        ask.layout().addLayout(opt_row)
        ask_out_form = QFormLayout()
        ask_filter_widget, self.ask_filters = self._make_filter_fields()
        ask_out_form.addRow("Limit to papers:", ask_filter_widget)
        self.ask_out = OutputChooser("File name, e.g. tin_stability.md (blank = automatic)")
        ask_out_form.addRow("Save result to:", self.ask_out)
        ask.layout().addLayout(ask_out_form)
        self.ask_panel = ProcessPanel("Ask Library", self.log_console, self.set_status)
        self.ask_panel.run_btn.setText("Ask")
        self.ask_panel.run_btn.clicked.connect(self.run_ask)
        ask.layout().addWidget(self.ask_panel)
        layout.addWidget(ask)

        layout.addStretch(1)
        return page

    def _row(self, widget, button) -> QWidget:
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.addWidget(widget, 1)
        l.addWidget(button)
        return w

    def _make_filter_fields(self) -> tuple[QWidget, dict]:
        """A compact 'limit to papers' block (journal / years / category /
        entry numbers, all optional) shared by the Ask and Research-pack
        cards. Blank fields mean no restriction — exactly the old behavior."""
        fields = {
            "journal": QLineEdit(), "years": QLineEdit(),
            "category": QLineEdit(), "entries": QLineEdit(),
        }
        fields["journal"].setPlaceholderText("Journal contains... e.g. nature energy,joule")
        fields["years"].setPlaceholderText("Years, e.g. 2020,2023-2025")
        fields["category"].setPlaceholderText("Category, e.g. solar-cell,LED")
        fields["entries"].setPlaceholderText("Entry numbers, e.g. 12,45,100-110")
        for edit in fields.values():
            edit.setClearButtonEnabled(True)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        grid.addWidget(fields["journal"], 0, 0)
        grid.addWidget(fields["years"], 0, 1)
        grid.addWidget(fields["category"], 1, 0)
        grid.addWidget(fields["entries"], 1, 1)
        w = QWidget()
        w.setLayout(grid)
        return w, fields

    @staticmethod
    def _filter_args(fields: dict) -> list[str]:
        args = []
        for key, flag in (("journal", "--journal"), ("years", "--years"),
                          ("category", "--category"), ("entries", "--entries")):
            value = fields[key].text().strip()
            if value:
                args += [flag, value]
        return args

    def _run_lookup(self, extra: list[str]):
        if len(extra) >= 2 and not str(extra[1]).strip():
            QMessageBox.warning(self, "Missing value", "Type something to search for first.")
            return
        args = extra + ["--top", str(self.lookup_top.value())] + self._filter_args(self.lookup_filters)
        self.run_in_panel(self.lookup_panel, "build_index.py", args)

    def run_ask(self):
        q = self.question_edit.toPlainText().strip()
        if not q:
            QMessageBox.warning(self, "No question", "Type a question first.")
            return
        args = []
        if self.pack_check.isChecked():
            args.append("--pack")
        if self.noai_check.isChecked():
            args.append("--no-ai")
        args += self._filter_args(self.ask_filters)
        args += self.ask_out.out_arg()
        # "--" ends option parsing, so a question that happens to start
        # with a dash ("-70C stability?") can't be mistaken for a flag.
        args += ["--", q]
        self.run_in_panel(self.ask_panel, "ask_library.py", args)

    # ---------------- Export & Draft ----------------

    def _build_export_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        catalog = Card("Export a library catalog", "Runs export_catalog.py — a filterable overview of your library to "
                       "upload to Claude when planning an outline.")
        form = QFormLayout()
        self.cat_category = QLineEdit()
        self.cat_category.setPlaceholderText("e.g. solar-cell,LED (blank = all)")
        form.addRow("Category:", self.cat_category)
        self.cat_journal = QLineEdit()
        self.cat_journal.setPlaceholderText("journal name contains, e.g. nature energy,joule (blank = all)")
        self.cat_journal.setClearButtonEnabled(True)
        form.addRow("Journal:", self.cat_journal)
        self.cat_entries = QLineEdit()
        self.cat_entries.setPlaceholderText("e.g. 12,45,100-110 (blank = all)")
        self.cat_entries.setClearButtonEnabled(True)
        form.addRow("Entries:", self.cat_entries)
        yr_row = QHBoxLayout()
        self.cat_since = QSpinBox(); self.cat_since.setRange(0, 2100); self.cat_since.setValue(0)
        self.cat_since.setSpecialValueText("any")
        self.cat_until = QSpinBox(); self.cat_until.setRange(0, 2100); self.cat_until.setValue(0)
        self.cat_until.setSpecialValueText("any")
        yr_row.addWidget(QLabel("from")); yr_row.addWidget(self.cat_since)
        yr_row.addWidget(QLabel("to")); yr_row.addWidget(self.cat_until)
        yr_row.addStretch()
        form.addRow("Years:", self._wrap(yr_row))
        self.cat_full = QCheckBox("Include abstracts + figure/table captions (--full)")
        form.addRow("", self.cat_full)
        self.cat_out = OutputChooser("File name, e.g. catalog_solarcell.md (blank = automatic)")
        form.addRow("Save to:", self.cat_out)
        catalog.layout().addLayout(form)
        self.catalog_panel = ProcessPanel("Export Catalog", self.log_console, self.set_status)
        self.catalog_panel.run_btn.setText("Export catalog")
        self.catalog_panel.run_btn.clicked.connect(self.run_export_catalog)
        catalog.layout().addWidget(self.catalog_panel)
        layout.addWidget(catalog)

        draft = Card("Build a research pack for an outline", "Runs export_for_claude.py — retrieves evidence for each "
                     "section of your outline (.txt, .md, or Word .docx), ready to upload to Claude for drafting. "
                     "The filters restrict which papers the pack may draw from — e.g. only a given journal or year range.")
        oform = QFormLayout()
        self.outline_picker = PathPicker(is_dir=False, filter_str="Outline files (*.txt *.md *.docx);;All files (*.*)")
        oform.addRow("Outline file:", self.outline_picker)
        self.per_section = QSpinBox(); self.per_section.setRange(0, 500); self.per_section.setValue(20)
        self.per_section.setSpecialValueText("default (15)")
        oform.addRow("Passages per section:", self.per_section)
        self.style_picker = PathPicker(is_dir=False, filter_str="Text files (*.txt)")
        oform.addRow("Style rules file:", self.style_picker)
        pack_filter_widget, self.pack_filters = self._make_filter_fields()
        oform.addRow("Limit to papers:", pack_filter_widget)
        self.pack_out = OutputChooser("File name, e.g. pack_section5.md (blank = automatic)")
        oform.addRow("Save to:", self.pack_out)
        draft.layout().addLayout(oform)
        self.draft_panel = ProcessPanel("Research Pack", self.log_console, self.set_status)
        self.draft_panel.run_btn.setText("Build research pack")
        self.draft_panel.run_btn.clicked.connect(self.run_export_for_claude)
        draft.layout().addWidget(self.draft_panel)
        edit_style_btn = QPushButton("Edit style_rules.txt")
        edit_style_btn.clicked.connect(lambda: self.sidebar.setCurrentRow(PAGES.index("Settings")))
        draft.layout().addWidget(edit_style_btn)
        layout.addWidget(draft)

        layout.addStretch(1)
        return page

    def _wrap(self, layout: QHBoxLayout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def run_export_catalog(self):
        args = []
        if self.cat_full.isChecked():
            args.append("--full")
        if self.cat_category.text().strip():
            args += ["--category", self.cat_category.text().strip()]
        if self.cat_journal.text().strip():
            args += ["--journal", self.cat_journal.text().strip()]
        if self.cat_entries.text().strip():
            args += ["--entries", self.cat_entries.text().strip()]
        if self.cat_since.value():
            args += ["--since", str(self.cat_since.value())]
        if self.cat_until.value():
            args += ["--until", str(self.cat_until.value())]
        args += self.cat_out.out_arg()
        self.run_in_panel(self.catalog_panel, "export_catalog.py", args)

    def run_export_for_claude(self):
        outline = self.outline_picker.value()
        if not outline:
            QMessageBox.warning(self, "Choose an outline", "Pick your outline .txt file first.")
            return
        args = [outline]
        if self.per_section.value():
            args += ["--per-section", str(self.per_section.value())]
        if self.style_picker.value():
            args += ["--style", self.style_picker.value()]
        args += self._filter_args(self.pack_filters)
        args += self.pack_out.out_arg()
        self.run_in_panel(self.draft_panel, "export_for_claude.py", args)

    # ---------------- Finalize Manuscript ----------------

    def _build_finalize_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        intro = Card("Finalize a manuscript", "Once a draft is final, pick the .docx here and pull out exactly what it cites — "
                     "papers, figure/table sources, and a citation-manager-ready library.")
        self.manuscript_picker = PathPicker(is_dir=False, filter_str="Documents (*.docx *.md *.txt)")
        intro.layout().addWidget(self.manuscript_picker)
        fin_form = QFormLayout()
        self.finalize_out = PathPicker("", is_dir=True,
                                       placeholder="Blank = finalized/<manuscript name>/ next to the scripts")
        fin_form.addRow("Output folder:", self.finalize_out)
        intro.layout().addLayout(fin_form)
        fin_hint = QLabel("Each tool below writes into its own subfolder there (cited_references, "
                          "cited_figures_tables, citation library), so nothing gets mixed together.")
        fin_hint.setObjectName("Hint")
        fin_hint.setWordWrap(True)
        intro.layout().addWidget(fin_hint)
        layout.addWidget(intro)

        refs = Card("1 — Cited papers", "Runs extract_cited_references.py — copies every cited [Entry N] PDF into its own folder.")
        self.refs_panel = ProcessPanel("Cited References", self.log_console, self.set_status)
        self.refs_panel.run_btn.setText("Extract cited papers")
        self.refs_panel.run_btn.clicked.connect(lambda: self._run_finalize_tool("extract_cited_references.py", self.refs_panel))
        refs.layout().addWidget(self.refs_panel)
        layout.addWidget(refs)

        figs = Card("2 — Cited figures & tables", "Runs extract_cited_figures.py — pulls the source image/page for every "
                    "figure or table placeholder box.")
        self.figs_panel = ProcessPanel("Cited Figures", self.log_console, self.set_status)
        self.figs_panel.run_btn.setText("Extract cited figures/tables")
        self.figs_panel.run_btn.clicked.connect(lambda: self._run_finalize_tool("extract_cited_figures.py", self.figs_panel))
        figs.layout().addWidget(self.figs_panel)
        layout.addWidget(figs)

        cite = Card("3 — Citation library (.ris)", "Runs export_citation_library.py — exports every cited paper as a .ris "
                    "file to import into EndNote or Zotero.")
        self.cite_panel = ProcessPanel("Citation Library", self.log_console, self.set_status)
        self.cite_panel.run_btn.setText("Export citation library")
        self.cite_panel.run_btn.clicked.connect(lambda: self._run_finalize_tool("export_citation_library.py", self.cite_panel))
        cite.layout().addWidget(self.cite_panel)
        layout.addWidget(cite)

        layout.addStretch(1)
        return page

    # Subfolder (or, for the .ris export, filename prefix) each finalize
    # tool gets inside the chosen output folder, so their outputs never mix.
    FINALIZE_SUBDIRS = {
        "extract_cited_references.py": "cited_references",
        "extract_cited_figures.py": "cited_figures_tables",
        "export_citation_library.py": "cited_papers",
    }

    def _run_finalize_tool(self, script_name: str, panel: ProcessPanel):
        manuscript = self.manuscript_picker.value()
        if not manuscript:
            QMessageBox.warning(self, "Choose a manuscript", "Pick your manuscript file first.")
            return
        args = [manuscript]
        base = self.finalize_out.value()
        if base:
            args += ["--out", str(Path(base) / self.FINALIZE_SUBDIRS[script_name])]
        self.run_in_panel(panel, script_name, args)

    # ---------------- Viewer ----------------

    def _build_viewer_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()
        card = Card("Integrated file viewer", "Open PDFs, images, spreadsheets, CSV, TXT and Markdown files. Drag and drop is supported.")
        split = QSplitter(Qt.Horizontal)
        recent_box = QWidget()
        rl = QVBoxLayout(recent_box)
        rl.setContentsMargins(0, 0, 10, 0)
        t = QLabel("Recent files")
        t.setObjectName("SectionTitle")
        rl.addWidget(t)
        rl.addWidget(self.viewer_recent, 1)
        split.addWidget(recent_box)
        split.addWidget(self.viewer)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 820])
        card.layout().addWidget(split, 1)
        layout.addWidget(card, 1)
        return page

    def _populate_recent(self, files: list[str]):
        if hasattr(self, "home_recent"):
            self.home_recent.set_files(files)
        if hasattr(self, "viewer_recent"):
            self.viewer_recent.set_files(files)

    # ---------------- Help ----------------

    def _build_help_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        card = Card("Help — every step, basic to advanced", "The same guide as the terminal command for each "
                     "app action, so nothing here needs the app to work. This is also a plain text file, "
                     f"{HELP_GUIDE_FILENAME}, in your pipeline folder — open it in Notepad any time.")

        self.help_browser = QTextBrowser()
        self.help_browser.setOpenExternalLinks(True)
        self.help_browser.setMinimumHeight(420)
        card.layout().addWidget(self.help_browser, 1)

        btn_row = QHBoxLayout()
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._load_help_guide)
        open_file_btn = QPushButton(f"Open {HELP_GUIDE_FILENAME} in the file viewer")
        open_file_btn.clicked.connect(self._open_help_guide_in_viewer)
        btn_row.addWidget(reload_btn)
        btn_row.addWidget(open_file_btn)
        btn_row.addStretch()
        card.layout().addLayout(btn_row)

        layout.addWidget(card, 1)
        self._load_help_guide()
        return page

    def _help_guide_path(self) -> Path:
        return Path(self.scripts_dir()) / HELP_GUIDE_FILENAME

    def _load_help_guide(self):
        path = self._help_guide_path()
        if not path.exists():
            self.help_browser.setPlainText(
                f"{HELP_GUIDE_FILENAME} was not found in:\n{path}\n\n"
                "Make sure it's in the same folder as the other pipeline scripts (see Settings "
                "→ Scripts folder)."
            )
            return
        try:
            self.help_browser.setMarkdown(path.read_text(encoding="utf-8"))
        except Exception as e:
            self.help_browser.setPlainText(f"Could not read {path}: {e}")

    def _open_help_guide_in_viewer(self):
        path = self._help_guide_path()
        if not path.exists():
            QMessageBox.warning(self, "Not found", f"{HELP_GUIDE_FILENAME} was not found in:\n{path}")
            return
        self.sidebar.setCurrentRow(PAGES.index("Viewer"))
        self.viewer.open_file(str(path))

    # ---------------- Settings ----------------

    def _build_settings_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()

        ws_card = Card("Workspace", "Where your pipeline scripts and config file live.")
        self.settings_scripts_picker = PathPicker(self.scripts_dir())
        self.settings_scripts_picker.changed.connect(self._on_scripts_dir_changed)
        ws_card.layout().addWidget(self.settings_scripts_picker)
        dark_check = QCheckBox("Use dark mode")
        dark_check.setChecked(self.dark_mode)
        dark_check.stateChanged.connect(lambda _: self.toggle_dark_mode(dark_check.isChecked()))
        self.dark_check = dark_check
        ws_card.layout().addWidget(dark_check)
        shortcuts = QLabel("Shortcuts: Ctrl+O open file · Ctrl+H home · Ctrl+L logs · Ctrl+D dark mode")
        shortcuts.setObjectName("Hint")
        ws_card.layout().addWidget(shortcuts)
        layout.addWidget(ws_card)

        tabs = QTabWidget()
        tabs.addTab(self._build_config_essentials_tab(), "Pipeline settings")
        tabs.addTab(self._build_config_advanced_tab(), "Advanced")
        tabs.addTab(self._build_credentials_tab(), "Login & API keys")
        tabs.addTab(self._build_style_rules_tab(), "Writing style rules")
        config_card = Card("Configuration", f"Reads and writes {CONFIG_FILENAME} in your scripts folder, keeping its comments intact.")
        config_card.layout().addWidget(tabs)
        layout.addWidget(config_card, 1)

        if not HAS_RUAMEL:
            warn = QLabel("The 'ruamel.yaml' package is not installed, so the config editor below is disabled. "
                           "Install it with:  pip install ruamel.yaml")
            warn.setObjectName("StatusWarn")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        return page

    def _config_field(self, form: QFormLayout, label: str, dotted_key: str, kind="text", **kw):
        data = self._config_data
        value = get_nested(data, dotted_key, kw.get("default", ""))
        if kind == "text":
            widget = QLineEdit(str(value) if value != "" else "")
            widget.setPlaceholderText(kw.get("placeholder", ""))
        elif kind == "int":
            widget = QSpinBox()
            widget.setRange(kw.get("min", 0), kw.get("max", 100000))
            widget.setSpecialValueText(kw.get("special", ""))
            try:
                widget.setValue(int(value))
            except (TypeError, ValueError):
                widget.setValue(kw.get("min", 0))
        elif kind == "float":
            widget = QDoubleSpinBox()
            widget.setRange(kw.get("min", 0.0), kw.get("max", 100.0))
            widget.setSingleStep(kw.get("step", 0.1))
            try:
                widget.setValue(float(value))
            except (TypeError, ValueError):
                widget.setValue(kw.get("min", 0.0))
        elif kind == "path":
            widget = PathPicker(str(value), is_dir=kw.get("is_dir", True))
        else:
            widget = QLineEdit(str(value))
        form.addRow(label, widget)
        self._config_widgets[dotted_key] = (kind, widget)
        return widget

    def _build_config_essentials_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self._config_widgets = {}
        self._config_data = load_yaml_roundtrip(self.config_path()) or {}

        form = QFormLayout()
        self._config_field(form, "Unpaywall e-mail:", "unpaywall_email", "text", placeholder="you@example.com")
        self._config_field(form, "Excel input folder:", "paths.excel_input_dir", "text", placeholder="publication_data")
        self._config_field(form, "Downloads folder:", "paths.downloads_dir", "text", placeholder="downloads")
        self._config_field(form, "Tracking sheet:", "paths.tracking_csv", "text", placeholder="download_tracking.csv")
        self._config_field(form, "Proxy login ID:", "login.username", "text", placeholder="your university login")
        self._config_field(form, "LibKey library ID:", "libkey.library_id", "text")
        self._config_field(form, "Hostname-mangling suffix:", "proxy.hostname_mangling_suffix", "text",
                            placeholder=".bib-proxy.youruniversity.edu")
        self._config_field(form, "Stage 1 papers per run (0 = unlimited):", "limits.stage1_papers_per_run", "int",
                            min=0, max=100000, special="unlimited")
        self._config_field(form, "Stage 2 papers per run (0 = unlimited):", "limits.stage2_papers_per_run", "int",
                            min=0, max=100000, special="unlimited")
        layout.addLayout(form)

        save_btn = QPushButton("Save settings")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self.save_config_essentials)
        self.config_status = QLabel("")
        row = QHBoxLayout()
        row.addWidget(save_btn)
        row.addWidget(self.config_status, 1)
        layout.addLayout(row)
        layout.addStretch(1)
        save_btn.setEnabled(HAS_RUAMEL)
        return widget

    def _build_config_advanced_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self._config_field(form, "Request delay (seconds):", "network.request_delay_seconds", "float", min=0.0, max=30.0, step=0.1)
        self._config_field(form, "Request timeout (seconds):", "network.timeout_seconds", "int", min=1, max=600)
        self._config_field(form, "Proxy login page URL:", "login.proxy_login_url", "text")
        self._config_field(form, "EZproxy URL prefix:", "proxy.url_prefix", "text")
        self._config_field(form, "Download-icon template image:", "download_icon.template_image", "path", is_dir=False)
        self._config_field(form, "Icon match threshold:", "download_icon.match_threshold", "float", min=0.0, max=1.0, step=0.05)
        self._config_field(form, "Ollama URL:", "assistant.ollama_url", "text")
        self._config_field(form, "Ollama model:", "assistant.ollama_model", "text")
        self._config_field(form, "llama-cpp GGUF repo:", "assistant.gguf_repo", "text")
        self._config_field(form, "llama-cpp GGUF file pattern:", "assistant.gguf_file", "text")
        self._config_field(form, "Gemini model:", "assistant.gemini_model", "text")
        layout.addLayout(form)

        save_btn = QPushButton("Save advanced settings")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self.save_config_essentials)
        save_btn.setEnabled(HAS_RUAMEL)
        layout.addWidget(save_btn)
        layout.addStretch(1)
        return widget

    def save_config_essentials(self):
        if not HAS_RUAMEL:
            return
        data = self._config_data
        for dotted_key, (kind, widget) in self._config_widgets.items():
            if kind == "text":
                set_nested(data, dotted_key, widget.text().strip())
            elif kind == "int":
                set_nested(data, dotted_key, widget.value())
            elif kind == "float":
                set_nested(data, dotted_key, widget.value())
            elif kind == "path":
                set_nested(data, dotted_key, widget.value())
        ok = save_yaml_roundtrip(self.config_path(), data)
        self.config_status.setText("Saved." if ok else "Could not save — check the scripts folder is writable.")
        self.config_status.setObjectName("StatusOk" if ok else "StatusErr")
        self.config_status.style().unpolish(self.config_status)
        self.config_status.style().polish(self.config_status)
        self.log_console.append_log(
            f"Config {'saved' if ok else 'save failed'}: {self.config_path()}", "SUCCESS" if ok else "ERROR", "Settings"
        )

    def _build_credentials_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        cred_group = QGroupBox("Proxy login (login_credentials.txt)")
        cform = QFormLayout(cred_group)
        self.cred_id = QLineEdit()
        self.cred_pw = QLineEdit()
        self.cred_pw.setEchoMode(QLineEdit.Password)
        show_pw = QCheckBox("Show password")
        show_pw.stateChanged.connect(lambda s: self.cred_pw.setEchoMode(QLineEdit.Normal if s else QLineEdit.Password))
        cform.addRow("University login ID:", self.cred_id)
        cform.addRow("Password:", self.cred_pw)
        cform.addRow("", show_pw)
        cred_note = QLabel("Saved only to login_credentials.txt in your scripts folder — never sent anywhere, "
                            "and excluded from git by .gitignore.")
        cred_note.setObjectName("Hint")
        cred_note.setWordWrap(True)
        cform.addRow(cred_note)
        cred_save = QPushButton("Save credentials")
        cred_save.setObjectName("Primary")
        cred_save.clicked.connect(self.save_credentials)
        cform.addRow(cred_save)
        layout.addWidget(cred_group)

        gemini_group = QGroupBox("Gemini API key (optional, gemini_api_key.txt)")
        gform = QFormLayout(gemini_group)
        self.gemini_key_edit = QLineEdit()
        self.gemini_key_edit.setEchoMode(QLineEdit.Password)
        self.gemini_key_edit.setPlaceholderText("AIza...")
        gform.addRow("API key:", self.gemini_key_edit)
        gemini_note = QLabel("Get a free key at aistudio.google.com. When set, ask_library.py answers instantly via "
                              "Gemini instead of the slower local model. Delete the file to go back to fully local.")
        gemini_note.setObjectName("Hint")
        gemini_note.setWordWrap(True)
        gform.addRow(gemini_note)
        gemini_save = QPushButton("Save API key")
        gemini_save.setObjectName("Primary")
        gemini_save.clicked.connect(self.save_gemini_key)
        gform.addRow(gemini_save)
        layout.addWidget(gemini_group)

        self.credentials_status = QLabel("")
        layout.addWidget(self.credentials_status)
        layout.addStretch(1)
        self._load_credentials_into_form()
        return widget

    def _load_credentials_into_form(self):
        cred_path = Path(self.scripts_dir()) / CREDENTIALS_FILENAME
        if cred_path.exists():
            try:
                text = cred_path.read_text(encoding="utf-8")
                for line in text.splitlines():
                    key, sep, value = line.partition(":")
                    if not sep:
                        continue
                    key, value = key.strip().lower(), value.strip()
                    if key in ("id", "username", "user", "login"):
                        self.cred_id.setText(value)
                    elif key in ("pw", "password", "pass"):
                        self.cred_pw.setText(value)
            except OSError:
                pass
        gemini_path = Path(self.scripts_dir()) / GEMINI_KEY_FILENAME
        if gemini_path.exists():
            try:
                self.gemini_key_edit.setText(gemini_path.read_text(encoding="utf-8").strip())
            except OSError:
                pass

    def save_credentials(self):
        cred_path = Path(self.scripts_dir()) / CREDENTIALS_FILENAME
        try:
            cred_path.write_text(f"ID: {self.cred_id.text().strip()}\nPW: {self.cred_pw.text()}\n", encoding="utf-8")
            self.credentials_status.setText(f"Saved to {cred_path}")
            self.credentials_status.setObjectName("StatusOk")
        except OSError as e:
            self.credentials_status.setText(f"Could not save: {e}")
            self.credentials_status.setObjectName("StatusErr")
        self.credentials_status.style().unpolish(self.credentials_status)
        self.credentials_status.style().polish(self.credentials_status)

    def save_gemini_key(self):
        gemini_path = Path(self.scripts_dir()) / GEMINI_KEY_FILENAME
        try:
            gemini_path.write_text(self.gemini_key_edit.text().strip() + "\n", encoding="utf-8")
            self.credentials_status.setText(f"Saved to {gemini_path}")
            self.credentials_status.setObjectName("StatusOk")
        except OSError as e:
            self.credentials_status.setText(f"Could not save: {e}")
            self.credentials_status.setObjectName("StatusErr")
        self.credentials_status.style().unpolish(self.credentials_status)
        self.credentials_status.style().polish(self.credentials_status)

    def _build_style_rules_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        note = QLabel("Edited by export_for_claude.py's --style flag (used by default when present). "
                       "Changes here shape every future research pack.")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.style_editor = QPlainTextEdit()
        self.style_editor.setMinimumHeight(320)
        layout.addWidget(self.style_editor, 1)
        row = QHBoxLayout()
        reload_btn = QPushButton("Reload from file")
        save_btn = QPushButton("Save style_rules.txt")
        save_btn.setObjectName("Primary")
        reload_btn.clicked.connect(self._load_style_rules)
        save_btn.clicked.connect(self._save_style_rules)
        row.addWidget(reload_btn)
        row.addWidget(save_btn)
        self.style_status = QLabel("")
        row.addWidget(self.style_status, 1)
        layout.addLayout(row)
        self._load_style_rules()
        return widget

    def _style_rules_path(self) -> Path:
        return Path(self.scripts_dir()) / STYLE_RULES_FILENAME

    def _load_style_rules(self):
        path = self._style_rules_path()
        if path.exists():
            try:
                self.style_editor.setPlainText(path.read_text(encoding="utf-8"))
                self.style_status.setText("")
            except OSError as e:
                self.style_status.setText(f"Could not read: {e}")
        else:
            self.style_editor.setPlainText("")
            self.style_status.setText(f"{STYLE_RULES_FILENAME} not found yet in the scripts folder — saving will create it.")

    def _save_style_rules(self):
        path = self._style_rules_path()
        try:
            path.write_text(self.style_editor.toPlainText(), encoding="utf-8")
            self.style_status.setText("Saved.")
            self.style_status.setObjectName("StatusOk")
        except OSError as e:
            self.style_status.setText(f"Could not save: {e}")
            self.style_status.setObjectName("StatusErr")
        self.style_status.style().unpolish(self.style_status)
        self.style_status.style().polish(self.style_status)

    # ---------------- Logs ----------------

    def _build_logs_page(self) -> QWidget:
        page = ScrollPage()
        layout = page.layout()
        card = Card("Activity log", "Every action across the app, with timestamps.")
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.log_console.clear)
        card.layout().addWidget(clear_btn, alignment=Qt.AlignRight)
        card.layout().addWidget(self.log_console, 1)
        layout.addWidget(card, 1)
        return page


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setOrganizationName(ORG)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
