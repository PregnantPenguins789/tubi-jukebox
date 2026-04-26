#!/usr/bin/env python3
"""
tubi_jukebox.py - Terminal jukebox for Tubi movie catalog
Requires: pip install textual --break-system-packages

Usage:
  python3 tubi_jukebox.py              # uses tubi.db in current dir
  python3 tubi_jukebox.py /path/to/tubi.db

Keys:
  / or ctrl+f  - search
  g            - genre filter
  p            - add to program block
  P            - view/manage program block
  enter        - open in browser
  q            - quit
"""

import sqlite3
import subprocess
import sys
import os
from datetime import datetime
from textual.app import App, ComposeResult
from textual.widgets import (
    Header, Footer, DataTable, Input, Label,
    ListItem, ListView, Static, Button, Log
)
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from textual import events
from textual.reactive import reactive

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "tubi.db"

GENRES = [
    "All", "Action", "Animation", "Comedy", "Crime", "Documentary",
    "Drama", "Fantasy", "Horror", "Music", "Mystery", "Romance",
    "Sci-Fi", "Short", "Thriller", "Western", "Foreign"
]

# ─────────────────────────────────────────────────────────────
# Database layer
# ─────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def search_movies(query="", genre="All", limit=500):
    conn = get_db()
    sql = """
        SELECT id, title, year, rating, duration_minutes,
               genres_raw, directors_raw, actors_raw, url
        FROM movies
        WHERE 1=1
    """
    params = []

    if genre and genre != "All":
        sql += " AND genres_raw LIKE ?"
        params.append(f"%{genre}%")

    if query:
        sql += """ AND (
            title LIKE ? OR
            directors_raw LIKE ? OR
            actors_raw LIKE ? OR
            description LIKE ?
        )"""
        q = f"%{query}%"
        params.extend([q, q, q, q])

    sql += " ORDER BY year DESC, title LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows

def get_movie(movie_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM movies WHERE id = ?", (movie_id,)
    ).fetchone()
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

def open_url(url):
    """Open Tubi URL in default browser"""
    subprocess.Popen(["xdg-open", url],
                     stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)

# ─────────────────────────────────────────────────────────────
# Program Block (the "TV Guide schedule" concept)
# ─────────────────────────────────────────────────────────────

