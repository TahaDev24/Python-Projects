# -*- coding: utf-8 -*-
"""
DB MASTER PRO — SQLite Edition
Android / Pydroid 3 / Kivy
Single-file SQLite administration studio.

Requirements:
    pip install kivy
Optional Persian shaping:
    pip install arabic-reshaper python-bidi
"""

import os
import sys
import csv
import json
import shutil
import sqlite3
import traceback
from datetime import datetime

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    def shape_text(value):
        try:
            return get_display(arabic_reshaper.reshape(str(value)))
        except Exception:
            return str(value)
except Exception:
    def shape_text(value):
        return str(value)

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.core.window import Window
from kivy.utils import platform
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.spinner import Spinner
from kivy.uix.screenmanager import ScreenManager, Screen, FadeTransition
from kivy.uix.widget import Widget
from kivy.graphics import Color, RoundedRectangle, Rectangle
from kivy.properties import ListProperty, StringProperty


APP_NAME = "DB MASTER PRO"
VERSION = "1.0.0"
BG = (0.035, 0.055, 0.085, 1)
PANEL = (0.065, 0.09, 0.13, 1)
PANEL2 = (0.085, 0.12, 0.17, 1)
ACCENT = (0.12, 0.62, 0.98, 1)
ACCENT_DARK = (0.07, 0.34, 0.62, 1)
TEXT = (0.91, 0.95, 0.99, 1)
MUTED = (0.55, 0.64, 0.73, 1)
GREEN = (0.20, 0.78, 0.55, 1)
RED = (0.92, 0.30, 0.34, 1)
ORANGE = (1.0, 0.65, 0.22, 1)


def qident(name):
    """Quote an SQLite identifier safely."""
    return '"' + str(name).replace('"', '""') + '"'


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def app_storage():
    try:
        base = App.get_running_app().user_data_dir
    except Exception:
        base = os.path.join(os.path.expanduser("~"), "DB_MASTER_PRO")
    os.makedirs(base, exist_ok=True)
    return base


class Panel(BoxLayout):
    def __init__(self, **kwargs):
        panel_padding = kwargs.pop("panel_padding", dp(12))
        panel_spacing = kwargs.pop("panel_spacing", dp(8))
        super().__init__(**kwargs)
        self.padding = panel_padding
        self.spacing = panel_spacing
        with self.canvas.before:
            Color(*PANEL)
            self.bg_rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(14)])
        self.bind(pos=self._sync, size=self._sync)

    def _sync(self, *_):
        self.bg_rect.pos = self.pos
        self.bg_rect.size = self.size


class DBManager:
    """Connection manager. Every opened database is tracked by path."""
    def __init__(self):
        self.connections = {}
        self.current_path = None

    @property
    def conn(self):
        if not self.current_path:
            return None
        return self.connections.get(self.current_path)

    def open(self, path):
        path = os.path.abspath(path)
        if path in self.connections:
            self.current_path = path
            return self.connections[path]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")
        self.connections[path] = con
        self.current_path = path
        return con

    def close_current(self):
        if self.current_path and self.current_path in self.connections:
            self.connections[self.current_path].close()
            del self.connections[self.current_path]
        self.current_path = None

    def close_all(self):
        for con in list(self.connections.values()):
            try:
                con.close()
            except Exception:
                pass
        self.connections.clear()
        self.current_path = None

    def tables(self):
        if not self.conn:
            return []
        rows = self.conn.execute("""
            SELECT name, type FROM sqlite_master
            WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'
            ORDER BY type, name
        """).fetchall()
        return [(r["name"], r["type"]) for r in rows]

    def table_info(self, table):
        return self.conn.execute(f"PRAGMA table_info({qident(table)})").fetchall()

    def row_count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {qident(table)}").fetchone()[0]

    def db_stats(self):
        con = self.conn
        page_count = con.execute("PRAGMA page_count").fetchone()[0]
        page_size = con.execute("PRAGMA page_size").fetchone()[0]
        freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
        return {
            "tables": len([x for x in self.tables() if x[1] == "table"]),
            "views": len([x for x in self.tables() if x[1] == "view"]),
            "size": page_count * page_size,
            "free": freelist * page_size,
            "encoding": con.execute("PRAGMA encoding").fetchone()[0],
            "foreign_keys": con.execute("PRAGMA foreign_keys").fetchone()[0],
            "journal": con.execute("PRAGMA journal_mode").fetchone()[0],
            "version": sqlite3.sqlite_version,
        }


