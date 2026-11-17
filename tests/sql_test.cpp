#include "sql.hpp"

#include <iostream>
#include <stdexcept>

namespace {

using datom::Row;
using datom::Statement;
using datom::parse_sql;

void check(bool condition, const char* message) {
    if (!condition) {
        throw std::runtime_error(message);
    }
}

void expect_syntax_error(std::string_view sql, std::string_view detail) {
    try {
        parse_sql(sql);
    } catch (const datom::DbError& e) {
        check(e.code == "42601", "expected syntax error SQLSTATE");
        check(std::string_view(e.what()).find(detail) != std::string_view::npos,
              "missing expected error location/detail");
        return;
    }
    throw std::runtime_error("invalid SQL accepted: " + std::string(sql));
}

void test_sql_file() {
    const auto sql = R"SQL(
        ;-- First seed rows; the semicolon lives inside this comment.
        InSeRt /* nested /* internal */ comment */ InTo USERS(Name, age)
        VaLuEs ('O''Brien; -- /*' , 21), ('', -1);
        sEleCt name, "ExactCase" fRoM Users wHeRe Id = 42;
        SELECT * FROM users WHERE name = '정글;DB';;
        SELECT * FROM users -- no final delimiter
    )SQL";
    const auto result = parse_sql(sql);
    check(result.size() == 4, "wrong statement count");
    const auto& ins = result[0];
    check(ins.kind == Statement::Kind::Insert && ins.table == "users", "INSERT header");
    check(ins.columns == Row{"name", "age"}, "INSERT columns");
    check(ins.values == std::vector<Row>{{"O'Brien; -- /*", "21"}, {"", "-1"}}, "INSERT values");
    const auto& sel = result[1];
    check(sel.kind == Statement::Kind::Select && sel.table == "users", "SELECT header");
    check(sel.columns == Row{"name", "ExactCase"}, "SELECT projection");
    check(sel.where && sel.where->column == "id" && sel.where->value == "42", "id predicate");
    check(result[2].columns.empty() && result[2].where->value == "정글;DB", "text predicate");
    check(result[3].columns.empty() && !result[3].where, "full scan");
}

void test_lexer_edges() {
    const auto result = parse_sql(R"SQL(INSERT INTO "Mixed""Case" VALUES (+123, '한글', 'a
b', ''''))SQL");
    check(result[0].table == "Mixed\"Case", "quoted identifier escape");
    check(result[0].columns.empty(), "implicit insert columns");
    check(result[0].values[0] == Row{"+123", "한글", "a\nb", "'"}, "literal preservation");
    check(parse_sql("  ; -- test\r\n /* ok */ ;; ").empty(), "empty SQL");
    expect_syntax_error("select * from users where id = ", "at byte 32");
    expect_syntax_error("insert into t values ('unfinished);", "unterminated string");
    expect_syntax_error("select * from \"broken", "unterminated identifier");
    expect_syntax_error("select * from \"\"", "empty quoted identifier");
    expect_syntax_error("select * from t /* outer /* inner */", "unterminated block comment");
    expect_syntax_error(std::string_view("select\0 x", 9), "at byte 7");
}

void test_invalid_statements() {
    for (const char* invalid : {
        "create table x (id int)", "insert users values (1)", "insert into t values ()",
        "insert into t values (1,)", "insert into t (name,) values ('x')",
        "insert into t values ('ok'),", "select from users", "select name, from t",
        "select * from t junk", "select * from t where id > 4",
        "select * from t where id = 1 and id = 2", "select * from t select * from t",
        "select * from t where id = 1.0", "insert into t values (null)", "select * from t; bad"
    }) {
        expect_syntax_error(invalid, "at byte");
    }
}

void test_incremental_parsing() {
    std::vector<std::string> visited_tables;
    const auto visit = [&](Statement&& stmt) { visited_tables.push_back(stmt.table); };
    try {
        datom::for_each_statement("select * from first; select * from second trailing", visit);
        throw std::runtime_error("trailing input accepted");
    } catch (const datom::DbError& e) {
        check(e.code == "42601", "expected incremental syntax error");
    }
    check(visited_tables == Row{"first"}, "only complete statements must reach the callback");
}

void test_encoding() {
    // Valid 2-, 3-, and 4-byte scalar values, including the top end of Unicode.
    const std::string valid = "é정🍎\xf4\x8f\xbf\xbf";
    const auto parsed = parse_sql("select * from users where name = '" + valid + "'");
    check(parsed[0].where->value == valid, "UTF-8 literal changed");
    for (std::string_view bytes : {"\x80", "\xc0\xaf", "\xe0\x9f\x80", "\xed\xa0\x80",
                                   "\xf4\x90\x80\x80", "\xf5\x80\x80\x80", "\xe2\x82", "\xc2Z"}) {
        const std::string script = "insert into t values (1); --" + std::string(bytes);
        bool visited = false;
        try {
            datom::for_each_statement(script, [&](Statement&&) { visited = true; });
            throw std::runtime_error("invalid UTF-8 accepted");
        } catch (const datom::DbError& e) {
            check(e.code == "22021", "expected encoding SQLSTATE");
        }
        check(!visited, "invalid UTF-8 must be rejected before execution");
    }
}

} // namespace

int main() {
    try {
        test_sql_file();
        test_lexer_edges();
        test_invalid_statements();
        test_incremental_parsing();
        test_encoding();
        std::cout << "SQL parser tests passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
