# -*- coding: utf-8 -*-
"""
سازمان‌دهنده فایل‌ها | ابزار گرافیکی مرتب‌سازی فایل‌ها با Kivy
نیازمندی اصلی: Kivy
"""

import os
import shutil
from pathlib import Path

from kivy.app import App
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.filechooser import FileChooserListView


try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    def fa(text):
        """نمایش بهتر متن فارسی در محیط‌هایی که شکل‌دهی حروف لازم است."""
        return get_display(arabic_reshaper.reshape(str(text)))
except ImportError:
    def fa(text):
        return str(text)


CATEGORIES = {
    "تصاویر": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".heic"},
    "ویدیوها": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".3gp"},
    "صداها": {".mp3", ".wav", ".aac", ".ogg", ".m4a", ".flac"},
    "اسناد": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"},
    "فشرده‌ها": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "کدنویسی": {".py", ".js", ".html", ".css", ".java", ".cpp", ".c", ".cs", ".php", ".json", ".xml", ".sql"},
    "نصب‌کننده‌ها": {".apk", ".exe", ".msi", ".dmg", ".deb"},
}


def get_category(filename):
    extension = Path(filename).suffix.lower()
    for category, extensions in CATEGORIES.items():
        if extension in extensions:
            return category
    return "سایر فایل‌ها"


def unique_destination(folder, filename, reserved):
    """در صورت وجود نام تکراری، نام تازه‌ای می‌سازد و چیزی را بازنویسی نمی‌کند."""
    original = Path(filename)
    candidate = folder / filename
    counter = 1

    while candidate.exists() or str(candidate).casefold() in reserved:
        new_name = f"{original.stem} ({counter}){original.suffix}"
        candidate = folder / new_name
        counter += 1

    reserved.add(str(candidate).casefold())
    return candidate


