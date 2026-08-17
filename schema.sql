PRAGMA foreign_keys = ON;

CREATE TABLE pages (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    see_also TEXT
);

CREATE TABLE sections (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL,
    number TEXT NOT NULL,
    title TEXT NOT NULL,
    FOREIGN KEY (page_id) REFERENCES pages(id)
);

CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    section_id INTEGER NOT NULL,
    number TEXT,
    source_text TEXT NOT NULL,
    visual_context TEXT,
    semantic_meaning TEXT,
    bbox_left REAL CHECK (bbox_left IS NULL OR bbox_left BETWEEN 0 AND 1),
    bbox_top REAL CHECK (bbox_top IS NULL OR bbox_top BETWEEN 0 AND 1),
    bbox_right REAL CHECK (bbox_right IS NULL OR bbox_right BETWEEN 0 AND 1),
    bbox_bottom REAL CHECK (bbox_bottom IS NULL OR bbox_bottom BETWEEN 0 AND 1),
    audio_url TEXT,
    FOREIGN KEY (section_id) REFERENCES sections(id)
);
