#!/usr/bin/env python3
"""
tubi_jukebox_advanced.py — Tubi Movie Jukebox with Advanced Search
Adds power search syntax: year:1950-1980 genre:horror vampire
"""

import sqlite3
import subprocess
import sys
import os
import re
from datetime import datetime
from textual.app import App, ComposeResult
from textual.widgets import (
    Header, Footer, DataTable, Input, Label,
    Static, Button, Select, RadioSet, RadioButton
)
from textual.containers import Horizontal, Vertical, ScrollableContainer, Grid
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from textual import events
from textual.reactive import reactive

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "tubi.db"

# SSH target — set via Settings screen, persisted in config table
DEFAULT_SSH_HOST = ""
DEFAULT_BROWSER  = "xdg-open"

GENRES = [
    "All", "Action", "Adventure", "Animation", "Anime", "Biography",
    "Comedy", "Crime", "Documentary", "Drama", "Fantasy", "Film Noir",
    "Foreign/International", "History", "Holiday", "Horror", "Independent",
    "Kids & Family", "LGBT", "Music", "Musicals", "Mystery", "Romance",
    "Sci-Fi", "Science & Nature", "Sport", "Thriller", "War", "Western"
]

SORT_OPTIONS = [
    ("Year ↓ (newest first)", "year DESC"),
    ("Year ↑ (oldest first)", "year ASC"),
    ("Title A–Z",             "title ASC"),
    ("Title Z–A",             "title DESC"),
    ("Runtime ↑ (shortest)",  "duration_minutes ASC"),
    ("Runtime ↓ (longest)",   "duration_minutes DESC"),
]

# ─────────────────────────────────────────────────────────────
# Database — movies + lists schema
# ─────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_lists_schema():
    """Create list/config tables if they don't exist yet."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS saved_lists (
            list_name   TEXT NOT NULL,
            movie_id    INTEGER NOT NULL,
            position    INTEGER DEFAULT 0,
            added_date  TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (list_name, movie_id)
        );

        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        INSERT OR IGNORE INTO config (key, value)
            VALUES ('ssh_host', ''),
                   ('browser', 'xdg-open');
    """)
    conn.commit()
    conn.close()

