INSERT INTO users (name, email) VALUES
    ('Ada', 'ada@example.org'),
    ('Grace', 'grace@example.org');

SELECT id, name FROM users WHERE id = 2;
SELECT id, name FROM users WHERE email = 'ada@example.org';
