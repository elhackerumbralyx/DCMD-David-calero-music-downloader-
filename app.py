# -*- coding: utf-8 -*-
# App: YouTube embebido + descarga con yt-dlp (MP3/MP4)
# Requisitos: pip install -U yt-dlp PySide6  (y FFmpeg en PATH)

import sys, os, re, webbrowser
from pathlib import Path
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThread, Signal, Slot, QUrl
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QLabel, QFileDialog, QProgressBar, QMessageBox, QSplitter
)
from PySide6.QtWebEngineWidgets import QWebEngineView

import yt_dlp

# ------------ Utilidades ------------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
def clean_err(msg: str) -> str:
    try: return ANSI_RE.sub("", msg)
    except: return str(msg)

def is_youtube_watch(url: str) -> bool:
    return ("youtube.com/watch" in url) or ("youtu.be/" in url)

# ------------ Modelo ------------
@dataclass
class VideoSel:
    url: str = ""
    title: str = ""

# ------------ Hilo de descarga ------------
class Downloader(QThread):
    progress = Signal(float, str)   # %
    done = Signal(str)              # ruta
    failed = Signal(str)

    def __init__(self, url: str, outdir: Path, kind: str, parent=None):
        super().__init__(parent)
        self.url = url
        self.outdir = Path(outdir)
        self.kind = kind            # "audio" | "video"
        self._stop = False

    def stop(self): self._stop = True

    def _hook(self, d):
        if self._stop:
            raise RuntimeError("Cancelado por el usuario")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            pct = (got/total*100.0) if total else 0.0
            txt = f"{pct:.1f}%"
            if d.get("speed"): txt += f" • {d['speed']/1024:.1f} KiB/s"
            if d.get("eta"):   txt += f" • ETA {d['eta']}s"
            self.progress.emit(pct, txt)
        elif d.get("status") == "finished":
            self.progress.emit(100.0, "Procesando…")

    def _common_opts(self, outtmpl: str):
        # Opciones robustas para evitar 403 y elegir buen formato
        return {
            "outtmpl": outtmpl,
            "quiet": True, "noprogress": True, "noplaylist": True,
            "retries": 10, "fragment_retries": 10, "concurrent_fragment_downloads": 1,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
            "http_headers": {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0 Safari/537.36"),
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
            "geo_bypass": True,
            "source_address": "0.0.0.0",   # fuerza IPv4
            "socket_timeout": 20,
            "overwrites": False,
            "progress_hooks": [self._hook],
            "postprocessors": [{"key": "FFmpegMetadata"}],
        }

    def run(self):
        try:
            self.outdir.mkdir(parents=True, exist_ok=True)
            template = str(self.outdir / ("%(title).200B [%(id)s].%(ext)s"))

            if self.kind == "audio":
                opts = self._common_opts(template)
                opts.update({
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                        {"key": "FFmpegMetadata"},
                    ],
                })
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self.url, download=True)
                    filename = ydl.prepare_filename(info)
                base = os.path.splitext(filename)[0]
                for cand in (base + ".mp3", base + ".m4a", base + ".webm"):
                    if os.path.exists(cand):
                        self.done.emit(cand); return
                self.done.emit(filename); return

            # Vídeo: probamos varios “format strings” para evitar el “Requested format…”
            format_candidates = [
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
                "bv[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
                "bv*+ba/b", "best"
            ]
            last = None
            for fmt in format_candidates:
                try:
                    opts = self._common_opts(template)
                    opts.update({"format": fmt, "merge_output_format": "mp4"})
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(self.url, download=True)
                        filename = ydl.prepare_filename(info)
                    base, ext = os.path.splitext(filename)
                    final_path = base + ".mp4" if os.path.exists(base + ".mp4") else filename
                    self.done.emit(final_path); return
                except Exception as ex:
                    last = ex
                    if "403" in str(ex) or "Requested format is not available" in str(ex):
                        continue
                    self.failed.emit(clean_err(str(ex))); return
            self.failed.emit(clean_err(str(last) if last else "No fue posible descargar el vídeo."))

        except Exception as ex:
            self.failed.emit(clean_err(str(ex)))