def get_config(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else ""

def set_config(key, value):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

def get_list_names():
    conn = get_db()
    rows = conn.execute(
        "SELECT list_name, COUNT(*) as cnt FROM saved_lists "
        "GROUP BY list_name ORDER BY list_name"
    ).fetchall()
    conn.close()
    return rows

def get_list_movies(list_name, sort_order="year DESC"):
    conn = get_db()
    rows = conn.execute(f"""
        SELECT m.id, m.title, m.year, m.rating, m.duration_minutes,
               m.genres_raw, m.directors_raw, m.actors_raw, m.url
        FROM movies m
        JOIN saved_lists sl ON m.id = sl.movie_id
        WHERE sl.list_name = ?
        ORDER BY {sort_order}
    """, (list_name,)).fetchall()
    conn.close()
    return rows

def add_to_list(list_name, movie_id):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO saved_lists (list_name, movie_id) VALUES (?,?)",
            (list_name, movie_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        conn.close()
        return False

def remove_from_list(list_name, movie_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM saved_lists WHERE list_name=? AND movie_id=?",
        (list_name, movie_id)
    )
    conn.commit()
    conn.close()

def delete_list(list_name):
    conn = get_db()
    conn.execute("DELETE FROM saved_lists WHERE list_name=?", (list_name,))
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────
# Advanced Search Parser
# ─────────────────────────────────────────────────────────────

def parse_advanced_query(query):
    """
    Parse queries like:
    - year:1950-1980 vampire
    - genre:horror lon chaney
    - director:"brian de palma" year:1980-
    - actor:"lon chaney" year:1950-1960
    """
    if not query:
        return {"text": "", "filters": {}}
    
    filters = {
        "year_min": None,
        "year_max": None,
        "genre": None,
        "director": None,
        "actor": None,
        "text_parts": []
    }
    
    # Split while preserving quoted strings
    import shlex
    try:
        tokens = shlex.split(query)
    except:
        # Fallback to simple split if shlex fails
        tokens = query.split()
    
    for token in tokens:
        if ':' in token:
            key, value = token.split(':', 1)
            key = key.lower()
            
            # Skip if value is empty - treat as regular text
            if not value:
                filters["text_parts"].append(token)
                continue
            
            if key == 'year':
                if '-' in value:
                    parts = value.split('-')
                    # Handle cases like "year:1950-" or "year:-1980"
                    if parts[0]:
                        try:
                            filters["year_min"] = int(parts[0])
                        except ValueError:
                            filters["text_parts"].append(token)
                            continue
                    if len(parts) > 1 and parts[1]:
                        try:
                            filters["year_max"] = int(parts[1])
                        except ValueError:
                            filters["text_parts"].append(token)
                            continue
                else:
                    # Single year
                    try:
                        year = int(value)
                        filters["year_min"] = filters["year_max"] = year
                    except ValueError:
                        # Not a valid number, treat as regular text
                        filters["text_parts"].append(token)
                        continue
                    
            elif key == 'genre':
                filters["genre"] = value
                
            elif key == 'director':
                filters["director"] = value
                
            elif key == 'actor':
                filters["actor"] = value
            else:
                # Unknown field, treat as regular text
                filters["text_parts"].append(token)
        else:
            filters["text_parts"].append(token)
    
    filters["text"] = ' '.join(filters["text_parts"])
    return filters

# ─────────────────────────────────────────────────────────────
# Movie search with advanced parsing
# ─────────────────────────────────────────────────────────────

def search_movies(query="", genre="All", year_min=None, year_max=None,
                  director=None, actor=None, sort_order="year DESC", 
                  limit=1000, show_unavailable=False):

    if ':' in query:
        parsed = parse_advanced_query(query)
        text_query = parsed["text"]
        if parsed["year_min"] is not None:
            year_min = parsed["year_min"]
        if parsed["year_max"] is not None:
            year_max = parsed["year_max"]
        if parsed["genre"]:
            genre = parsed["genre"]
        if parsed["director"]:
            director = parsed["director"]
        if parsed["actor"]:
            actor = parsed["actor"]
    else:
        text_query = query

    conn = get_db()
    sql = """
        SELECT id, title, year, rating, duration_minutes,
               genres_raw, directors_raw, actors_raw, url
        FROM movies WHERE 1=1
    """
    params = []

    if not show_unavailable:
        sql += " AND (available = 1 OR available IS NULL)"

    if year_min is not None:
        sql += " AND year >= ?"
        params.append(year_min)
    if year_max is not None:
        sql += " AND year <= ?"
        params.append(year_max)
    if genre and genre != "All":
        sql += " AND genres_raw LIKE ?"
        params.append(f"%{genre}%")
    if director:
        sql += " AND directors_raw LIKE ?"
        params.append(f"%{director}%")
    if actor:
        sql += " AND actors_raw LIKE ?"
        params.append(f"%{actor}%")
    if text_query.strip():
        sql += """ AND (title LIKE ? OR directors_raw LIKE ?
                       OR actors_raw LIKE ? OR description LIKE ?)"""
        q = f"%{text_query.strip()}%"
        params.extend([q, q, q, q])

    sql += f" ORDER BY {sort_order} LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def get_movie(movie_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    conn.close()
    return row

def get_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    years = conn.execute(
        "SELECT MIN(year), MAX(year) FROM movies WHERE year > 0"
    ).fetchone()
    conn.close()
    return total, years[0], years[1]

# ─────────────────────────────────────────────────────────────
# Playback
# ─────────────────────────────────────────────────────────────

def play_local(url):
    browser = get_config("browser") or "xdg-open"
    subprocess.Popen([browser, url],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def play_remote(url):
    ssh_host = get_config("ssh_host")
    if not ssh_host:
        return False
    browser = get_config("browser") or "xdg-open"
    subprocess.Popen(
        ["ssh", ssh_host, f"DISPLAY=:0 {browser} '{url}'"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return True

# ─────────────────────────────────────────────────────────────
# Program Block (in-memory queue)
# ─────────────────────────────────────────────────────────────

class ProgramBlock:
    def __init__(self):
        self.items = []

    def add(self, movie_row):
        item = {
            "id":       movie_row["id"],
            "title":    movie_row["title"],
            "year":     movie_row["year"],
            "duration": movie_row["duration_minutes"] or 0,
            "url":      movie_row["url"],
            "genres":   movie_row["genres_raw"] or "",
        }
        if not any(i["id"] == item["id"] for i in self.items):
            self.items.append(item)
            return True
        return False

    def remove(self, idx):
        if 0 <= idx < len(self.items):
            self.items.pop(idx)

    def move_up(self, idx):
        if idx > 0:
            self.items[idx-1], self.items[idx] = self.items[idx], self.items[idx-1]

    def move_down(self, idx):
        if idx < len(self.items) - 1:
            self.items[idx], self.items[idx+1] = self.items[idx+1], self.items[idx]

    def total_duration(self):
        return sum(i["duration"] for i in self.items)

    def save_to_list(self, list_name):
        for item in self.items:
            add_to_list(list_name, item["id"])

    def save_txt(self, path="program_block.txt"):
        total = self.total_duration()
        with open(path, "w") as f:
            f.write(f"# Tubi Program Block — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"# {len(self.items)} titles  •  {total//60}h {total%60}m\n\n")
            for i, item in enumerate(self.items, 1):
                f.write(f"{i}. {item['title']} ({item['year']}) "
                        f"— {item['duration']}min\n   {item['url']}\n\n")

program = ProgramBlock()

# ─────────────────────────────────────────────────────────────
# Shared table renderer
# ─────────────────────────────────────────────────────────────

def populate_table(table, rows):
    table.clear()
    for row in rows:
        dur = str(row["duration_minutes"]) if row["duration_minutes"] else ""
        genres = (row["genres_raw"] or "")[:28]
        director = (row["directors_raw"] or "").split("|")[0][:22]
        table.add_row(
            str(row["id"]),
            (row["title"] or "")[:48],
            str(row["year"] or ""),
            row["rating"] or "",
            dur,
            genres,
            director,
            key=str(row["id"])
        )

# ─────────────────────────────────────────────────────────────
# Filter Modal
# ─────────────────────────────────────────────────────────────

class FilterScreen(ModalScreen):
    """Year range, genre, sort order."""

    BINDINGS = [
        Binding("escape", "dismiss", "Cancel"),
        Binding("enter", "apply", "Apply"),
    ]

    def __init__(self, current_filters):
        super().__init__()
        self.filters = dict(current_filters)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("[bold cyan]── Filter & Sort ──[/bold cyan]", id="filter-title"),
            Static(""),
            Static("[dim]Year range (leave blank for all):[/dim]"),
            Horizontal(
                Input(
                    value=str(self.filters.get("year_min") or ""),
                    placeholder="from e.g. 1950",
                    id="year-min"
                ),
                Static("  –  "),
                Input(
                    value=str(self.filters.get("year_max") or ""),
                    placeholder="to e.g. 1980",
                    id="year-max"
                ),
            ),
            Static(""),
            Static("[dim]Genre:[/dim]"),
            Select(
                [(g, g) for g in GENRES],
                value=self.filters.get("genre", "All"),
                id="genre-select"
            ),
            Static(""),
            Static("[dim]Sort by:[/dim]"),
            Select(
                [(label, val) for label, val in SORT_OPTIONS],
                value=self.filters.get("sort_order", "year DESC"),
                id="sort-select"
            ),
            Static(""),
            Horizontal(
                Button("Apply  [Enter]", variant="primary", id="btn-apply"),
                Button("Clear Filters", variant="default", id="btn-clear"),
                Button("Cancel [Esc]",  variant="default", id="btn-cancel"),
            ),
            id="filter-box"
        )

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-apply":
            self.action_apply()
        elif event.button.id == "btn-clear":
            self.dismiss({
                "genre": "All",
                "year_min": None,
                "year_max": None,
                "sort_order": "year DESC"
            })
        elif event.button.id == "btn-cancel":
            self.dismiss(None)

    def action_apply(self):
        year_min_raw = self.query_one("#year-min", Input).value.strip()
        year_max_raw = self.query_one("#year-max", Input).value.strip()
        genre        = self.query_one("#genre-select", Select).value
        sort_order   = self.query_one("#sort-select", Select).value

        result = {
            "genre":      genre if genre else "All",
            "year_min":   int(year_min_raw) if year_min_raw.isdigit() else None,
            "year_max":   int(year_max_raw) if year_max_raw.isdigit() else None,
            "sort_order": sort_order or "year DESC",
        }
        self.dismiss(result)

    def action_dismiss(self):
        self.dismiss(None)

    CSS = """
    FilterScreen { align: center middle; }
    #filter-box {
        background: $surface;
        border: thick $accent;
        padding: 2 4;
        width: 60;
        height: auto;
    }
    #filter-title { text-align: center; margin-bottom: 1; }
    Select { width: 100%; }
    Input  { width: 20; }
    Button { margin-right: 1; }
    """

# ─────────────────────────────────────────────────────────────
# Add-to-list modal
# ─────────────────────────────────────────────────────────────

class AddToListScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, movie_id, movie_title):
        super().__init__()
        self.movie_id    = movie_id
        self.movie_title = movie_title

    def compose(self) -> ComposeResult:
        list_names = [r["list_name"] for r in get_list_names()]
        yield Vertical(
            Static(f"[bold cyan]Add to list:[/bold cyan]"),
            Static(f"[yellow]{self.movie_title}[/yellow]"),
            Static(""),
            Static("[dim]Add to program block:[/dim]"),
            Button("+ Program Block", id="btn-program", variant="primary"),
            Static(""),
            Static("[dim]Add to saved list:[/dim]"),
            # Fix: Use index-based IDs and store the actual list name in data
            *[Button(f"  {name}", id=f"list_{i}", data_list=name) for i, name in enumerate(list_names)],
            Static(""),
            Static("[dim]Create new list:[/dim]"),
            Input(placeholder="New list name...", id="new-list-name"),
            Button("Create & Add", id="btn-new-list"),
            Static(""),
            Button("Cancel [Esc]", id="btn-cancel"),
            id="addlist-box"
        )

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        movie = get_movie(self.movie_id)

        if bid == "btn-program":
            added = program.add(movie)
            self.dismiss(("program", added))
        elif bid and bid.startswith("list_"):
            # Get the actual list name from the button's data attribute
            list_name = event.button.data_list
            if list_name:
                add_to_list(list_name, self.movie_id)
                self.dismiss(("list", list_name))
        elif bid == "btn-new-list":
            name = self.query_one("#new-list-name", Input).value.strip()
            if name:
                add_to_list(name, self.movie_id)
                self.dismiss(("list", name))
        elif bid == "btn-cancel":
            self.dismiss(None)

    CSS = """
    AddToListScreen { align: center middle; }
    #addlist-box {
        background: $surface;
        border: thick $accent;
        padding: 2 3;
        width: 50;
        max-height: 40;
    }
    Button { width: 100%; margin-bottom: 1; }
    Input  { width: 100%; }
    """

# ─────────────────────────────────────────────────────────────
# Movie Detail Modal
# ─────────────────────────────────────────────────────────────

class MovieDetail(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Back"),
        Binding("l", "play_local", "Play Here"),
        Binding("t", "play_tv", "Play on TV"),
        Binding("a", "add_to", "Add to..."),
    ]

    def __init__(self, movie_id):
        super().__init__()
        self.movie_id = movie_id
        self.movie    = get_movie(movie_id)

    def compose(self) -> ComposeResult:
        m   = self.movie
        dur = f"{m['duration_minutes']}min" if m['duration_minutes'] else "?"
        ssh = get_config("ssh_host")
        tv_label = f"[bold]T[/bold] Play on TV ({ssh})" if ssh else "[dim]T Play on TV (no SSH set)[/dim]"

        yield Vertical(
            Static(f"[bold cyan]{m['title']}[/bold cyan]", id="d-title"),
            Static(f"[yellow]{m['year']}[/yellow]   {m['rating'] or ''}   {dur}"),
            Static(f"[green]{(m['genres_raw'] or '').replace('|', '  ·  ')}[/green]"),
            Static(""),
            Static(f"[dim]Director:[/dim]  {(m['directors_raw'] or 'Unknown').replace('|',', ')}"),
            Static(f"[dim]Cast:[/dim]      {(m['actors_raw'] or 'Unknown').replace('|',', ')}"),
            Static(""),
            Static(f"{m['description'] or ''}", id="d-desc"),
            Static(""),
            Static(f"[dim]{m['url']}[/dim]"),
            Static(""),
            Horizontal(
                Button("▶ Play Here [L]", id="btn-local", variant="primary"),
                Button("📺 Play on TV [T]", id="btn-tv",
                       variant="success" if ssh else "default"),
                Button("+ Add to... [A]", id="btn-add"),
                Button("Back [Esc]", id="btn-back"),
            ),
            id="detail-box"
        )

    def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        if bid == "btn-local":   self.action_play_local()
        elif bid == "btn-tv":    self.action_play_tv()
        elif bid == "btn-add":   self.action_add_to()
        elif bid == "btn-back":  self.dismiss()

    def action_play_local(self):
        play_local(self.movie["url"])
        self.dismiss()

    def action_play_tv(self):
        ok = play_remote(self.movie["url"])
        if ok:
            self.notify(f"Sent to TV: {self.movie['title']}")
            self.dismiss()
        else:
            self.notify("No SSH host set — go to Settings (S)", severity="warning")

    def action_add_to(self):
        self.app.push_screen(
            AddToListScreen(self.movie_id, self.movie["title"]),
            self._after_add
        )

    def _after_add(self, result):
        if result:
            kind, detail = result
            if kind == "program":
                msg = f"Added to program block" if detail else "Already in program block"
            else:
                msg = f"Added to list: {detail}"
            self.notify(msg)

    CSS = """
    MovieDetail { align: center middle; }
    #detail-box {
        background: $surface;
        border: thick $accent;
        padding: 2 4;
        width: 75;
        max-height: 35;
    }
    #d-title { text-style: bold; margin-bottom: 1; }
    #d-desc  { color: $text-muted; width: 65; }
    Button   { margin-right: 1; margin-top: 1; }
    """

# ─────────────────────────────────────────────────────────────
# Program Block Screen
# ─────────────────────────────────────────────────────────────

class ProgramScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("l",        "play_local",     "Play Here"),
        Binding("t",        "play_tv",        "Play on TV"),
        Binding("d",        "remove",         "Remove"),
        Binding("u",        "move_up",        "Up"),
        Binding("n",        "move_down",      "Down"),
        Binding("s",        "save_txt",       "Save .txt"),
        Binding("w",        "save_to_list",   "Save as List"),
        Binding("c",        "clear_all",      "Clear All"),
    ]

    
    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Handle row selection in program block"""
        movie_id = int(event.row_key.value)
        self.app.push_screen(MovieDetail(movie_id))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="prog-header")
        yield DataTable(id="prog-table", cursor_type="row")
        yield Static(
            "[dim]L[/dim] play here  [dim]T[/dim] play on TV  "
            "[dim]D[/dim] remove  [dim]U/N[/dim] reorder  "
            "[dim]S[/dim] save txt  [dim]W[/dim] save as list  "
            "[dim]C[/dim] clear  [dim]Q[/dim] back",
            id="prog-footer"
        )

    def on_mount(self):
        table = self.query_one("#prog-table", DataTable)
        table.add_columns("#", "Title", "Year", "Min", "Genres")
        self.refresh_all()

    def refresh_all(self):
        total = program.total_duration()
        self.query_one("#prog-header", Static).update(
            f"[bold]Program Block[/bold]  —  "
            f"{len(program.items)} titles  •  "
            f"{total//60}h {total%60}m total"
        )
        table = self.query_one("#prog-table", DataTable)
        table.clear()
        for i, item in enumerate(program.items, 1):
            table.add_row(
                str(i), item["title"][:50],
                str(item["year"] or ""),
                str(item["duration"]),
                item["genres"][:30],
                key=str(item["id"])
            )

    def _current_idx(self):
        return self.query_one("#prog-table", DataTable).cursor_row

    def _current_item(self):
        idx = self._current_idx()
        if 0 <= idx < len(program.items):
            return program.items[idx]
        return None

    def action_play_local(self):
        item = self._current_item()
        if item: play_local(item["url"])

    def action_play_tv(self):
        item = self._current_item()
        if not item: return
        ok = play_remote(item["url"])
        self.notify("Sent to TV" if ok else "No SSH host set", 
                    severity="information" if ok else "warning")

    def action_remove(self):
        idx = self._current_idx()
        program.remove(idx)
        self.refresh_all()

    def action_move_up(self):
        idx = self._current_idx()
        program.move_up(idx)
        self.refresh_all()
        self.query_one("#prog-table", DataTable).move_cursor(row=max(0, idx-1))

    def action_move_down(self):
        idx = self._current_idx()
        program.move_down(idx)
        self.refresh_all()
        n = len(program.items)
        self.query_one("#prog-table", DataTable).move_cursor(row=min(n-1, idx+1))

    def action_save_txt(self):
        program.save_txt()
        self.notify("Saved: program_block.txt")

    def action_save_to_list(self):
        self.app.push_screen(
            NameInputScreen("Save program as list named:"),
            self._do_save_list
        )

    def _do_save_list(self, name):
        if name:
            program.save_to_list(name)
            self.notify(f"Saved as list: {name}")

    def action_clear_all(self):
        program.items.clear()
        self.refresh_all()

    CSS = """
    ProgramScreen { background: $background; }
    #prog-header  { padding: 1 2; background: $primary-darken-2; }
    #prog-footer  { padding: 0 2; background: $surface; dock: bottom; }
    #prog-table   { height: 1fr; }
    """

# ─────────────────────────────────────────────────────────────
# My Lists Screen
# ─────────────────────────────────────────────────────────────

class ListsScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("enter",    "open_list",      "Open"),
        Binding("d",        "delete_list",    "Delete List"),
        Binding("n",        "new_list",       "New List"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("[bold]My Saved Lists[/bold]", id="lists-header")
        yield DataTable(id="lists-table", cursor_type="row")
        yield Static(
            "[dim]ENTER[/dim] open  [dim]D[/dim] delete  "
            "[dim]N[/dim] new list  [dim]Q[/dim] back",
            id="lists-footer"
        )

    def on_mount(self):
        table = self.query_one("#lists-table", DataTable)
        table.add_columns("List Name", "Movies")
        self.refresh_lists()

    def refresh_lists(self):
        table = self.query_one("#lists-table", DataTable)
        table.clear()
        for row in get_list_names():
            table.add_row(row["list_name"], str(row["cnt"]),
                          key=row["list_name"])

    def action_open_list(self):
        table = self.query_one("#lists-table", DataTable)
        if table.cursor_row is None: return
        cell = table.get_cell_at((table.cursor_row, 0))
        self.app.push_screen(SingleListScreen(cell))

    def action_delete_list(self):
        table = self.query_one("#lists-table", DataTable)
        if table.cursor_row is None: return
        name = table.get_cell_at((table.cursor_row, 0))
        delete_list(name)
        self.refresh_lists()
        self.notify(f"Deleted: {name}")

    def action_new_list(self):
        self.app.push_screen(
            NameInputScreen("New list name:"),
            lambda name: self.notify(f"List '{name}' ready — add movies via detail view") if name else None
        )

    CSS = """
    ListsScreen  { background: $background; }
    #lists-header{ padding: 1 2; background: $primary-darken-2; }
    #lists-footer{ padding: 0 2; background: $surface; dock: bottom; }
    #lists-table { height: 1fr; }
    """

# ─────────────────────────────────────────────────────────────
# Single List Screen
# ─────────────────────────────────────────────────────────────

class SingleListScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("enter",    "open_detail",    "Detail"),
        Binding("l",        "play_local",     "Play Here"),
        Binding("t",        "play_tv",        "Play on TV"),
        Binding("d",        "remove_movie",   "Remove"),
        Binding("p",        "to_program",     "→ Program"),
        Binding("f",        "filter",         "Filter/Sort"),
    ]

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        """Handle row selection - only for main table"""
        # Check if the focused widget is the main table
        focused = self.focused
        if focused and hasattr(focused, 'id') and focused.id == "main-table":
            movie_id = int(event.row_key.value)
            self.push_screen(MovieDetail(movie_id))
        # Otherwise, let the focused screen handle it (do nothing here)

    def __init__(self, list_name):
        super().__init__()
        self.list_name  = list_name
        self.sort_order = "year DESC"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="slist-header")
        yield DataTable(id="slist-table", cursor_type="row")
        yield Static(
            "[dim]ENTER[/dim] detail  [dim]L[/dim] play  [dim]T[/dim] TV  "
            "[dim]D[/dim] remove  [dim]P[/dim] → program  [dim]F[/dim] sort  [dim]Q[/dim] back",
            id="slist-footer"
        )

    def on_mount(self):
        table = self.query_one("#slist-table", DataTable)
        table.add_columns("ID", "Title", "Year", "Rat", "Min", "Genres", "Director")
        self.refresh_list()

    def refresh_list(self):
        rows = get_list_movies(self.list_name, self.sort_order)
        self.query_one("#slist-header", Static).update(
            f"[bold]{self.list_name}[/bold]  —  {len(rows)} movies"
        )
        populate_table(self.query_one("#slist-table", DataTable), rows)

    def _current_id(self):
        table = self.query_one("#slist-table", DataTable)
        if table.cursor_row is None: return None
        return int(table.get_cell_at((table.cursor_row, 0)))

    def action_open_detail(self):
        mid = self._current_id()
        if mid: self.app.push_screen(MovieDetail(mid))

    def action_play_local(self):
        mid = self._current_id()
        if mid: play_local(get_movie(mid)["url"])

    def action_play_tv(self):
        mid = self._current_id()
        if not mid: return
        ok = play_remote(get_movie(mid)["url"])
        self.notify("Sent to TV" if ok else "No SSH host set",
                    severity="information" if ok else "warning")

    def action_remove_movie(self):
        mid = self._current_id()
        if mid:
            remove_from_list(self.list_name, mid)
            self.refresh_list()

    def action_to_program(self):
        mid = self._current_id()
        if mid:
            movie = get_movie(mid)
            added = program.add(movie)
            self.notify("Added to program" if added else "Already in program")

    def action_filter(self):
        filters = {"genre": "All", "year_min": None,
                   "year_max": None, "sort_order": self.sort_order}
        self.app.push_screen(FilterScreen(filters), self._apply_filter)

    def _apply_filter(self, result):
        if result:
            self.sort_order = result.get("sort_order", "year DESC")
            self.refresh_list()

    CSS = """
    SingleListScreen { background: $background; }
    #slist-header    { padding: 1 2; background: $primary-darken-2; }
    #slist-footer    { padding: 0 2; background: $surface; dock: bottom; }
    #slist-table     { height: 1fr; }
    """

# ─────────────────────────────────────────────────────────────
# Settings Screen
# ─────────────────────────────────────────────────────────────

class SettingsScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Cancel"),
        Binding("ctrl+s", "save",    "Save"),
    ]

    def compose(self) -> ComposeResult:
        ssh  = get_config("ssh_host") or ""
        brow = get_config("browser")  or "xdg-open"
        yield Vertical(
            Static("[bold cyan]── Settings ──[/bold cyan]"),
            Static(""),
            Static("[dim]SSH host for TV playback:[/dim]"),
            Static("[dim]  Format: user@192.168.1.x  (leave blank for local only)[/dim]"),
            Input(value=ssh, placeholder="user@192.168.1.x", id="ssh-input"),
            Static(""),
            Static("[dim]Browser command:[/dim]"),
            Input(value=brow, placeholder="xdg-open", id="browser-input"),
            Static(""),
            Static("[dim]SSH tip: set up key-based auth so no password is needed[/dim]"),
            Static("[dim]  ssh-copy-id user@192.168.1.x[/dim]"),
            Static(""),
            Horizontal(
                Button("Save [Ctrl+S]", variant="primary", id="btn-save"),
                Button("Cancel [Esc]",  variant="default", id="btn-cancel"),
            ),
            id="settings-box"
        )

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-save":    self.action_save()
        elif event.button.id == "btn-cancel": self.dismiss()

    def action_save(self):
        set_config("ssh_host", self.query_one("#ssh-input", Input).value.strip())
        set_config("browser",  self.query_one("#browser-input", Input).value.strip())
        self.notify("Settings saved")
        self.dismiss()

    CSS = """
    SettingsScreen { align: center middle; }
    #settings-box {
        background: $surface;
        border: thick $accent;
        padding: 2 4;
        width: 65;
        height: auto;
    }
    Input  { width: 100%; margin-bottom: 1; }
    Button { margin-right: 1; }
    """

# ─────────────────────────────────────────────────────────────
# Name input helper modal
# ─────────────────────────────────────────────────────────────

class NameInputScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Cancel"),
        Binding("enter",  "confirm", "OK"),
    ]

    def __init__(self, prompt):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"[bold]{self.prompt}[/bold]"),
            Input(id="name-input"),
            Horizontal(
                Button("OK [Enter]",   variant="primary", id="btn-ok"),
                Button("Cancel [Esc]", variant="default", id="btn-cancel"),
            ),
            id="name-box"
        )

    def on_mount(self):
        self.query_one("#name-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "btn-ok":     self.action_confirm()
        elif event.button.id == "btn-cancel": self.dismiss(None)

    def action_confirm(self):
        val = self.query_one("#name-input", Input).value.strip()
        self.dismiss(val if val else None)

    CSS = """
    NameInputScreen { align: center middle; }
    #name-box {
        background: $surface;
        border: thick $accent;
        padding: 2 4;
        width: 50;
        height: auto;
    }
    Input  { width: 100%; margin-bottom: 1; }
    Button { margin-right: 1; }
    """

# ─────────────────────────────────────────────────────────────
# Main App
# ─────────────────────────────────────────────────────────────

class TubiJukebox(App):
    TITLE = "Tubi Jukebox v2 (Advanced Search)"

    BINDINGS = [
        Binding("u",     "toggle_unavailable", "Show Unavailable"),
        Binding("q",     "quit",         "Quit"),
        Binding("/",     "focus_search", "Search"),
        Binding("f",     "open_filter",  "Filter"),
        Binding("l",     "open_lists",   "My Lists"),
        Binding("p",     "open_program", "Program"),
        Binding("s",     "open_settings","Settings"),
        Binding("escape","clear_search", "Clear"),
        Binding("1",     "genre_all",    "All"),
        Binding("2",     "genre_horror", "Horror"),
        Binding("3",     "genre_action", "Action"),
        Binding("4",     "genre_comedy", "Comedy"),
        Binding("5",     "genre_doc",    "Doc"),
        Binding("6",     "genre_noir",   "Film Noir"),
        Binding("7",     "genre_western","Western"),
        Binding("8",     "genre_foreign","Foreign"),
    ]

    current_query  = reactive("")
    current_genre  = reactive("All")
    current_ymin   = reactive(None)
    current_ymax   = reactive(None)
    current_sort   = reactive("year DESC")
    current_director = reactive(None)
    current_actor    = reactive(None)
    show_unavailable = reactive(False)

    CSS = """
    TubiJukebox { background: $background; }

    #top-bar {
        height: 3;
        background: $primary-darken-3;
        padding: 0 1;
        align: left middle;
    }
    #search-input {
        width: 45;
        margin: 0 1;
    }
    #filter-label {
        padding: 0 1;
        color: $accent;
    }
    #count-label {
        padding: 0 1;
        color: $text-muted;
        width: 1fr;
        content-align: right middle;
    }
    #prog-label {
        padding: 0 1;
        color: $warning;
    }
    #main-table {
        height: 1fr;
    }
    #status-bar {
        height: 1;
        background: $surface;
        padding: 0 2;
        color: $text-muted;
    }
    #search-hint {
        color: $text-muted;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        total, yr_min, yr_max = get_stats()
        yield Header(show_clock=True)
        yield Horizontal(
            Input(placeholder="Search (try: year:1950-1980 genre:horror vampire)",
                  id="search-input"),
            Label("", id="filter-label"),
            Label(f"📺 {total:,} movies  {yr_min}–{yr_max}", id="count-label"),
            Label("🎬 0", id="prog-label"),
            id="top-bar"
        )
        yield DataTable(id="main-table", cursor_type="row")
        yield Static(
            "[dim]ENTER[/dim] detail  [dim]A[/dim] add to...  "
            "[dim]/[/dim] search  [dim]F[/dim] filter  "
            "[dim]L[/dim] lists  [dim]P[/dim] program  [dim]S[/dim] settings  "
            "[dim]1-8[/dim] genres  [dim]Q[/dim] quit  "
            "[dim]U[/dim] toggle unavailable  "
            "[dim]Search syntax: year:1950-1980 genre:horror director:\"de palma\"[/dim]",
            id="status-bar"
        )
        yield Footer()

    def on_mount(self):
        table = self.query_one("#main-table", DataTable)
        table.add_columns("ID", "Title", "Year", "Rat", "Min", "Genres", "Director")
        table.focus()
        self.load_movies()

    def load_movies(self):
        rows = search_movies(
            query=self.current_query,
            genre=self.current_genre,
            year_min=self.current_ymin,
            year_max=self.current_ymax,
            director=self.current_director,
            actor=self.current_actor,
            sort_order=self.current_sort,
            show_unavailable=self.show_unavailable,
        )

        populate_table(self.query_one("#main-table", DataTable), rows)

        # Update filter label
        parts = []
        parts.append(self.current_genre if self.current_genre != "All" else "All")
        if self.current_ymin or self.current_ymax:
            y1 = self.current_ymin or "?"
            y2 = self.current_ymax or "?"
            parts.append(f"{y1}–{y2}")
        if self.current_director:
            parts.append(f"dir:{self.current_director}")
        if self.current_actor:
            parts.append(f"actor:{self.current_actor}")
        if not self.show_unavailable:
            parts.append("✅ available only")
        sort_label = next((l for l, v in SORT_OPTIONS if v == self.current_sort), "")
        parts.append(sort_label)
        self.query_one("#filter-label", Label).update(
            "  |  ".join(parts) + f"  [{len(rows)}]"
        )

    def update_prog_label(self):
        n = len(program.items)
        self.query_one("#prog-label", Label).update(f"🎬 {n}")

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search-input":
            self.current_query = event.value
            # Reset field filters when using advanced syntax
            if ':' in event.value:
                self.current_genre = "All"
                self.current_ymin = None
                self.current_ymax = None
                self.current_director = None
                self.current_actor = None
            self.load_movies()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        movie_id = int(event.row_key.value)
        self.push_screen(MovieDetail(movie_id))

    def on_key(self, event: events.Key):
        if event.key == "a":
            table = self.query_one("#main-table", DataTable)
            if table.cursor_row is not None:
                try:
                    movie_id = int(table.get_cell_at((table.cursor_row, 0)))
                    movie = get_movie(movie_id)
                    if movie:
                        self.push_screen(
                            AddToListScreen(movie_id, movie["title"]),
                            self._after_add
                        )
                except Exception:
                    pass

    def _after_add(self, result):
        if result:
            kind, detail = result
            if kind == "program":
                self.update_prog_label()
                msg = "Added to program block" if detail else "Already in program block"
            else:
                msg = f"Added to list: {detail}"
            self.notify(msg)

    def action_toggle_unavailable(self):
        self.show_unavailable = not self.show_unavailable
        self.load_movies()
        self.notify(
            f"Showing {'all' if self.show_unavailable else 'available only'} movies",
            severity="information"
        )

    def action_focus_search(self):
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self):
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self.current_query = ""
        self.current_genre = "All"
        self.current_ymin  = None
        self.current_ymax  = None
        self.current_director = None
        self.current_actor = None
        self.query_one("#main-table", DataTable).focus()
        self.load_movies()

    def action_open_filter(self):
        current = {
            "genre":      self.current_genre,
            "year_min":   self.current_ymin,
            "year_max":   self.current_ymax,
            "sort_order": self.current_sort,
        }
        self.push_screen(FilterScreen(current), self._apply_filter)

    def _apply_filter(self, result):
        if result is None: return
        self.current_genre = result.get("genre", "All")
        self.current_ymin  = result.get("year_min")
        self.current_ymax  = result.get("year_max")
        self.current_sort  = result.get("sort_order", "year DESC")
        self.load_movies()

    def action_open_lists(self):
        self.push_screen(ListsScreen())

    def action_open_program(self):
        self.push_screen(ProgramScreen())
        self.update_prog_label()

    def action_open_settings(self):
        self.push_screen(SettingsScreen())

    def _set_genre(self, genre):
        self.current_genre = genre
        self.load_movies()

    def action_genre_all(self):     self._set_genre("All")
    def action_genre_horror(self):  self._set_genre("Horror")
    def action_genre_action(self):  self._set_genre("Action")
    def action_genre_comedy(self):  self._set_genre("Comedy")
    def action_genre_doc(self):     self._set_genre("Documentary")
    def action_genre_noir(self):    self._set_genre("Film Noir")
    def action_genre_western(self): self._set_genre("Western")
    def action_genre_foreign(self): self._set_genre("Foreign/International")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found: {DB_PATH}")
        print("Run tubi_setup.sh first.")
        sys.exit(1)

    init_lists_schema()
    app = TubiJukebox()
    app.run()