import datetime
import os
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from shiboken6 import isValid
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def steam_config_candidates():
    env_path = os.environ.get("STEAM_CONFIG_DIR")
    candidates = [Path(env_path).expanduser()] if env_path else []
    home = Path.home()

    if sys.platform.startswith("win"):
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        candidates.extend([
            Path(program_files_x86) / "Steam" / "config",
            Path(program_files) / "Steam" / "config",
        ])
    elif sys.platform == "darwin":
        candidates.append(home / "Library" / "Application Support" / "Steam" / "config")
    else:
        candidates.extend([
            home / ".steam" / "steam" / "config",
            home / ".local" / "share" / "Steam" / "config",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / "config",
        ])

    return candidates


def resolve_steam_config_dir():
    candidates = steam_config_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def resolve_geckodriver_path():
    env_path = os.environ.get("GECKODRIVER")
    if env_path and Path(env_path).expanduser().exists():
        return str(Path(env_path).expanduser())

    found = shutil.which("geckodriver")
    if found:
        return found

    suffix = ".exe" if sys.platform.startswith("win") else ""
    download_path = Path.home() / "Downloads" / f"geckodriver{suffix}"
    if download_path.exists():
        return str(download_path)

    return None


def resolve_firefox_binary():
    env_path = os.environ.get("FIREFOX_BINARY")
    if env_path and Path(env_path).expanduser().exists():
        return str(Path(env_path).expanduser())

    if sys.platform.startswith("win"):
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Mozilla Firefox" / "firefox.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Mozilla Firefox" / "firefox.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Firefox.app/Contents/MacOS/firefox")]
    else:
        candidates = [
            Path("/usr/bin/firefox"),
            Path("/usr/local/bin/firefox"),
            Path("/snap/bin/firefox"),
            Path("/var/lib/flatpak/exports/bin/org.mozilla.firefox"),
            Path.home() / ".local" / "share" / "flatpak" / "exports" / "bin" / "org.mozilla.firefox",
        ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return shutil.which("firefox")


def safe_extract_zip(zip_ref, target_dir):
    target_path = Path(target_dir).resolve()
    for member in zip_ref.infolist():
        member_path = (target_path / member.filename).resolve()
        if target_path != member_path and target_path not in member_path.parents:
            raise ValueError(f"Blocked unsafe zip entry: {member.filename}")
    zip_ref.extractall(target_path)


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            result = self.function(*self.args, **self.kwargs)
            self.signals.result.emit(result)
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class DropArea(QFrame):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setObjectName("dropArea")
        layout = QVBoxLayout(self)
        label = QLabel("Drop .lua and .manifest files here")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(label)

    @staticmethod
    def files_from_mime_data(mime_data):
        files = []
        if mime_data.hasUrls():
            for url in mime_data.urls():
                if url.isLocalFile():
                    files.append(url.toLocalFile())
        if not files and mime_data.hasText():
            for line in mime_data.text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                url = QUrl(line)
                if url.isLocalFile():
                    files.append(url.toLocalFile())
                elif os.path.exists(line):
                    files.append(line)
        return files

    def dragEnterEvent(self, event):
        if self.files_from_mime_data(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.files_from_mime_data(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        files = self.files_from_mime_data(event.mimeData())
        if files:
            self.files_dropped.emit(files)
            event.acceptProposedAction()
        else:
            event.ignore()


class OpenSteamToolsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.steam_config_dir = resolve_steam_config_dir()
        self.manifest_dir = str(self.steam_config_dir / "depotcache")
        self.lua_st_dir = str(self.steam_config_dir / "stplug-in")
        self.driver_path = resolve_geckodriver_path()
        self.app_id = "383980"
        self.thread_pool = QThreadPool.globalInstance()
        self.screen_stack = []

        self.firefox_options = webdriver.FirefoxOptions()
        firefox_binary = resolve_firefox_binary()
        if firefox_binary:
            self.firefox_options.binary_location = firefox_binary
        self.firefox_options.add_argument("--headless")
        self.firefox_service = Service(self.driver_path) if self.driver_path else Service()

        self.setWindowTitle("OpenSteamTools")
        self.resize(760, 560)
        self.apply_theme()
        self.show_screen(self.main_menu)

    def apply_theme(self):
        app = QApplication.instance()
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#17191f"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#f4f7fb"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#232733"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#2c3140"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#f4f7fb"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#2b6f88"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#2d8f67"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        app.setPalette(palette)
        app.setStyleSheet("""
            QWidget {
                font-family: Segoe UI, Inter, sans-serif;
                font-size: 13px;
            }
            QPushButton {
                border: 1px solid #4b596d;
                border-radius: 6px;
                padding: 9px 12px;
                background: #253044;
            }
            QPushButton:hover {
                background: #31415c;
            }
            QPushButton:disabled {
                color: #818898;
                background: #202633;
            }
            QLineEdit, QListWidget, QComboBox, QTextEdit {
                border: 1px solid #465266;
                border-radius: 6px;
                padding: 6px;
                background: #202634;
            }
            QLabel#title {
                font-size: 24px;
                font-weight: 700;
            }
            QLabel#sectionTitle {
                font-size: 20px;
                font-weight: 700;
            }
            QLabel#muted {
                color: #a9b2c3;
                font-size: 11px;
            }
            QFrame#dropArea {
                border: 2px dashed #5f6f84;
                border-radius: 8px;
                background: #202634;
            }
        """)

    def _log(self, message):
        os.makedirs("workshop_mod", exist_ok=True)
        log_file = os.path.join("workshop_mod", "log.txt")
        timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        with open(log_file, "a", encoding="utf-8") as file:
            file.write(f"{timestamp} {message}\n")

    def make_page(self, title, include_back=True):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        top_row = QHBoxLayout()
        if include_back:
            back_button = QPushButton("Back")
            back_button.clicked.connect(self.return_to_previous)
            top_row.addWidget(back_button, alignment=Qt.AlignmentFlag.AlignLeft)
        top_row.addStretch()
        layout.addLayout(top_row)

        title_label = QLabel(title)
        title_label.setObjectName("title" if not include_back else "sectionTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        return page, layout

    def show_screen(self, screen_func):
        self.screen_stack.append(screen_func)
        self.setCentralWidget(screen_func())

    def return_to_previous(self):
        if len(self.screen_stack) > 1:
            self.screen_stack.pop()
            self.setCentralWidget(self.screen_stack[-1]())
        else:
            self.screen_stack = []
            self.show_screen(self.main_menu)

    def main_menu(self):
        page, layout = self.make_page("Main Menu", include_back=False)

        config_label = QLabel(f"Steam config: {self.steam_config_dir}")
        config_label.setObjectName("muted")
        config_label.setWordWrap(True)
        layout.addWidget(config_label)

        buttons = [
            ("Mod Downloader", self.mod_downloader),
            ("Lua & Manifest Mode", self.workshop_file_mover),
            ("Uninstaller", self.uninstaller),
            ("View Log", self.view_log),
            ("Credits", self.credits_screen),
        ]
        for label, screen in buttons:
            button = QPushButton(label)
            button.setMinimumHeight(44)
            button.clicked.connect(lambda checked=False, target=screen: self.show_screen(target))
            layout.addWidget(button)

        layout.addStretch()
        return page

    def mod_downloader(self):
        page, layout = self.make_page("Mod Downloader")

        layout.addWidget(QLabel("Search for Mods"))
        self.search_entry = QLineEdit()
        layout.addWidget(self.search_entry)

        layout.addWidget(QLabel("App ID"))
        self.app_id_entry = QLineEdit(self.app_id)
        self.app_id_entry.setMaximumWidth(160)
        layout.addWidget(self.app_id_entry)

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.on_search)
        layout.addWidget(self.search_button)

        self.link_list = QListWidget()
        layout.addWidget(self.link_list, stretch=1)

        self.install_button = QPushButton("Install Selected Mod")
        self.install_button.clicked.connect(self.on_install)
        layout.addWidget(self.install_button)
        return page

    def search_smods(self, query):
        self.app_id = self.app_id_entry.text().strip() or self.app_id
        base_url = "https://catalogue.smods.ru/?s="
        search_url = f"{base_url}{quote_plus(query)}&app={self.app_id}"
        response = requests.get(search_url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        return [
            link["href"]
            for link in soup.find_all("a", class_="skymods-excerpt-btn")
            if "modsbase.com" in link.get("href", "")
        ]

    def extract_download_link(self, download_url):
        driver = None
        try:
            driver = webdriver.Firefox(service=self.firefox_service, options=self.firefox_options)
            driver.get(download_url)
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.ID, "downloadbtn")))
            download_button = driver.find_element(By.ID, "downloadbtn")
            driver.execute_script("arguments[0].scrollIntoView(true);", download_button)
            WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.ID, "downloadbtn")))
            download_button.click()
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="cgi-bin/dl.cgi"]'))
            )
            return driver.find_element(By.CSS_SELECTOR, 'a[href*="cgi-bin/dl.cgi"]').get_attribute("href")
        finally:
            if driver:
                driver.quit()

    def install_mod(self, url):
        self._log(f"Starting install from {url}")
        download_link = self.extract_download_link(url)
        if not download_link:
            raise RuntimeError("Failed to get download link.")

        os.makedirs("workshop_mod", exist_ok=True)
        zip_path = os.path.join("workshop_mod", "mod.zip")
        try:
            with requests.get(download_link, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(zip_path, "wb") as file:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                safe_extract_zip(zip_ref, "workshop_mod")
            self._log(f"Installed mod from {url} ({download_link})")
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

    def run_worker(self, function, on_result=None, on_error=None, on_finished=None):
        worker = Worker(function)
        if on_result:
            worker.signals.result.connect(on_result)
        if on_error:
            worker.signals.error.connect(on_error)
        if on_finished:
            worker.signals.finished.connect(on_finished)
        self.thread_pool.start(worker)

    def on_search(self):
        query = self.search_entry.text().strip()
        if not query:
            QMessageBox.warning(self, "Warning", "Enter a search term.")
            return

        self.search_button.setEnabled(False)
        self.search_button.setText("Searching...")
        self.link_list.clear()

        def handle_result(links):
            for link in links:
                self.link_list.addItem(link)
            if not links:
                QMessageBox.information(self, "No Results", "No modsbase links found.")

        def handle_error(error):
            self._log(f"search_smods error: {error}")
            QMessageBox.critical(self, "Error", f"Search failed: {error}")

        def handle_finished():
            self.search_button.setEnabled(True)
            self.search_button.setText("Search")

        self.run_worker(lambda: self.search_smods(query), handle_result, handle_error, handle_finished)

    def on_install(self):
        item = self.link_list.currentItem()
        if not item:
            QMessageBox.warning(self, "Warning", "No mod selected.")
            return

        url = item.text()
        self.install_button.setEnabled(False)
        self.install_button.setText("Installing...")

        def handle_result(_):
            QMessageBox.information(self, "Success", "Mod installed successfully.")

        def handle_error(error):
            self._log(f"Install failed for {url}: {error}")
            QMessageBox.critical(self, "Error", f"Install failed: {error}")

        def handle_finished():
            self.install_button.setEnabled(True)
            self.install_button.setText("Install Selected Mod")

        self.run_worker(lambda: self.install_mod(url), handle_result, handle_error, handle_finished)

    def workshop_file_mover(self):
        page, layout = self.make_page("Lua & Manifests")
        target_label = QLabel(f"Manifest target: {self.manifest_dir}\nLua target: {self.lua_st_dir}")
        target_label.setObjectName("muted")
        target_label.setWordWrap(True)
        layout.addWidget(target_label)

        drop_area = DropArea()
        drop_area.files_dropped.connect(self.move_workshop_files)
        layout.addWidget(drop_area)

        select_button = QPushButton("Select Files")
        select_button.clicked.connect(self.select_workshop_files)
        layout.addWidget(select_button)

        layout.addStretch()
        return page

    def select_workshop_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Lua and Manifest Files",
            str(Path.home()),
            "Workshop files (*.lua *.manifest);;All files (*)",
        )
        if files:
            self.move_workshop_files(files)

    def move_workshop_files(self, files):
        moved = []
        failed = []
        for file_path in files:
            target_dir = None
            lower_path = file_path.lower()
            if lower_path.endswith(".manifest"):
                target_dir = self.manifest_dir
            elif lower_path.endswith(".lua"):
                target_dir = self.lua_st_dir

            if not target_dir:
                failed.append(f"{file_path}: unsupported file type")
                continue

            try:
                os.makedirs(target_dir, exist_ok=True)
                shutil.move(file_path, target_dir)
                moved.append(os.path.basename(file_path))
                self._log(f"Moved {file_path} to {target_dir}")
            except Exception as exc:
                failed.append(f"{file_path}: {exc}")
                self._log(f"Error moving {file_path}: {exc}")

        if moved:
            QMessageBox.information(self, "Success", "Moved:\n" + "\n".join(moved))
        if failed:
            QMessageBox.warning(self, "Some Files Failed", "\n".join(failed))

    def uninstaller(self):
        page, layout = self.make_page("Uninstaller")

        self.game_count_label = QLabel("Scanning...")
        layout.addWidget(self.game_count_label)

        layout.addWidget(QLabel("Select App ID"))
        self.appid_combo = QComboBox()
        layout.addWidget(self.appid_combo)

        uninstall_button = QPushButton("Uninstall Selected")
        uninstall_button.clicked.connect(self.on_uninstall)
        layout.addWidget(uninstall_button)

        layout.addWidget(QLabel("Search App ID"))
        self.search_uninstall_entry = QLineEdit()
        layout.addWidget(self.search_uninstall_entry)

        search_button = QPushButton("Search")
        search_button.clicked.connect(self.on_search_uninstall)
        layout.addWidget(search_button)

        self.search_listbox = QListWidget()
        layout.addWidget(self.search_listbox, stretch=1)

        uninstall_search_button = QPushButton("Uninstall From Search")
        uninstall_search_button.clicked.connect(self.on_search_uninstall_delete)
        layout.addWidget(uninstall_search_button)

        self.populate_appids()
        return page

    def installed_appids(self):
        os.makedirs(self.lua_st_dir, exist_ok=True)
        appids = []
        for file_name in os.listdir(self.lua_st_dir):
            if file_name.endswith(".lua"):
                appid = file_name[:-4]
                appids.append(int(appid) if appid.isdigit() else appid)
        return [str(appid) for appid in sorted(appids, key=lambda value: (isinstance(value, str), value))]

    def populate_appids(self):
        try:
            appids = self.installed_appids()
            self.appid_combo.clear()
            self.appid_combo.addItems(appids)
            self.game_count_label.setText(f"{len(appids)} games installed")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to scan App IDs: {exc}")

    def on_uninstall(self):
        appid = self.appid_combo.currentText()
        if not appid:
            QMessageBox.warning(self, "Warning", "No App ID selected.")
            return
        self._uninstall_by_appid(appid)

    def on_search_uninstall(self):
        query = self.search_uninstall_entry.text().strip()
        self.search_listbox.clear()
        if not query:
            return
        try:
            for appid in self.installed_appids():
                if query in appid:
                    self.search_listbox.addItem(appid)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Search failed: {exc}")

    def on_search_uninstall_delete(self):
        item = self.search_listbox.currentItem()
        if not item:
            QMessageBox.warning(self, "Warning", "No App ID selected from search results.")
            return
        self._uninstall_by_appid(item.text())

    def _uninstall_by_appid(self, appid):
        filename = f"{appid}.lua"
        filepath = os.path.join(self.lua_st_dir, filename)
        if not os.path.exists(filepath):
            QMessageBox.critical(self, "Error", f"{filename} not found")
            return
        try:
            os.remove(filepath)
            self._log(f"Uninstalled {filename} from {self.lua_st_dir}")
            QMessageBox.information(self, "Success", f"Uninstalled {filename}")
            self.populate_appids()
            self.on_search_uninstall()
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to uninstall {filename}: {exc}")

    def view_log(self):
        page, layout = self.make_page("Installation Log")
        text_box = QTextEdit()
        text_box.setReadOnly(True)
        log_file = os.path.join("workshop_mod", "log.txt")
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as file:
                text_box.setPlainText(file.read())
        else:
            text_box.setPlainText("No logs yet.")
        layout.addWidget(text_box, stretch=1)
        return page

    def credits_screen(self):
        page, layout = self.make_page("Credits")
        layout.addStretch()
        self.credits_label = QLabel("Meng (This took a LONG time)")
        self.credits_label.setObjectName("title")
        self.credits_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.credits_label)
        layout.addStretch()

        self.current_rgb = (255, 0, 0)
        self.target_rgb = (255, 127, 0)
        self.color_index = 1
        self.fade_step = 0
        self.credits_timer = QTimer(page)
        self.credits_timer.timeout.connect(self.animate_rainbow_fade)
        page.destroyed.connect(self.credits_timer.stop)
        self.credits_timer.start(50)
        return page

    def animate_rainbow_fade(self):
        if not getattr(self, "credits_label", None) or not isValid(self.credits_label):
            if getattr(self, "credits_timer", None) and isValid(self.credits_timer):
                self.credits_timer.stop()
            return

        red1, green1, blue1 = self.current_rgb
        red2, green2, blue2 = self.target_rgb
        step_fraction = self.fade_step / 15
        red = int(red1 + (red2 - red1) * step_fraction)
        green = int(green1 + (green2 - green1) * step_fraction)
        blue = int(blue1 + (blue2 - blue1) * step_fraction)
        self.credits_label.setStyleSheet(f"color: #{red:02x}{green:02x}{blue:02x};")

        if self.fade_step < 15:
            self.fade_step += 1
        else:
            self.fade_step = 0
            self.current_rgb = self.target_rgb
            rainbow_colors = [
                (255, 0, 0), (255, 127, 0), (255, 255, 0),
                (0, 255, 0), (0, 0, 255), (75, 0, 130), (148, 0, 211),
            ]
            self.color_index = (self.color_index + 1) % len(rainbow_colors)
            self.target_rgb = rainbow_colors[self.color_index]


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = OpenSteamToolsWindow()
    window.show()
    sys.exit(app.exec())
