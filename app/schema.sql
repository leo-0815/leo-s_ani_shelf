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
    series_key VARCHAR(190) NULL,
    volume_label VARCHAR(80) NULL,
    edition_type VARCHAR(40) NOT NULL DEFAULT 'standard',
    media_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    content_rating VARCHAR(30) NOT NULL DEFAULT 'unknown',
    rating_raw VARCHAR(100) NULL,
    rating_source VARCHAR(30) NOT NULL DEFAULT 'unknown',
    rating_confidence TINYINT UNSIGNED NOT NULL DEFAULT 0,
    rating_locked BOOLEAN NOT NULL DEFAULT FALSE,
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
    KEY idx_books_series (publisher_id, series_key),
    KEY idx_books_rating (content_rating, rating_locked),
    CONSTRAINT fk_books_publisher FOREIGN KEY (publisher_id) REFERENCES publishers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS catalog_changes (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    book_id BIGINT UNSIGNED NOT NULL,
    change_type VARCHAR(30) NOT NULL,
    change_origin VARCHAR(30) NOT NULL DEFAULT 'crawler',
    source_hash CHAR(64) NOT NULL,
    changed_fields VARCHAR(500) NOT NULL DEFAULT '*',
    changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_catalog_changes_book (book_id, id),
    KEY idx_catalog_changes_origin (change_origin, id),
    CONSTRAINT fk_catalog_changes_book FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS catalog_sync_state (
    peer_code VARCHAR(100) NOT NULL,
    last_pulled_change_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    last_pushed_change_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    last_verified_at DATETIME NULL,
    last_success_at DATETIME NULL,
    last_error TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (peer_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS users (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    provider VARCHAR(30) NOT NULL,
    provider_subject VARCHAR(190) NOT NULL,
    email VARCHAR(320) NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    avatar_url VARCHAR(1000) NULL,
    role VARCHAR(20) NOT NULL DEFAULT 'user',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_provider_subject (provider, provider_subject),
    UNIQUE KEY uq_users_email (email),
    KEY idx_users_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash CHAR(64) NOT NULL,
    user_id BIGINT UNSIGNED NOT NULL,
    csrf_token CHAR(64) NOT NULL,
    expires_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (token_hash),
    KEY idx_sessions_user (user_id),
    KEY idx_sessions_expiry (expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS oauth_login_states (
    state_hash CHAR(64) NOT NULL,
    nonce VARCHAR(100) NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (state_hash),
    KEY idx_oauth_states_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS wishlist_items (
    user_id BIGINT UNSIGNED NOT NULL,
    book_id BIGINT UNSIGNED NOT NULL,
    state VARCHAR(30) NOT NULL DEFAULT 'wanted',
    notes TEXT NULL,
    follow_series BOOLEAN NOT NULL DEFAULT FALSE,
    priority TINYINT UNSIGNED NOT NULL DEFAULT 0,
    store_name VARCHAR(200) NULL,
    order_number VARCHAR(200) NULL,
    paid_price INT UNSIGNED NULL,
    owned_format VARCHAR(20) NOT NULL DEFAULT 'paper',
    purchased_at DATE NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, book_id),
    KEY idx_wishlist_state (user_id, state),
    KEY idx_wishlist_book (book_id),
    CONSTRAINT fk_wishlist_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_wishlist_book FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS followed_series (
    user_id BIGINT UNSIGNED NOT NULL,
    publisher_id BIGINT UNSIGNED NOT NULL,
    series_title VARCHAR(500) NOT NULL,
    normalized_series VARCHAR(190) NOT NULL,
    media_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, publisher_id, normalized_series, media_type),
    KEY idx_followed_series_publisher (publisher_id),
    CONSTRAINT fk_followed_series_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_followed_series_publisher FOREIGN KEY (publisher_id) REFERENCES publishers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS series_aliases (
    publisher_id BIGINT UNSIGNED NOT NULL,
    alias_key VARCHAR(190) NOT NULL,
    canonical_title VARCHAR(500) NOT NULL,
    canonical_key VARCHAR(190) NOT NULL,
    match_method VARCHAR(30) NOT NULL DEFAULT 'manual',
    confidence TINYINT UNSIGNED NOT NULL DEFAULT 100,
    approved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (publisher_id, alias_key),
    KEY idx_series_alias_canonical (publisher_id, canonical_key),
    CONSTRAINT fk_series_alias_publisher FOREIGN KEY (publisher_id) REFERENCES publishers(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS recommendation_dismissals (
    user_id BIGINT UNSIGNED NOT NULL,
    book_id BIGINT UNSIGNED NOT NULL,
    reason_type VARCHAR(40) NOT NULL DEFAULT 'all',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, book_id),
    KEY idx_recommendation_dismissal_book (book_id),
    CONSTRAINT fk_recommendation_dismissal_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NULL,
    book_id BIGINT UNSIGNED NULL,
    channel VARCHAR(30) NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    event_key VARCHAR(255) NOT NULL,
    delivered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_notification_delivery (channel, event_key),
    KEY idx_notification_user (user_id, delivered_at),
    KEY idx_notification_book (book_id, delivered_at),
    CONSTRAINT fk_notification_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT fk_notification_book FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS notification_preferences (
    user_id BIGINT UNSIGNED NOT NULL,
    email_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    lead_days VARCHAR(100) NOT NULL DEFAULT '7,3,1,0',
    notify_release_date_changes BOOLEAN NOT NULL DEFAULT TRUE,
    notify_followed_series BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    KEY idx_notification_preferences_email (email_enabled, user_id),
    CONSTRAINT fk_notification_preferences_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS collection_items (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
    book_id BIGINT UNSIGNED NULL,
    title VARCHAR(500) NULL,
    author VARCHAR(500) NULL,
    publisher_name VARCHAR(200) NULL,
    media_type VARCHAR(40) NOT NULL DEFAULT 'unknown',
    isbn VARCHAR(30) NULL,
    edition_type VARCHAR(100) NULL,
    release_date DATE NULL,
    owned_format VARCHAR(20) NOT NULL DEFAULT 'paper',
    purchased_at DATE NULL,
    store_name VARCHAR(200) NULL,
    order_number VARCHAR(200) NULL,
    paid_price INT UNSIGNED NULL,
    notes TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY idx_collection_user_book (user_id, book_id),
    KEY idx_collection_user (user_id),
    CONSTRAINT fk_collection_book FOREIGN KEY (book_id) REFERENCES books(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
