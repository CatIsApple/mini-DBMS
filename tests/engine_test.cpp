#include "engine.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <unistd.h>

namespace {

namespace fs = std::filesystem;
using datom::AccessPath;
using datom::Engine;
using datom::Result;
using datom::Row;

void check(bool condition, const char* detail) {
    if (!condition) throw std::runtime_error(detail);
}

class TempDirectory {
public:
    TempDirectory() {
        std::string pattern = (fs::temp_directory_path() / "datom-engine-XXXXXX").string();
        if (!::mkdtemp(pattern.data())) throw std::runtime_error("cannot create test directory");
        path = pattern;
    }

    ~TempDirectory() {
        std::error_code ignored;
        fs::remove_all(path, ignored);
    }

    fs::path path;
};

Result run(Engine& engine, std::string_view sql) {
    auto statements = datom::parse_sql(sql);
    check(statements.size() == 1, "test helper expected one SQL statement");
    return engine.execute(statements.front());
}

template<class Operation>
void expect_error(std::string_view code, Operation operation) {
    try {
        operation();
    } catch (const datom::DbError& error) {
        if (error.code == code) return;
        throw std::runtime_error("unexpected SQLSTATE " + error.code + ": " + error.what());
    }
    throw std::runtime_error("expected SQLSTATE " + std::string(code));
}

void test_schema_and_queries() {
    TempDirectory temp;
    Engine engine(temp.path);
    auto empty = run(engine, "select * from users");
    check(empty.columns == Row{"id", "name", "email"} && empty.rows.empty(), "empty table schema");
    const auto ins = run(engine, "insert into users(email,name) values "
                                 "('ada@test','Ada'), ('bob@test','Bob'), ('ada2@test','Ada')");
    check(ins.tag == "INSERT 0 3" && ins.access == AccessPath::None, "insert tag and access path");
    auto hit = run(engine, "select email,id,name from users where id = +002");
    const std::vector<Row> bob{{"bob@test", "2", "Bob"}};
    check(hit.columns == Row{"email", "id", "name"} && hit.rows == bob, "projected index row");
    check(hit.access == AccessPath::IndexLookup && hit.rows_examined == 1, "index hit metrics");

    const auto scan = run(engine, "select email,id,name from users where email = 'bob@test'");
    check(scan.rows == hit.rows && scan.tag == "SELECT 1", "scan and index return identical rows");
    check(scan.access == AccessPath::SequentialScan && scan.rows_examined == 3, "scan metrics");
    const auto several = run(engine, "select id from users where name = 'Ada'");
    check(several.rows == std::vector<Row>{{"1"}, {"3"}}, "all matching rows from scan");
    for (const char* missing_id : {"0", "-3", "4"}) {
        const auto miss = run(engine, std::string("select id from users where id = ") + missing_id);
        check(miss.rows.empty() && miss.access == AccessPath::IndexLookup && miss.rows_examined == 0,
              "index miss must not read file rows");
    }

    for (const auto& entry : std::vector<std::pair<const char*,const char*>>{
            {"42P01", "select * from absent"}, {"42P01", "insert into absent values ('x','y')"},
            {"42703", "select unknown from users"}, {"42703", "select * from users where x = 'x'"},
            {"42703", "insert into users(name,x) values ('a','b')"},
            {"23502", "insert into users(name) values ('a')"},
            {"42701", "insert into users(name,name) values ('a','b')"},
            {"428C9", "insert into users(id,name,email) values (8,'a','b')"},
            {"22P02", "select * from users where id = '4x'"},
            {"22P02", "select * from users where id = '+-4'"},
            {"22003", "select * from users where id = 9223372036854775808"},
            {"42601", "insert into users values ('must rollback','r'), ('incomplete')"}}) {
        expect_error(entry.first, [&] { run(engine, entry.second); });
    }
    check(run(engine, "select * from users").rows.size() == 3, "failed insert changed table");
    run(engine, "insert into users values ('after','z')");
    check(run(engine, "select name from users where id = 4").rows == std::vector<Row>{{"after"}},
          "failed insert consumed an id or changed the index");
}

void test_table_isolation_and_restart() {
    TempDirectory temp;
    {
        Engine engine(temp.path);
        expect_error("55006", [&] { Engine second(temp.path); });
        run(engine, "insert into users values ('u1','e'),('u2','f')");
        run(engine, "insert into products(sku,name) values ('s1','p1')");
        expect_error("42703", [&] { run(engine, "select email from products"); });
    }
    check(fs::is_regular_file(temp.path / "users.tbl") && fs::is_regular_file(temp.path / "products.tbl"),
          "per-table data files");
    {
        Engine reopened(temp.path);
        auto product = run(reopened, "select name,sku from products where id=1");
        check(product.access == AccessPath::IndexLookup && product.rows == std::vector<Row>{{"p1","s1"}},
              "restored product index");
        check(run(reopened, "select name from users where id=1").rows == std::vector<Row>{{"u1"}},
              "restored user index");
        run(reopened, "insert into users values ('u3','e3')");
        run(reopened, "insert into products values ('p2','s2')");
        check(run(reopened, "select name from products where id=2").rows == std::vector<Row>{{"p2"}},
              "product id sequence");
        check(run(reopened, "select name from users where id=3").rows == std::vector<Row>{{"u3"}},
              "user id sequence");
    }
}

void test_incomplete_frame() {
    TempDirectory temp;
    const auto file = temp.path / "users.tbl";
    std::uintmax_t committed_size;
    {
        Engine engine(temp.path);
        run(engine, "insert into users values ('kept','1')");
        committed_size = fs::file_size(file);
        run(engine, "insert into users values ('lost-a','2'),('lost-b','3')");
    }
    fs::resize_file(file, fs::file_size(file) - 1); // Missing the final byte of the commit marker.
    Engine recovered(temp.path);
    check(fs::file_size(file) == committed_size, "incomplete frame was not discarded");
    check(run(recovered, "select name from users").rows == std::vector<Row>{{"kept"}},
          "torn batch became partly visible");
    run(recovered, "insert into users values ('next','2')");
    check(run(recovered, "select name from users where id=2").rows == std::vector<Row>{{"next"}},
          "index/ID sequence after recovery");
}

void test_corruption_refused() {
    TempDirectory temp;
    const auto file = temp.path / "users.tbl";
    {
        Engine engine(temp.path);
        run(engine, "insert into users values ('known-payload','x')");
        run(engine, "insert into users values ('later-valid-frame','y')");
    }
    {
        std::fstream data(file, std::ios::binary | std::ios::in | std::ios::out);
        data.exceptions(std::ios::badbit | std::ios::failbit);
        const std::string bytes{std::istreambuf_iterator<char>(data), std::istreambuf_iterator<char>()};
        const auto at = bytes.find("known-payload");
        check(at != std::string::npos, "corruption fixture missing");
        data.seekp(static_cast<std::streamoff>(at));
        data.put('X');
    }
    const auto size = fs::file_size(file);
    expect_error("XX001", [&] { Engine reopened(temp.path); });
    check(fs::file_size(file) == size, "corrupt committed frame was silently truncated");
}

void test_parallel_statements() {
    TempDirectory temp;
    Engine engine(temp.path);
    constexpr int writers = 4;
    constexpr int repeats = 8;
    std::promise<void> start;
    auto ready = start.get_future().share();
    std::vector<std::future<void>> tasks;
    for (int w = 0; w < writers; ++w) {
        tasks.push_back(std::async(std::launch::async, [&, w] {
            ready.wait();
            const auto sql = "insert into users values ('w" + std::to_string(w) + "','1'),('w" +
                             std::to_string(w) + "','2')";
            for (int i = 0; i < repeats; ++i) run(engine, sql);
        }));
    }
    tasks.push_back(std::async(std::launch::async, [&] {
        ready.wait();
        for (int read = 0; read < 24; ++read) {
            const auto snapshot = run(engine, "select id from users");
            check(snapshot.rows.size() % 2 == 0, "reader saw part of a multirow INSERT");
            for (std::size_t i = 0; i < snapshot.rows.size(); ++i)
                check(snapshot.rows[i] == Row{std::to_string(i + 1)}, "invalid snapshot ID sequence");
        }
    }));
    start.set_value();
    for (auto& task : tasks) task.get();
    const auto rows = run(engine, "select * from users").rows;
    check(rows.size() == 2 * writers * repeats, "lost concurrent writes");
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto indexed = run(engine, "select * from users where id=" + std::to_string(i + 1));
        check(indexed.rows == std::vector<Row>{rows[i]}, "concurrent index/file mismatch");
    }
}