class ProgramBlock:
    """In-memory queue of movies to watch tonight"""
    def __init__(self):
        self.items = []  # list of dicts

    def add(self, movie_row):
        item = {
            "id": movie_row["id"],
            "title": movie_row["title"],
            "year": movie_row["year"],
            "duration": movie_row["duration_minutes"] or 0,
            "url": movie_row["url"],
            "genres": movie_row["genres_raw"] or "",
        }
        # Avoid duplicates
        if not any(i["id"] == item["id"] for i in self.items):
            self.items.append(item)
            return True
        return False

    def remove(self, idx):
        if 0 <= idx < len(self.items):
            self.items.pop(idx)

    def move_up(self, idx):
        if idx > 0:
            self.items[idx-1], self.items[idx] = \
                self.items[idx], self.items[idx-1]

    def move_down(self, idx):
        if idx < len(self.items) - 1:
            self.items[idx], self.items[idx+1] = \
                self.items[idx+1], self.items[idx]

    def total_duration(self):
        return sum(i["duration"] for i in self.items)

    def save(self, path="program_block.txt"):
        with open(path, "w") as f:
            f.write(f"# Tubi Program Block — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            f.write(f"# Total runtime: {self.total_duration()} minutes\n\n")
            for i, item in enumerate(self.items, 1):
                f.write(f"{i}. {item['title']} ({item['year']}) "
                        f"— {item['duration']}min\n   {item['url']}\n\n")

program = ProgramBlock()

# ─────────────────────────────────────────────────────────────
# Movie detail modal
# ─────────────────────────────────────────────────────────────

class MovieDetail(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("enter", "play", "Open in Browser"),
        Binding("p", "add_to_program", "Add to Program"),
    ]

    def __init__(self, movie_id):
        super().__init__()
        self.movie_id = movie_id
        self.movie = get_movie(movie_id)

    def compose(self) -> ComposeResult:
        m = self.movie
        dur = f"{m['duration_minutes']}min" if m['duration_minutes'] else "?"
        yield Vertical(
            Static(f"[bold cyan]{m['title']}[/bold cyan]", id="detail-title"),
            Static(f"[yellow]{m['year']}[/yellow]  {m['rating'] or ''}  {dur}"),
            Static(f"[green]{m['genres_raw'] or ''}[/green]"),
            Static(""),
            Static(f"[dim]Director:[/dim] {m['directors_raw'] or 'Unknown'}"),
            Static(f"[dim]Cast:[/dim] {m['actors_raw'] or 'Unknown'}"),
            Static(""),
            Static(f"{m['description'] or ''}", id="detail-desc"),
            Static(""),
            Static(f"[dim]{m['url']}[/dim]"),
            Static(""),
            Static("[bold]ENTER[/bold] Open in Browser  "
                   "[bold]P[/bold] Add to Program Block  "
                   "[bold]ESC[/bold] Back"),
            id="detail-box"
        )

    def action_play(self):
        open_url(self.movie["url"])
        self.dismiss()

    def action_add_to_program(self):
        added = program.add(self.movie)
        msg = "Added to program!" if added else "Already in program"
        self.notify(msg)

    CSS = """
    MovieDetail {
        align: center middle;
    }
    #detail-box {
        background: $surface;
        border: thick $accent;
        padding: 2 4;
        width: 70;
        max-height: 30;
    }
    #detail-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #detail-desc {
        color: $text-muted;
        width: 60;
    }
    """

# ─────────────────────────────────────────────────────────────
# Program Block screen
# ─────────────────────────────────────────────────────────────

class ProgramScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "dismiss", "Back"),
        Binding("enter", "play_selected", "Play"),
        Binding("d,delete", "remove_selected", "Remove"),
        Binding("u", "move_up", "Move Up"),
        Binding("n", "move_down", "Move Down"),
        Binding("s", "save_block", "Save to file"),
    ]

    def compose(self) -> ComposeResult:
        total_min = program.total_duration()
        hours = total_min // 60
        mins = total_min % 60
        yield Header()
        yield Static(
            f"[bold]Tonight's Program Block[/bold]  —  "
            f"{len(program.items)} titles  •  "
            f"{hours}h {mins}m total runtime",
            id="prog-header"
        )
        yield DataTable(id="prog-table")
        yield Static(
            "[dim]ENTER[/dim] play  [dim]D[/dim] remove  "
            "[dim]U[/dim] up  [dim]N[/dim] down  "
            "[dim]S[/dim] save  [dim]Q[/dim] back",
            id="prog-footer"
        )

    def on_mount(self):
        self.refresh_table()

    def refresh_table(self):
        table = self.query_one("#prog-table", DataTable)
        table.clear(columns=True)
        table.add_columns("#", "Title", "Year", "Min", "Genres")
        for i, item in enumerate(program.items, 1):
            table.add_row(
                str(i),
                item["title"],
                str(item["year"] or ""),
                str(item["duration"]),
                item["genres"][:30],
                key=str(item["id"])
            )

    def action_play_selected(self):
        table = self.query_one("#prog-table", DataTable)
        row_key = table.cursor_row
        if row_key < len(program.items):
            open_url(program.items[row_key]["url"])

    def action_remove_selected(self):
        table = self.query_one("#prog-table", DataTable)
        idx = table.cursor_row
        program.remove(idx)
        self.refresh_table()

    def action_move_up(self):
        table = self.query_one("#prog-table", DataTable)
        idx = table.cursor_row
        program.move_up(idx)
        self.refresh_table()
        table.move_cursor(row=max(0, idx-1))

    def action_move_down(self):
        table = self.query_one("#prog-table", DataTable)
        idx = table.cursor_row
        program.move_down(idx)
        self.refresh_table()
        table.move_cursor(row=min(len(program.items)-1, idx+1))

    def action_save_block(self):
        program.save()
        self.notify("Saved to program_block.txt")

    CSS = """
    ProgramScreen {
        background: $background;
    }
    #prog-header {
        padding: 1 2;
        background: $primary-darken-2;
    }
    #prog-footer {
        padding: 0 2;
        background: $surface;
        dock: bottom;
    }
    #prog-table {
        height: 1fr;
    }
    """

# ─────────────────────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────────────────────

