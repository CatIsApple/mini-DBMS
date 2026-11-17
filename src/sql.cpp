#include "sql.hpp"

#include <utility>

namespace datom {

DbError::DbError(std::string error_code, std::string message)
    : std::runtime_error(std::move(message)), code(std::move(error_code)) {}

namespace {

bool is_letter(char ch) {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
}

bool is_digit(char ch) {
    return ch >= '0' && ch <= '9';
}

bool is_space(char ch) {
    return ch == ' ' || (ch >= '\t' && ch <= '\r');
}

[[noreturn]] void syntax_error(std::size_t position, std::string message) {
    throw DbError("42601", std::move(message) + " at byte " + std::to_string(position + 1));
}

void validate_encoding(std::string_view input) {
    for (std::size_t i = 0; i < input.size();) {
        const auto lead = static_cast<unsigned char>(input[i]);
        if (lead == 0) syntax_error(i, "unexpected NUL character");
        if (lead < 0x80) {
            ++i;
            continue;
        }
        auto invalid = [&] {
            throw DbError("22021", "invalid UTF-8 sequence at byte " + std::to_string(i + 1));
        };
        if (lead < 0xc2 || lead > 0xf4) invalid();
        const std::size_t width = lead < 0xe0 ? 2 : lead < 0xf0 ? 3 : 4;
        if (input.size() - i < width) invalid();
        for (std::size_t n = 1; n < width; ++n) {
            const auto byte = static_cast<unsigned char>(input[i + n]);
            if (byte < 0x80 || byte > 0xbf) invalid();
        }
        // Disallow overlong encodings, UTF-16 surrogates, and values above U+10FFFF.
        const auto second = static_cast<unsigned char>(input[i + 1]);
        if ((lead == 0xe0 && second < 0xa0) || (lead == 0xed && second > 0x9f) ||
            (lead == 0xf0 && second < 0x90) || (lead == 0xf4 && second > 0x8f)) invalid();
        i += width;
    }
}

enum class Type {
    Word, QuotedIdentifier, String, Integer, Star, Comma, Open, Close, Equal, EndStatement, End
};

struct Token {
    Type type;
    std::size_t position;
    std::string text;
};

class Lexer {
public:
    explicit Lexer(std::string_view input) : input_(input) {
        validate_encoding(input_);
    }

    Token next() {
        skip_trivia();
        const auto start = offset_;
        if (start == input_.size()) {
            return {Type::End, start, {}};
        }

        const char ch = input_[offset_++];
        if (is_letter(ch) || ch == '_') {
            while (offset_ < input_.size()) {
                const char c = input_[offset_];
                if (!is_letter(c) && !is_digit(c) && c != '_' && c != '$') {
                    break;
                }
                ++offset_;
            }
            std::string word(input_.substr(start, offset_ - start));
            for (char& c : word) {
                if (c >= 'A' && c <= 'Z') {
                    c = static_cast<char>(c + ('a' - 'A'));
                }
            }
            return {Type::Word, start, std::move(word)};
        }
        if (is_digit(ch) || ((ch == '+' || ch == '-') &&
                             offset_ < input_.size() && is_digit(input_[offset_]))) {
            while (offset_ < input_.size() && is_digit(input_[offset_])) {
                ++offset_;
            }
            return {Type::Integer, start, std::string(input_.substr(start, offset_ - start))};
        }

        switch (ch) {
        case '\'': return quoted(Type::String, start, ch);
        case '"': return quoted(Type::QuotedIdentifier, start, ch);
        case '*': return {Type::Star, start, {}};
        case ',': return {Type::Comma, start, {}};
        case '(': return {Type::Open, start, {}};
        case ')': return {Type::Close, start, {}};
        case '=': return {Type::Equal, start, {}};
        case ';': return {Type::EndStatement, start, {}};
        default: syntax_error(start, "unexpected character");
        }
    }

private:
    void skip_trivia() {
        while (offset_ < input_.size()) {
            if (is_space(input_[offset_])) {
                ++offset_;
            } else if (input_.substr(offset_, 2) == "--") {
                offset_ = input_.find_first_of("\r\n", offset_ + 2);
                if (offset_ == std::string_view::npos) {
                    offset_ = input_.size();
                }
            } else if (input_.substr(offset_, 2) == "/*") {
                skip_block_comment();
            } else {
                return;
            }
        }
    }

