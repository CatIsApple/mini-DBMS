-- id is auto-assigned
INSERT INTO users (name, age, email) VALUES ('Alice', 30, 'alice@example.com'), ('Bob', 25, NULL);
INSERT INTO users (name, age, email) VALUES ('O''Brien', 41, 'ob@example.com');
SELECT * FROM users;
SELECT name, age FROM users WHERE id = 2;
SELECT * FROM users WHERE age >= 30 AND (name = 'Alice' OR email IS NULL) LIMIT 5;
SELECT COUNT(*) FROM users WHERE id > 1;
EXPLAIN SELECT * FROM users WHERE id = 2;
