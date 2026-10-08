-- Sample dump used by the data migration tests.
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO users (id, email, active) VALUES
    (1, 'alice@example.com', TRUE),
    (2, 'bob@example.com', FALSE);