void test_stream_snapshot() {
    TempDirectory temp;
    Engine engine(temp.path);
    run(engine, "insert into users values ('first','a'),('second','b')");
    Row columns;
    std::vector<Row> rows;
    std::future<Result> writer;
    datom::ResultSink sink{
        [&](const Row& names) { columns = names; },
        [&](const Row& row) {
            rows.push_back(row);
            if (rows.size() == 1) {
                writer = std::async(std::launch::async, [&] {
                    return run(engine, "insert into users values ('new','c')");
                });
                check(writer.wait_for(std::chrono::seconds(5)) == std::future_status::ready,
                      "row callback held the table lock");
                writer.get();
            }
        }
    };
    const auto statement = datom::parse_sql("select id from users").front();
    const auto result = engine.execute(statement, &sink);
    check(columns == Row{"id"} && rows == std::vector<Row>{{"1"}, {"2"}}, "unstable streamed snapshot");
    check(result.rows.empty() && result.tag == "SELECT 2" && result.rows_examined == 2,
          "streamed results retained rows or reported the wrong count");
    check(run(engine, "select id from users where id=3").rows == std::vector<Row>{{"3"}},
          "concurrent stream write missing");
}

} // namespace

int main() {
    try {
        test_schema_and_queries();
        test_table_isolation_and_restart();
        test_incomplete_frame();
        test_corruption_refused();
        test_parallel_statements();
        test_stream_snapshot();
        std::cout << "Engine schema, access path, recovery and concurrency tests passed\n";
    } catch (const std::exception& e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
