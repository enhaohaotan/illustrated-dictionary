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
    forms TEXT,
    visual_context TEXT,
    semantic_meaning TEXT,
    FOREIGN KEY (section_id) REFERENCES sections(id)
);