# ------------ Ventana principal ------------
class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube buscador embebido + Descargas (yt-dlp)")
        self.resize(1200, 720)

        self.sel = VideoSel()
        self.download_dir = Path.home() / "Desktop" / "Mi Música"
        self.dlt: Downloader | None = None

        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Top bar: búsqueda + botones carpeta/navegador
        top = QHBoxLayout()
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("Escribe artista o canción…")
        self.search_btn = QPushButton("Buscar", self)
        self.search_btn.clicked.connect(self.on_search)
        self.search_edit.returnPressed.connect(self.on_search)

        self.choose_btn = QPushButton("Carpeta…", self)
        self.choose_btn.clicked.connect(self.on_choose_dir)

        self.open_dir_btn = QPushButton("Abrir carpeta", self)
        self.open_dir_btn.clicked.connect(self.on_open_dir)

        self.open_in_browser_btn = QPushButton("Abrir en navegador", self)
        self.open_in_browser_btn.clicked.connect(lambda: webbrowser.open("https://www.youtube.com/"))

        top.addWidget(self.search_edit, 5)
        top.addWidget(self.search_btn, 1)
        top.addWidget(self.choose_btn, 1)
        top.addWidget(self.open_dir_btn, 1)
        top.addWidget(self.open_in_browser_btn, 1)

        # Split: WebView (izq) + Panel acciones (dcha)
        split = QSplitter(self)
        split.setOrientation(Qt.Horizontal)

        self.web = QWebEngineView(self)
        self.web.setUrl(QUrl("https://www.youtube.com/"))
        self.web.urlChanged.connect(self.on_url_changed)
        self.web.titleChanged.connect(self.on_title_changed)

        right = QWidget(self)
        right_l = QVBoxLayout(right)

        self.curr_url_lbl = QLabel("URL actual: (ninguna)", right)
        self.curr_title_lbl = QLabel("Título: (ninguno)", right)
        self.curr_url_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.curr_title_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.use_current_btn = QPushButton("Usar este vídeo", right)
        self.use_current_btn.clicked.connect(self.on_use_current)
        self.use_current_btn.setEnabled(False)

        self.open_curr_btn = QPushButton("Abrir URL actual en navegador", right)
        self.open_curr_btn.clicked.connect(self.on_open_current)

        self.dl_mp3_btn = QPushButton("Descargar Audio (MP3)", right)
        self.dl_mp3_btn.clicked.connect(lambda: self.on_download("audio"))
        self.dl_mp4_btn = QPushButton("Descargar Vídeo (MP4)", right)
        self.dl_mp4_btn.clicked.connect(lambda: self.on_download("video"))

        self.cancel_btn = QPushButton("Cancelar descarga", right)
        self.cancel_btn.clicked.connect(self.on_cancel)
        self.cancel_btn.setEnabled(False)

        self.progress = QProgressBar(right); self.progress.setRange(0, 100); self.progress.setValue(0)
        self.status = QLabel("", right)

        right_l.addWidget(self.curr_url_lbl)
        right_l.addWidget(self.curr_title_lbl)
        right_l.addWidget(self.use_current_btn)
        right_l.addWidget(self.open_curr_btn)
        right_l.addSpacing(8)
        right_l.addWidget(self.dl_mp3_btn)
        right_l.addWidget(self.dl_mp4_btn)
        right_l.addWidget(self.cancel_btn)
        right_l.addSpacing(12)
        right_l.addWidget(self.progress)
        right_l.addWidget(self.status)
        right_l.addStretch(1)

        split.addWidget(self.web)
        split.addWidget(right)
        split.setSizes([900, 300])

        root.addLayout(top)
        root.addWidget(split)
        self._refresh_dir_tooltips()

    # -------- top bar --------
    def _refresh_dir_tooltips(self):
        self.choose_btn.setToolTip(str(self.download_dir))
        self.open_dir_btn.setToolTip(str(self.download_dir))

    @Slot()
    def on_search(self):
        q = self.search_edit.text().strip()
        if not q:
            QMessageBox.information(self, "Buscar", "Escribe algo para buscar.")
            return
        url = QUrl(f"https://www.youtube.com/results?search_query={QUrl.toPercentEncoding(q).data().decode()}")
        self.web.setUrl(url)

    @Slot()
    def on_choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Elegir carpeta de descargas", str(self.download_dir))
        if d:
            self.download_dir = Path(d)
            self._refresh_dir_tooltips()

    @Slot()
    def on_open_dir(self):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        webbrowser.open(self.download_dir.as_uri())

    # -------- WebView callbacks --------
    @Slot(QUrl)
    def on_url_changed(self, qurl: QUrl):
        url = qurl.toString()
        self.curr_url_lbl.setText(f"URL actual: {url}")
        self.use_current_btn.setEnabled(is_youtube_watch(url))

    @Slot(str)
    def on_title_changed(self, title: str):
        self.curr_title_lbl.setText(f"Título: {title}")

    @Slot()
    def on_use_current(self):
        url = self.web.url().toString()
        if not is_youtube_watch(url):
            QMessageBox.information(self, "Seleccionar", "Navega a un vídeo de YouTube y vuelve a pulsar.")
            return
        self.sel.url = url
        self.sel.title = self.web.title()
        QMessageBox.information(self, "Vídeo seleccionado", f"Usando:\n{self.sel.title}\n{self.sel.url}")

    @Slot()
    def on_open_current(self):
        webbrowser.open(self.web.url().toString())

    # -------- Descargar --------
    def _ensure_selected(self) -> str | None:
        url = self.sel.url or self.web.url().toString()
        if not is_youtube_watch(url):
            QMessageBox.information(self, "Descargar", "Selecciona un vídeo (botón 'Usar este vídeo').")
            return None
        return url

    @Slot()
    def on_download(self, kind: str):
        url = self._ensure_selected()
        if not url: return
        if self.dlt and self.dlt.isRunning():
            QMessageBox.warning(self, "Descargar", "Ya hay una descarga en curso.")
            return
        self.progress.setValue(0)
        self.status.setText("Preparando descarga…")
        self.cancel_btn.setEnabled(True)
        self.dlt = Downloader(url, self.download_dir, kind)
        self.dlt.progress.connect(self.on_progress)
        self.dlt.done.connect(self.on_done)
        self.dlt.failed.connect(self.on_failed)
        self.dlt.finished.connect(lambda: self.cancel_btn.setEnabled(False))
        self.dlt.start()

    @Slot(float, str)
    def on_progress(self, p: float, txt: str):
        self.progress.setValue(int(p))
        self.status.setText(txt)

    @Slot(str)
    def on_done(self, path: str):
        self.progress.setValue(100)
        self.status.setText("Completado")
        QMessageBox.information(self, "Descarga completada", f"Guardado en:\n{path}")

    @Slot(str)
    def on_failed(self, msg: str):
        self.status.setText("")
        QMessageBox.critical(self, "Error de descarga", clean_err(msg))

    @Slot()
    def on_cancel(self):
        if self.dlt and self.dlt.isRunning():
            self.dlt.stop()
            self.status.setText("Cancelando…")

# ------------ Main ------------
def main():
    app = QApplication(sys.argv)
    w = Main()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