class TubiJukebox(App):
    TITLE = "Tubi Jukebox"
    CSS_PATH = None
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+f,/", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear"),
        Binding("p", "view_program", "Program Block"),
        Binding("1", "genre_all", "All"),
        Binding("2", "genre_horror", "Horror"),
        Binding("3", "genre_action", "Action"),
        Binding("4", "genre_comedy", "Comedy"),
        Binding("5", "genre_documentary", "Documentary"),
        Binding("6", "genre_thriller", "Thriller"),
        Binding("7", "genre_classics", "Pre-1970"),
    ]

    current_genre = reactive("All")
    current_query = reactive("")

    CSS = """
    TubiJukebox {
        background: $background;
    }
    #top-bar {
        height: 3;
        background: $primary-darken-3;
        padding: 0 1;
    }
    #search-input {
        width: 40;
        margin: 0 2;
    }
    #genre-label {
        padding: 1 1;
        color: $accent;
    }
    #stats-label {
        padding: 1 1;
        color: $text-muted;
        text-align: right;
        width: 1fr;
    }
    #prog-count {
        padding: 1 1;
        color: $warning;
    }
    #main-table {
        height: 1fr;
        border: none;
    }
    #status-bar {
        height: 1;
        background: $surface;
        padding: 0 2;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        total, yr_min, yr_max = get_stats()
        yield Header(show_clock=True)
        yield Horizontal(
            Input(placeholder="Search title, director, actor...",
                  id="search-input"),
            Label("Genre: All", id="genre-label"),
            Label(f"📺 {total:,} movies  {yr_min}–{yr_max}", id="stats-label"),
            Label("🎬 0 queued", id="prog-count"),
            id="top-bar"
        )
        yield DataTable(id="main-table", cursor_type="row")
        yield Static(
            "ENTER detail/play  P add to program  /  search  "
            "1-7 genre filters  P view program",
            id="status-bar"
        )
        yield Footer()

    def on_mount(self):
        table = self.query_one("#main-table", DataTable)
        table.add_columns(
            "ID", "Title", "Year", "Rat", "Min", "Genres", "Director"
        )
        table.focus()
        self.load_movies()

    def load_movies(self):
        table = self.query_one("#main-table", DataTable)
        table.clear()
        rows = search_movies(
            query=self.current_query,
            genre=self.current_genre
        )
        for row in rows:
            dur = str(row["duration_minutes"]) if row["duration_minutes"] else ""
            genres = (row["genres_raw"] or "")[:25]
            director = (row["directors_raw"] or "").split("|")[0][:20]
            table.add_row(
                str(row["id"]),
                (row["title"] or "")[:45],
                str(row["year"] or ""),
                row["rating"] or "",
                dur,
                genres,
                director,
                key=str(row["id"])
            )
        genre_label = self.query_one("#genre-label", Label)
        genre_label.update(f"Genre: {self.current_genre}  [{len(rows)}]")

    def update_prog_count(self):
        label = self.query_one("#prog-count", Label)
        n = len(program.items)
        label.update(f"🎬 {n} queued")

    def on_input_changed(self, event: Input.Changed):
        self.current_query = event.value
        self.load_movies()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        # Row selected = show detail modal
        movie_id = int(event.row_key.value)
        self.push_screen(MovieDetail(movie_id))

    def on_key(self, event: events.Key):
        # Quick-add to program with 'a' key from main table
        if event.key == "a":
            table = self.query_one("#main-table", DataTable)
            if table.cursor_row is not None:
                row_key = table.get_row_at(table.cursor_row)
                movie_id = int(table.get_cell_at(
                    (table.cursor_row, 0)
                ))
                movie = get_movie(movie_id)
                if movie and program.add(movie):
                    self.notify(f"Added: {movie['title']}")
                    self.update_prog_count()

    def action_focus_search(self):
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self):
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        self.current_query = ""
        self.query_one("#main-table", DataTable).focus()
        self.load_movies()

    def action_view_program(self):
        self.push_screen(ProgramScreen())
        self.update_prog_count()

    def _set_genre(self, genre):
        self.current_genre = genre
        self.load_movies()

    def action_genre_all(self):       self._set_genre("All")
    def action_genre_horror(self):    self._set_genre("Horror")
    def action_genre_action(self):    self._set_genre("Action")
    def action_genre_comedy(self):    self._set_genre("Comedy")
    def action_genre_documentary(self): self._set_genre("Documentary")
    def action_genre_thriller(self):  self._set_genre("Thriller")
    def action_genre_classics(self):
        # Override search for pre-1970
        self.current_genre = "All"
        conn = get_db()
        rows = conn.execute(
            "SELECT id, title, year, rating, duration_minutes, "
            "genres_raw, directors_raw, url FROM movies "
            "WHERE year > 0 AND year < 1970 ORDER BY year DESC LIMIT 500"
        ).fetchall()
        conn.close()
        table = self.query_one("#main-table", DataTable)
        table.clear()
        for row in rows:
            dur = str(row["duration_minutes"]) if row["duration_minutes"] else ""
            table.add_row(
                str(row["id"]),
                (row["title"] or "")[:45],
                str(row["year"] or ""),
                row["rating"] or "",
                dur,
                (row["genres_raw"] or "")[:25],
                (row["directors_raw"] or "").split("|")[0][:20],
                key=str(row["id"])
            )
        self.query_one("#genre-label", Label).update(
            f"Genre: Pre-1970  [{len(rows)}]"
        )


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found: {DB_PATH}")
        print("Run tubi_setup.sh first to create tubi.db from your CSV.")
        sys.exit(1)

    print(f"Opening {DB_PATH}...")
    app = TubiJukebox()
    app.run()