    void skip_block_comment() {
        const auto start = offset_;
        offset_ += 2;
        std::size_t depth = 1;
        while (offset_ < input_.size()) {
            const auto pair = input_.substr(offset_, 2);
            if (pair == "*/") {
                offset_ += 2;
                if (--depth == 0) {
                    return;
                }
            } else if (pair == "/*") {
                offset_ += 2;
                ++depth;
            } else {
                ++offset_;
            }
        }
        syntax_error(start, "unterminated block comment");
    }

    Token quoted(Type type, std::size_t start, char delimiter) {
        std::string decoded;
        while (offset_ < input_.size()) {
            const char ch = input_[offset_++];
            if (ch == delimiter) {
                if (offset_ == input_.size() || input_[offset_] != delimiter) {
                    if (type == Type::QuotedIdentifier && decoded.empty()) {
                        syntax_error(start, "empty quoted identifier");
                    }
                    return {type, start, std::move(decoded)};
                }
                ++offset_;
            }
            decoded.push_back(ch);
        }
        syntax_error(start, type == Type::String ? "unterminated string" : "unterminated identifier");
    }

    std::string_view input_;
    std::size_t offset_ = 0;
};

class Parser {
public:
    explicit Parser(std::string_view input) : lexer_(input), token_(lexer_.next()) {}

    void parse(const std::function<void(Statement&&)>& visit) {
        while (token_.type != Type::End) {
            if (consume(Type::EndStatement)) {
                continue;
            }
            Statement statement{};
            if (keyword("insert")) {
                statement = insert();
            } else if (keyword("select")) {
                statement = select();
            } else {
                fail("expected INSERT or SELECT");
            }
            if (token_.type != Type::End && token_.type != Type::EndStatement) {
                fail("expected ';' between statements");
            }
            visit(std::move(statement));
        }
    }

private:
    [[noreturn]] void fail(std::string message) const {
        syntax_error(token_.position, std::move(message));
    }

    bool consume(Type type) {
        if (token_.type != type) {
            return false;
        }
        token_ = lexer_.next();
        return true;
    }

    void expect(Type type, const char* message) {
        if (!consume(type)) {
            fail(message);
        }
    }

    bool keyword(std::string_view name) {
        if (token_.type != Type::Word || token_.text != name) {
            return false;
        }
        token_ = lexer_.next();
        return true;
    }

    std::string take_text() {
        std::string value = std::move(token_.text);
        token_ = lexer_.next();
        return value;
    }

    std::string identifier() {
        if (token_.type == Type::QuotedIdentifier) {
            return take_text();
        }
        if (token_.type == Type::Word) {
            for (const auto* keyword : {"select", "from", "where", "insert", "into", "values"}) {
                if (token_.text == keyword) {
                    fail("expected an identifier");
                }
            }
            return take_text();
        }
        fail("expected an identifier");
    }

    std::string literal() {
        if (token_.type != Type::String && token_.type != Type::Integer) {
            fail("expected a string or integer literal");
        }
        return take_text();
    }

    std::vector<std::string> columns() {
        std::vector<std::string> result;
        do {
            result.push_back(identifier());
        } while (consume(Type::Comma));
        return result;
    }

    Statement insert() {
        Statement stmt{};
        stmt.kind = Statement::Kind::Insert;
        if (!keyword("into")) {
            fail("expected INTO after INSERT");
        }
        stmt.table = identifier();
        if (consume(Type::Open)) {
            stmt.columns = columns();
            expect(Type::Close, "expected ')' after column list");
        }
        if (!keyword("values")) {
            fail("expected VALUES");
        }
        do {
            expect(Type::Open, "expected '(' before row values");
            Row row;
            do {
                row.push_back(literal());
            } while (consume(Type::Comma));
            expect(Type::Close, "expected ')' after row values");
            stmt.values.push_back(std::move(row));
        } while (consume(Type::Comma));
        return stmt;
    }

    Statement select() {
        Statement stmt{};
        stmt.kind = Statement::Kind::Select;
        if (!consume(Type::Star)) {
            stmt.columns = columns();
        }
        if (!keyword("from")) {
            fail("expected FROM after SELECT list");
        }
        stmt.table = identifier();
        if (keyword("where")) {
            auto column = identifier();
            expect(Type::Equal, "expected '=' in WHERE condition");
            stmt.where = Predicate{std::move(column), literal()};
        }
        return stmt;
    }

    Lexer lexer_;
    Token token_;
};

} // namespace

void for_each_statement(std::string_view sql, const std::function<void(Statement&&)>& visit) {
    Parser(sql).parse(visit);
}

std::vector<Statement> parse_sql(std::string_view sql) {
    std::vector<Statement> result;
    for_each_statement(sql, [&](Statement&& stmt) { result.push_back(std::move(stmt)); });
    return result;
}

} // namespace datom