class FileOrganizerApp(App):
    title = "سازمان‌دهنده فایل‌ها"

    def build(self):
        Window.clearcolor = (0.055, 0.075, 0.11, 1)

        self.current_folder = self.get_start_folder()
        self.plan = []

        root = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))

        title = Label(
            text=fa("سازمان‌دهنده فایل‌ها"),
            font_size="23sp",
            bold=True,
            color=(0.35, 0.78, 1, 1),
            size_hint_y=None,
            height=dp(48),
        )
        root.add_widget(title)

        subtitle = Label(
            text=fa("مرتب‌سازی امن فایل‌ها بر اساس نوع"),
            font_size="13sp",
            color=(0.75, 0.81, 0.9, 1),
            size_hint_y=None,
            height=dp(28),
        )
        root.add_widget(subtitle)

        self.folder_label = Label(
            text="",
            font_size="12sp",
            color=(0.9, 0.92, 0.96, 1),
            size_hint_y=None,
            height=dp(52),
            halign="center",
            valign="middle",
        )
        self.folder_label.bind(size=self._fit_label)
        root.add_widget(self.folder_label)

        choose_button = self.make_button("انتخاب پوشه", (0.12, 0.42, 0.68, 1))
        choose_button.bind(on_release=self.open_folder_picker)
        root.add_widget(choose_button)

        self.summary_label = Label(
            text=fa("برای شروع، یک پوشه انتخاب کن."),
            font_size="13sp",
            color=(0.72, 0.82, 0.92, 1),
            size_hint_y=None,
            height=dp(42),
        )
        root.add_widget(self.summary_label)

        self.preview_list = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=dp(5),
            padding=(dp(4), dp(4)),
        )
        self.preview_list.bind(minimum_height=self.preview_list.setter("height"))

        scroll = ScrollView(do_scroll_x=False, bar_width=dp(4))
        scroll.add_widget(self.preview_list)
        root.add_widget(scroll)

        preview_button = self.make_button("۱. پیش‌نمایش مرتب‌سازی", (0.13, 0.48, 0.62, 1))
        preview_button.bind(on_release=self.make_preview)
        root.add_widget(preview_button)

        self.run_button = self.make_button("۲. اجرای مرتب‌سازی", (0.12, 0.58, 0.34, 1))
        self.run_button.bind(on_release=self.ask_before_run)
        self.run_button.disabled = True
        root.add_widget(self.run_button)

        note = Label(
            text=fa("هیچ فایلی بدون تأیید تو جابه‌جا نمی‌شود؛ فایل تکراری بازنویسی نخواهد شد."),
            font_size="10sp",
            color=(0.62, 0.7, 0.8, 1),
            size_hint_y=None,
            height=dp(34),
            halign="center",
            valign="middle",
        )
        note.bind(size=self._fit_label)
        root.add_widget(note)

        self.refresh_folder_label()
        return root

    @staticmethod
    def _fit_label(widget, size):
        widget.text_size = (size[0] - dp(8), size[1] - dp(4))

    @staticmethod
    def make_button(text, color):
        button = Button(
            text=fa(text),
            size_hint_y=None,
            height=dp(48),
            background_normal="",
            background_down="",
            background_color=color,
            color=(1, 1, 1, 1),
            font_size="14sp",
        )
        return button

    @staticmethod
    def get_start_folder():
        android_storage = "/storage/emulated/0"
        if os.path.isdir(android_storage) and os.access(android_storage, os.R_OK):
            return android_storage
        return str(Path.home())

    def refresh_folder_label(self):
        self.folder_label.text = fa("پوشه انتخاب‌شده:\n" + self.current_folder)

    def open_folder_picker(self, *_):
        start = self.current_folder if os.path.isdir(self.current_folder) else self.get_start_folder()

        chooser = FileChooserListView(
            path=start,
            dirselect=False,
            multiselect=False,
        )

        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))
        content.add_widget(chooser)

        actions = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        cancel = self.make_button("انصراف", (0.38, 0.4, 0.45, 1))
        select = self.make_button("انتخاب این پوشه", (0.12, 0.48, 0.62, 1))
        actions.add_widget(cancel)
        actions.add_widget(select)
        content.add_widget(actions)

        popup = Popup(
            title=fa("انتخاب پوشه"),
            content=content,
            size_hint=(0.96, 0.92),
            auto_dismiss=False,
        )
        cancel.bind(on_release=popup.dismiss)

        def select_folder(*_):
            chosen = chooser.path
            if os.path.isdir(chosen):
                self.current_folder = os.path.abspath(chosen)
                self.plan = []
                self.run_button.disabled = True
                self.clear_preview()
                self.refresh_folder_label()
                self.summary_label.text = fa("پوشه انتخاب شد؛ حالا پیش‌نمایش را بزن.")
                popup.dismiss()

        select.bind(on_release=select_folder)
        popup.open()

    def clear_preview(self):
        self.preview_list.clear_widgets()

    def make_preview(self, *_):
        folder = Path(self.current_folder)

        if not folder.is_dir():
            self.summary_label.text = fa("پوشه انتخاب‌شده معتبر نیست.")
            return

        if not os.access(str(folder), os.R_OK | os.W_OK):
            self.summary_label.text = fa("دسترسی خواندن یا نوشتن در این پوشه وجود ندارد.")
            return

        try:
            entries = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        except OSError as error:
            self.summary_label.text = fa("خطا در خواندن پوشه: " + str(error))
            return

        files = []
        for item in entries:
            try:
                if not item.is_file() or item.is_symlink():
                    continue
                if item.resolve() == Path(__file__).resolve():
                    continue
                files.append(item)
            except OSError:
                continue

        reserved = set()
        plan = []
        for source in files:
            category = get_category(source.name)
            destination_folder = folder / category
            destination = unique_destination(destination_folder, source.name, reserved)
            plan.append((source, category, destination))

        self.plan = plan
        self.clear_preview()

        if not plan:
            self.summary_label.text = fa("فایل قابل مرتب‌سازی در این پوشه پیدا نشد.")
            self.run_button.disabled = True
            return

        counts = {}
        for _, category, _ in plan:
            counts[category] = counts.get(category, 0) + 1

        self.summary_label.text = fa(
            f"{len(plan)} فایل برای مرتب‌سازی پیدا شد | {len(counts)} دسته"
        )

        for source, category, destination in plan:
            line = Label(
                text=fa(f"{source.name}  ←  {category}"),
                size_hint_y=None,
                height=dp(34),
                font_size="11sp",
                color=(0.88, 0.92, 0.98, 1),
                halign="right",
                valign="middle",
            )
            line.bind(size=self._fit_label)
            self.preview_list.add_widget(line)

        self.run_button.disabled = False

    def ask_before_run(self, *_):
        if not self.plan:
            return

        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(12))
        message = Label(
            text=fa(f"قرار است {len(self.plan)} فایل به پوشه‌های دسته‌بندی منتقل شود.\nفایل اصلی حذف نمی‌شود، اما مسیر آن تغییر می‌کند. ادامه می‌دهی؟"),
            halign="center",
            valign="middle",
        )
        message.bind(size=self._fit_label)
        content.add_widget(message)

        actions = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        cancel = self.make_button("نه، فعلاً نه", (0.38, 0.4, 0.45, 1))
        confirm = self.make_button("بله، مرتب کن", (0.12, 0.58, 0.34, 1))
        actions.add_widget(cancel)
        actions.add_widget(confirm)
        content.add_widget(actions)

        popup = Popup(
            title=fa("تأیید نهایی"),
            content=content,
            size_hint=(0.88, 0.42),
            auto_dismiss=False,
        )
        cancel.bind(on_release=popup.dismiss)

        def run(*_):
            popup.dismiss()
            self.organize_files()

        confirm.bind(on_release=run)
        popup.open()

    def organize_files(self):
        root = Path(self.current_folder)
        moved = 0
        failed = []

        for source, category, _old_destination in self.plan:
            try:
                if not source.is_file() or source.parent != root:
                    continue

                destination_folder = root / category
                destination_folder.mkdir(exist_ok=True)

                reserved = set()
                destination = unique_destination(destination_folder, source.name, reserved)

                shutil.move(str(source), str(destination))
                moved += 1
            except (OSError, shutil.Error) as error:
                failed.append(f"{source.name}: {error}")

        self.plan = []
        self.run_button.disabled = True
        self.clear_preview()

        if failed:
            summary = f"تعداد جابه‌جایی موفق: {moved}\nتعداد خطا: {len(failed)}"
            details = "\n".join(failed[:8])
            self.summary_label.text = fa(summary)
            self.show_message("گزارش عملیات", summary + "\n\n" + details)
        else:
            summary = f"مرتب‌سازی تمام شد؛ {moved} فایل جابه‌جا شد."
            self.summary_label.text = fa(summary)
            self.show_message("عملیات موفق", summary)

    def show_message(self, title, message):
        content = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(10))
        label = Label(text=fa(message), halign="center", valign="middle")
        label.bind(size=self._fit_label)
        content.add_widget(label)

        close = self.make_button("بستن", (0.12, 0.48, 0.62, 1))
        content.add_widget(close)

        popup = Popup(
            title=fa(title),
            content=content,
            size_hint=(0.9, 0.55),
        )
        close.bind(on_release=popup.dismiss)
        popup.open()


if __name__ == "__main__":
    FileOrganizerApp().run()