class DBMasterApp(App):
    title = APP_NAME

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.manager = DBManager()
        self.current_table = None
        self.current_page = 0
        self.page_size = 100
        self.last_query = ""
        self.query_history = []
        self.selected_rows = []
        self.language = "EN"
        self.theme_name = "Dark"
        self.status = "Ready"
        self.result_rows = []
        self.result_columns = []
        self._busy = False

    def build(self):
        Window.clearcolor = BG
        if platform != "android":
            Window.size = (430, 850)
        self.root = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(9))
        self.root.add_widget(self.build_header())
        self.content = BoxLayout(orientation="vertical")
        self.root.add_widget(self.content)
        self.root.add_widget(self.build_statusbar())
        self.show_dashboard()
        return self.root

    def build_header(self):
        bar = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        brand = BoxLayout(orientation="vertical", spacing=0)
        brand.add_widget(Label(text="[b]DB MASTER[/b] [color=36A0FA]PRO[/color]",
                               markup=True, color=TEXT, font_size="20sp",
                               halign="left", valign="middle"))
        brand.add_widget(Label(text=f"SQLite Studio  •  v{VERSION}",
                               color=MUTED, font_size="10sp", halign="left"))
        bar.add_widget(brand)
        self.db_badge = Button(text="No database", size_hint_x=None, width=dp(142),
                               background_normal="", background_color=PANEL2,
                               color=MUTED, font_size="11sp")
        self.db_badge.bind(on_release=lambda *_: self.open_database_dialog())
        bar.add_widget(self.db_badge)
        return bar

    def build_statusbar(self):
        box = BoxLayout(size_hint_y=None, height=dp(27), padding=(dp(6), 0))
        self.status_label = Label(text=self.status, color=MUTED, font_size="10sp",
                                  halign="left", valign="middle")
        box.add_widget(self.status_label)
        box.add_widget(Label(text="LOCAL • SQLITE", color=ACCENT,
                             font_size="9sp", size_hint_x=None, width=dp(92)))
        return box

    def set_status(self, text, error=False):
        self.status = str(text)
        if hasattr(self, "status_label"):
            self.status_label.text = self.status
            self.status_label.color = RED if error else MUTED

    def clear_content(self):
        self.content.clear_widgets()

    def make_button(self, text, callback, color=ACCENT_DARK, height=dp(42), **kwargs):
        b = Button(text=text, size_hint_y=None, height=height,
                   background_normal="", background_down="",
                   background_color=color, color=TEXT,
                   font_size=kwargs.get("font_size", "12sp"),
                   halign="center", valign="middle")
        b.bind(on_release=callback)
        return b

    def make_label(self, text, size=dp(18), color=TEXT, bold=False, height=None):
        lbl = Label(text=f"[b]{text}[/b]" if bold else str(text),
                    markup=True, color=color, font_size=size,
                    halign="left", valign="middle",
                    size_hint_y=None if height else 1,
                    height=height or dp(28))
        lbl.bind(size=lambda inst, val: setattr(inst, "text_size", (val[0], None)))
        return lbl

    def nav_bar(self):
        nav = GridLayout(cols=4, spacing=dp(5), size_hint_y=None, height=dp(43))
        items = [
            ("⌂  Home", self.show_dashboard),
            ("▦  Tables", self.show_tables),
            ("SQL", self.show_sql),
            ("⚙  Tools", self.show_tools),
        ]
        for title, fn in items:
            nav.add_widget(self.make_button(title, lambda *_ , f=fn: f(),
                                            color=PANEL2, height=dp(43), font_size="11sp"))
        return nav

    def page(self, title, subtitle=None):
        self.clear_content()
        wrapper = BoxLayout(orientation="vertical", spacing=dp(8))
        wrapper.add_widget(self.nav_bar())
        head = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(54))
        head.add_widget(self.make_label(title, dp(19), TEXT, True, dp(29)))
        if subtitle:
            head.add_widget(self.make_label(subtitle, dp(10), MUTED, False, dp(19)))
        wrapper.add_widget(head)
        body = BoxLayout(orientation="vertical", spacing=dp(8))
        wrapper.add_widget(body)
        self.content.add_widget(wrapper)
        return body

    def scroll(self, widget=None):
        sc = ScrollView(do_scroll_x=True, do_scroll_y=True, bar_width=dp(4))
        if widget is not None:
            sc.add_widget(widget)
        return sc

    def show_dashboard(self, *_):
        body = self.page("Dashboard", "Database overview and quick actions")
        if not self.manager.conn:
            card = Panel(orientation="vertical", size_hint_y=None, height=dp(220))
            card.add_widget(self.make_label("Welcome to DB MASTER PRO", dp(18), TEXT, True))
            card.add_widget(self.make_label(
                "Open an existing SQLite database or create a new one to start managing your data.",
                dp(12), MUTED))
            card.add_widget(self.make_button("＋  Create database", self.create_database_dialog))
            card.add_widget(self.make_button("▣  Open database", self.open_database_dialog, PANEL2))
            body.add_widget(card)
            body.add_widget(self.make_label("PRO WORKSPACE", dp(12), ACCENT, True))
            body.add_widget(self.make_label(
                "SQL Editor • Table Browser • Schema Inspector • Backup • CSV / JSON • Integrity Check",
                dp(12), MUTED))
            return
        stats = self.manager.db_stats()
        grid = GridLayout(cols=2, spacing=dp(8), size_hint_y=None, height=dp(142))
        for title, value in [
            ("TABLES", stats["tables"]), ("VIEWS", stats["views"]),
            ("DATABASE SIZE", human_size(stats["size"])),
            ("FREE PAGES", human_size(stats["free"]))
        ]:
            tile = Panel(orientation="vertical", spacing=dp(2))
            tile.add_widget(self.make_label(str(value), dp(21), ACCENT, True))
            tile.add_widget(self.make_label(title, dp(10), MUTED))
            grid.add_widget(tile)
        body.add_widget(grid)
        info = Panel(orientation="vertical", size_hint_y=None, height=dp(145))
        info.add_widget(self.make_label("DATABASE INFORMATION", dp(12), ACCENT, True))
        info.add_widget(self.make_label(os.path.basename(self.manager.current_path), dp(12), TEXT))
        info.add_widget(self.make_label(
            f"SQLite {stats['version']}  •  {stats['encoding']}  •  Journal: {stats['journal']}",
            dp(10), MUTED))
        info.add_widget(self.make_label(
            f"Foreign keys: {'ON' if stats['foreign_keys'] else 'OFF'}",
            dp(10), GREEN if stats["foreign_keys"] else ORANGE))
        body.add_widget(info)
        actions = GridLayout(cols=2, spacing=dp(7), size_hint_y=None, height=dp(100))
        actions.add_widget(self.make_button("＋ New table", self.create_table_dialog))
        actions.add_widget(self.make_button("SQL Editor", self.show_sql, PANEL2))
        actions.add_widget(self.make_button("Backup database", self.backup_database))
        actions.add_widget(self.make_button("Analyze database", self.analyze_database, PANEL2))
        body.add_widget(actions)
        body.add_widget(self.make_label("RECENT TABLES", dp(12), ACCENT, True))
        recent = self.manager.tables()[:6]
        for name, typ in recent:
            body.add_widget(self.make_button(f"▦  {name}   ({typ})",
                lambda *_ , n=name: self.open_table(n), PANEL2, dp(38)))

    def show_tables(self, *_):
        body = self.page("Tables & Views", "Browse schema objects in the active database")
        if not self.manager.conn:
            body.add_widget(self.make_label("Open a database first.", dp(13), MUTED))
            body.add_widget(self.make_button("Open database", self.open_database_dialog))
            return
        top = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        top.add_widget(self.make_button("＋ Create table", self.create_table_dialog))
        top.add_widget(self.make_button("↻ Refresh", self.show_tables, PANEL2))
        body.add_widget(top)
        items = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
        items.bind(minimum_height=items.setter("height"))
        for name, typ in self.manager.tables():
            row = BoxLayout(size_hint_y=None, height=dp(47), spacing=dp(5))
            row.add_widget(self.make_button(
                f"{'▦' if typ == 'table' else '◇'}  {name}   [{typ}]",
                lambda *_ , n=name: self.open_table(n), PANEL2, dp(45)))
            if typ == "table":
                row.add_widget(self.make_button("⋯", lambda *_ , n=name: self.table_actions(n),
                                                ACCENT_DARK, dp(45)))
            items.add_widget(row)
        body.add_widget(self.scroll(items))

    def open_table(self, name):
        self.current_table = name
        self.current_page = 0
        self.show_table_data()

    def show_table_data(self, *_):
        name = self.current_table
        if not self.manager.conn or not name:
            self.show_tables()
            return
        body = self.page(name, "Data browser • schema • paginated records")
        tabs = GridLayout(cols=3, spacing=dp(5), size_hint_y=None, height=dp(39))
        tabs.add_widget(self.make_button("Data", self.show_table_data))
        tabs.add_widget(self.make_button("Structure", self.show_table_schema, PANEL2))
        tabs.add_widget(self.make_button("Export", lambda *_: self.export_dialog(name), PANEL2))
        body.add_widget(tabs)
        tools = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(5))
        self.table_search = TextInput(hint_text="Filter rows (text search)", multiline=False,
                                      size_hint_x=1, background_color=PANEL2,
                                      foreground_color=TEXT, hint_text_color=MUTED,
                                      cursor_color=ACCENT, font_size="12sp")
        tools.add_widget(self.table_search)
        tools.add_widget(self.make_button("Search", self.search_table, ACCENT_DARK, dp(42)))
        body.add_widget(tools)
        actions = GridLayout(cols=2, spacing=dp(5), size_hint_y=None, height=dp(40))
        actions.add_widget(self.make_button("＋ Insert", self.insert_row_dialog))
        actions.add_widget(self.make_button("↻ Refresh", self.show_table_data, PANEL2))
        body.add_widget(actions)
        self.data_area = BoxLayout(orientation="vertical", spacing=dp(4), size_hint_y=None)
        self.data_area.bind(minimum_height=self.data_area.setter("height"))
        body.add_widget(self.scroll(self.data_area))
        self.load_table_rows()

    def load_table_rows(self, search=None):
        if not self.manager.conn or not self.current_table:
            return
        con = self.manager.conn
        table = self.current_table
        cols = [r["name"] for r in self.manager.table_info(table)]
        if not cols:
            self.data_area.clear_widgets()
            self.data_area.add_widget(self.make_label("No columns found.", dp(12), MUTED))
            return
        where = ""
        params = []
        if search:
            where = " WHERE " + " OR ".join(f"CAST({qident(c)} AS TEXT) LIKE ?" for c in cols)
            params = [f"%{search}%"] * len(cols)
        total = con.execute(f"SELECT COUNT(*) FROM {qident(table)}{where}", params).fetchone()[0]
        offset = self.current_page * self.page_size
        rows = con.execute(
            f"SELECT rowid AS __dbm_rowid__, * FROM {qident(table)}{where} LIMIT ? OFFSET ?",
            params + [self.page_size, offset]).fetchall()
        self.data_area.clear_widgets()
        summary = BoxLayout(size_hint_y=None, height=dp(30))
        summary.add_widget(self.make_label(f"{total} records • Page {self.current_page + 1}",
                                            dp(10), MUTED))
        self.data_area.add_widget(summary)
        pager = BoxLayout(size_hint_y=None, height=dp(35), spacing=dp(5))
        pager.add_widget(self.make_button("‹ Previous", self.prev_page, PANEL2, dp(34)))
        pager.add_widget(self.make_button("Next ›", self.next_page, PANEL2, dp(34)))
        self.data_area.add_widget(pager)
        for row in rows:
            rid = row["__dbm_rowid__"]
            card = Panel(orientation="vertical", size_hint_y=None, height=dp(84),
                         panel_padding=dp(7), panel_spacing=dp(2))
            first = next(iter(cols))
            card.add_widget(self.make_label(f"rowid {rid}  •  {first}: {str(row[first])[:90]}",
                                            dp(11), ACCENT, True, dp(24)))
            preview = "  |  ".join(f"{c}={str(row[c])[:35]}" for c in cols[1:4])
            card.add_widget(self.make_label(preview or "(single-column record)",
                                            dp(9), MUTED, False, dp(22)))
            controls = BoxLayout(size_hint_y=None, height=dp(27), spacing=dp(5))
            controls.add_widget(self.make_button("Edit", lambda *_ , r=rid: self.edit_row_dialog(r),
                                                  PANEL2, dp(27), font_size="10sp"))
            controls.add_widget(self.make_button("Delete", lambda *_ , r=rid: self.confirm(
                f"Delete record rowid {r} from {table}?", lambda: self._delete_row(r), destructive=True),
                RED, dp(27), font_size="10sp"))
            card.add_widget(controls)
            self.data_area.add_widget(card)
        if not rows:
            self.data_area.add_widget(self.make_label("No records match this view.", dp(12), MUTED))

    def search_table(self, *_):
        if not self.manager.conn:
            return
        self.current_page = 0
        self.load_table_rows(self.table_search.text.strip() or None)

    def prev_page(self, *_):
        self.current_page = max(0, self.current_page - 1)
        self.load_table_rows(self.table_search.text.strip() or None)

    def next_page(self, *_):
        self.current_page += 1
        self.load_table_rows(self.table_search.text.strip() or None)

    def show_table_schema(self, *_):
        name = self.current_table
        body = self.page(f"{name} • Structure", "Columns, constraints and indexes")
        tabs = GridLayout(cols=3, spacing=dp(5), size_hint_y=None, height=dp(39))
        tabs.add_widget(self.make_button("Data", self.show_table_data, PANEL2))
        tabs.add_widget(self.make_button("Structure", self.show_table_schema))
        tabs.add_widget(self.make_button("Export", lambda *_: self.export_dialog(name), PANEL2))
        body.add_widget(tabs)
        info = self.manager.table_info(name)
        grid = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
        grid.bind(minimum_height=grid.setter("height"))
        for c in info:
            pk = " • PRIMARY KEY" if c["pk"] else ""
            nn = " • NOT NULL" if c["notnull"] else ""
            grid.add_widget(self.make_label(
                f"{c['cid']}: {c['name']}   |   {c['type'] or 'ANY'}{pk}{nn}",
                dp(11), TEXT, False, dp(34)))
        body.add_widget(self.scroll(grid))
        body.add_widget(self.make_label("INDEXES", dp(12), ACCENT, True, dp(28)))
        indexes = self.manager.conn.execute(f"PRAGMA index_list({qident(name)})").fetchall()
        for idx in indexes:
            body.add_widget(self.make_label(
                f"{idx['name']}  •  {'UNIQUE' if idx['unique'] else 'NON-UNIQUE'}",
                dp(10), MUTED, False, dp(25)))
        body.add_widget(self.make_button("＋ Create index", self.create_index_dialog))

    def show_sql(self, *_):
        body = self.page("SQL Studio", "Execute SQL against the active SQLite database")
        if not self.manager.conn:
            body.add_widget(self.make_label("Open or create a database first.", dp(12), MUTED))
            body.add_widget(self.make_button("Open database", self.open_database_dialog))
            return
        row = BoxLayout(size_hint_y=None, height=dp(36), spacing=dp(5))
        row.add_widget(self.make_button("SELECT template", lambda *_: self.insert_sql_template(
            "SELECT * FROM sqlite_master WHERE type='table';"), PANEL2, dp(36)))
        row.add_widget(self.make_button("Clear", lambda *_: setattr(self.sql_input, "text", ""),
                                        PANEL2, dp(36)))
        body.add_widget(row)
        self.sql_input = TextInput(
            text="SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%';",
            hint_text="Write SQL here...", multiline=True, size_hint_y=0.40,
            background_color=(0.025, 0.04, 0.065, 1), foreground_color=TEXT,
            cursor_color=ACCENT, hint_text_color=MUTED, font_size="12sp",
            padding=dp(10))
        body.add_widget(self.sql_input)
        controls = GridLayout(cols=2, spacing=dp(6), size_hint_y=None, height=dp(42))
        controls.add_widget(self.make_button("▶ Execute", self.execute_sql, GREEN))
        controls.add_widget(self.make_button("Execute script", self.execute_script, ACCENT_DARK))
        body.add_widget(controls)
        self.sql_result = BoxLayout(orientation="vertical", spacing=dp(4))
        body.add_widget(self.scroll(self.sql_result))
        self.sql_result.add_widget(self.make_label("Results will appear here.", dp(11), MUTED))

    def insert_sql_template(self, value):
        self.sql_input.text = value

    def execute_sql(self, *_):
        if not self.manager.conn:
            return
        sql = self.sql_input.text.strip()
        if not sql:
            return self.notify("SQL is empty", error=True)
        try:
            cur = self.manager.conn.execute(sql)
            if cur.description:
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                self.render_sql_result(cols, rows)
                self.set_status(f"Query returned {len(rows)} row(s)")
            else:
                self.manager.conn.commit()
                self.render_sql_result(["Result"], [(f"Success • {cur.rowcount} affected row(s)",)])
                self.set_status(f"Statement executed • {cur.rowcount} affected")
        except Exception as e:
            self.render_sql_result(["SQL ERROR"], [(str(e),)])
            self.set_status(f"SQL error: {e}", True)

    def execute_script(self, *_):
        if not self.manager.conn:
            return
        sql = self.sql_input.text.strip()
        if not sql:
            return self.notify("SQL is empty", True)
        try:
            self.manager.conn.executescript(sql)
            self.manager.conn.commit()
            self.render_sql_result(["Result"], [("Script executed successfully",)])
            self.set_status("SQL script executed")
        except Exception as e:
            self.render_sql_result(["SCRIPT ERROR"], [(str(e),)])
            self.set_status(f"Script error: {e}", True)

    def render_sql_result(self, columns, rows):
        self.sql_result.clear_widgets()
        self.sql_result.add_widget(self.make_label(
            f"RESULT • {len(rows)} row(s) • {len(columns)} column(s)",
            dp(10), ACCENT, True, dp(28)))
        for row in rows[:250]:
            vals = [str(row[c]) if isinstance(row, sqlite3.Row) else str(v)
                    for c, v in (zip(columns, row) if not isinstance(row, sqlite3.Row)
                                 else [(c, row[c]) for c in columns])]
            self.sql_result.add_widget(self.make_label(
                "  |  ".join(f"{columns[i]}: {vals[i][:100]}" for i in range(len(columns))),
                dp(9), TEXT, False, dp(32)))
        if len(rows) > 250:
            self.sql_result.add_widget(self.make_label(
                "Only first 250 rows are displayed.", dp(10), ORANGE, False, dp(25)))

    def show_tools(self, *_):
        body = self.page("Database Tools", "Maintenance, backup, import and export")
        if not self.manager.conn:
            body.add_widget(self.make_label("Open a database to use maintenance tools.", dp(12), MUTED))
            body.add_widget(self.make_button("Open database", self.open_database_dialog))
            return
        tools = [
            ("Create backup", self.backup_database, GREEN),
            ("Restore backup", self.restore_backup_dialog, PANEL2),
            ("Integrity check", self.integrity_check, PANEL2),
            ("Analyze database", self.analyze_database, PANEL2),
            ("VACUUM • Rebuild database", self.vacuum_database, ORANGE),
            ("Export table (CSV / JSON)", self.export_current_dialog, PANEL2),
            ("Import CSV", self.import_csv_dialog, PANEL2),
            ("Database details", self.database_details, PANEL2),
            ("Close database", self.close_database, RED),
        ]
        for label, fn, color in tools:
            body.add_widget(self.make_button(label, fn, color, dp(43)))

    def create_database_dialog(self, *_):
        content = BoxLayout(orientation="vertical", spacing=dp(9), padding=dp(12))
        name = TextInput(hint_text="Database name (e.g. project.db)", multiline=False,
                         size_hint_y=None, height=dp(44))
        content.add_widget(self.make_label("Create a new SQLite database", dp(14), TEXT, True))
        content.add_widget(name)
        content.add_widget(self.make_label("Saved inside the app's DB_MASTER folder.", dp(10), MUTED))
        pop = Popup(title="New database", content=content, size_hint=(.92, .42))
        def create(*_):
            filename = name.text.strip()
            if not filename:
                return self.notify("Enter a database name", True)
            if not filename.lower().endswith((".db", ".sqlite", ".sqlite3")):
                filename += ".db"
            if os.path.basename(filename) != filename:
                return self.notify("Use a file name, not a path", True)
            path = os.path.join(app_storage(), "databases", filename)
            if os.path.exists(path):
                return self.notify("A database with this name already exists", True)
            try:
                self.manager.open(path)
                self.db_badge.text = filename
                pop.dismiss()
                self.set_status("Database created")
                self.show_dashboard()
            except Exception as e:
                self.notify(str(e), True)
        content.add_widget(self.make_button("Create database", create))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def open_database_dialog(self, *_):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        path_input = TextInput(hint_text="Full path to .db / .sqlite file",
                               multiline=False, size_hint_y=None, height=dp(44))
        content.add_widget(self.make_label("Open SQLite database", dp(14), TEXT, True))
        content.add_widget(path_input)
        content.add_widget(self.make_label("Recent app databases", dp(11), ACCENT, True))
        recent = GridLayout(cols=1, spacing=dp(4), size_hint_y=None)
        recent.bind(minimum_height=recent.setter("height"))
        folder = os.path.join(app_storage(), "databases")
        os.makedirs(folder, exist_ok=True)
        for filename in sorted(os.listdir(folder))[-12:]:
            if filename.lower().endswith((".db", ".sqlite", ".sqlite3")):
                recent.add_widget(self.make_button(filename,
                    lambda *_ , f=os.path.join(folder, filename): self._open_path(f, pop),
                    PANEL2, dp(35)))
        content.add_widget(self.scroll(recent))
        pop = Popup(title="Open database", content=content, size_hint=(.94, .78))
        content.add_widget(self.make_button("Open path", lambda *_: self._open_path(path_input.text.strip(), pop)))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def _open_path(self, path, pop=None):
        if not path:
            return self.notify("Enter a database path", True)
        if not os.path.isfile(path):
            return self.notify("File not found. Create it or check the path.", True)
        try:
            self.manager.open(path)
            self.db_badge.text = os.path.basename(path)
            if pop:
                pop.dismiss()
            self.set_status("Database opened")
            self.show_dashboard()
        except Exception as e:
            self.notify(f"Cannot open database: {e}", True)

    def close_database(self, *_):
        self.confirm("Close the active database connection?", self._confirm_close)

    def _confirm_close(self):
        self.manager.close_current()
        self.current_table = None
        self.db_badge.text = "No database"
        self.show_dashboard()
        self.set_status("Database closed")

    def create_table_dialog(self, *_):
        if not self.manager.conn:
            return self.notify("Open a database first", True)
        content = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(10))
        table_name = TextInput(hint_text="Table name", multiline=False,
                               size_hint_y=None, height=dp(42))
        columns = TextInput(
            text="id INTEGER PRIMARY KEY AUTOINCREMENT,\nname TEXT NOT NULL,\ncreated_at TEXT",
            hint_text="column definitions, comma separated", multiline=True,
            size_hint_y=None, height=dp(150))
        content.add_widget(self.make_label("Create table", dp(14), TEXT, True))
        content.add_widget(table_name)
        content.add_widget(self.make_label("Column definitions (SQLite syntax)", dp(10), MUTED))
        content.add_widget(columns)
        content.add_widget(self.make_label(
            "Example: id INTEGER PRIMARY KEY, title TEXT NOT NULL, amount REAL",
            dp(9), MUTED))
        pop = Popup(title="Table Designer", content=content, size_hint=(.94, .70))
        def create(*_):
            name, definition = table_name.text.strip(), columns.text.strip()
            if not name or not definition:
                return self.notify("Table name and columns are required", True)
            try:
                self.manager.conn.execute(
                    f"CREATE TABLE {qident(name)} ({definition})")
                self.manager.conn.commit()
                pop.dismiss()
                self.set_status(f"Table {name} created")
                self.current_table = name
                self.show_table_data()
            except Exception as e:
                self.notify(f"Create table failed: {e}", True)
        content.add_widget(self.make_button("Create", create, GREEN))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def table_actions(self, name):
        content = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(10))
        pop = Popup(title=f"Table: {name}", content=content, size_hint=(.88, .48))
        content.add_widget(self.make_button("Open data", lambda *_: (pop.dismiss(), self.open_table(name))))
        content.add_widget(self.make_button("View structure", lambda *_: (pop.dismiss(),
            setattr(self, "current_table", name), self.show_table_schema()), PANEL2))
        content.add_widget(self.make_button("Drop table", lambda *_: (
            pop.dismiss(), self.confirm(f"PERMANENTLY drop table '{name}' and all its data?",
                                        lambda: self.drop_table(name), destructive=True)), RED))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def drop_table(self, name):
        try:
            self.manager.conn.execute(f"DROP TABLE {qident(name)}")
            self.manager.conn.commit()
            self.current_table = None
            self.set_status(f"Table {name} dropped")
            self.show_tables()
        except Exception as e:
            self.notify(str(e), True)

    def insert_row_dialog(self, *_):
        self.edit_row_dialog(None)

    def edit_row_dialog(self, rowid):
        table = self.current_table
        info = self.manager.table_info(table)
        if not info:
            return
        content = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        fields = {}
        existing = None
        if rowid is not None:
            existing = self.manager.conn.execute(
                f"SELECT * FROM {qident(table)} WHERE rowid=?", (rowid,)).fetchone()
        content.add_widget(self.make_label("Insert record" if rowid is None else f"Edit rowid {rowid}",
                                            dp(14), TEXT, True))
        sc_grid = GridLayout(cols=1, spacing=dp(5), size_hint_y=None)
        sc_grid.bind(minimum_height=sc_grid.setter("height"))
        for col in info:
            cname = col["name"]
            if rowid is None and col["pk"] and "INT" in (col["type"] or "").upper():
                continue
            field = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(64), spacing=dp(2))
            field.add_widget(self.make_label(
                f"{cname}  •  {col['type'] or 'ANY'}" + ("  (NOT NULL)" if col["notnull"] else ""),
                dp(10), ACCENT, True, dp(23)))
            inp = TextInput(text="" if existing is None or existing[cname] is None
                            else str(existing[cname]),
                            hint_text="NULL / empty value", multiline=False,
                            size_hint_y=None, height=dp(37), font_size="12sp",
                            background_color=PANEL2, foreground_color=TEXT,
                            cursor_color=ACCENT)
            fields[cname] = inp
            field.add_widget(inp)
            sc_grid.add_widget(field)
        content.add_widget(self.scroll(sc_grid))
        pop = Popup(title="Record editor", content=content, size_hint=(.95, .88))
        def save(*_):
            names, values = [], []
            for col in info:
                cname = col["name"]
                if cname not in fields:
                    continue
                raw = fields[cname].text
                names.append(cname)
                values.append(None if raw == "" else raw)
            try:
                if rowid is None:
                    marks = ",".join("?" for _ in names)
                    self.manager.conn.execute(
                        f"INSERT INTO {qident(table)} ({','.join(qident(n) for n in names)}) VALUES ({marks})",
                        values)
                else:
                    assignments = ",".join(f"{qident(n)}=?" for n in names)
                    self.manager.conn.execute(
                        f"UPDATE {qident(table)} SET {assignments} WHERE rowid=?",
                        values + [rowid])
                self.manager.conn.commit()
                pop.dismiss()
                self.load_table_rows(self.table_search.text.strip() or None)
                self.set_status("Record saved")
            except Exception as e:
                self.notify(f"Save failed: {e}", True)
        content.add_widget(self.make_button("Save record", save, GREEN))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def delete_selected_row(self, *_):
        self.notify("Use the Delete action on the record you want to remove.")

    def _delete_row(self, rowid):
        try:
            self.manager.conn.execute(
                f"DELETE FROM {qident(self.current_table)} WHERE rowid=?", (rowid,))
            self.manager.conn.commit()
            self.load_table_rows(self.table_search.text.strip() or None)
            self.set_status(f"Record {rowid} deleted")
        except Exception as e:
            self.notify(f"Delete failed: {e}", True)

    def create_index_dialog(self, *_):
        content = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(10))
        idx_name = TextInput(hint_text="Index name", multiline=False, size_hint_y=None, height=dp(40))
        cols = [r["name"] for r in self.manager.table_info(self.current_table)]
        col_spinner = Spinner(text=cols[0] if cols else "", values=cols,
                               size_hint_y=None, height=dp(40))
        unique = Spinner(text="NON-UNIQUE", values=["NON-UNIQUE", "UNIQUE"],
                         size_hint_y=None, height=dp(40))
        content.add_widget(idx_name)
        content.add_widget(col_spinner)
        content.add_widget(unique)
        pop = Popup(title="Create index", content=content, size_hint=(.9, .5))
        def create(*_):
            if not idx_name.text.strip():
                return self.notify("Index name required", True)
            try:
                prefix = "UNIQUE " if unique.text == "UNIQUE" else ""
                self.manager.conn.execute(
                    f"CREATE {prefix}INDEX {qident(idx_name.text.strip())} "
                    f"ON {qident(self.current_table)} ({qident(col_spinner.text)})")
                self.manager.conn.commit()
                pop.dismiss()
                self.show_table_schema()
                self.set_status("Index created")
            except Exception as e:
                self.notify(str(e), True)
        content.add_widget(self.make_button("Create index", create, GREEN))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def backup_database(self, *_):
        if not self.manager.conn:
            return self.notify("No active database", True)
        try:
            folder = os.path.join(app_storage(), "backups")
            os.makedirs(folder, exist_ok=True)
            base = os.path.splitext(os.path.basename(self.manager.current_path))[0]
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = os.path.join(folder, f"{base}_{stamp}.db")
            dest = sqlite3.connect(target)
            self.manager.conn.backup(dest)
            dest.close()
            self.set_status(f"Backup created: {os.path.basename(target)}")
            self.notify(f"Backup saved:\n{target}")
        except Exception as e:
            self.notify(f"Backup failed: {e}", True)

    def restore_backup_dialog(self, *_):
        folder = os.path.join(app_storage(), "backups")
        os.makedirs(folder, exist_ok=True)
        files = [f for f in os.listdir(folder) if f.lower().endswith((".db", ".sqlite", ".sqlite3"))]
        if not files:
            return self.notify(f"No backups found in {folder}")
        content = BoxLayout(orientation="vertical", spacing=dp(5), padding=dp(9))
        pop = Popup(title="Restore backup (replaces current database)", content=content,
                    size_hint=(.94, .75))
        for filename in sorted(files, reverse=True):
            content.add_widget(self.make_button(filename,
                lambda *_ , f=os.path.join(folder, filename): (
                    pop.dismiss(), self.confirm(
                        f"Replace current database contents with {filename}?",
                        lambda: self.restore_backup(f), destructive=True)),
                PANEL2, dp(38)))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), RED))
        pop.open()

    def restore_backup(self, source):
        try:
            src = sqlite3.connect(source)
            src.backup(self.manager.conn)
            src.close()
            self.manager.conn.commit()
            self.show_dashboard()
            self.set_status("Backup restored")
        except Exception as e:
            self.notify(f"Restore failed: {e}", True)

    def integrity_check(self, *_):
        try:
            rows = self.manager.conn.execute("PRAGMA integrity_check").fetchall()
            result = "\n".join(str(r[0]) for r in rows)
            self.notify("INTEGRITY CHECK\n" + result[:1400],
                        error=not (len(rows) == 1 and rows[0][0] == "ok"))
        except Exception as e:
            self.notify(str(e), True)

    def analyze_database(self, *_):
        try:
            self.manager.conn.execute("ANALYZE")
            self.manager.conn.commit()
            self.notify("ANALYZE completed. SQLite query planner statistics were refreshed.")
            self.set_status("Database analyzed")
        except Exception as e:
            self.notify(str(e), True)

    def vacuum_database(self, *_):
        self.confirm("VACUUM rebuilds the database and may take time. Continue?",
                     self._do_vacuum, destructive=True)

    def _do_vacuum(self):
        try:
            self.manager.conn.execute("VACUUM")
            self.set_status("VACUUM completed")
            self.notify("Database rebuilt successfully.")
            self.show_dashboard()
        except Exception as e:
            self.notify(f"VACUUM failed: {e}", True)

    def export_current_dialog(self, *_):
        if not self.current_table:
            return self.notify("Open a table first")
        self.export_dialog(self.current_table)

    def export_dialog(self, table):
        content = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(10))
        fmt = Spinner(text="CSV", values=["CSV", "JSON"],
                      size_hint_y=None, height=dp(42))
        content.add_widget(self.make_label(f"Export table: {table}", dp(13), TEXT, True))
        content.add_widget(fmt)
        pop = Popup(title="Export data", content=content, size_hint=(.9, .4))
        def export(*_):
            try:
                rows = self.manager.conn.execute(f"SELECT * FROM {qident(table)}").fetchall()
                cols = [d["name"] for d in self.manager.table_info(table)]
                folder = os.path.join(app_storage(), "exports")
                os.makedirs(folder, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ext = fmt.text.lower()
                path = os.path.join(folder, f"{table}_{stamp}.{ext}")
                if ext == "json":
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump([dict(r) for r in rows], f, ensure_ascii=False, indent=2)
                else:
                    with open(path, "w", encoding="utf-8-sig", newline="") as f:
                        writer = csv.writer(f)
                        writer.writerow(cols)
                        writer.writerows([[r[c] for c in cols] for r in rows])
                pop.dismiss()
                self.notify(f"Exported {len(rows)} records:\n{path}")
                self.set_status("Export completed")
            except Exception as e:
                self.notify(f"Export failed: {e}", True)
        content.add_widget(self.make_button("Export", export, GREEN))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def import_csv_dialog(self, *_):
        content = BoxLayout(orientation="vertical", spacing=dp(7), padding=dp(10))
        path = TextInput(hint_text="Absolute path to CSV file", multiline=False,
                         size_hint_y=None, height=dp(42))
        table = TextInput(hint_text="Destination table name", multiline=False,
                          size_hint_y=None, height=dp(42))
        content.add_widget(self.make_label("Import CSV into a table", dp(14), TEXT, True))
        content.add_widget(path)
        content.add_widget(table)
        content.add_widget(self.make_label(
            "First CSV row must contain column names. Existing destination table required.",
            dp(10), MUTED))
        pop = Popup(title="CSV Import", content=content, size_hint=(.94, .53))
        def run(*_):
            try:
                if not os.path.isfile(path.text.strip()):
                    raise FileNotFoundError("CSV file not found")
                t = table.text.strip()
                if not t:
                    raise ValueError("Destination table name required")
                valid_cols = [r["name"] for r in self.manager.table_info(t)]
                with open(path.text.strip(), "r", encoding="utf-8-sig", newline="") as f:
                    reader = csv.DictReader(f)
                    if not reader.fieldnames:
                        raise ValueError("CSV header is missing")
                    unknown = [c for c in reader.fieldnames if c not in valid_cols]
                    if unknown:
                        raise ValueError("Unknown destination columns: " + ", ".join(unknown))
                    names = reader.fieldnames
                    marks = ",".join("?" for _ in names)
                    sql = f"INSERT INTO {qident(t)} ({','.join(qident(n) for n in names)}) VALUES ({marks})"
                    count = 0
                    with self.manager.conn:
                        for row in reader:
                            self.manager.conn.execute(sql, [row.get(n) or None for n in names])
                            count += 1
                pop.dismiss()
                self.notify(f"Imported {count} records into {t}")
                self.set_status("CSV import completed")
            except Exception as e:
                self.notify(f"Import failed: {e}", True)
        content.add_widget(self.make_button("Import", run, GREEN))
        content.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        pop.open()

    def database_details(self, *_):
        stats = self.manager.db_stats()
        details = [
            f"File: {self.manager.current_path}",
            f"File size: {human_size(os.path.getsize(self.manager.current_path))}",
            f"Logical size: {human_size(stats['size'])}",
            f"Tables: {stats['tables']}   Views: {stats['views']}",
            f"SQLite: {stats['version']}",
            f"Encoding: {stats['encoding']}",
            f"Journal mode: {stats['journal']}",
            f"Foreign keys: {'ON' if stats['foreign_keys'] else 'OFF'}",
            f"Free pages: {human_size(stats['free'])}",
        ]
        self.notify("\n".join(details))

    def notify(self, message, error=False):
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        lbl = Label(text=str(message), color=TEXT, font_size="12sp",
                    halign="left", valign="middle")
        lbl.bind(size=lambda inst, val: setattr(inst, "text_size", (val[0], None)))
        content.add_widget(lbl)
        pop = Popup(title="Error" if error else "DB MASTER PRO",
                    content=content, size_hint=(.92, .52 if len(str(message)) > 160 else .38))
        content.add_widget(self.make_button("OK", lambda *_: pop.dismiss(), RED if error else ACCENT_DARK))
        pop.open()
        if error:
            self.set_status(str(message).splitlines()[0][:90], True)

    def confirm(self, message, callback, destructive=False):
        content = BoxLayout(orientation="vertical", spacing=dp(10), padding=dp(12))
        lbl = Label(text=message, color=TEXT, font_size="12sp",
                    halign="left", valign="middle")
        lbl.bind(size=lambda inst, val: setattr(inst, "text_size", (val[0], None)))
        content.add_widget(lbl)
        row = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(7))
        pop = Popup(title="Confirm operation", content=content, size_hint=(.92, .42))
        row.add_widget(self.make_button("Confirm", lambda *_: (pop.dismiss(), callback()),
                                        RED if destructive else GREEN))
        row.add_widget(self.make_button("Cancel", lambda *_: pop.dismiss(), PANEL2))
        content.add_widget(row)
        pop.open()

    def on_stop(self):
        self.manager.close_all()


if __name__ == "__main__":
    DBMasterApp().run()
