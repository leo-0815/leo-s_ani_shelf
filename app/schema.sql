CREATE TABLE IF NOT EXISTS publishers (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code VARCHAR(40) NOT NULL,
    name VARCHAR(100) NOT NULL,
    homepage_url VARCHAR(500) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    last_success_at DATETIME NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_publishers_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS books (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    publisher_id BIGINT UNSIGNED NOT NULL,
    source_key VARCHAR(190) NOT NULL,
    title VARCHAR(500) NOT NULL,
    normalized_title VARCHAR(500) NOT NULL,
    series_title VARCHAR(500) NULL,
    volume_label VARCHAR(80) NULL,
    edition_type VARCHAR(40) NOT NULL DEFAULT 'standard',
    media_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    author VARCHAR(500) NULL,
    isbn VARCHAR(32) NULL,
    cover_url VARCHAR(1000) NULL,
    list_price INT UNSIGNED NULL,
    release_date DATE NULL,
    release_precision VARCHAR(20) NOT NULL DEFAULT 'unknown',
    release_status VARCHAR(30) NOT NULL DEFAULT 'unknown',
    source_url VARCHAR(1000) NOT NULL,
    source_hash CHAR(64) NOT NULL,
    first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_books_source (publisher_id, source_key),
    KEY idx_books_release (release_date, release_status),
    KEY idx_books_isbn (isbn),
    KEY idx_books_title (normalized_title(190)),
    CONSTRAINT fk_books_publisher FOREIGN KEY (publisher_id) REFERENCES publishers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS wishlist_items (
    book_id BIGINT UNSIGNED NOT NULL,
    state VARCHAR(30) NOT NULL DEFAULT 'wanted',
    notes TEXT NULL,
    follow_series BOOLEAN NOT NULL DEFAULT FALSE,
    priority TINYINT UNSIGNED NOT NULL DEFAULT 0,
    store_name VARCHAR(200) NULL,
    order_number VARCHAR(200) NULL,
    paid_price INT UNSIGNED NULL,
    owned_format VARCHAR(20) NOT NULL DEFAULT 'paper',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (book_id),
    KEY idx_wishlist_state (state),
    CONSTRAINT fk_wishlist_book FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS followed_series (
    publisher_id BIGINT UNSIGNED NOT NULL,
    series_title VARCHAR(500) NOT NULL,
    normalized_series VARCHAR(190) NOT NULL,
    media_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (publisher_id, normalized_series),
    CONSTRAINT fk_followed_series_publisher FOREIGN KEY (publisher_id) REFERENCES publishers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS recommendation_dismissals (
    book_id BIGINT UNSIGNED NOT NULL,
    reason_type VARCHAR(40) NOT NULL DEFAULT 'all',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (book_id),
    CONSTRAINT fk_recommendation_dismissal_book
        FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS release_history (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    book_id BIGINT UNSIGNED NOT NULL,
    field_name VARCHAR(50) NOT NULL,
    old_value VARCHAR(1000) NULL,
    new_value VARCHAR(1000) NULL,
    observed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_history_book (book_id, observed_at),
    CONSTRAINT fk_history_book FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS crawl_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_code VARCHAR(40) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    discovered_count INT UNSIGNED NOT NULL DEFAULT 0,
    inserted_count INT UNSIGNED NOT NULL DEFAULT 0,
    updated_count INT UNSIGNED NOT NULL DEFAULT 0,
    skipped_count INT UNSIGNED NOT NULL DEFAULT 0,
    error_count INT UNSIGNED NOT NULL DEFAULT 0,
    message TEXT NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_jobs_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS source_sync_state (
    source_code VARCHAR(40) NOT NULL,
    cursor_value VARCHAR(1000) NULL,
    cursor_date DATE NULL,
    last_mode VARCHAR(20) NOT NULL DEFAULT 'incremental',
    backfill_completed BOOLEAN NOT NULL DEFAULT FALSE,
    last_success_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS source_backfill_progress (
    source_code VARCHAR(40) NOT NULL,
    segment VARCHAR(100) NOT NULL,
    next_page INT UNSIGNED NOT NULL DEFAULT 1,
    completed BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (source_code, segment)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO publishers (code, name, homepage_url, enabled)
VALUES
    ('tohan', '台灣東販', 'https://www.tohan.com.tw/', TRUE),
    ('chingwin', '青文出版社', 'https://www.ching-win.com.tw/', TRUE),
    ('kadokawa', '台灣角川', 'https://www.kadokawa.com.tw/', TRUE),
    ('tongli', '東立出版社', 'https://www.tongli.com.tw/', TRUE),
    ('spp', '尖端出版', 'https://www.spp.com.tw/', TRUE),
    ('egmanga', '長鴻出版社', 'https://www.egmanga.com.tw/', TRUE);

UPDATE publishers
SET enabled = TRUE
WHERE code IN ('tohan', 'chingwin', 'kadokawa', 'tongli', 'spp', 'egmanga');
